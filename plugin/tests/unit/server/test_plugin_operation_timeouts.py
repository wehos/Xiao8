"""Every wait in the plugin server needs an upper bound or a reason not to have one.

The failures these guard are not crashes — they are a UI that spins until the
front end gives up at 30 s, and in the lock's case an operation that lands
anyway after the user was told it failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

pytestmark = pytest.mark.plugin_unit


# ── 抢锁的截止期 ───────────────────────────────────────────────────────


def test_bounded_wait_sets_and_clears_the_deadline() -> None:
    """Outside the block there must be no deadline at all.

    Leaking one would put every later background operation — autostart
    reconcile, install transactions — under a budget meant for a human who is
    watching a spinner.
    """
    from plugin.server.application.plugins import operation_lock as module

    assert module._OPERATION_WAIT_BUDGET.get() is None
    with module.bounded_operation_wait(5.0):
        assert module._OPERATION_WAIT_BUDGET.get() == 5.0
    assert module._OPERATION_WAIT_BUDGET.get() is None


@pytest.mark.asyncio
async def test_an_expired_deadline_refuses_instead_of_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the real async path, executor hop included.

    That hop is the whole point: the acquire runs via
    ``loop.run_in_executor``, which — unlike ``asyncio.to_thread`` — does NOT
    propagate contextvars. Setting the deadline in the request context and
    reading it inside the worker gets ``None``, so the budget silently does
    nothing. The first version of this test set the deadline *inside* the
    worker thread and passed while production was dead (Greptile caught it).

    Mutation: read the deadline from the ContextVar inside
    ``_acquire_file_lock_sync`` instead of taking it as an argument.
    """
    from plugin.server.application.plugins import operation_lock as module

    def _always_contended(handle):
        raise OSError(module.errno.EACCES, "held")

    monkeypatch.setattr(module, "_lock_file_once", _always_contended)
    monkeypatch.setattr(module, "_is_file_lock_contention", lambda exc: True)
    monkeypatch.setattr(module, "_FILE_LOCK_RETRY_INTERVAL_SECONDS", 0.01)

    started = time.monotonic()
    with module.bounded_operation_wait(0.15):
        with pytest.raises(module.PluginOperationBusy):
            await asyncio.wait_for(
                module._acquire_file_lock_cancellation_safe(), timeout=8.0
            )
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"截止期没穿过 executor，等了 {elapsed:.1f}s"


def test_run_in_executor_really_does_drop_contextvars() -> None:
    """Pins the reason the deadline is passed as an argument, not inherited.

    If a later refactor swaps the executor for ``asyncio.to_thread`` this stops
    being true, and whoever reads it should know the argument is then optional
    rather than load-bearing.
    """
    import contextvars

    probe: contextvars.ContextVar[str | None] = contextvars.ContextVar(
        "probe", default=None
    )

    async def _check() -> tuple[object, object]:
        probe.set("SET")
        loop = asyncio.get_event_loop()
        via_executor = await loop.run_in_executor(None, probe.get)
        via_to_thread = await asyncio.to_thread(probe.get)
        return via_executor, via_to_thread

    via_executor, via_to_thread = asyncio.run(_check())

    assert via_executor is None, "run_in_executor 开始传播上下文了——注释要更新"
    assert via_to_thread == "SET"


def test_without_a_deadline_it_still_queues(monkeypatch: pytest.MonkeyPatch) -> None:
    """Background callers must keep the old unbounded behaviour.

    Guards the half of the change that is *not* supposed to happen: a global
    timeout here would make autostart reconcile fail under normal contention.
    """
    from plugin.server.application.plugins import operation_lock as module

    attempts: list[int] = []

    def _contended_then_ok(handle):
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError(module.errno.EACCES, "held")

    monkeypatch.setattr(module, "_lock_file_once", _contended_then_ok)
    monkeypatch.setattr(module, "_is_file_lock_contention", lambda exc: True)
    monkeypatch.setattr(module, "_FILE_LOCK_RETRY_INTERVAL_SECONDS", 0.01)

    handle = module._acquire_file_lock_sync()
    try:
        assert len(attempts) == 3, "无截止期时应该一直重试到拿到锁"
    finally:
        # 和过期预算那条用例同一个理由：_lock_file_once 被打成了空操作，文件区间
        # 从没被真的锁过，所以不能走 _release_file_lock_sync；但 handle 已经登记进
        # 模块全局了，只 close 会留一个已关闭的 handle 给后面的用例踩。
        with module._FILE_LOCK_HANDLE_GUARD:
            if module._ACTIVE_FILE_LOCK_HANDLE is handle:
                module._ACTIVE_FILE_LOCK_HANDLE = None
            module._OPEN_FILE_LOCK_HANDLES.discard(handle)
        handle.close()


# ── 没有工具就不该发那次 HTTP ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_clearing_tools_still_asks_when_local_tracking_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty local table does not mean the remote has nothing.

    After a plugin-server restart ``_plugin_tools`` is empty while main_server
    can still hold tools tagged with this plugin's source. Skipping the request
    on an empty table — which an earlier version of this change did, to dodge
    the connect cost — leaves ghost tools the model can still call. Local
    bookkeeping is not authoritative for remote state.

    The cost is handled by a shorter timeout instead, because this await sits
    inside stop_plugin's cross-process lock.

    Mutation: reinstate the ``if not owned: return`` early exit.
    """
    from plugin.server.messaging import llm_tool_registry as module

    seen: list[object] = []

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"ok": True}

    class _Client:
        async def post(self, url, **kwargs):
            seen.append(kwargs.get("timeout"))
            return _Resp()

    monkeypatch.setattr(module, "_get_http_client", lambda: _Client())

    await module.clear_plugin_tools("plugin-with-no-local-record")

    assert len(seen) == 1, "本地表为空就不发了——重启后会留下幽灵工具"
    timeout = seen[0]
    assert timeout is not None, "用了默认超时——这一步在锁里面，连不上要等满 2s"
    assert getattr(timeout, "connect", None) == 0.3


# ── reload-all 的总预算 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reload_all_stops_at_its_budget_and_says_which_were_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stops cannot run concurrently, so the cost grows with plugin count.

    Each ``stop_plugin`` takes the cross-process lock on its own, and the lock's
    reentrancy is keyed on the asyncio Task, so gathering them buys nothing —
    N plugins is N serial lock acquisitions while the front end waits.

    The budget cuts it off. What matters as much as stopping is *reporting*:
    a plugin that was never attempted must not simply vanish from the result,
    or the caller sees a short list with nothing explaining the gap.

    Mutation: drop the deadline check, or drop the skipped-list reporting.
    """
    from plugin.server.application.plugins import lifecycle_service as module

    plugin_ids = [f"p{i}" for i in range(6)]
    monkeypatch.setattr(module, "_list_running_plugin_ids_sync", lambda: list(plugin_ids))
    monkeypatch.setattr(module, "_emit_lifecycle_event", lambda **kw: None)

    async def _noop_refresh(*a, **k):
        return {"success": True}

    monkeypatch.setattr(module.plugin_registry_service, "refresh_registry", _noop_refresh)
    monkeypatch.setattr(module, "_RELOAD_ALL_BUDGET_SECONDS", 0.25)

    service = module.PluginLifecycleService()
    stopped: list[str] = []

    async def _slow_stop(plugin_id: str, *, stop_deadline=None):
        stopped.append(plugin_id)
        await asyncio.sleep(0.12)
        return module._ReloadOutcome(plugin_id=plugin_id, success=False, error="x")

    monkeypatch.setattr(service, "_safe_stop_for_reload", _slow_stop)

    result = await service.reload_all_plugins()

    assert len(stopped) < len(plugin_ids), "预算没有截断，全部都试了一遍"
    reported = {entry["plugin_id"] for entry in result.get("failed", [])}
    assert reported == set(plugin_ids), (
        f"被跳过的插件从结果里消失了：只报告了 {sorted(reported)}"
    )
    skipped_reasons = [
        entry["error"] for entry in result["failed"] if "budget" in str(entry["error"])
    ]
    assert skipped_reasons, "跳过的原因没写进结果，调用方无从知道为什么少了几个"


@pytest.mark.asyncio
async def test_a_slow_but_successful_stop_is_not_reported_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``asyncio.wait_for`` cannot bound a ``@serialized_plugin_operation``.

    Once that wrapper holds the lock it swallows cancellation and waits for the
    inner call to finish before re-raising, so the outer ``wait_for`` blocks for
    the whole shutdown anyway and *then* reports a stop that actually succeeded
    as a timeout. The plugin drops out of the restart list and is left stopped
    rather than reloaded — a reload that quietly turns into a stop (codex).

    The budget is threaded into the operation instead. This stub carries the
    real decorator, because the bug lives in the decorator's behaviour.

    Mutation: wrap the stop in ``asyncio.wait_for(..., timeout=remaining)``.
    """
    from plugin.server.application.plugins import lifecycle_service as module
    from plugin.server.application.plugins.operation_lock import (
        serialized_plugin_operation,
    )

    monkeypatch.setattr(module, "_list_running_plugin_ids_sync", lambda: ["p0"])
    monkeypatch.setattr(module, "_emit_lifecycle_event", lambda **kw: None)
    monkeypatch.setattr(module, "_RELOAD_ALL_BUDGET_SECONDS", 0.20)

    async def _noop_refresh(*a, **k):
        return {"success": True}

    monkeypatch.setattr(module.plugin_registry_service, "refresh_registry", _noop_refresh)

    service = module.PluginLifecycleService()

    @serialized_plugin_operation
    async def _slow_success(plugin_id: str, *, stop_deadline=None):
        # 比剩余预算长，但确实成功了。
        await asyncio.sleep(0.45)
        return {"success": True}

    started: list[str] = []

    async def _start(plugin_id: str, *, start_deadline=None):
        started.append(plugin_id)
        return module._ReloadOutcome(plugin_id=plugin_id, success=True)

    monkeypatch.setattr(service, "stop_plugin", _slow_success)
    monkeypatch.setattr(service, "_safe_start_for_reload", _start)

    async def _ordered(ids):
        return list(ids)

    monkeypatch.setattr(module.plugin_registry_service, "order_plugin_ids", _ordered)

    result = await service.reload_all_plugins()

    assert started == ["p0"], (
        f"停成功了却没重启——被当成超时丢掉了：failed={result.get('failed')}"
    )
    assert result["reloaded"] == ["p0"]
    assert not result["failed"], f"成功的停止被记成失败：{result['failed']}"


@pytest.mark.asyncio
async def test_a_stopped_plugin_is_always_started_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping a plugin creates an obligation to start it.

    The two phases must not share one deadline: a slow stop phase would then
    leave nothing for the starts, and every plugin already taken down would be
    reported as "over budget" and left stopped. That turns a reload into a
    silent mass stop — far worse than a reload that answers late, since the
    operation itself succeeded and only the response was slow.

    Mutation: reuse ``stop_deadline`` in the start loop.
    """
    from plugin.server.application.plugins import lifecycle_service as module

    monkeypatch.setattr(module, "_list_running_plugin_ids_sync", lambda: ["p0", "p1"])
    monkeypatch.setattr(module, "_emit_lifecycle_event", lambda **kw: None)
    monkeypatch.setattr(module, "_RELOAD_ALL_BUDGET_SECONDS", 0.30)

    async def _noop_refresh(*a, **k):
        return {"success": True}

    monkeypatch.setattr(module.plugin_registry_service, "refresh_registry", _noop_refresh)

    async def _ordered(ids):
        return list(ids)

    monkeypatch.setattr(module.plugin_registry_service, "order_plugin_ids", _ordered)

    service = module.PluginLifecycleService()
    started: list[str] = []

    async def _stop(plugin_id: str, *, stop_deadline=None):
        # 停止阶段把预算花光——但两个都停成功了。
        await asyncio.sleep(0.20)
        return module._ReloadOutcome(plugin_id=plugin_id, success=True)

    budgets: list[float | None] = []

    async def _start(plugin_id: str, *, start_deadline=None):
        started.append(plugin_id)
        budgets.append(
            None if start_deadline is None else start_deadline - time.monotonic()
        )
        return module._ReloadOutcome(plugin_id=plugin_id, success=True)

    monkeypatch.setattr(service, "_safe_stop_for_reload", _stop)
    monkeypatch.setattr(service, "_safe_start_for_reload", _start)

    result = await service.reload_all_plugins()

    assert started == ["p0", "p1"], (
        f"停掉了却没重新启动，reload 变成了 stop：failed={result.get('failed')}"
    )
    assert result["reloaded"] == ["p0", "p1"]
    # 停止阶段（两次各 0.20s）已经超出 0.30s 的预算。启动阶段如果沿用同一个截止期，
    # 这里拿到的就是负数——插件确实还会被尝试启动（那道守卫在另一条用例里），但每次
    # 都只剩下界那点时间，慢一点的插件就起不来了。启动阶段有自己的预算，这个数才是正的。
    assert budgets[0] is not None and budgets[0] > 0, (
        f"启动阶段沿用了停止阶段的截止期，一上来预算就是负的：{budgets}"
    )


@pytest.mark.asyncio
async def test_a_start_that_begins_late_gets_a_shortened_startup_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checking the deadline before a start does not bound the start itself.

    A plugin whose start begins a moment before the deadline still waits out its
    own startup timeout — ten seconds by default — so reload-all overruns its
    advertised wall clock and keeps mutating plugin state long after the front
    end gave up (codex / CodeRabbit / Greptile). The remaining budget is pushed
    down into the start instead, with a floor so a nearly-spent budget still
    buys a real attempt rather than an instant failure.

    Mutation: drop the clamp, or drop its lower bound.
    """
    from plugin.server.application.plugins import lifecycle_service as module

    monkeypatch.setattr(module, "_list_running_plugin_ids_sync", lambda: ["p0", "p1"])
    monkeypatch.setattr(module, "_emit_lifecycle_event", lambda **kw: None)
    # 预算和睡眠都放大，给被负载拖慢的 CI 留出余量：判据是"第二个看到的剩余量
    # 明显更小"，不是某个精确的秒数。
    monkeypatch.setattr(module, "_RELOAD_ALL_BUDGET_SECONDS", 1.20)

    async def _noop_refresh(*a, **k):
        return {"success": True}

    monkeypatch.setattr(module.plugin_registry_service, "refresh_registry", _noop_refresh)

    async def _ordered(ids):
        return list(ids)

    monkeypatch.setattr(module.plugin_registry_service, "order_plugin_ids", _ordered)

    service = module.PluginLifecycleService()

    async def _stop(plugin_id: str, *, stop_deadline=None):
        return module._ReloadOutcome(plugin_id=plugin_id, success=True)

    seen: list[float | None] = []

    async def _start(plugin_id: str, *, refresh_registry=True, start_deadline=None):
        # 记的是"这一刻还剩多少"，不是原始参数：传的是绝对截止期，所以第二个插件
        # 看到的剩余量必须比第一个小。记参数本身的话，"每个插件各起一份新预算"
        # 这个退化是看不出来的。
        seen.append(None if start_deadline is None else start_deadline - time.monotonic())
        await asyncio.sleep(0.45)
        return {"success": True}

    monkeypatch.setattr(service, "_safe_stop_for_reload", _stop)
    monkeypatch.setattr(service, "start_plugin", _start)

    await service.reload_all_plugins()

    assert len(seen) == 2, f"两个插件都该被尝试启动：{seen}"
    # 留一点余量：seen 记的是 deadline - monotonic()，紧挨着设截止期那一刻算出来
    # 的值可以是 1.2000000000116415，浮点上并不 <= 1.20。这条只是个粗上界，真正
    # 承重的是下面那个"差值必须显著"的断言。
    assert seen[0] is not None and seen[0] <= 1.25, (
        f"第一个启动拿到的上限比整轮预算还大：{seen[0]}"
    )
    # 要求一个**真实的**间隔，而不是 seen[1] < seen[0]。后者在变异下是掷硬币：
    # 每个插件各起一份新预算的话，两个数都约等于整轮预算，谁大谁小由微秒级抖动
    # 决定，变异有一半概率活下来（本轮对抗复审）。第一次启动睡了 0.45s，所以真
    # 实差值应当接近 0.45s。
    assert seen[1] is not None and seen[1] < seen[0] - 0.20, (
        f"第二个启动没有拿到**剩余**预算，而是又一份完整的：{seen}"
    )


def test_the_step_clamp_never_widens_and_never_reaches_zero() -> None:
    """Every direction of the clamp, on the function production actually calls.

    A spent budget must still buy a short attempt: every plugin reaching the
    start phase was just stopped by us, so refusing to try leaves it down — the
    opposite of a reload. And a plugin that declared a *shorter* timeout of its
    own must not have it widened, by a generous budget **or by the floor**. The
    sub-second case is the one the first version of this test missed: with the
    floor applied outside the ``min`` a plugin asking for 0.5 s was handed 1.0 s
    (本轮对抗复审).

    Mutation: drop the lower bound, drop the outer ``min``, or put the floor
    back on the outside.
    """
    from plugin.server.application.plugins import lifecycle_service as module

    start_floor = module._MIN_CLAMPED_START_TIMEOUT
    stop_floor = module._MIN_CLAMPED_STOP_TIMEOUT
    # 两个下界必须是各自独立的常量。合成一个数的话，为了让启动买得起一次真正的
    # 尝试而抬高它，会连带把关停的最坏墙钟一起抬上去——那是两件无关的事。
    assert start_floor != stop_floor, (
        "启动和关停的下界又被合成同一个数了"
    )

    for floor in (start_floor, stop_floor):
        assert module._clamp_step_timeout(10.0, 0.0, floor=floor) > 0, (
            "预算见底时算出了 0，等于直接判失败"
        )
        assert module._clamp_step_timeout(10.0, 5.0, floor=floor) == 5.0, (
            "剩余预算没有压住上限"
        )
        assert module._clamp_step_timeout(2.0, 30.0, floor=floor) == 2.0, (
            "自己更短的超时被预算放宽了"
        )
        assert module._clamp_step_timeout(0.5, 0.0, floor=floor) == 0.5, (
            "自己更短的超时被下界放宽了"
        )
        assert module._clamp_step_timeout(10.0, None, floor=floor) == 10.0, (
            "没有预算时不该改动配置值"
        )

    # 启动的下界必须买得起一次真正的尝试：起子进程加导入框架实测 0.74s，插件自己
    # 的导入还没算。低于这个数等于发一个必然超时的窗口，健康插件会被记成启动失败。
    assert start_floor >= 2.0, (
        f"启动下界 {start_floor}s 买不到一次真正的启动尝试"
    )


@pytest.mark.asyncio
async def test_a_late_stop_is_still_asked_to_shut_down_not_just_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop side needs the same floor the start side has, derived just in time.

    Two things at once, because they are the same line. `min(SHUTDOWN, remaining)`
    with a spent budget hands the host `shutdown_timeout≈0`, which skips straight
    to killing a plugin that would have flushed and closed cleanly. And deriving
    it in the *caller* is too early: the value is computed before `stop_plugin`
    enters its decorator, so a stop that then waits out most of the budget on the
    lock still gets a timeout sized as if no waiting had happened (codex).

    So this asserts on what the host was actually told, after an already-expired
    deadline — the number has to come from the floor, not from the arithmetic.

    Mutation: derive the timeout in the reload loop again, or drop the floor.
    """
    from plugin.server.application.plugins import lifecycle_service as module

    handed: list[float] = []

    class _Boom(Exception):
        pass

    class _Host:
        async def shutdown(self, timeout=None):
            handed.append(timeout)
            # 到这里要验的都验完了，别再往下走 stop_plugin 的收尾。
            raise _Boom

        async def start(self, *a, **k):  # pragma: no cover - contract shape only
            return None

    monkeypatch.setattr(module, "_get_plugin_host_sync", lambda plugin_id: _Host())
    monkeypatch.setattr(module, "_emit_lifecycle_event", lambda **kw: None)
    monkeypatch.setattr(module, "PluginHostContract", _Host)

    service = module.PluginLifecycleService()
    with pytest.raises(BaseException):
        await service.stop_plugin(
            "p0", stop_deadline=time.monotonic() - 5.0  # 预算早就见底了
        )

    assert handed, "前提没成立：根本没走到 host.shutdown"
    # 下界只抬高被预算压扁的值，从不放宽插件自己配置的上限。配置的关停超时本来
    # 就可能低于下界，那时拿到配置值才是对的；这里该守的是"没被压到 0 附近直接
    # 杀掉"。
    assert handed[0] >= min(
        module.PLUGIN_SHUTDOWN_TIMEOUT, module._MIN_CLAMPED_STOP_TIMEOUT
    ), (
        f"预算见底时把关停上限压到了下界以下，插件会被直接杀掉：{handed}"
    )
    assert handed[0] <= module.PLUGIN_SHUTDOWN_TIMEOUT, (
        f"关停上限比配置值还大：{handed}"
    )


@pytest.mark.asyncio
async def test_a_spent_budget_still_buys_a_real_wait_for_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero wait is not an attempt, it is a refusal wearing an attempt's clothes.

    Removing the over-budget ``break`` only helps if what replaces it can
    actually succeed. With a spent budget ``bounded_operation_wait(0.0)`` means
    "do not wait at all", so any concurrent plugin operation — a user pressing
    start, an uninstall, a source switch — makes ``start_plugin`` raise
    ``PluginOperationBusy`` immediately, and a plugin we just stopped stays
    stopped. That is the same outcome the ``break`` was removed to prevent, let
    back in through the side door (CodeRabbit). The lock wait shares the step
    floor.

    Mutation: floor the lock wait at 0.0 again.
    """
    from plugin.server.application.plugins import lifecycle_service as module
    from plugin.server.application.plugins.operation_lock import (
        serialized_plugin_operation,
    )
    from plugin.server.application.plugins import operation_lock

    monkeypatch.setattr(module, "_list_running_plugin_ids_sync", lambda: ["p0"])
    monkeypatch.setattr(module, "_emit_lifecycle_event", lambda **kw: None)
    # 预算小到"轮到启动时早就见底了"。
    monkeypatch.setattr(module, "_RELOAD_ALL_BUDGET_SECONDS", 0.01)

    async def _noop_refresh(*a, **k):
        return {"success": True}

    monkeypatch.setattr(module.plugin_registry_service, "refresh_registry", _noop_refresh)

    async def _ordered(ids):
        return list(ids)

    monkeypatch.setattr(module.plugin_registry_service, "order_plugin_ids", _ordered)

    service = module.PluginLifecycleService()
    started: list[str] = []

    async def _stop(plugin_id: str, *, stop_deadline=None):
        return module._ReloadOutcome(plugin_id=plugin_id, success=True)

    # 带真decorator：这条用例要的就是它去抢那把真锁。
    @serialized_plugin_operation
    async def _start(plugin_id: str, *, refresh_registry=True, start_deadline=None):
        started.append(plugin_id)
        return {"success": True}

    monkeypatch.setattr(service, "_safe_stop_for_reload", _stop)
    monkeypatch.setattr(service, "start_plugin", _start)

    # 启动开始时锁被别人握着，0.3s 后放开——远小于下界，够等。
    await operation_lock._PROCESS_LOCK.acquire()

    async def _release_later():
        await asyncio.sleep(0.30)
        operation_lock._PROCESS_LOCK.release()

    releaser = asyncio.create_task(_release_later())
    try:
        result = await service.reload_all_plugins()
    finally:
        await releaser

    assert started == ["p0"], (
        f"预算见底时等锁被压成零，插件被直接判 busy 而留在停止状态：{result.get('failed')}"
    )
    assert result["reloaded"] == ["p0"]
@pytest.mark.asyncio
async def test_every_stopped_plugin_gets_a_start_attempt_even_over_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping a start is not deferring it — nothing comes back for it.

    Autostart runs once, at server startup; there is no periodic reconcile. So a
    plugin dropped from the start phase for being over budget stays down until
    the user starts it by hand or restarts the whole server (Greptile). Stopping
    a plugin is a promise to start it, and the budget does not release us from
    a promise we already made — the two phases are asymmetric on purpose: a
    skipped *stop* leaves a plugin running, a skipped *start* leaves it dead.

    Mutation: put the over-budget ``break`` back into the start loop.
    """
    from plugin.server.application.plugins import lifecycle_service as module

    plugin_ids = [f"p{i}" for i in range(4)]
    monkeypatch.setattr(module, "_list_running_plugin_ids_sync", lambda: list(plugin_ids))
    monkeypatch.setattr(module, "_emit_lifecycle_event", lambda **kw: None)
    monkeypatch.setattr(module, "_RELOAD_ALL_BUDGET_SECONDS", 0.20)

    async def _noop_refresh(*a, **k):
        return {"success": True}

    monkeypatch.setattr(module.plugin_registry_service, "refresh_registry", _noop_refresh)

    async def _ordered(ids):
        return list(ids)

    monkeypatch.setattr(module.plugin_registry_service, "order_plugin_ids", _ordered)

    service = module.PluginLifecycleService()
    attempted: list[str] = []

    async def _stop(plugin_id: str, *, stop_deadline=None):
        return module._ReloadOutcome(plugin_id=plugin_id, success=True)

    async def _start(plugin_id: str, *, start_deadline=None):
        attempted.append(plugin_id)
        # 每次启动都比整轮预算长：第一次之后预算就见底了。
        await asyncio.sleep(0.15)
        return module._ReloadOutcome(plugin_id=plugin_id, success=True)

    monkeypatch.setattr(service, "_safe_stop_for_reload", _stop)
    monkeypatch.setattr(service, "_safe_start_for_reload", _start)

    result = await service.reload_all_plugins()

    assert attempted == plugin_ids, (
        f"预算见底之后就不再尝试启动了，这些插件会一直停着：试过 {attempted}"
    )
    assert result["reloaded"] == plugin_ids


def test_the_file_lock_stage_is_only_entered_under_the_process_lock() -> None:
    """This is what keeps the single-worker executor from swallowing deadlines.

    ``_FILE_LOCK_EXECUTOR`` has one worker, so if two callers could submit to it
    at once, the second would sit in the executor *queue* — where no deadline is
    consulted, because the check lives inside the worker function — and sail
    past the front end's timeout (Greptile).

    They cannot, and the reason is ordering: ``__aenter__`` takes the in-process
    ``_PROCESS_LOCK`` first and only then reaches the file lock, so at most one
    task is ever in that executor. A second caller waits on ``_CrossLoopLock``,
    which *is* deadline-aware and refuses with ``PluginOperationBusy``.

    That invariant is load-bearing and invisible, so pin it: move the file lock
    outside the process lock, or acquire it anywhere else, and this fails.

    Mutation: acquire the file lock before ``_PROCESS_LOCK`` in ``__aenter__``.
    """
    from plugin.server.application.plugins import operation_lock as module

    held_when_entered: list[bool] = []
    real = module._acquire_file_lock_cancellation_safe

    async def _record(deadline=None):
        with module._PROCESS_LOCK._state_lock:
            held_when_entered.append(module._PROCESS_LOCK._held)
        return await real(deadline)

    async def _scenario() -> None:
        module._acquire_file_lock_cancellation_safe = _record
        try:
            async with module.plugin_operation_lock.hold():
                pass
        finally:
            module._acquire_file_lock_cancellation_safe = real

    asyncio.run(_scenario())

    assert held_when_entered == [True], (
        "进文件锁那一层时进程锁没被握着——单 worker 的 executor 会开始排队，"
        f"而排队期间没有任何东西看截止期：{held_when_entered}"
    )


@pytest.mark.asyncio
async def test_the_tool_cleanup_request_honours_the_caller_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parameter that is accepted and ignored is worse than no parameter.

    The tool cleanup runs on the stop path with the operation lock held. Its own
    two-second timeout is independent of the round budget, so a stalled
    ``main_server`` lets a stop spend that long past ``stop_deadline`` while
    holding the lock (codex). Passing a budget in must actually reach the
    request — the first version of this change added the keyword argument and
    left the body using the module constant, and every test still passed.

    Mutation: ignore ``timeout`` and always use ``_CLEAR_TOOLS_TIMEOUT``.
    """
    from plugin.server.messaging import llm_tool_registry as module

    seen: list = []

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = "{}"

        def json(self):
            return {"success": True}

    class _Client:
        async def post(self, url, json=None, timeout=None):
            seen.append(timeout)
            return _Resp()

    monkeypatch.setattr(module, "_get_http_client", lambda: _Client())

    await module.clear_plugin_tools("demo", timeout=0.25)

    assert seen, "前提没成立：根本没发出请求"
    handed = seen[0]
    total = getattr(handed, "read", None) or getattr(handed, "pool", None)
    assert total == pytest.approx(0.25), (
        f"预算没有传到真正发请求那一步，还是用的模块常量：{handed}"
    )
    assert getattr(handed, "connect", None) == pytest.approx(0.25), (
        "connect 超时没有跟着收窄，会比整段预算还长"
    )


@pytest.mark.asyncio
async def test_the_tool_cleanup_is_bounded_as_a_whole_not_per_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``httpx.Timeout`` bounds phases, not the call.

    A response that keeps dribbling bytes never trips connect/read/write
    individually, so the whole request can run far past the budget while the
    operation lock is held (CodeRabbit demonstrated this against a real chunked
    server). The per-phase limits stay; an outer ``asyncio.wait_for`` is what
    makes the number a total.

    Mutation: drop the ``asyncio.wait_for`` wrapper.
    """
    import time as _time

    from plugin.server.messaging import llm_tool_registry as module

    class _Client:
        async def post(self, url, json=None, timeout=None):
            # 每个阶段都不超时，就是一直不结束。
            await asyncio.sleep(5)
            raise AssertionError("should have been cut off")

    monkeypatch.setattr(module, "_get_http_client", lambda: _Client())

    started = _time.monotonic()
    result = await module.clear_plugin_tools("demo", timeout=0.2)
    elapsed = _time.monotonic() - started

    assert elapsed < 2.0, f"整通调用没有被总时长兜住，花了 {elapsed:.1f}s"
    assert result["ok"] is False and result.get("error") == "timeout", (
        f"超时没有按尽力而为处理：{result}"
    )


@pytest.mark.asyncio
async def test_a_spent_stop_budget_still_attempts_the_cleanup_on_a_short_leash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ways to get this wrong, and this pins both.

    Too generous: ``_clamp_step_timeout``'s one-second floor exists because a
    plugin we stopped must get a real start attempt. Reusing it here meant an
    exhausted stop budget still bought a second of lock time per plugin for a
    cleanup that is explicitly best-effort (CodeRabbit).

    Too stingy: the version that fixed that skipped the cleanup outright once the
    budget was spent — and this is the only place in the tree that clears the
    remote tool registration, with no reconciliation and no retry behind it. The
    host is already gone, so the skipped tools stay advertised to the model,
    which then picks one and gets "plugin not running" forever (Greptile).

    So: always attempt, on a leash far shorter than the start-attempt floor. The
    POST clears by source and is idempotent, so a failure is no worse than the
    skip was.

    Mutations: clamp with ``_clamp_step_timeout`` again; or skip when the budget
    is spent.
    """
    from plugin.server.application.plugins import lifecycle_service as module

    handed: list = []

    async def _clear(plugin_id, *, timeout=None):
        handed.append(timeout)
        return {"ok": True}

    class _Host:
        async def shutdown(self, timeout=None):
            return None

        async def start(self, *a, **k):  # pragma: no cover - contract shape only
            return None

    monkeypatch.setattr(module, "clear_plugin_llm_tools", _clear)
    monkeypatch.setattr(module, "_get_plugin_host_sync", lambda plugin_id: _Host())
    monkeypatch.setattr(module, "PluginHostContract", _Host)
    monkeypatch.setattr(module, "_emit_lifecycle_event", lambda **kw: None)

    service = module.PluginLifecycleService()
    # 这个替身 host 只把 shutdown 走通，stop_plugin 后面那些收尾会因为注册表里
    # 没有这个插件而抛领域错误。我们要看的是"有没有发起清理"，抛什么不重要——
    # 但也不能连 KeyboardInterrupt 一起吞掉，所以只挡 Exception。
    with contextlib.suppress(Exception):
        await service.stop_plugin("p0", stop_deadline=time.monotonic() - 5.0)

    assert handed, (
        "预算见底就跳过了清理——host 已经摘掉，而远端工具表还在向模型公布，"
        "没有任何东西会重试"
    )
    assert handed[0] == module._MIN_TOOL_CLEANUP_TIMEOUT, (
        f"预算见底时给的不是那一小段下界：{handed}"
    )
    assert handed[0] < module._MIN_CLAMPED_STOP_TIMEOUT, (
        f"又把「启动尝试」的下界套回到这次尽力而为的远端调用上了：{handed}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["returns_not_ok", "raises"])
async def test_a_failed_tool_cleanup_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A cleanup that did not land must leave a trace naming the plugin.

    ``clear_plugin_tools`` is deliberately quiet inside itself — a noisy failure
    there would mask the real shutdown reason — and it returns its verdict as a
    value. The stop path used to discard that value, so a stop that left the
    model holding an uncallable tool looked exactly like a clean stop. Nothing
    else reconciles the remote registry, so that trace is the only way a ghost
    tool is ever attributable.

    两条失败路都要盯，而且要盯出同一句话：返回 ``{"ok": false}``，和**抛出**。
    抛出那条隐蔽得多——``clear_plugin_tools`` 内部只挡 ``httpx.HTTPError`` 和
    ``asyncio.TimeoutError``，一个 content-type 声明是 JSON、正文却坏掉的响应会让
    ``resp.json()`` 抛 ``ValueError`` 冒到调用方的 ``except Exception``，第一版修复
    只堵了返回值那条（CodeRabbit）。后果完全一样，所以日志级别和措辞也必须一样，
    否则运维要 grep 两个不同的句子才找得全。

    Mutation: drop either warning, or go back to discarding the returned dict.
    """
    from plugin.server.application.plugins import lifecycle_service as module

    warnings: list = []

    async def _clear(plugin_id, *, timeout=None):
        if failure == "raises":
            # httpx 在 content-type 说是 JSON、正文却不是时抛的就是 ValueError
            # 的子类，而 clear_plugin_tools 不挡它。
            raise ValueError("malformed JSON body")
        return {"ok": False, "error": "timeout", "owned_count": 3}

    class _Host:
        async def shutdown(self, timeout=None):
            return None

        async def start(self, *a, **k):  # pragma: no cover - contract shape only
            return None

    monkeypatch.setattr(module, "clear_plugin_llm_tools", _clear)
    monkeypatch.setattr(module, "_get_plugin_host_sync", lambda plugin_id: _Host())
    monkeypatch.setattr(module, "PluginHostContract", _Host)
    monkeypatch.setattr(module, "_emit_lifecycle_event", lambda **kw: None)
    class _RecordingLogger:
        # 换掉整个 logger，而不是 patch 它的 warning：PluginLoggerAdapter 的方法是
        # 只读属性，setattr 会直接抛。
        def warning(self, msg, *a, **k):
            warnings.append((msg, a))

        def __getattr__(self, _name):
            return lambda *a, **k: None

    monkeypatch.setattr(module, "logger", _RecordingLogger())

    service = module.PluginLifecycleService()
    with contextlib.suppress(Exception):
        await service.stop_plugin("p0")

    hits = [w for w in warnings if "tools may still be advertised" in w[0]]
    assert hits, f"清理失败没有留下任何可追查的痕迹：{warnings}"
    assert "p0" in hits[0][1], f"痕迹里没有插件 id，出现幽灵工具时无从归因：{hits[0]}"
def test_one_deadline_covers_both_lock_layers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two lock stages share a budget; they must not each spend a full one.

    ``_CrossLoopLock`` runs first and the file lock second. With each stage
    calling ``_wait_deadline()`` for itself, a request waiting out most of its
    budget behind a same-process operation then starts a *fresh* full budget on
    the file lock, so a nominally 20 s request can still mutate state well past
    the front end's 30 s (codex).

    Mutation: have ``__aenter__`` pass ``None`` to either stage.
    """
    from plugin.server.application.plugins import operation_lock as module

    budget = 0.60
    process_lock_held_for = 0.40

    # 文件锁永远争用：预算怎么分配，全看跨进程那一层还剩多少。
    reached_file_lock: list[int] = []

    def _always_contended(handle):
        reached_file_lock.append(1)
        raise OSError(module.errno.EACCES, "held")

    monkeypatch.setattr(module, "_lock_file_once", _always_contended)
    monkeypatch.setattr(module, "_FILE_LOCK_RETRY_INTERVAL_SECONDS", 0.01)

    async def _scenario() -> tuple[str, float]:
        held = module._HeldPluginOperationLock()
        try:
            await module._PROCESS_LOCK.acquire()

            async def _release_later():
                await asyncio.sleep(process_lock_held_for)
                module._PROCESS_LOCK.release()

            releaser = asyncio.create_task(_release_later())
            started = time.monotonic()
            with module.bounded_operation_wait(budget):
                try:
                    await held.__aenter__()
                except module.PluginOperationBusy:
                    return "busy", time.monotonic() - started
                finally:
                    await releaser
            return "acquired", time.monotonic() - started
        finally:
            # 这个用例直接调 __aenter__，所以没有 __aexit__ 替我们收尾。真让它拿到
            # 了锁（说明被测的行为坏了）就必须在这里还回去：全局那把锁留在持有态
            # 的话，后面每一个要用它的用例都会永远挂住——而挂住不是失败，是没有
            # 结果。这一段本身不是断言，是不让一条红用例把整个会话带走。
            if held._acquired:
                if held._file_lock_handle is not None:
                    module._release_file_lock_sync(held._file_lock_handle)
                module._PROCESS_LOCK.release()

    outcome, elapsed = asyncio.run(_scenario())

    assert outcome == "busy"
    # 前提：真的走到了第二层。如果预算在跨进程那一层就耗光，这个用例会以 busy
    # 通过，却根本没检验"两层共用一个截止期"——那正是它存在的理由（本轮对抗复审）。
    assert reached_file_lock, "没走到文件锁那一层，这一轮什么也没验证"
    assert elapsed < budget + 0.30, (
        f"两层锁各花了一份预算：等了 {elapsed:.2f}s，预算只有 {budget}s"
    )


def test_an_expired_budget_still_takes_an_uncontended_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Wait zero seconds" is a legitimate answer, and it is not a refusal.

    reload-all hands each stop whatever is left of the round budget, so the
    last one routinely gets ~0 s. Checking the deadline *before* trying meant a
    caller with nothing left was refused while the lock sat there free — a 409
    with no contention anywhere.

    Mutation: move the deadline check back above ``_lock_file_once``.
    """
    from plugin.server.application.plugins import operation_lock as module

    attempts: list[int] = []

    def _free(handle):
        attempts.append(1)

    monkeypatch.setattr(module, "_lock_file_once", _free)

    handle = module._acquire_file_lock_sync(None, time.monotonic() - 5.0)
    try:
        assert attempts == [1], "预算过期就拒绝了，可锁根本没人占"
    finally:
        # 拿锁成功时 handle 会被登记进 _ACTIVE_FILE_LOCK_HANDLE 和
        # _OPEN_FILE_LOCK_HANDLES，只 close 就会把一个已关闭的 handle 留在全局里
        # 给后面的用例踩（CodeRabbit）。
        #
        # 但也不能直接调 _release_file_lock_sync：这个用例把 _lock_file_once 打成了
        # 空操作，文件区间其实从没被锁过，走那条路会在 msvcrt 解锁那步抛
        # PermissionError。所以只做登记那两步，再关。
        with module._FILE_LOCK_HANDLE_GUARD:
            if module._ACTIVE_FILE_LOCK_HANDLE is handle:
                module._ACTIVE_FILE_LOCK_HANDLE = None
            module._OPEN_FILE_LOCK_HANDLES.discard(handle)
        handle.close()


def test_a_domain_error_passes_through_the_wrapper_untouched() -> None:
    """The wrapper must not touch exceptions travelling through it.

    ``@contextmanager``'s ``__exit__`` assigns ``exc.__traceback__`` before
    throwing back into the generator, and ``ServerDomainError`` refuses
    attribute assignment — so a generator-based wrapper turned every domain
    error raised inside a wrapped endpoint into
    ``TypeError: super(type, obj): obj must be an instance or subtype of type``,
    losing the real 409 and its error code. Caught by a route test, not by the
    unit tests here, because it only happens when a *real* domain error
    propagates.

    Mutation: reimplement ``bounded_operation_wait`` with ``@contextmanager``.
    """
    from plugin.server.application.plugins.operation_lock import (
        bounded_operation_wait,
    )
    from plugin.server.domain.errors import ServerDomainError

    original = ServerDomainError(
        code="PLUGIN_MANUAL_NOT_MANAGED",
        message="manual plugin is not managed",
        status_code=409,
    )

    with pytest.raises(ServerDomainError) as excinfo:
        with bounded_operation_wait(5.0):
            raise original

    assert excinfo.value is original, "异常在穿过包装时被换掉或被改写了"
    assert excinfo.value.code == "PLUGIN_MANUAL_NOT_MANAGED"


def test_the_same_process_lock_also_honours_the_deadline() -> None:
    """The process lock is taken *before* the file lock, so bounding only the
    file lock bounds the rarer half.

    Two HTTP requests hitting the same server contend here, not on the file
    lock — that is the ordinary case, and it queued unboundedly.

    Mutation: drop the deadline branch from ``_CrossLoopLock.acquire``.
    """
    from plugin.server.application.plugins import operation_lock as module

    async def _scenario() -> str:
        lock = module._CrossLoopLock()
        await lock.acquire()  # 先被别人占住
        with module.bounded_operation_wait(0.15):
            try:
                await asyncio.wait_for(lock.acquire(), timeout=6.0)
            except module.PluginOperationBusy:
                return "busy"
            except asyncio.TimeoutError:
                return "queued-forever"
        return "acquired"

    assert asyncio.run(_scenario()) == "busy"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20s", 12.0),      # 解析不了
        ("inf", 12.0),      # 无穷大：max() 留不住它，而无限超时等于没有截止期
        ("1e309", 12.0),    # 溢出成 inf
        ("-5", 1.0),        # 负数：夹到下界，不是拒绝
        ("30", 30.0),       # 正常值照常生效
    ],
)
def test_unusable_budget_env_vars_fall_back_instead_of_breaking(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    """These parse at import time, so anything they raise stops the server.

    ``inf`` is the subtle one: it parses fine and survives ``max()``, and an
    infinite timeout is worse than the default because it looks configured
    while removing the deadline entirely (CodeRabbit).

    Mutation: drop the ``math.isfinite`` check, or the try/except.
    """
    from plugin.server.application.plugins._env_budgets import env_seconds

    monkeypatch.setenv("NEKO_TEST_BUDGET", raw)

    assert env_seconds("NEKO_TEST_BUDGET", 12.0) == expected


# 这里原本有一条 test_clearing_one_plugin_leaves_the_others_cached，钉的是
# clear_plugin_metadata_scan_cache 的 config_path 分支。那个分支从来没有生产调用方
# ——单插件刷新走的是 force，而 force 自己就会把那把键从缓存里删掉——所以这条守卫
# 让一段没人执行的代码看起来是被覆盖的。而且它是照着键的下标顺序手搓元组的：改
# 键的排布只会让测试跟着改，证明不了任何事。分支删了，守卫跟着删。


def test_work_before_the_lock_does_not_eat_the_wait_budget() -> None:
    """The budget is "how long to wait for the lock", not "how long the request may take".

    ``reload_all_plugins`` runs a registry refresh *before* its first serialized
    stop, and that refresh has its own budget of the same size. Storing an
    absolute deadline at request entry meant the refresh could exhaust it, so
    the first acquisition raised 409 with nobody holding the lock — and since
    timed-out scans are not cached, every retry did the same (codex).

    Mutation: store ``time.monotonic() + seconds`` at ``__enter__`` again.
    """
    from plugin.server.application.plugins import operation_lock as module

    with module.bounded_operation_wait(0.20):
        first = module._wait_deadline()
        assert first is not None
        # 模拟抢锁之前的慢活儿，长于整个预算
        time.sleep(0.30)
        second = module._wait_deadline()

    assert second is not None
    assert second > time.monotonic(), (
        "抢锁之前的耗时把等锁预算吃光了——没人占锁也会立刻 409"
    )
