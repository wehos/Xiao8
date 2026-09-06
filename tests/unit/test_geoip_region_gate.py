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

"""Region-resolution regression tests.

Structured around the five invariants in ``core_config``'s module docstring:
a single background probe owns the IP verdict, everyone else reads it; IP
outranks Steam and Steam never latches; only free-route users are probed;
the probe never gives up; and every path that freezes a session route settles
the region first.
"""
import asyncio
import os
import sys
import threading
import time as real_time
from types import SimpleNamespace

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

# 只用一种导入形式：既要 monkeypatch 包属性（is_livestream_active），又要拿到
# ConfigManager / core_config，混用 import 与 from-import 会被静态检查判为风格问题。
import utils.config_manager as config_manager_pkg  # noqa: E402

from tests.repo_ast_cache import parse_source_file

ConfigManager = config_manager_pkg.ConfigManager
core_config_mod = config_manager_pkg.core_config


class _Probe(core_config_mod.CoreConfigMixin):
    """Bare mixin carrier — _check_non_mainland only needs the sub-checks."""


def _async_return(value):
    async def _coro(*a, **kw):
        return value
    return _coro


class _JsonResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload.encode()


@pytest.fixture()
def config_manager(clean_user_data_dir, monkeypatch):
    """Real ConfigManager on a temp config dir.

    ``get_config_manager`` hands back a process-wide singleton, so whichever test
    file ran first leaves it bound to *its* (now deleted) temp dir — these tests then
    read a stale config and fail only when run alongside that file. Rebuild the
    singleton here so the instance actually belongs to this test's directory.
    """
    monkeypatch.setattr(config_manager_pkg, '_config_manager', None, raising=False)
    monkeypatch.setattr(config_manager_pkg, '_config_manager_migrated', False, raising=False)
    cm = config_manager_pkg.get_config_manager('N.E.K.O')
    cm.config_dir.mkdir(parents=True, exist_ok=True)
    cm._core_config_cache = None
    return cm


@pytest.fixture(autouse=True)
def reset_geo_state(monkeypatch):
    monkeypatch.setattr(core_config_mod, 'GEOIP_FORCE_NON_MAINLAND', None)
    monkeypatch.setattr(ConfigManager, '_ip_probe_wake', threading.Event())
    monkeypatch.setattr(ConfigManager, '_ip_probe_in_flight', threading.Event())
    monkeypatch.setattr(ConfigManager, '_ip_probe_stopping', False)
    # 默认「仍在免费路由」，否则探测循环每轮都会去读真实配置并提前收工。
    # 专测「切走免费路由」的用例自行覆盖它。
    monkeypatch.setattr(
        ConfigManager, '_free_route_still_needs_region', staticmethod(lambda: True))
    for name, value in (
        ('_region_cache', None),
        ('_ip_check_cache', None),
        ('_steam_check_cache', None),
        ('_geo_indeterminate_logged', False),
        ('_geo_steam_fallback_logged', False),
        ('_ip_probe_thread', None),
    ):
        monkeypatch.setattr(ConfigManager, name, value)
    yield
    # 背景探测线程是无限重试循环（永不放弃），必须主动终止再 join，否则泄漏的线程
    # 会带着真实网络污染后续用例。写 cache 打破 while、set wake 唤醒退避 sleep。
    # 本 fixture 声明了 monkeypatch，故先于它 teardown：断言/桩仍在位。
    thread = ConfigManager._ip_probe_thread
    if thread is not None:
        if ConfigManager._ip_check_cache is None:
            ConfigManager._ip_check_cache = False
        ConfigManager._ip_probe_stopping = True
        ConfigManager._ip_probe_wake.set()
        thread.join(5)
        assert not thread.is_alive(), '探测线程泄漏，会污染后续用例'


def _probe(ip, steam):
    """A carrier whose sub-checks return fixed values (no real network/Steam)."""
    p = _Probe()
    # 实例属性 → 无描述符协议，调用时不多传 self
    p._ensure_ip_probe_started = lambda: None
    p._check_ip_non_mainland_http = staticmethod(lambda: ip)
    p._check_steam_non_mainland = lambda: steam
    return p


def _patch_probe_once(monkeypatch, responses):
    """Drive ``_ip_probe_once`` off a scripted list (Exception=failure, str=country)."""
    calls = {'n': 0}

    def _once():
        i = calls['n']
        calls['n'] += 1
        outcome = responses[i] if i < len(responses) else responses[-1]
        if isinstance(outcome, Exception):
            raise outcome
        return (outcome != 'CN') if outcome else None

    monkeypatch.setattr(ConfigManager, '_ip_probe_once', staticmethod(_once))
    return calls


# ---------------------------------------------------------------------------
# #3 — IP decides; Steam is only the (never-latching) fallback
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_steam_silent_overseas_ip_routes_overseas():
    """Non-Steam / Steam-not-running overseas users are no longer pinned mainland."""
    assert _probe(ip=True, steam=None)._check_non_mainland() is True
    assert ConfigManager._region_cache is True


@pytest.mark.unit
@pytest.mark.parametrize('steam', [True, False, None])
def test_ip_outranks_steam(steam):
    """The probe bypasses proxies, so it geolocates better than Steam's exit IP."""
    assert _probe(ip=True, steam=steam)._check_non_mainland() is True
    ConfigManager._region_cache = None
    assert _probe(ip=False, steam=steam)._check_non_mainland() is False


@pytest.mark.unit
def test_mainland_ip_routes_mainland():
    assert _probe(ip=False, steam=None)._check_non_mainland() is False
    assert ConfigManager._region_cache is False


@pytest.mark.unit
@pytest.mark.parametrize('steam, expected', [(True, True), (False, False)])
def test_steam_breaks_the_tie_when_ip_is_silent(steam, expected):
    assert _probe(ip=None, steam=steam)._check_non_mainland() is expected


@pytest.mark.unit
@pytest.mark.parametrize('steam', [True, False])
def test_steam_fallback_never_latches(steam):
    """Latching Steam would freeze out the IP takeover — it must stay provisional."""
    assert _probe(ip=None, steam=steam)._check_non_mainland() is steam
    assert ConfigManager._region_cache is None
    # IP 稍后落地、即便方向相反，也立刻接管
    assert _probe(ip=not steam, steam=steam)._check_non_mainland() is (not steam)
    assert ConfigManager._region_cache is (not steam)


@pytest.mark.unit
def test_both_indeterminate_defaults_mainland_without_caching():
    assert _probe(ip=None, steam=None)._check_non_mainland() is False
    assert ConfigManager._region_cache is None
    # 网络稍后就绪 → 无需重启即可翻成海外
    assert _probe(ip=True, steam=None)._check_non_mainland() is True


# ---------------------------------------------------------------------------
# The single background probe (#1, #4)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_probe_loop_retries_until_it_lands_a_verdict(monkeypatch):
    """Cold-boot failures are retried; the loop is the sole writer of the cache."""
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_BASE_S', 0.0)
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_MAX_S', 0.0)
    calls = _patch_probe_once(monkeypatch, [OSError('cold boot'), OSError('again'), 'US'])

    _Probe()._ensure_ip_probe_started()
    ConfigManager._ip_probe_thread.join(5)

    assert calls['n'] == 3
    assert ConfigManager._ip_check_cache is True


@pytest.mark.unit
def test_probe_loop_never_gives_up(monkeypatch):
    """Connectivity can arrive tens of minutes in; the loop must still be trying."""
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_BASE_S', 0.0)
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_MAX_S', 0.0)
    # 长时间只失败，然后成功——中途从不写永久放弃标记
    calls = _patch_probe_once(monkeypatch, [OSError('down')] * 50 + ['JP'])

    _Probe()._ensure_ip_probe_started()
    ConfigManager._ip_probe_thread.join(5)

    assert calls['n'] == 51
    assert ConfigManager._ip_check_cache is True


@pytest.mark.unit
def test_probe_is_idempotent_and_single(monkeypatch):
    """Only ever one probe thread: repeated starts do not stack writers."""
    release = threading.Event()
    entered = threading.Event()

    def _once():
        entered.set()
        release.wait(5)
        raise OSError('slow')

    monkeypatch.setattr(ConfigManager, '_ip_probe_once', staticmethod(_once))
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_BASE_S', 0.0)

    first = None
    try:
        _Probe()._ensure_ip_probe_started()
        first = ConfigManager._ip_probe_thread
        assert entered.wait(5)
        for _ in range(5):
            _Probe()._ensure_ip_probe_started()
            assert ConfigManager._ip_probe_thread is first, '不应另起第二个探测线程'
    finally:
        release.set()


@pytest.mark.unit
def test_probe_thread_is_daemon(monkeypatch):
    """A probe hung on a 3s connect must never hold up process exit."""
    release = threading.Event()

    def _once():
        release.wait(5)
        raise OSError('slow')

    monkeypatch.setattr(ConfigManager, '_ip_probe_once', staticmethod(_once))
    try:
        _Probe()._ensure_ip_probe_started()
        thread = ConfigManager._ip_probe_thread
        assert thread is not None and thread.daemon
    finally:
        release.set()


@pytest.mark.unit
def test_read_never_blocks_the_caller(monkeypatch):
    """_check_ip_non_mainland_http is a pure read — no network on the caller thread."""
    def _boom():
        raise AssertionError('read path must not probe')

    monkeypatch.setattr(ConfigManager, '_ip_probe_once', staticmethod(_boom))
    started = real_time.monotonic()
    assert ConfigManager._check_ip_non_mainland_http() is None
    assert real_time.monotonic() - started < 0.1


@pytest.mark.unit
@pytest.mark.parametrize('failures', [0, 1, 2, 33, 1025, 10 ** 6])
def test_backoff_stays_finite_for_any_failure_count(failures):
    """A machine offline for days keeps failing; 2 ** huge would raise OverflowError."""
    wait = ConfigManager._ip_check_backoff_s(failures)
    assert isinstance(wait, float)
    assert 0.0 <= wait <= ConfigManager._IP_CHECK_RETRY_MAX_S


# ---------------------------------------------------------------------------
# #2 — only free-route users are probed
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_paid_route_config_read_never_probes(config_manager, monkeypatch):
    """Reading config on a paid/custom route must not start the geolocation probe."""
    def _boom():
        raise AssertionError('paid-route read must not probe')

    monkeypatch.setattr(ConfigManager, '_ip_probe_once', staticmethod(_boom))

    import json as _json
    path = config_manager.get_config_path('core_config.json')
    with open(str(path), 'w', encoding='utf-8') as fh:
        _json.dump({'coreApi': 'qwen'}, fh)
    config_manager._core_config_cache = None

    cfg = config_manager.get_core_config()
    assert not [v for k, v in cfg.items()
                if k.endswith('_URL') and isinstance(v, str) and 'lanlan.tech' in v], \
        '前置条件：该配置不应处于免费路由'
    assert ConfigManager._ip_probe_thread is None, '自配 API 用户不应启动 GeoIP 探测'


@pytest.mark.unit
def test_free_route_config_read_starts_the_probe(config_manager, monkeypatch):
    """The free route is exactly where probing is allowed."""
    started = threading.Event()

    def _once():
        started.set()
        real_time.sleep(0.3)
        raise OSError('slow')

    monkeypatch.setattr(ConfigManager, '_ip_probe_once', staticmethod(_once))

    import json as _json
    path = config_manager.get_config_path('core_config.json')
    with open(str(path), 'w', encoding='utf-8') as fh:
        _json.dump({'coreApi': 'free'}, fh)
    config_manager._core_config_cache = None

    try:
        config_manager.get_core_config()
        assert started.wait(5), '免费路由读配置应当启动探测'
    finally:
        pass
@pytest.mark.unit
def test_one_config_snapshot_uses_one_region_verdict(config_manager, monkeypatch):
    """All URLs in a snapshot must agree on the region.

    Resolving per URL would let Steam initialising mid-loop leave earlier URLs on
    lanlan.tech and later ones on lanlan.app — one config pointing at two regions.
    Asserted on the real ``get_core_config`` loop (an earlier draft passed
    ``non_mainland=`` by hand and never exercised the call site).
    """
    import json as _json
    path = config_manager.get_config_path('core_config.json')
    with open(str(path), 'w', encoding='utf-8') as fh:
        _json.dump({'coreApi': 'free'}, fh)
    config_manager._core_config_cache = None

    calls = {'n': 0}
    flips = iter([False] + [True] * 50)

    def _flipping(self):
        calls['n'] += 1
        return next(flips)

    monkeypatch.setattr(type(config_manager), '_check_non_mainland', _flipping)
    cfg = config_manager.get_core_config()

    assert calls['n'] == 1, f'一次快照内判定了 {calls["n"]} 次，各 URL 可能不一致'
    lanlan = [v for k, v in cfg.items()
              if k.endswith('_URL') and isinstance(v, str) and 'lanlan.' in v]
    assert lanlan, '前置条件：配置必须处于免费路由'
    hosts = {'lanlan.app' if 'lanlan.app' in v else 'lanlan.tech' for v in lanlan}
    assert len(hosts) == 1, f'同一份快照指向了两个区域: {lanlan}'


# ---------------------------------------------------------------------------
# #5 — sessions settle the region before freezing a route
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_startup_warmup_waits_for_the_verdict(monkeypatch):
    """The first session must not be pinned to the transient mainland fallback."""
    class _Slow:
        def open(self, req, timeout=None):
            real_time.sleep(0.3)
            return _JsonResp('{"countryCode": "US"}')

    import urllib.request
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a, **kw: _Slow())

    probe = _Probe()
    probe.aget_core_config = _async_return(
        # 真实的免费路由形态：空真判定按「free provider + 区域敏感 URL」合取，
        # None/空配置会被判成无需区域而跳过等待，测不到等待路径
        {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'})

    ConfigManager._ensure_ip_probe_started()
    assert ConfigManager._ip_check_cache is None, '前置条件：预热开始时结论尚未落地'

    assert asyncio.run(probe.awarmup_region_check(timeout=5)) is True
    assert ConfigManager._ip_check_cache is True


@pytest.mark.unit
def test_startup_warmup_does_not_block_the_event_loop(monkeypatch):
    """Waiting is allowed at startup, but never on the loop itself."""
    release = threading.Event()

    class _Hanging:
        def open(self, req, timeout=None):
            release.wait(5)
            raise OSError('timed out')

    import urllib.request
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a, **kw: _Hanging())

    probe = _Probe()
    probe.aget_core_config = _async_return(
        # 真实的免费路由形态：空真判定按「free provider + 区域敏感 URL」合取，
        # None/空配置会被判成无需区域而跳过等待，测不到等待路径
        {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'})
    ConfigManager._ensure_ip_probe_started()

    async def _run():
        gaps = []
        stop = asyncio.Event()

        async def _beat():
            last = real_time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.02)
                now = real_time.monotonic()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(_beat())
        await asyncio.sleep(0.1)
        release.set()
        await probe.awarmup_region_check(timeout=5)
        stop.set()
        await beat
        return max(gaps)

    try:
        worst = asyncio.run(_run())
        assert worst < 0.5, f'预热期间事件循环被占用 {worst:.2f}s'
    finally:
        release.set()


@pytest.mark.unit
def test_session_start_waits_out_a_probe_still_in_flight(monkeypatch):
    """A session freezes its route, so it waits for a still-running probe."""
    class _Slow:
        def open(self, req, timeout=None):
            real_time.sleep(0.3)
            return _JsonResp('{"countryCode": "US"}')

    import urllib.request
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a, **kw: _Slow())

    probe = _Probe()
    probe.aget_core_config = _async_return(
        # 真实的免费路由形态：空真判定按「free provider + 区域敏感 URL」合取，
        # None/空配置会被判成无需区域而跳过等待，测不到等待路径
        {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'})
    ConfigManager._ensure_ip_probe_started()
    assert ConfigManager._ip_check_cache is None

    assert asyncio.run(probe.aensure_region_resolved(timeout=5)) is True
    assert ConfigManager._ip_check_cache is True


@pytest.mark.unit
def test_session_start_is_free_when_already_resolved(monkeypatch):
    """Zero cost on the normal path: verdict in hand, no waiting."""
    monkeypatch.setattr(ConfigManager, '_region_cache', True)

    def _boom(*a, **kw):
        raise AssertionError('已落定时不应等待探测')

    probe = _Probe()
    # 桩在实例上：_Probe 不经过 ConfigManager，patch 类属性是死桩（见
    # test_custom_route_settle_is_vacuously_true 的同款说明）。
    probe.join_ip_probe = _boom
    started = real_time.monotonic()
    assert asyncio.run(probe.aensure_region_resolved()) is True
    assert real_time.monotonic() - started < 0.2


@pytest.mark.unit
def test_session_start_logs_when_the_wait_expires(monkeypatch):
    """Waiting forever is not an option, so the give-up must be diagnosable.

    Records straight off the module logger rather than via ``caplog``: the app's
    logging setup puts ``propagate=False`` on the ``N.E.K.O`` parent, so caplog's
    root handler sees nothing once any test has pulled that setup in.
    """
    release = threading.Event()

    class _Hanging:
        def open(self, req, timeout=None):
            release.wait(5)
            raise OSError('timed out')

    import urllib.request
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a, **kw: _Hanging())

    warnings = []
    monkeypatch.setattr(
        core_config_mod.logger, 'warning',
        lambda msg, *a, **kw: warnings.append(str(msg) % a if a else str(msg)),
    )

    probe = _Probe()
    probe.aget_core_config = _async_return(
        # 真实的免费路由形态：空真判定按「free provider + 区域敏感 URL」合取，
        # None/空配置会被判成无需区域而跳过等待，测不到等待路径
        {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'})
    ConfigManager._ensure_ip_probe_started()
    try:
        assert asyncio.run(probe.aensure_region_resolved(timeout=0.1)) is False
        assert any('GeoIP' in w for w in warnings), f'放弃等待必须留下日志，实际: {warnings}'
    finally:
        release.set()


@pytest.mark.unit
def test_steam_users_do_not_pay_for_the_ip_wait(monkeypatch):
    """Having Steam's answer is enough to pick a route — do not wait for IP.

    The wait avoids routing on *no* information; Steam's answer is information.
    Making Steam users sit through a probe timeout is pure first-session latency
    and buys nothing — the Steam verdict is never latched, so the probe still
    takes over for later sessions once it lands.
    """
    release = threading.Event()

    class _Hanging:
        def open(self, req, timeout=None):
            release.wait(10)
            raise OSError('timed out')

    import urllib.request
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a, **kw: _Hanging())

    try:
        ConfigManager._ensure_ip_probe_started()
        monkeypatch.setattr(ConfigManager, '_steam_check_cache', True)

        started = real_time.monotonic()
        assert ConfigManager.join_ip_probe(timeout=5) is True
        waited = real_time.monotonic() - started
        assert waited < 0.5, f'Steam 已有结论却仍等了 {waited:.2f}s'
    finally:
        release.set()


@pytest.mark.unit
def test_skipping_the_wait_does_not_promote_steam():
    """Not waiting is a latency call, not a correctness one — Steam must not latch."""
    probe = _Probe()
    probe._check_steam_non_mainland = lambda: True
    probe.aget_core_config = _async_return(
        # 真实的免费路由形态：空真判定按「free provider + 区域敏感 URL」合取，
        # None/空配置会被判成无需区域而跳过等待，测不到等待路径
        {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'})

    assert asyncio.run(probe.aensure_region_resolved(timeout=5)) is True
    assert ConfigManager._region_cache is None, 'Steam 票不得因跳过等待而落定'
    assert _probe(ip=False, steam=True)._check_non_mainland() is False
    assert ConfigManager._region_cache is False


@pytest.mark.unit
def test_every_session_preparation_path_settles_the_region():
    """Each path that builds a session (and freezes its base URL) settles first.

    Structural, because the real risk is a *new* path added later that a
    behavioural test of the existing two would never notice. Compared by line
    number, not by call-name membership: an unordered set only proves the
    settle call exists somewhere — moving it *after* the config read that
    freezes the route would keep a membership assertion green.
    """
    import ast
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[2] / 'main_logic' / 'core' / 'lifecycle.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))

    missing = []
    checked = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        lines = {}
        for c in ast.walk(node):
            if isinstance(c, ast.Call):
                name = getattr(c.func, 'attr', None)
                if name:
                    lines.setdefault(name, []).append(c.lineno)
        if 'aget_core_config' not in lines:
            continue
        checked.append(node.name)
        settles = lines.get('aensure_region_resolved')
        if not settles:
            missing.append(f'{node.name} (line {node.lineno}) 未落定')
        elif min(settles) >= min(lines['aget_core_config']):
            missing.append(
                f'{node.name}: 落定在 line {min(settles)}，晚于首次配置读取'
                f' line {min(lines["aget_core_config"])}'
            )

    assert checked, '未找到任何会话准备路径，断言失效'
    assert not missing, f'这些路径会冻结会话线路却未先落定区域判定: {missing}'


@pytest.mark.unit
def test_game_session_pool_settles_the_region():
    """The game pool caches an OmniOfflineClient with its base_url — same freeze.

    Line-ordered like the lifecycle guard above: the settle must precede the
    session build that freezes the route, not merely exist in the function.
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[2]
              / 'main_routers' / 'game_router' / 'session_pool.py')
    tree = ast.parse(source.read_text(encoding='utf-8'))

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == '_get_or_create_session':
            lines = {}
            for c in ast.walk(node):
                if isinstance(c, ast.Call):
                    name = getattr(c.func, 'attr', None) or getattr(c.func, 'id', None)
                    if name:
                        lines.setdefault(name, []).append(c.lineno)
            settles = lines.get('aensure_region_resolved')
            builds = lines.get('_build_and_register_game_session')
            assert settles, '游戏会话池会缓存 base_url，必须先落定区域判定'
            assert builds, '未找到 _build_and_register_game_session 调用，锚点失效'
            assert min(settles) < min(builds), (
                f'落定(line {min(settles)}) 必须早于会话创建(line {min(builds)})，'
                '否则冻结的线路用的还是落定前的结论'
            )
            break
    else:
        pytest.fail('未找到 _get_or_create_session，断言失效')


# ---------------------------------------------------------------------------
# Steam country write-back (/api/config/steam_language)
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize('country, expect_cache', [
    ('US', True),
    ('CN', False),
    ('', None),      # 拿不到国家码 = 暂时不知道，不是"海外"
    (None, None),
])
def test_steam_country_writeback_only_on_real_data(monkeypatch, country, expect_cache):
    """An empty GetIPCountry() means "no answer yet", never "overseas"."""
    from main_routers.config_router import language as lang_mod

    monkeypatch.setattr(
        lang_mod, 'ensure_steamworks',
        lambda: SimpleNamespace(
            Apps=SimpleNamespace(GetCurrentGameLanguage=lambda: 'english'),
            Utils=SimpleNamespace(GetIPCountry=lambda: country),
        ),
    )
    monkeypatch.setattr(lang_mod, 'aload_ui_language_override', _async_return(None))
    monkeypatch.setattr(lang_mod.get_steam_language, '_logged', True, raising=False)

    result = asyncio.run(lang_mod.get_steam_language())

    assert result['success'] is True
    assert ConfigManager._steam_check_cache is expect_cache


@pytest.mark.unit
def test_dns_wedged_iteration_recovers_without_a_replacement_thread(monkeypatch):
    """A DNS-wedged iteration must not stall recovery — and must not need a replacement.

    ``getaddrinfo`` ignores the socket timeout, so one iteration can hang far longer
    than 3s. That is survivable precisely because the thread is a *loop*: the wedged
    call eventually raises (OS resolver timeout), the loop backs off and retries.
    Spawning a "replacement" would call the same ``getaddrinfo``, hang identically,
    and only buy multi-writer races plus a thread leak — which is what this design
    exists to remove. Asserted as behaviour so the point is not re-litigated.
    """
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_BASE_S', 0.0)
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_MAX_S', 0.0)

    wedged_entered = threading.Event()
    unwedge = threading.Event()
    calls = {'n': 0}

    def _once():
        calls['n'] += 1
        if calls['n'] == 1:
            wedged_entered.set()
            unwedge.wait(10)          # 模拟卡在 getaddrinfo 里
            raise OSError('resolver timed out')
        return True                    # 网络恢复

    monkeypatch.setattr(ConfigManager, '_ip_probe_once', staticmethod(_once))

    _Probe()._ensure_ip_probe_started()
    thread = ConfigManager._ip_probe_thread
    assert wedged_entered.wait(5), '第一次探测未进入卡死状态'

    # 卡死期间反复触发启动：不得另起线程（活着 == 重试计划在跑）
    for _ in range(5):
        _Probe()._ensure_ip_probe_started()
        assert ConfigManager._ip_probe_thread is thread, '卡死期间不应另起替代探测'
    assert ConfigManager._ip_check_cache is None

    # 解析超时返回后，同一个循环自行重试并拿到结论——无需任何外部干预
    unwedge.set()
    thread.join(5)
    assert ConfigManager._ip_check_cache is True, '卡死迭代后循环应自行恢复'
    assert calls['n'] == 2


@pytest.mark.unit
def test_probe_stops_when_user_leaves_the_free_route_mid_backoff(monkeypatch):
    """Switching to a paid/custom provider *while backing off* must stop the probe.

    Two things this must not do, both of which make the test vacuous:
    - fix eligibility to False before the thread starts (only proves "exits after
      the first failure", never exercises the mid-backoff switch);
    - use ``_ip_probe_wake`` to wake the sleeper — that event *also* terminates the
      loop, so the thread would exit even with the eligibility check deleted.
    So: let the backoff expire naturally and assert no second request goes out.
    """
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_BASE_S', 0.3)
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_MAX_S', 0.3)
    eligible = {'v': True}
    monkeypatch.setattr(
        ConfigManager, '_free_route_still_needs_region',
        staticmethod(lambda: eligible['v']))

    probed = threading.Event()
    calls = {'n': 0}

    def _once():
        calls['n'] += 1
        probed.set()
        raise OSError('down')

    monkeypatch.setattr(ConfigManager, '_ip_probe_once', staticmethod(_once))

    _Probe()._ensure_ip_probe_started()
    thread = ConfigManager._ip_probe_thread
    assert probed.wait(5), '首次探测未发生'

    # 等它真正进入退避 sleep，再模拟用户切走免费线路
    for _ in range(200):
        if not ConfigManager._ip_probe_in_flight.is_set():
            break
        real_time.sleep(0.005)
    assert not ConfigManager._ip_probe_in_flight.is_set(), '前置条件：应已进入退避'
    assert thread.is_alive(), '前置条件：循环仍在退避中'
    eligible['v'] = False

    # 退避自然到期后循环回到顶部，应当据资格判定收工——而不是再敲一次
    thread.join(5)
    assert not thread.is_alive(), '切走免费线路后循环应收工'
    assert calls['n'] == 1, f'退避到期后不应再探测，实际探了 {calls["n"]} 次'
    assert ConfigManager._ip_check_cache is None


@pytest.mark.unit
def test_waiters_skip_a_probe_that_is_only_backing_off(monkeypatch):
    """Backoff sleep is not in-flight: no verdict can arrive, so do not pay the join.

    The loop stays alive while sleeping 30-600s. Treating that as "in flight" makes
    every session pay the full join timeout for the whole duration of a GeoIP outage.
    """
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_BASE_S', 30.0)
    backing_off = threading.Event()

    def _once():
        backing_off.set()
        raise OSError('down')

    monkeypatch.setattr(ConfigManager, '_ip_probe_once', staticmethod(_once))

    _Probe()._ensure_ip_probe_started()
    assert backing_off.wait(5)
    # 等它进入退避 sleep（in_flight 被清掉）
    for _ in range(200):
        if not ConfigManager._ip_probe_in_flight.is_set():
            break
        real_time.sleep(0.01)
    assert not ConfigManager._ip_probe_in_flight.is_set(), '退避期间不应标记为在飞'
    assert ConfigManager._ip_probe_thread.is_alive(), '前置条件：线程仍活着（在退避）'

    started = real_time.monotonic()
    assert ConfigManager.join_ip_probe(timeout=5) is False
    waited = real_time.monotonic() - started
    assert waited < 0.5, f'退避期间不应等待，实际等了 {waited:.2f}s'


@pytest.mark.unit
def test_ip_verdict_landing_during_the_steam_check_still_wins():
    """The probe can publish while ``_check_steam_non_mainland`` is running.

    Returning Steam anyway would let the fallback outrank the authoritative verdict,
    and since ``get_core_config`` decides per URL, one snapshot could mix
    ``lanlan.tech`` and ``lanlan.app`` — they disagree exactly when a proxy is in play.
    """
    probe = _Probe()
    probe._ensure_ip_probe_started = lambda: None
    probe._check_ip_non_mainland_http = staticmethod(
        lambda: ConfigManager._ip_check_cache)

    def _steam_then_verdict_lands():
        ConfigManager._ip_check_cache = True     # 探测恰在此刻落地
        return False                             # Steam 说大陆（代理出口）

    probe._check_steam_non_mainland = _steam_then_verdict_lands
    assert probe._check_non_mainland() is True, 'IP 权威结论应压过 Steam 兜底票'
    assert ConfigManager._region_cache is True


@pytest.mark.unit
def test_livestream_derived_urls_do_not_trigger_the_probe(monkeypatch):
    """Livestream takes those routes over before the region is consulted.

    ``_adjust_free_api_url`` derives /core, /text/v1 and /tts from the livestream
    prefix without asking for a verdict, so a livestream user needs no probe for
    them and must not have their IP sent to ip-api.com on their account.
    """
    cfg = {
        'CORE_URL': 'wss://www.lanlan.tech/core',
        'TTS_URL': 'wss://www.lanlan.tech/tts',
        'ASSIST_URL': 'https://www.lanlan.tech/text/v1',
    }
    monkeypatch.setattr(config_manager_pkg, 'is_livestream_active', lambda: True)
    monkeypatch.setattr(
        config_manager_pkg, 'get_livestream_config',
        lambda: {'server_prefix': 'https://live.example/tok'})
    assert ConfigManager._config_needs_region(cfg) is False

    # 非派生路径仍然需要判定（livestream 只接管那三个端点）
    cfg['OTHER_URL'] = 'https://www.lanlan.tech/something-else'
    assert ConfigManager._config_needs_region(cfg) is True

    monkeypatch.setattr(config_manager_pkg, 'is_livestream_active', lambda: False)
    assert ConfigManager._config_needs_region(
        {'CORE_URL': 'wss://www.lanlan.tech/core'}) is True


@pytest.mark.unit
def test_startup_warmup_runs_after_runtime_config_is_finalized():
    """Warmup must sit after the Cloud Save import / Steamworks init.

    Reading config before that can see the pre-import values, conclude "no region
    needed", and never start the probe — leaving the first session on the fallback.
    Structural because the ordering, not the call itself, is the invariant.
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[2]
              / 'app' / 'main_server' / '__init__.py')
    tree = ast.parse(source.read_text(encoding='utf-8'))

    for node in ast.walk(tree):
        if (isinstance(node, ast.AsyncFunctionDef)
                and node.name == '_ensure_main_server_runtime_initialized'):
            break
    else:
        pytest.fail('未找到 _ensure_main_server_runtime_initialized，断言失效')

    # 只断言「在这个函数里」是不够的：预热被挪到 Cloud Save 导入或 Steamworks
    # 初始化之前时那样仍会通过，而那正是本 PR 要防的时序回归。比行号。
    seen = {}
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        name = getattr(call.func, 'attr', None) or getattr(call.func, 'id', None)
        if name in ('awarmup_region_check', 'initialize_steamworks',
                    '_sync_memory_server_after_startup_import',
                    '_disable_main_storage_limited_mode'):
            seen.setdefault(name, call.lineno)

    assert 'awarmup_region_check' in seen, 'GeoIP 预热必须在本函数内执行'
    for anchor, what in (('_sync_memory_server_after_startup_import', 'Cloud Save 导入'),
                         ('initialize_steamworks', 'Steamworks 初始化')):
        assert anchor in seen, f'锚点 {anchor} 不见了，本断言已失效'
        assert seen['awarmup_region_check'] > seen[anchor], \
            f'GeoIP 预热必须晚于{what}，否则可能读到成型前的配置'

    # 另一侧的边界：也不能晚于「放开会话准入」。那之后请求就能进来，若预热尚未
    # 落地，首个会话会整场钉在兜底线路——预热必须夹在「配置成型」与「准入」之间。
    assert '_disable_main_storage_limited_mode' in seen, '准入锚点不见了，本断言已失效'
    assert seen['awarmup_region_check'] < seen['_disable_main_storage_limited_mode'], \
        'GeoIP 预热必须早于解除 limited mode，否则会话可在区域未落定时进来'


def _plugin_files_constructing_offline_clients():
    """Every plugin file that builds an OmniOfflineClient — discovered, not listed.

    A hardcoded list is exactly how bilibili_danmaku and reply_buffer_service were
    missed: two plugins had the same freeze and the test only knew about the other
    two. Discovery makes a newly added plugin fail this test instead of shipping
    an unsettled route.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / 'plugin'
    return sorted(
        p for p in root.rglob('*.py')
        if 'OmniOfflineClient(' in p.read_text(encoding='utf-8')
    )


@pytest.mark.unit
def test_every_plugin_offline_client_settles_the_region():
    """Any plugin building an OmniOfflineClient must settle the region first.

    The client captures base_url at construction, so a route picked before the
    verdict lands is what that client keeps using. Checked per enclosing function
    and by line order: a settle call in some other function, or after the client is
    built, would satisfy a naive count while guaranteeing nothing.
    """
    import ast

    files = _plugin_files_constructing_offline_clients()
    assert files, '未发现任何构造 OmniOfflineClient 的插件文件，本断言已失效'

    problems = []
    for path in files:
        tree = parse_source_file(path)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            builds, settles = [], []
            for call in ast.walk(func):
                if not isinstance(call, ast.Call):
                    continue
                name = getattr(call.func, 'attr', None) or getattr(call.func, 'id', None)
                if name == 'OmniOfflineClient':
                    builds.append(call.lineno)
                elif name == 'aensure_region_resolved':
                    settles.append(call.lineno)
            for build_line in builds:
                if not [x for x in settles if x < build_line]:
                    problems.append(
                        f'{path.name}:{build_line} in {func.name}()'
                        f'（该函数内的落定调用: {settles or "无"}）'
                    )

    assert not problems, (
        '这些插件在构造 OmniOfflineClient 前没有先落定区域判定: ' + '; '.join(problems)
    )


@pytest.mark.unit
def test_custom_url_merely_containing_the_brand_string_is_not_a_free_route():
    """Eligibility keys on hostname, not substring.

    A custom endpoint like ``https://custom.example/v1/lanlan.tech`` is not the
    official free route; treating it as one starts the probe and discloses the
    user's IP for nothing.
    """
    assert ConfigManager._config_needs_region(
        {'CORE_URL': 'https://custom.example/v1/lanlan.tech'}) is False
    assert ConfigManager._config_needs_region(
        {'CORE_URL': 'https://www.lanlan.tech.evil.example/core'}) is False
    assert ConfigManager._config_needs_region(
        {'CORE_URL': 'wss://www.lanlan.tech/core'}) is True


@pytest.mark.unit
def test_eligibility_recheck_survives_the_overseas_rewrite():
    """The loop re-checks against an *already adjusted* snapshot.

    Once the region resolves overseas, ``get_core_config`` hands back ``lanlan.app``
    URLs. If eligibility only recognised ``lanlan.tech``, a Steam-overseas user with
    the IP probe still unresolved would look like "no longer on the free route" and
    the probe would quit after its first failure.
    """
    cfg = {'CORE_URL': 'wss://www.lanlan.app/core'}
    assert ConfigManager._config_needs_region(
        cfg, ConfigManager._REGION_HOSTS_ADJUSTED) is True


@pytest.mark.unit
def test_raw_config_gate_ignores_an_explicit_lanlan_app_endpoint():
    """Only ``lanlan.tech`` is ever rewritten, so a raw ``lanlan.app`` is a custom route.

    Accepting ``.app`` for raw user config — needed only when the loop inspects its
    own rewritten snapshot — would probe on behalf of someone whose URLs no region
    decision will ever touch. That is a privacy-gate violation, not a wasted request,
    which is why the two questions now take different host sets.
    """
    cfg = {'CORE_URL': 'wss://www.lanlan.app/core'}
    assert ConfigManager._config_needs_region(cfg) is False              # 默认 = RAW
    assert ConfigManager._config_needs_region(
        cfg, ConfigManager._REGION_HOSTS_RAW) is False
    # 免费路由本身不受影响
    assert ConfigManager._config_needs_region(
        {'CORE_URL': 'wss://www.lanlan.tech/core'}) is True


@pytest.mark.unit
def test_waiter_stops_when_the_attempt_fails_mid_wait(monkeypatch):
    """The wait tracks the current attempt, not the thread's lifetime.

    Joining the loop thread would block for the whole timeout whenever an attempt
    fails while someone is waiting: the loop stays alive in its 30-600s backoff,
    during which no verdict can possibly arrive. Startup and every session would
    pay the full timeout for nothing.
    """
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_BASE_S', 30.0)
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_MAX_S', 30.0)

    entered = threading.Event()
    fail_now = threading.Event()

    def _once():
        entered.set()
        fail_now.wait(10)          # 等测试发话再失败
        raise OSError('down')

    monkeypatch.setattr(ConfigManager, '_ip_probe_once', staticmethod(_once))

    _Probe()._ensure_ip_probe_started()
    assert entered.wait(5), '探测未进入请求阶段'
    assert ConfigManager._ip_probe_in_flight.is_set()

    # 在等待过程中让本次尝试失败：循环转入长退避但线程仍 alive
    def _fail_soon():
        real_time.sleep(0.15)
        fail_now.set()

    threading.Thread(target=_fail_soon, daemon=True).start()

    started = real_time.monotonic()
    assert ConfigManager.join_ip_probe(timeout=5) is False
    waited = real_time.monotonic() - started
    assert waited < 2.0, f'本次尝试已失败仍等了 {waited:.2f}s（应在转入退避时立刻返回）'
    assert ConfigManager._ip_probe_thread.is_alive(), '循环应仍在退避中（并未结束）'


@pytest.mark.unit
def test_malformed_livestream_prefix_still_needs_the_region(monkeypatch):
    """Excluding a URL is only safe when its livestream derivation actually succeeds.

    ``_derive_livestream_url`` rejects a prefix without scheme/netloc and falls back
    to the regional rewrite. Excluding on ``is_livestream_active()`` alone would then
    say "no region needed", start no probe, and pin an overseas user to lanlan.tech.
    """
    cfg = {'CORE_URL': 'wss://www.lanlan.tech/core'}
    monkeypatch.setattr(config_manager_pkg, 'is_livestream_active', lambda: True)

    # 畸形 prefix（缺 scheme）：派生会失败 → 仍然需要区域判定
    monkeypatch.setattr(
        config_manager_pkg, 'get_livestream_config',
        lambda: {'server_prefix': 'localhost:8080/tok'})
    assert ConfigManager._config_needs_region(cfg) is True

    # 空 prefix 同理
    monkeypatch.setattr(
        config_manager_pkg, 'get_livestream_config', lambda: {'server_prefix': ''})
    assert ConfigManager._config_needs_region(cfg) is True

    # 合法 prefix：派生成功 → 用不到区域判定
    monkeypatch.setattr(
        config_manager_pkg, 'get_livestream_config',
        lambda: {'server_prefix': 'https://live.example/tok'})
    assert ConfigManager._config_needs_region(cfg) is False


@pytest.mark.unit
def test_plugin_geoip_fallback_logging_uses_a_real_facility():
    """The fail-open handler must not raise on its own.

    These handlers exist so a probe error cannot stop a plugin session. Logging
    through an attribute the class does not define turns that inside out: the
    ``except`` raises ``AttributeError``, the original error is lost, and fail-open
    becomes fail-closed. Copying a logging idiom between plugin files is exactly how
    that slipped in twice, so check each site against its own class.
    """
    import ast

    problems = []
    for path in _plugin_files_constructing_offline_clients():
        source = path.read_text(encoding='utf-8')
        tree = ast.parse(source)
        lines = source.split('\n')

        for lineno, line in enumerate(lines, 1):
            if 'GeoIP' not in line or ('warning' not in line and '_emit_log' not in line):
                continue
            owner = None
            for node in ast.walk(tree):
                if (isinstance(node, ast.ClassDef)
                        and node.lineno <= lineno <= node.end_lineno):
                    owner = node
            if owner is None:
                problems.append(f'{path.name}:{lineno} 不在任何类内')
                continue

            body = lines[owner.lineno - 1:owner.end_lineno]
            expr = line.strip()
            if 'self.plugin.' in expr:
                attr = expr.split('self.plugin.')[1].split('(')[0]
                # 同类里别处也这么用 → 是该插件的既有惯例
                ok = sum(1 for x in body if f'self.plugin.{attr}' in x) > 1
                what = f'self.plugin.{attr}'
            elif 'self.logger' in expr:
                ok = 'logger' in {
                    t.attr for n in ast.walk(owner) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Attribute)
                    and isinstance(t.value, ast.Name) and t.value.id == 'self'
                }
                what = 'self.logger'
            else:
                ok, what = False, expr[:40]
            if not ok:
                problems.append(f'{path.name}:{lineno} [{owner.name}] 用了 {what}')

    assert not problems, (
        'GeoIP fail-open 处理器用了该类不存在的日志设施（会在 except 里再抛）: '
        + '; '.join(problems)
    )


@pytest.mark.unit
def test_loop_eligibility_reads_the_rewritten_snapshot_correctly(monkeypatch):
    """Exercises the call site, not just the predicate.

    ``_free_route_still_needs_region`` re-reads ``get_core_config()``, whose free URLs
    are already rewritten to ``lanlan.app`` once the region resolves overseas. Passing
    the raw host set there would read "user left the free route" and kill the probe
    after one failure — a mistake a predicate-only test cannot see.
    """
    class _FakeCM:
        @staticmethod
        def get_core_config():
            return {'CORE_URL': 'wss://www.lanlan.app/core', 'coreApi': 'free'}

    # autouse fixture 把这个方法桩成了恒 True（供其它用例用），本用例要测真实实现
    monkeypatch.setattr(
        ConfigManager, '_free_route_still_needs_region',
        core_config_mod.CoreConfigMixin.__dict__['_free_route_still_needs_region'])
    monkeypatch.setattr(config_manager_pkg, 'get_config_manager', lambda *a, **kw: _FakeCM())
    monkeypatch.setattr(config_manager_pkg, 'is_livestream_active', lambda: False)

    assert ConfigManager._free_route_still_needs_region() is True, \
        '海外改写后的快照仍属免费路由，探测不应因此收工'

    # 真正切走免费线路时才该收工
    class _CustomCM:
        @staticmethod
        def get_core_config():
            return {'CORE_URL': 'https://api.openai.com/v1', 'coreApi': 'openai'}

    monkeypatch.setattr(config_manager_pkg, 'get_config_manager', lambda *a, **kw: _CustomCM())
    assert ConfigManager._free_route_still_needs_region() is False

    # 自配端点恰好也在 lanlan.app：只看 host 分不清它和「被改写的免费 URL」，
    # 会让切走免费线路的用户继续被探测。路由选择字段不受改写影响，能区分。
    class _CustomAppCM:
        @staticmethod
        def get_core_config():
            return {'CORE_URL': 'wss://www.lanlan.app/core', 'coreApi': 'openai'}

    monkeypatch.setattr(config_manager_pkg, 'get_config_manager', lambda *a, **kw: _CustomAppCM())
    assert ConfigManager._free_route_still_needs_region() is False,         '显式配在 lanlan.app 的自配线路不应让探测继续'


# ---------------------------------------------------------------------------
# Voice cleanup must not act on a guessed region (it writes to characters.json)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_voice_cleanup_is_skipped_while_the_region_is_provisional(monkeypatch):
    """Clearing a voice is a permanent write; a provisional region is a guess.

    On the transient mainland fallback an overseas-only voice (``yui``, Gemini
    voices) is absent from the mainland catalog, so cleanup would strip it from
    characters.json. The verdict landing a second later fixes the endpoint but
    cannot restore the user's choice — so cleanup waits instead.
    """

    class _CM(config_manager_pkg.voice_storage.VoiceStorageMixin):
        def __init__(self, cfg):
            self._cfg = cfg

        def get_core_config(self):
            return dict(self._cfg)

        def load_characters(self):
            raise AssertionError('区域未落定时不应读取/改写角色数据')

    free_cfg = {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'}
    cm = _CM(free_cfg)
    cm._config_needs_region = ConfigManager._config_needs_region

    monkeypatch.setattr(ConfigManager, '_region_cache', None)
    assert cm._region_verdict_is_provisional() is True
    assert cm.cleanup_invalid_voice_ids() == (0, []), '未落定时应整体跳过清理'


@pytest.mark.unit
@pytest.mark.parametrize('region, cfg, provisional', [
    # 已落定 → 可清理
    (True, {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'}, False),
    (False, {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'}, False),
    # 自配线路与区域无关 → 可清理（否则它们的区域永不落定，清理会被永久禁用）
    (None, {'coreApi': 'openai', 'CORE_URL': 'https://api.openai.com/v1'}, False),
    (None, {'coreApi': 'openai', 'CORE_URL': 'wss://www.lanlan.app/core'}, False),
    # 免费 + 未落定 → 跳过。第二格是关键：Steam 临时判海外时快照已被改写成
    # lanlan.app，按 URL host 判会误判成自配线路而放行清理。
    (None, {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'}, True),
    (None, {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.app/core'}, True),
    # 配置残缺读不到路由选择 → 保守不删
    (None, {'CORE_URL': 'wss://www.lanlan.tech/core'}, True),
])
def test_provisional_region_predicate(monkeypatch, region, cfg, provisional):

    class _CM(config_manager_pkg.voice_storage.VoiceStorageMixin):
        def get_core_config(self):
            return dict(cfg)

    cm = _CM()
    cm._config_needs_region = ConfigManager._config_needs_region
    monkeypatch.setattr(ConfigManager, '_region_cache', region)
    assert cm._region_verdict_is_provisional() is provisional


@pytest.mark.unit
def test_this_file_has_no_duplicate_test_names():
    """A redefined test silently replaces the earlier one — it simply never runs.

    This file has grown by repeated appends and hit that twice already; both times a
    whole block of assertions was quietly dead until a reviewer noticed. Cheap to
    check, and it fails loudly instead.
    """
    import ast
    import collections
    import pathlib

    tree = ast.parse(pathlib.Path(__file__).read_text(encoding='utf-8'))
    names = [
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    dupes = {n: c for n, c in collections.Counter(names).items() if c > 1}
    assert not dupes, f'重名的测试函数（早先定义从未运行）: {dupes}'


@pytest.mark.unit
@pytest.mark.parametrize('cfg, expected', [
    ({'coreApi': 'free'}, True),
    ({'CORE_API_TYPE': 'free'}, True),
    # 付费 core + 免费 assist：assist 的 lanlan.tech URL 同样要区域改写，
    # 只看 core 会让这些用户的探测提前收工、assist 线路停在国内。
    ({'coreApi': 'openai', 'assistApi': 'free'}, True),
    ({'coreApi': 'openai', 'assistApi': 'qwen'}, False),
    ({}, False),
])
def test_free_provider_detection_covers_every_slot(cfg, expected):
    assert ConfigManager._any_free_provider(cfg) is expected


@pytest.mark.unit
def test_livestream_derived_route_is_not_provisional_forever(monkeypatch):
    """Livestream that derives every free endpoint needs no verdict — so never wait.

    Those configs deliberately start no probe, so ``_region_cache`` stays ``None``
    for the life of the process. Judging provisional on the provider slot alone would
    therefore disable voice cleanup and default-voice binding permanently for them.
    """
    monkeypatch.setattr(ConfigManager, '_region_cache', None)
    monkeypatch.setattr(config_manager_pkg, 'is_livestream_active', lambda: True)
    monkeypatch.setattr(
        config_manager_pkg, 'get_livestream_config',
        lambda: {'server_prefix': 'https://live.example/tok'})

    cfg = {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'}

    class _CM(config_manager_pkg.voice_storage.VoiceStorageMixin):
        def get_core_config(self):
            return dict(cfg)

    cm = _CM()
    cm._config_needs_region = ConfigManager._config_needs_region
    assert cm._region_verdict_is_provisional() is False, \
        'livestream 已派生掉全部免费端点，不该被永远判成未落定'

    # 而 livestream 没接管的路径仍然需要判定
    monkeypatch.setattr(
        config_manager_pkg, 'get_livestream_config', lambda: {'server_prefix': ''})
    assert cm._region_verdict_is_provisional() is True


@pytest.mark.unit
def test_startup_warmup_retries_a_backed_off_probe(monkeypatch):
    """Startup must get a verdict, even if the first attempt already failed.

    Voice cleanup now reads the config (and thus starts the probe) before warmup
    runs, so by warmup time the first attempt has often already failed on a
    not-yet-ready network and entered a 30s backoff. Returning immediately there
    would make warmup a no-op and admit sessions with no verdict.
    """
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_BASE_S', 30.0)
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_MAX_S', 30.0)
    calls = _patch_probe_once(monkeypatch, [OSError('network not up'), 'US'])

    probe = _Probe()
    probe.aget_core_config = _async_return(
        # 真实的免费路由形态：空真判定按「free provider + 区域敏感 URL」合取，
        # None/空配置会被判成无需区域而跳过等待，测不到等待路径
        {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'})

    # 首探失败并进入 30 秒退避（远长于预热愿意等的时间）
    ConfigManager._ensure_ip_probe_started()
    for _ in range(500):
        if calls['n'] >= 1 and not ConfigManager._ip_probe_in_flight.is_set():
            break
        real_time.sleep(0.01)
    assert calls['n'] == 1 and ConfigManager._ip_check_cache is None

    started = real_time.monotonic()
    assert asyncio.run(probe.awarmup_region_check(timeout=5)) is True, \
        '预热应当催重试并拿到结论，而不是因为在退避就立刻返回'
    assert real_time.monotonic() - started < 5, '不应等满 30 秒退避'
    assert calls['n'] == 2


@pytest.mark.unit
def test_warmup_does_not_wait_when_no_probe_is_running():
    """``through_backoff`` means "wait through a backoff", not "always wait".

    A paid/custom provider (or a fully livestream-derived route) never starts a
    probe at all. Startup warmup is the only ``through_backoff=True`` caller, so
    treating "no probe" like "backing off" made every such user pay the full
    timeout on every boot before session admission opened.
    """
    assert ConfigManager._ip_probe_thread is None or not ConfigManager._ip_probe_thread.is_alive()
    assert ConfigManager._ip_check_cache is None and ConfigManager._steam_check_cache is None

    started = real_time.monotonic()
    assert ConfigManager.join_ip_probe(timeout=5, through_backoff=True) is False
    elapsed = real_time.monotonic() - started
    assert elapsed < 1.0, f'没有探测在跑却等了 {elapsed:.2f}s'


@pytest.mark.unit
@pytest.mark.parametrize('settled', [True, False])
def test_game_session_refreshes_the_character_after_the_wait(monkeypatch, settled):
    """The character can change while we wait — regardless of the wait's outcome.

    Re-reading only inside ``if settled:`` covered the route-rewrite reason but
    not this one: on a fail-open timeout the pool would build the client from the
    pre-wait character and cache it under the stale key, so the event runs the
    wrong persona and leaves an entry no later event can hit.
    """
    from main_routers.game_router import session_pool as sp
    from main_routers import shared_state as sp_shared

    sp._game_sessions.clear()
    names = iter(['旧角色', '新角色'])
    monkeypatch.setattr(sp, '_get_character_info', lambda n=None: {'lanlan_name': next(names)})

    class _CM:
        async def aensure_region_resolved(self, timeout=1.5):
            return settled

    monkeypatch.setattr(sp_shared, 'get_config_manager', lambda: _CM())

    built = {}

    async def _fake_build(key, game_type, session_id, char_info, *, postgame_snapshot=None):
        built['key'] = key
        built['name'] = char_info.get('lanlan_name')
        return {'last_activity': 0.0}

    monkeypatch.setattr(sp, '_build_and_register_game_session', _fake_build)

    asyncio.run(sp._get_or_create_session('mc', 'sid'))

    assert built['name'] == '新角色', '等待期间角色已切换，必须用切换后的角色建会话'
    assert '新角色' in built['key'], f'会话被挂到了等待前的 key 上: {built["key"]}'


@pytest.mark.unit
def test_every_voice_cleanup_path_also_retries_the_deferred_binding():
    """A deferred default-voice binding needs a path that comes back for it.

    ``ensure_default_yui_voice_for_free_api`` skips binding while the region is
    provisional, promising "next round". Its only original callers were the
    config-save route and ``clear_voice_ids`` — neither of which runs again on
    its own, so switching to the free API left the default card permanently
    unbound. Session preparation is that next round; discovered automatically
    from the voice-cleanup call sites so a third path cannot silently skip it.
    """
    import ast
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[2] / 'main_logic' / 'core' / 'lifecycle.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))

    missing = []
    checked = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = set()
        for call in (c for c in ast.walk(node) if isinstance(c, ast.Call)):
            # 清理走 to_thread(方法引用)，绑定是直接函数调用——两种形态都要收
            for sub in ast.walk(call):
                if isinstance(sub, ast.Attribute):
                    calls.add(sub.attr)
                elif isinstance(sub, ast.Name):
                    calls.add(sub.id)
        if 'cleanup_invalid_voice_ids' not in calls:
            continue
        checked.append(node.name)
        if 'ensure_default_yui_voice_for_free_api' not in calls:
            missing.append(f'{node.name} (line {node.lineno})')

    assert len(checked) >= 2, f'未找到足够的音色清理路径，断言失效: {checked}'
    assert not missing, f'这些路径清理了音色却没补上被推迟的默认音色绑定: {missing}'


@pytest.mark.unit
@pytest.mark.parametrize('url', [
    'https://www.lanlan.tech/text/v1',
    'https://www.lanlan.app/text/v1',
])
def test_agent_url_is_exempt_from_the_region_rewrite(config_manager, url):
    """``AGENT_MODEL_URL`` deliberately never follows the region switch.

    free-agent-model is pinned to the CN text entry, so ``_normalize_agent_url``
    is an identity function and the Agent route carries no region dependency at
    all. Pinned because the exemption is easy to mistake for a missing rewrite —
    a review already read it that way — and because turning it into a real
    rewrite would silently move every Agent request to a different endpoint.
    """
    assert config_manager._normalize_agent_url(url) == url


@pytest.mark.unit
def test_region_sensitive_voice_endpoints_settle_first():
    """Endpoints serving the voice catalog settle the region before reading it.

    The mainland ``free`` and overseas ``free_intl`` catalogs are disjoint, so a
    response assembled across a landing verdict can offer a voice the runtime
    route then refuses. Discovered from the catalog readers rather than a
    hardcoded endpoint list, so a third endpoint cannot quietly skip it.
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[2]
              / 'main_routers' / 'characters_router' / 'voice_preview.py')
    tree = ast.parse(source.read_text(encoding='utf-8'))

    readers = {'get_voices_for_current_api', 'get_active_realtime_native_provider_for_ui'}
    missing = []
    checked = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # 只管 HTTP 端点：helper 由端点调用，落定在端点入口做一次即可
        is_endpoint = any(
            isinstance(d, ast.Call) and getattr(d.func, 'attr', None) in {'get', 'post'}
            and getattr(getattr(d.func, 'value', None), 'id', None) == 'router'
            for d in node.decorator_list
        )
        if not is_endpoint:
            continue
        calls = {getattr(c.func, 'attr', None) or getattr(c.func, 'id', None)
                 for c in ast.walk(node) if isinstance(c, ast.Call)}
        if not (calls & readers):
            continue
        checked.append(node.name)
        if 'aensure_region_resolved' not in calls:
            missing.append(f'{node.name} (line {node.lineno})')

    assert len(checked) >= 2, f'未找到足够的音色目录端点，断言失效: {checked}'
    assert not missing, f'这些端点按区域出音色目录却未先落定: {missing}'


def _yui_binding_manager(authoritative_cfg, saved, probe_calls=None, non_mainland=False):
    """A minimal manager for ``ensure_default_yui_voice_for_free_api``."""

    class _CM:
        def __init__(self):
            # 生产签名带可选 cfg 快照参数，替身要兼容两种调用形态
            self._region_verdict_is_provisional = lambda *_a: False

        async def aget_core_config(self):
            return dict(authoritative_cfg)

        async def aload_characters(self):
            model_path = config_manager_pkg.persona_payload.DEFAULT_YUI_LIVE2D_MODEL_PATH
            return {
                '当前猫娘': 'YUI',
                '猫娘': {'YUI': {'昵称': 'YUI', 'live2d': model_path, 'voice_id': ''}},
            }

        async def asave_characters(self, characters):
            # set_reserved 写的是 reserved 结构，不是扁平键——按生产的读法取回
            from utils.config_manager.reserved_schema import get_reserved
            from utils.voice_config import read_legacy_voice_id
            saved['voice_id'] = read_legacy_voice_id(get_reserved(
                characters['猫娘']['YUI'], 'voice_id', default='', legacy_keys=('voice_id',),
            ))

        def _check_non_mainland(self):
            # 计数而不是抛：helper 把这里的异常吞成 overseas=False，与「压根没调用」
            # 结果完全一样——抛异常的断言测不出差别（变异实测漏过）。
            if probe_calls is not None:
                probe_calls['n'] += 1
            return non_mainland

    return _CM()


@pytest.mark.unit
def test_yui_binding_ignores_a_stale_caller_snapshot(monkeypatch):
    """The binding is permanent, so it must read the verdict as of *now*.

    ``start_session`` assembles its core-config snapshot before this helper runs.
    If Steam provisionally said overseas, that snapshot already carries the
    ``.app`` URL — and the authoritative probe may resolve mainland right after.
    Trusting the caller's snapshot would write literal ``yui`` into a mainland
    user's character card, and the helper never overwrites a nonempty voice, so
    no later session could correct it. The two free catalogs are disjoint, so
    that card would simply never get its voice.
    """
    monkeypatch.setattr(ConfigManager, '_region_cache', False)      # 权威结论：大陆
    saved = {}
    stale = {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.app/core'}
    authoritative = {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'}
    cm = _yui_binding_manager(authoritative, saved)
    cm._check_non_mainland = lambda: ConfigManager._region_cache

    assert asyncio.run(config_manager_pkg.ensure_default_yui_voice_for_free_api(cm, stale)) is True
    assert saved.get('voice_id') and saved['voice_id'] != 'yui', \
        f'用了调用方的陈旧 .app 快照，给大陆用户绑了海外音色: {saved}'


@pytest.mark.unit
def test_yui_binding_does_not_probe_for_a_livestream_derived_route():
    """A fully livestream-derived route is deterministic — it needs no GeoIP.

    ``_check_non_mainland()`` starts the probe internally, so falling through to
    it here would hand these users' IP to ip-api.com just to pick a voice, which
    is exactly what invariant #2 forbids. They bind the mainland free voice:
    what livestream derives is the equivalent of the ``lanlan.tech`` layout.
    """
    saved = {}
    derived = {
        'coreApi': 'free',
        'CORE_URL': 'ws://192.168.1.9:8000/tok/core',
        'livestream_server_prefix': 'http://192.168.1.9:8000/tok',
    }
    probe_calls = {'n': 0}
    cm = _yui_binding_manager(derived, saved, probe_calls)

    assert asyncio.run(config_manager_pkg.ensure_default_yui_voice_for_free_api(cm, dict(derived))) is True
    assert probe_calls['n'] == 0, '_check_non_mainland 内部会起探测，这条线路不该问它'
    assert saved.get('voice_id') and saved['voice_id'] != 'yui'
    assert ConfigManager._ip_probe_thread is None, '不该为绑音色起 GeoIP 探测'


@pytest.mark.unit
def test_newly_selected_free_route_wakes_a_backed_off_probe(config_manager, monkeypatch):
    """Switching to the free route at runtime must not wait out a long backoff.

    Only startup warmup called ``_kick_ip_probe``; the config-save and session
    paths never did. A user who switches to the free provider while the probe
    sleeps off an earlier failure would otherwise start a session pinned to the
    mainland fallback for up to the remaining 600s — and a session freezes its
    route for its whole lifetime.
    """
    import json as _json

    kicks = {'n': 0}
    monkeypatch.setattr(ConfigManager, '_kick_ip_probe',
                        staticmethod(lambda: kicks.__setitem__('n', kicks['n'] + 1)))
    monkeypatch.setattr(ConfigManager, '_check_non_mainland', lambda self: False)
    monkeypatch.setattr(ConfigManager, '_free_route_selected', False)

    path = config_manager.get_config_path('core_config.json')

    def _read(provider):
        with open(str(path), 'w', encoding='utf-8') as fh:
            _json.dump({'coreApi': provider}, fh)
        config_manager._core_config_cache = None
        config_manager.get_core_config()

    _read('qwen')
    assert kicks['n'] == 0, '没选中免费路由时不该催醒'

    _read('free')
    assert kicks['n'] == 1, '新选中免费路由应催醒一次'

    _read('free')
    assert kicks['n'] == 1, '每次读配置都催会让退避形同虚设，被墙时变成请求风暴'

    _read('qwen')
    _read('free')
    assert kicks['n'] == 2, '切走再切回是新的一次边沿，应再催一次'


@pytest.mark.unit
def test_yui_binding_survives_a_raw_config_without_urls():
    """The config-save caller passes the *persisted* config, which has no URLs.

    core_config.json holds only route-selection fields (coreApi / assistApi);
    every ``*_URL`` is assembled by ``get_core_config`` from the profile. Gating
    the region check on that raw dict makes it look like nothing depends on the
    region, which binds every overseas user to the mainland voice — permanently,
    since a nonempty voice is never overwritten.
    """
    saved = {}
    raw = {'coreApi': 'free', 'assistApi': 'free'}       # 持久化形态：一个 URL 都没有
    assembled = {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.app/core'}
    cm = _yui_binding_manager(assembled, saved, non_mainland=True)

    ok = asyncio.run(config_manager_pkg.ensure_default_yui_voice_for_free_api(cm, raw))
    assert ok is True
    assert saved.get('voice_id') == 'yui',         f'拿没有 URL 的 raw 配置判「是否依赖区域」，把海外用户绑成了大陆音色: {saved}'


@pytest.mark.unit
def test_paths_that_pick_a_voice_and_build_a_tts_url_settle_first():
    """One operation reading the region twice must pin it once, up front.

    Picking the voice reads the region-dependent catalog; building the TTS
    endpoint reads the region again. A verdict landing between them sends a
    mainland ``free_voices`` id to ``lanlan.app`` (or the reverse), and the two
    catalogs are disjoint, so that synthesis simply fails. Discovered from the
    pair of calls rather than a hardcoded file list.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    missing = []
    checked = []
    for base in ('plugin', 'main_routers'):
        for source in (root / base).rglob('*.py'):
            try:
                tree = ast.parse(source.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                # 留行号，别压成集合：只断言「调用存在」的话，落定被挪到两个读取
                # **之后**测试照样绿——那正是这道护栏要防的东西。
                lines = {}
                for c in ast.walk(node):
                    if not isinstance(c, ast.Call):
                        continue
                    name = getattr(c.func, 'attr', None) or getattr(c.func, 'id', None)
                    if name:
                        lines.setdefault(name, []).append(c.lineno)
                if not ({'get_voices_for_current_api', '_adjust_free_tts_url'} <= lines.keys()):
                    continue
                rel = source.relative_to(root).as_posix()
                checked.append(f'{rel}::{node.name}')
                settles = lines.get('aensure_region_resolved')
                if not settles:
                    missing.append(f'{rel}::{node.name} (line {node.lineno}) 未落定')
                    continue
                first_read = min(min(lines['get_voices_for_current_api']),
                                 min(lines['_adjust_free_tts_url']))
                if min(settles) >= first_read:
                    missing.append(
                        f'{rel}::{node.name} 落定在 line {min(settles)}，'
                        f'晚于第一次区域敏感读取 line {first_read}'
                    )

    if not checked:
        # 唯一符合条件的样本（plugin/plugins/qq_auto_reply/voice_reply_service.py 的
        # synthesize_reply_voice_audio）随市场插件一起被 gitignore，CI 检出里不存在，
        # 于是扫描为空。此时跳过而非断言失败——测试只对「仓库内确实存在该模式」时生效，
        # 避免护栏在无样本的检出里因空集而阻塞 CI。
        pytest.skip('未找到任何「挑音色 + 拼 TTS 端点」的路径（样本在 gitignore 的市场插件中），跳过')
    assert not missing, f'这些路径在一次操作里两次读区域却未先落定: {missing}'


@pytest.mark.unit
def test_agent_deduper_is_built_after_the_region_settles():
    """``TaskDeduper`` freezes the ``summary`` base URL in ``__init__`` forever.

    It caches the built client on ``self.llm``, so a transient mainland answer
    pins every later duplicate check for the whole process. agent_server running
    as its own process never sees the main server's warmup. Distinct from the
    Agent proxy, which is deliberately exempt from the region rewrite.
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[2]
              / 'app' / 'agent_server' / 'api_runtime.py')
    tree = ast.parse(source.read_text(encoding='utf-8'))

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != 'startup':
            continue
        # 必须是启动预热原语，会话级 aensure 不够：上游 ComputerUseAdapter 构造时
        # 已读配置起了探测，首探在网络未就绪时快速失败进 30s 退避——aensure 不
        # kick、不穿退避，撞上退避就放弃，本进程照旧按大陆兜底构造 deduper。
        settles = [c.lineno for c in ast.walk(node)
                   if isinstance(c, ast.Call)
                   and getattr(c.func, 'attr', None) == 'awarmup_region_check']
        builds = [c.lineno for c in ast.walk(node)
                  if isinstance(c, ast.Call) and getattr(c.func, 'id', None) == 'TaskDeduper']
        assert builds, '未找到 TaskDeduper 构造，断言失效'
        assert settles, 'agent_server 启动未落定区域判定'
        assert min(settles) < min(builds), \
            f'落定(line {min(settles)}) 必须早于 TaskDeduper 构造(line {min(builds)})'
        break
    else:
        pytest.fail('未找到 agent_server startup，断言失效')


@pytest.mark.unit
def test_deduper_rebuilds_its_client_when_the_route_changes(monkeypatch):
    """The ``summary`` route can change after construction — the client must follow.

    A Steam answer is deliberately usable-but-not-latched, so the authoritative
    IP probe can overturn it seconds after ``agent_server`` starts. Freezing the
    client in ``__init__`` pinned every later dedup call to whichever endpoint
    happened to win that instant, for the whole process lifetime. Waiting longer
    at startup cannot fix that — only rechecking the route on use can.
    """
    from brain import deduper as deduper_mod

    cfg = {'model': 'm', 'base_url': 'https://www.lanlan.tech/text/v1', 'api_key': 'k'}

    class _CM:
        def get_model_api_config(self, kind):
            assert kind == 'summary'
            return dict(cfg)

    monkeypatch.setattr(deduper_mod, 'get_config_manager', lambda *a, **kw: _CM())

    built = []

    def _fake_create(model, base_url, api_key, **kwargs):
        built.append(base_url)
        return object()

    monkeypatch.setattr(deduper_mod, 'create_chat_llm', _fake_create)

    d = deduper_mod.TaskDeduper()
    assert built == ['https://www.lanlan.tech/text/v1']

    d._get_llm()
    assert len(built) == 1, '路由没变不该重建'

    cfg['base_url'] = 'https://www.lanlan.app/text/v1'
    d._get_llm()
    assert built[-1] == 'https://www.lanlan.app/text/v1', \
        'IP 结论推翻 Steam 兜底后，去重器必须跟着换端点'


# ---------------------------------------------------------------------------
# "Settled" is vacuously true when nothing in the config depends on the region
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_custom_route_settle_is_vacuously_true(monkeypatch):
    """A config with no region-dependent URL has nothing to settle — that is success.

    Returning False there made every voice-catalog / voice-reply request from a
    custom-API, non-Steam user log a spurious "region still unresolved" warning
    — burying the one signal those warnings exist for (a free-route user whose
    probe has not landed).
    """
    def _boom(*a, **kw):
        raise AssertionError('自配 API 不该为落定发起任何等待')

    probe = _Probe()
    # 桩在实例上而不是 ConfigManager 上：_Probe 只继承 CoreConfigMixin，
    # self.join_ip_probe 解析不到 ConfigManager 的类属性，patch 那边是死桩。
    probe.join_ip_probe = _boom
    probe._check_steam_non_mainland = lambda: None
    probe.aget_core_config = _async_return(
        {'coreApi': 'openai', 'CORE_URL': 'https://api.openai.com/v1'})

    started = real_time.monotonic()
    assert asyncio.run(probe.aensure_region_resolved(timeout=5)) is True, \
        '无区域敏感 URL 的配置没有什么可落定，应当视为已落定'
    assert real_time.monotonic() - started < 0.5
    assert ConfigManager._ip_probe_thread is None, '自配 API 不该起探测'


@pytest.mark.unit
def test_free_route_without_a_probe_still_reports_unsettled(monkeypatch):
    """The False branch must survive: free route + no verdict is the real alarm case.

    The vacuous-truth carve-out above is keyed on ``_config_needs_region``; a
    free-route snapshot (either host form — the loop may already have rewritten
    it) with no probe running and no verdict must keep returning False so the
    voice endpoints' warning still fires when it should.
    """
    probe = _Probe()
    probe._check_steam_non_mainland = lambda: None
    probe.aget_core_config = _async_return(
        {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.app/core'})

    assert ConfigManager._ip_probe_thread is None, '前置条件：没有探测在跑'
    assert asyncio.run(probe.aensure_region_resolved(timeout=0.1)) is False


@pytest.mark.unit
def test_warmup_reports_success_when_no_region_is_needed():
    """Startup warmup on a custom/paid route: nothing to resolve is not a failure.

    Returning False there made ``_ensure_main_server_runtime_initialized`` log
    its retry-pending message on every boot for users whose config never
    starts a probe — there is no retry coming, and nothing was missing.
    """
    probe = _Probe()
    probe.aget_core_config = _async_return(
        {'coreApi': 'openai', 'CORE_URL': 'https://api.openai.com/v1'})

    started = real_time.monotonic()
    assert asyncio.run(probe.awarmup_region_check(timeout=5)) is True
    assert real_time.monotonic() - started < 1.0, '无需区域判定时预热不应等待'
    assert ConfigManager._ip_probe_thread is None


@pytest.mark.unit
def test_warmup_still_reports_failure_on_the_free_route(monkeypatch):
    """Free route with an unreachable probe: warmup must still say "no verdict"."""
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_BASE_S', 30.0)
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_MAX_S', 30.0)
    _patch_probe_once(monkeypatch, [OSError('down')])

    probe = _Probe()
    probe.aget_core_config = _async_return(
        {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'})

    ConfigManager._ensure_ip_probe_started()
    assert asyncio.run(probe.awarmup_region_check(timeout=0.3)) is False, \
        '免费路由拿不到结论时预热必须如实报 False（启动日志靠它区分场景）'


# ---------------------------------------------------------------------------
# Restarting the probe must not inherit a leftover stop signal
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_probe_restart_clears_a_leftover_stop_signal(monkeypatch):
    """A stop signal belongs to the loop it was aimed at, not to later restarts.

    ``_ip_probe_stopping`` is set by shutdown/test cleanup to kill the current
    loop. If a restart (user switches away from the free route and back) does
    not reset it, the new loop dies on its first wake-up and ``_kick_ip_probe``
    refuses to kick — the startup warmup can then never shorten a backoff.
    """
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_BASE_S', 30.0)
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_MAX_S', 30.0)
    monkeypatch.setattr(ConfigManager, '_ip_probe_stopping', True)   # 上一轮的残留
    calls = _patch_probe_once(monkeypatch, [OSError('down'), 'US'])

    _Probe()._ensure_ip_probe_started()
    assert ConfigManager._ip_probe_stopping is False, '重启探测必须复位停止位'

    # 等首探失败、进入 30s 退避
    for _ in range(500):
        if calls['n'] >= 1 and not ConfigManager._ip_probe_in_flight.is_set():
            break
        real_time.sleep(0.01)
    assert calls['n'] == 1 and ConfigManager._ip_check_cache is None

    # 停止位已清 → kick 必须能催醒退避中的循环并让它重试成功，
    # 而不是被拒绝（kick 检查 stopping）或把循环误杀（循环唤醒时检查 stopping）
    ConfigManager._kick_ip_probe()
    thread = ConfigManager._ip_probe_thread
    thread.join(5)
    assert ConfigManager._ip_check_cache is True, \
        '催醒后的循环应重试并落地结论，而不是因残留停止位退出'
    assert calls['n'] == 2


@pytest.mark.unit
def test_explicit_lanlan_app_custom_endpoint_settles_vacuously():
    """A custom provider explicitly hosted at lanlan.app is not a free route.

    The vacuous-truth predicate must be the conjunction the codebase already
    uses (``_any_free_provider`` AND ``_config_needs_region``, matching
    ``_free_route_still_needs_region``): keying on the host alone would judge
    these users "unresolved" — the RAW gate never probes for them and their
    URLs are never rewritten, so no probe would ever end that false warning.
    """
    cfg = {'coreApi': 'openai', 'CORE_URL': 'wss://www.lanlan.app/core'}

    probe = _Probe()
    probe._check_steam_non_mainland = lambda: None
    probe.aget_core_config = _async_return(dict(cfg))
    assert asyncio.run(probe.aensure_region_resolved(timeout=5)) is True, \
        '显式配在 lanlan.app 的自配线路不依赖区域判定，应视为已落定'

    warm = _Probe()
    warm.aget_core_config = _async_return(dict(cfg))
    assert asyncio.run(warm.awarmup_region_check(timeout=5)) is True
    assert ConfigManager._ip_probe_thread is None, '自配线路不该起探测'


@pytest.mark.unit
def test_warmup_recognises_the_rewritten_free_snapshot_as_unsettled():
    """The warmup fallback must judge the *adjusted* snapshot with ADJUSTED hosts.

    A free-route snapshot can already carry ``lanlan.app`` URLs when warmup
    reads it. With RAW hosts the fallback would call that "nothing depends on
    the region" and report success — hiding exactly the free-route-unsettled
    case the startup log message exists for. Mirror of
    ``test_free_route_without_a_probe_still_reports_unsettled`` on the warmup
    side (a RAW mutation here survived the rest of the suite).
    """
    probe = _Probe()
    probe.aget_core_config = _async_return(
        {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.app/core'})

    assert ConfigManager._ip_probe_thread is None, '前置条件：没有探测在跑'
    assert asyncio.run(probe.awarmup_region_check(timeout=0.3)) is False


@pytest.mark.unit
def test_deduper_concurrent_refresh_never_mismatches_route_and_client(monkeypatch):
    """Behavioural smoke: the cached (route, client) pair stays coherent under load.

    Four threads hammer refreshes through 200 route flips with a shrunk GIL
    switch interval, asserting every observed cache pair is self-consistent.
    The mismatch window of a split-write implementation is microseconds wide,
    so this hammer alone cannot reliably catch one — the atomicity guarantee
    itself is pinned structurally by
    ``test_deduper_cache_is_published_atomically``; this test covers the read
    side (lookup and return path never hand out a client that contradicts the
    published route) under real concurrency.
    """
    from brain import deduper as deduper_mod

    route_holder = {'url': 'https://www.lanlan.tech/text/v1'}

    class _CM:
        def get_model_api_config(self, kind):
            return {'model': 'm', 'base_url': route_holder['url'], 'api_key': 'k'}

    monkeypatch.setattr(deduper_mod, 'get_config_manager', lambda *a, **kw: _CM())

    class _Client:
        def __init__(self, base_url):
            self.built_for = base_url

    def _fake_create(model, base_url, api_key, **kwargs):
        real_time.sleep(0.0005)      # 拉开「构建」窗口，让并发刷新真正重叠
        return _Client(base_url)

    monkeypatch.setattr(deduper_mod, 'create_chat_llm', _fake_create)
    d = deduper_mod.TaskDeduper()

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)      # 放大线程抢占，让相邻两次属性写之间可被插队
    stop = threading.Event()
    seen_mismatch = []
    worker_errors = []

    def _hammer():
        # 异常必须收集：worker 静默死掉的话 seen_mismatch 恒空、终态断言又被
        # __init__ 的首次发布兜住，测试会假绿。
        try:
            while not stop.is_set():
                d._get_llm()
                cached = d._llm_cache
                # 不变量：任何时刻读到的缓存对都必须自洽（client 按 route 构建）。
                # 分离写的实现在两次赋值之间必然暴露不自洽窗口，高频读能撞到。
                if cached is not None and cached[1].built_for != cached[0][0]:
                    seen_mismatch.append((cached[1].built_for, cached[0][0]))
        except Exception as e:      # noqa: BLE001
            worker_errors.append(repr(e))

    workers = [threading.Thread(target=_hammer, daemon=True) for _ in range(4)]
    try:
        for w in workers:
            w.start()
        urls = ('https://www.lanlan.app/text/v1', 'https://www.lanlan.tech/text/v1')
        for i in range(200):
            route_holder['url'] = urls[i % 2]
            real_time.sleep(0.001)
    finally:
        stop.set()
        for w in workers:
            w.join(5)
        sys.setswitchinterval(old_interval)

    assert not worker_errors, f'压测线程异常退出（压测未真正执行）: {worker_errors[:3]}'
    assert not seen_mismatch, f'缓存出现过 route/client 错配: {seen_mismatch[:3]}'
    route, client = d._llm_cache
    assert client.built_for == route[0], \
        f'终态错配：client 按 {client.built_for} 构建，route 标记是 {route[0]}'


@pytest.mark.unit
def test_deduper_cache_is_published_atomically():
    """The deduper's (route, client) cache must be one tuple, published whole.

    Split ``_llm`` / ``_llm_route`` attributes — or a tuple write that reuses a
    component of the previous cache — reintroduce the interleaving where an old
    client ends up tagged with the new route fingerprint and every later call
    trusts it as current. The mismatch window is microseconds wide, far below
    what a probabilistic hammer can reliably hit, so the atomicity is pinned
    structurally: one cache attribute, and every publish is a self-contained
    two-tuple (or the None initializer).
    """
    import ast
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[2] / 'brain' / 'deduper.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == 'TaskDeduper':
            cls = node
            break
    else:
        pytest.fail('未找到 TaskDeduper，断言失效')

    assigns = {}
    for n in ast.walk(cls):
        # AnnAssign（self.x: T = ...）也要收：分离属性用带类型注解的赋值形式
        # 写，只遍历 ast.Assign 的话会漏检。
        if isinstance(n, ast.Assign):
            targets = n.targets
        elif isinstance(n, ast.AnnAssign):
            targets = [n.target]
        else:
            continue
        for t in targets:
            if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                    and t.value.id == 'self'):
                assigns.setdefault(t.attr, []).append(n)

    banned = {'_llm', '_llm_route'} & assigns.keys()
    assert not banned, \
        f'route 与 client 必须收在一个元组属性里原子发布，发现分离属性: {sorted(banned)}'

    # AnnAssign 允许无值的纯声明（self.x: T），那不是一次发布，发布形态检查
    # 只看带值的赋值；banned 检查在上面，声明形式的分离属性照拦。
    cache_writes = [n for n in assigns.get('_llm_cache', []) if n.value is not None]
    assert cache_writes, '未找到 _llm_cache 发布点，断言失效'
    for n in cache_writes:
        v = n.value
        # 合法发布只有两种形态：None 初始化，或两个**裸局部名**组成的二元组
        # （route, llm）。收得比「不含 self.xxx」更紧：旧缓存分量可以先存进局部
        # （cached = self._llm_cache）再以 cached[1] / cached[1].x 之类的表达式
        # 混进元组，属性检查拦不住——裸 Name 白名单把下标/属性/调用/条件表达式
        # 一律拒绝。启发式护栏：Name 的**数据流**来源不做深度分析（x = cached[1]
        # 再放 x 仍可绕过），那一步交给 review；这里挡住所有直接形态。
        whole = (isinstance(v, ast.Constant) and v.value is None) or (
            isinstance(v, ast.Tuple) and len(v.elts) == 2
            and all(isinstance(e, ast.Name) for e in v.elts)
        )
        assert whole, (
            f'deduper.py:{n.lineno}: _llm_cache 的发布必须是「两个裸局部名的'
            '二元组」或 None 初始化——任何引用旧缓存分量的表达式都会重新引入'
            '交错错配'
        )


@pytest.mark.unit
def test_agent_url_survives_the_overseas_rewrite_end_to_end(config_manager, monkeypatch):
    """The Agent exemption must hold on the assembled snapshot, not just the predicate.

    ``_normalize_agent_url`` is an identity function — it cannot undo a rewrite
    the generic ``*_URL`` loop already applied, so the exemption has to happen
    inside that loop. The predicate-only test above kept passing while the
    assembled config shipped a ``lanlan.app`` Agent URL; this pins the actual
    ``get_core_config`` output: overseas verdict, free route — every other free
    URL flips to ``.app``, the Agent URL stays on the CN ``lanlan.tech`` entry.
    """
    import json as _json

    path = config_manager.get_config_path('core_config.json')
    with open(str(path), 'w', encoding='utf-8') as fh:
        _json.dump({'coreApi': 'free'}, fh)

    monkeypatch.setattr(ConfigManager, '_region_cache', True)
    monkeypatch.setattr(ConfigManager, '_ip_check_cache', True)

    cfg = config_manager.get_core_config()

    agent_url = cfg.get('AGENT_MODEL_URL') or ''
    assert 'lanlan.tech' in agent_url, \
        f'AGENT_MODEL_URL 被区域改写卷走了（free-agent-model 固定 CN 入口）: {agent_url}'
    rewritten = [v for k, v in cfg.items()
                 if k.endswith('_URL') and k != 'AGENT_MODEL_URL'
                 and isinstance(v, str) and 'lanlan.app' in v]
    assert rewritten, '前置条件：海外判定下其它免费 URL 应已改写为 lanlan.app'


@pytest.mark.unit
def test_kicked_probe_counts_as_in_flight_immediately(monkeypatch):
    """A kicked (deliberately awakened) attempt must be waitable before it is scheduled.

    ``_kick_ip_probe`` used to only set the wake event; until the OS scheduled
    the sleeping loop, ``_ip_probe_in_flight`` stayed clear, so a join arriving
    in that window (switch back to the free route, immediately start a session)
    treated the deliberately awakened attempt as ordinary backoff and gave up
    instantly — freezing that session on the mainland fallback while the
    awakened HTTP attempt started moments later.
    """
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_BASE_S', 30.0)
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_MAX_S', 30.0)
    calls = _patch_probe_once(monkeypatch, [OSError('down'), 'US'])

    _Probe()._ensure_ip_probe_started()
    for _ in range(500):
        if calls['n'] >= 1 and not ConfigManager._ip_probe_in_flight.is_set():
            break
        real_time.sleep(0.01)
    assert calls['n'] == 1 and not ConfigManager._ip_probe_in_flight.is_set(), \
        '前置条件：首探已失败并进入退避'

    # kick 后立刻 join——不给被唤醒的线程任何调度先机
    ConfigManager._kick_ip_probe()
    assert ConfigManager._ip_probe_in_flight.is_set(), 'kick 必须同步预置 in-flight'
    assert ConfigManager.join_ip_probe(timeout=5) is True, \
        '被刻意唤醒的尝试应当被等到，而不是被当成普通退避直接放弃'
    assert ConfigManager._ip_check_cache is True


@pytest.mark.unit
@pytest.mark.parametrize('forced', [True, False])
def test_forced_override_counts_as_settled(monkeypatch, forced):
    """``GEOIP_FORCE_NON_MAINLAND`` bypasses probing entirely — nothing is pending.

    The override populates no cache and starts no probe, so cache-only
    settlement predicates reported "unresolved" forever: startup warmup burned
    its full timeout every boot, and the provisional gate permanently
    suppressed voice cleanup and default-YUI binding on free routes.
    """
    monkeypatch.setattr(core_config_mod, 'GEOIP_FORCE_NON_MAINLAND', forced)

    probe = _Probe()
    probe.aget_core_config = _async_return(
        {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'})
    started = real_time.monotonic()
    assert asyncio.run(probe.aensure_region_resolved(timeout=5)) is True
    assert asyncio.run(probe.awarmup_region_check(timeout=5)) is True
    assert real_time.monotonic() - started < 1.0, 'override 下不应发生任何等待'
    assert ConfigManager._ip_probe_thread is None, 'override 下不应起探测'

    class _CM(config_manager_pkg.voice_storage.VoiceStorageMixin):
        def get_core_config(self):
            return {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'}

    cm = _CM()
    cm._config_needs_region = ConfigManager._config_needs_region
    assert cm._region_verdict_is_provisional() is False, \
        'override 是确定结论，不该判成 provisional（会永久禁用清理与默认音色绑定）'


@pytest.mark.unit
def test_yui_binding_rechecks_provider_on_the_assembled_snapshot():
    """A free→paid switch racing the binding must not persist a free-only voice.

    The caller's snapshot passed the free-only gate, but the awaited assembled
    read can come back with the new paid provider. Without a recheck the code
    falls through with overseas=False and writes ``yui_cn`` into a paid
    configuration — and the helper never overwrites a nonempty voice, so no
    later run corrects it.
    """
    saved = {}
    caller_raw = {'coreApi': 'free'}
    assembled_paid = {'coreApi': 'qwen', 'CORE_URL': 'https://api.qwen.example/v1'}
    cm = _yui_binding_manager(assembled_paid, saved)

    ok = asyncio.run(config_manager_pkg.ensure_default_yui_voice_for_free_api(cm, caller_raw))
    assert ok is False
    assert saved == {}, f'free→paid 切换竞态下不该写入任何音色: {saved}'


@pytest.mark.unit
def test_provisional_predicate_with_snapshot_does_not_reread_config(monkeypatch):
    """Passing an assembled snapshot must keep the predicate pure in-memory.

    Its production callers sit on the shared event loop; the self-read branch
    is a sync open()+json.load() that belongs on worker threads only.
    """
    reads = {'n': 0}

    class _CM(config_manager_pkg.voice_storage.VoiceStorageMixin):
        def get_core_config(self):
            # 计数而不是抛：谓词整体包在 try/except 里，抛异常会被吞成
            # 「保守 True」，与正确行为在断言上无差别（变异实测漏过）。
            reads['n'] += 1
            return {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'}

    cm = _CM()
    cm._config_needs_region = ConfigManager._config_needs_region
    monkeypatch.setattr(ConfigManager, '_region_cache', None)

    cfg = {'coreApi': 'free', 'CORE_URL': 'wss://www.lanlan.tech/core'}
    assert cm._region_verdict_is_provisional(cfg) is True
    assert reads['n'] == 0, '传入快照时不应再自读配置（同步读会跑在事件循环上）'
    # 不传快照的自读分支仍然可用
    assert cm._region_verdict_is_provisional() is True
    assert reads['n'] == 1


@pytest.mark.unit
def test_deprecated_voice_migration_respects_the_privacy_gate(monkeypatch):
    """The remap path must not start a probe for a route that needs no verdict.

    A fully livestream-derived free route is deterministic, yet
    ``remap_deprecated_free_yui_voice_id`` used to fall through to
    ``_check_non_mainland()`` unconditionally — starting the indefinitely
    retrying ip-api.com loop just to migrate a voice id (invariant #2).
    """
    deprecated = sorted(config_manager_pkg.voice_storage._DEPRECATED_FREE_YUI_VOICE_IDS)[0]
    monkeypatch.setattr(
        'utils.api_config_loader.get_free_voices', lambda: {'yui_cn': 'voice-tone-current'})

    geo_calls = {'n': 0}

    class _CM(config_manager_pkg.voice_storage.VoiceStorageMixin):
        def get_core_config(self):
            # livestream 全派生形态：free 路由但没有任何 lanlan 官方 host
            return {'coreApi': 'free', 'CORE_API_TYPE': 'free',
                    'CORE_URL': 'ws://192.168.1.9:8000/tok/core'}

        def _check_non_mainland(self):
            # 计数而不是抛：remap 的兜底把异常吞成 overseas=False，抛异常的
            # 断言与「压根没调用」结果相同，测不出差别（变异实测漏过）。
            geo_calls['n'] += 1
            return False

    cm = _CM()
    assert cm.remap_deprecated_free_yui_voice_id(deprecated) == 'voice-tone-current', \
        '国内派生路由的废弃音色仍应迁移到现役 yui_cn'
    assert geo_calls['n'] == 0, \
        '无区域敏感 URL 的迁移不该问地理判定（_check_non_mainland 内部会起探测）'
    assert ConfigManager._ip_probe_thread is None, '迁移路径不该起 GeoIP 探测'


@pytest.mark.unit
def test_memory_server_warms_the_region_before_outbox_replay():
    """memory_server runs standalone — the main process's warmup does not reach it.

    Its first free-route LLM work (pending-outbox replay, first memory update)
    would otherwise read the transient mainland snapshot while this process's
    own probe was only just starting. Structural and line-ordered, matching the
    main-server warmup guard.
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[2]
              / 'app' / 'memory_server' / 'runtime.py')
    tree = ast.parse(source.read_text(encoding='utf-8'))

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        calls = {}
        for c in ast.walk(node):
            if isinstance(c, ast.Call):
                name = getattr(c.func, 'attr', None)
                if name:
                    calls.setdefault(name, []).append(c.lineno)
        if '_replay_pending_outbox' not in calls:
            continue
        assert 'awarmup_region_check' in calls, \
            'memory_server 启动未做区域预热（独立进程不经过 main_server 的预热）'
        assert min(calls['awarmup_region_check']) < min(calls['_replay_pending_outbox']), \
            '预热必须早于 outbox 补跑，否则补跑的 LLM 调用会读到临时大陆快照'
        break
    else:
        pytest.fail('未找到 outbox 补跑调用，断言失效')


@pytest.mark.unit
def test_awakened_retry_stays_in_flight_across_rollover(monkeypatch):
    """A kick landing mid-attempt keeps the marker set through the rollover.

    Sequence: the kick arrives while an HTTP attempt is running; that attempt
    fails, and its ``finally`` used to clear the pre-set marker; the loop then
    consumes the wake and spends the eligibility re-read (a disk read) before
    setting the event again. A session join sampling that gap treated the
    deliberately awakened retry as ordinary backoff, returned False, and froze
    the mainland fallback moments before the retry ran.
    """
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_BASE_S', 30.0)
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_MAX_S', 30.0)

    entered = threading.Event()
    fail_gate = threading.Event()
    calls = {'n': 0}

    def _once():
        calls['n'] += 1
        if calls['n'] == 1:
            entered.set()
            fail_gate.wait(10)
            raise OSError('down')
        return True

    monkeypatch.setattr(ConfigManager, '_ip_probe_once', staticmethod(_once))

    # 拉宽滚动窗口：下一轮尝试置位前的资格复查改成慢读，确定性地暴露
    # 「失败 finally 清位 → 复查 → 再置位」之间的间隙
    def _slow_eligibility():
        real_time.sleep(0.2)
        return True

    monkeypatch.setattr(ConfigManager, '_free_route_still_needs_region',
                        staticmethod(_slow_eligibility))

    _Probe()._ensure_ip_probe_started()
    assert entered.wait(5), '首次尝试未开始'

    ConfigManager._kick_ip_probe()      # kick 恰落在在飞尝试期间
    fail_gate.set()                     # 让该尝试立刻失败，进入滚动
    assert ConfigManager.join_ip_probe(timeout=5) is True, \
        '滚动窗口（失败 finally → 资格复查 → 下一轮置位）里 join 不该把被唤醒的重试当成退避放弃'
    assert ConfigManager._ip_check_cache is True
    assert calls['n'] == 2


@pytest.mark.unit
def test_kick_racing_the_attempt_finally_keeps_the_marker(monkeypatch):
    """A kick interleaving with the failing attempt's finally must not lose the marker.

    ``_kick_ip_probe`` writes (in_flight, wake) in two steps and the loop's
    finally decides "clear or keep" by reading wake. Unserialized, the finally
    can read wake before the kick sets it and then clear the marker the kick
    just preset — the pre-set is voided, and a session join sampling the
    rollover gap gives the awakened retry up as ordinary backoff. Both critical
    sections share ``_geo_probe_lock``, so either runs whole: kick first →
    finally sees wake and keeps; finally first → the kick re-sets afterwards.
    The hooked event blocks inside the finally's clear to force the bad window.
    """
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_BASE_S', 30.0)
    monkeypatch.setattr(ConfigManager, '_IP_CHECK_RETRY_MAX_S', 30.0)

    entered_clear = threading.Event()
    allow_clear = threading.Event()

    class _GatedClearEvent(threading.Event):
        def __init__(self):
            super().__init__()
            self._gated_once = False

        def clear(self):
            if not self._gated_once:
                self._gated_once = True
                entered_clear.set()
                allow_clear.wait(10)
            super().clear()

    monkeypatch.setattr(ConfigManager, '_ip_probe_in_flight', _GatedClearEvent())
    calls = _patch_probe_once(monkeypatch, [OSError('down'), 'US'])

    def _slow_eligibility():
        real_time.sleep(0.2)        # 拉宽滚动窗口，让误清必然被 join 采样到
        return True

    monkeypatch.setattr(ConfigManager, '_free_route_still_needs_region',
                        staticmethod(_slow_eligibility))

    _Probe()._ensure_ip_probe_started()
    assert entered_clear.wait(5), '首次失败未进入 finally 清位'

    # kick 在「finally 已判定要清、尚未清完」的窗口里到达——锁实现把它挡在临界区
    # 外直到清位完成，随后整体置位；无锁实现的两步会被这次 clear 抹掉
    kicker = threading.Thread(target=ConfigManager._kick_ip_probe, daemon=True)
    kicker.start()
    real_time.sleep(0.1)
    allow_clear.set()
    kicker.join(5)

    assert ConfigManager.join_ip_probe(timeout=5) is True, \
        'kick 与失败 finally 交错后，被唤醒的重试仍应被等到（预置标记不该被误清）'
    assert ConfigManager._ip_check_cache is True
    assert calls['n'] == 2


@pytest.mark.unit
def test_agent_url_alone_never_triggers_the_probe():
    """``AGENT_MODEL_URL`` is exempt from the rewrite — probing for it buys nothing.

    With core/assist on custom providers, a lanlan-hosted Agent URL was the
    only match in the ``*_URL`` scan, so the eligibility gate started the
    ip-api.com probe for a URL no region verdict will ever touch — pure IP
    disclosure (invariant #2).
    """
    assert ConfigManager._config_needs_region(
        {'AGENT_MODEL_URL': 'https://www.lanlan.tech/text/v1',
         'CORE_URL': 'https://api.openai.com/v1'}) is False
    # 其它免费 URL 照常触发——排除只针对 Agent 槽
    assert ConfigManager._config_needs_region(
        {'AGENT_MODEL_URL': 'https://www.lanlan.tech/text/v1',
         'CORE_URL': 'wss://www.lanlan.tech/core'}) is True


@pytest.mark.unit
def test_custom_url_with_brand_substring_survives_the_rewrite(config_manager):
    """The rewrite keys on hostname, mirroring ``_config_needs_region``.

    A substring gate plus ``str.replace`` would mangle a custom endpoint whose
    *path* happens to contain the brand string whenever the snapshot resolves
    overseas (e.g. core=free): ``/v1/lanlan.tech`` → ``/v1/lanlan.app``.
    """
    custom = 'https://custom.example/v1/lanlan.tech'
    assert config_manager._adjust_free_api_url(custom, True, non_mainland=True) == custom

    official = config_manager._adjust_free_api_url(
        'wss://www.lanlan.tech/core', True, non_mainland=True)
    assert official == 'wss://www.lanlan.app/core', \
        f'官方免费 URL 的海外改写不受影响: {official}'
    kept = config_manager._adjust_free_api_url(
        'wss://www.lanlan.tech/core', True, non_mainland=False)
    assert kept == 'wss://www.lanlan.tech/core'


@pytest.mark.unit
def test_leaving_the_free_route_bypasses_a_wedged_probe(monkeypatch):
    """A config that needs no verdict settles vacuously even with a live stale probe.

    Switching from free to a paid/custom provider does not kill an HTTP lookup
    already wedged in DNS — the old thread stays alive with the in-flight
    marker set for as long as ``getaddrinfo`` blocks. Evaluating the vacuous
    truth only when the thread was dead made every later session and guarded
    plugin request pay the full join timeout for a route no verdict will ever
    touch.
    """
    wedged = threading.Event()
    release = threading.Event()

    def _once():
        wedged.set()
        release.wait(10)          # 模拟卡在 getaddrinfo
        raise OSError('resolver timed out')

    monkeypatch.setattr(ConfigManager, '_ip_probe_once', staticmethod(_once))
    try:
        ConfigManager._ensure_ip_probe_started()
        assert wedged.wait(5), '前置条件：探测已卡在请求中'
        assert ConfigManager._ip_probe_in_flight.is_set()

        probe = _Probe()
        probe._check_steam_non_mainland = lambda: None
        probe.aget_core_config = _async_return(
            {'coreApi': 'openai', 'CORE_URL': 'https://api.openai.com/v1'})

        started = real_time.monotonic()
        assert asyncio.run(probe.aensure_region_resolved(timeout=5)) is True, \
            '切走免费路由后不该再为残留的卡死探测等待'
        assert real_time.monotonic() - started < 0.5, '空真判定不该看探测死活'

        warm = _Probe()
        warm.aget_core_config = _async_return(
            {'coreApi': 'openai', 'CORE_URL': 'https://api.openai.com/v1'})
        started = real_time.monotonic()
        assert asyncio.run(warm.awarmup_region_check(timeout=5)) is True
        assert real_time.monotonic() - started < 0.5
    finally:
        release.set()


@pytest.mark.unit
def test_case_variant_official_hostname_still_rewrites(config_manager):
    """A case-variant official hostname must still follow the region switch.

    The host gate normalizes case (``parsed.hostname``), but a case-sensitive
    netloc replace would silently no-op on ``WWW.LANLAN.TECH`` — the recognized
    free route would stay pinned to the mainland endpoint after an overseas
    verdict.
    """
    out = config_manager._adjust_free_api_url(
        'https://WWW.LANLAN.TECH/text/v1', True, non_mainland=True)
    assert 'lanlan.app' in out, f'大小写变体的官方 host 未被改写: {out}'
    assert out.endswith('/text/v1')


@pytest.mark.unit
def test_every_offline_client_constructor_in_lifecycle_applies_the_flip_failsafe():
    """Every lifecycle path that builds an OmniOfflineClient runs the flip fail-safe.

    Normal-session, hot-swap, and multimodal handoff are parallel entry points;
    guarding only some lets another freeze a voice from the pre-flip catalog.
    Follow the shared constructor helper at its call sites, line-ordered, so a
    later path cannot silently skip the guard.
    """
    import ast
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[2] / 'main_logic' / 'core' / 'lifecycle.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))

    class _DirectCallVisitor(ast.NodeVisitor):
        """Collect calls without merging a nested callable's lexical scope."""

        def __init__(self):
            self.calls = []

        def visit_Call(self, node):
            self.calls.append(node)
            self.generic_visit(node)

        def visit_FunctionDef(self, node):
            return

        def visit_AsyncFunctionDef(self, node):
            return

        def visit_Lambda(self, node):
            return

        def visit_ClassDef(self, node):
            return

    checked = []
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name == '_create_offline_vlm_client':
            continue
        builds, drops = [], []
        visitor = _DirectCallVisitor()
        for statement in node.body:
            visitor.visit(statement)
        for c in visitor.calls:
            name = getattr(c.func, 'attr', None) or getattr(c.func, 'id', None)
            if name in {'OmniOfflineClient', '_create_offline_vlm_client'}:
                builds.append(c.lineno)
            elif name == '_drop_free_voice_on_route_flip':
                drops.append(c.lineno)
        if not builds:
            continue
        checked.append(node.name)
        if not drops:
            problems.append(f'{node.name} (line {node.lineno}) 未做翻转 fail-safe')
        elif min(drops) >= min(builds):
            problems.append(
                f'{node.name}: fail-safe 在 line {min(drops)}，晚于 client 构造 line {min(builds)}')

    assert len(checked) >= 3, f'未找到足够的 OmniOfflineClient 构造入口，断言失效: {checked}'
    assert not problems, f'这些构造点缺少区域翻转音色 fail-safe: {problems}'
