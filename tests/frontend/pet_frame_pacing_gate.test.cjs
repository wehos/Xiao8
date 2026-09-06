const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test, mock } = require('node:test');
const vm = require('node:vm');

// pytest 包装层把本文件内容写到临时路径再交给 node 跑，__dirname 不是仓库根。
const fileRoot = path.resolve(__dirname, '..', '..');
const PROJECT_ROOT = fs.existsSync(path.join(fileRoot, 'static')) ? fileRoot : process.cwd();
const read = (rel) => fs.readFileSync(path.join(PROJECT_ROOT, ...rel.split('/')), 'utf8');

const FRAME_PACING_SRC = read('static/frame-pacing.js');
const LIVE2D_CORE_SRC = read('static/live2d/live2d-core.js');
const VRM_MANAGER_SRC = read('static/vrm/vrm-manager.js');
const MMD_CORE_SRC = read('static/mmd/mmd-core.js');

// ─────────────────────────────────────────────────────────────────────────────
// 沙盒：真实加载 frame-pacing.js + 目标后端源码；rAF 由测试手动推帧（可指定刷新率），
// setTimeout/setInterval 走 node:test 的 mock timers。
// ─────────────────────────────────────────────────────────────────────────────
function makeTicker(name) {
    return {
        name,
        started: true,
        maxFPS: 0,
        updates: 0,
        startCalls: 0,
        stopCalls: 0,
        update() { this.updates++; },
        start() { this.started = true; this.startCalls++; },
        stop() { this.started = false; this.stopCalls++; },
    };
}

function createSandbox({ pet, targetFrameRate }) {
    const rafQueue = [];
    let rafId = 0;
    const listeners = {};
    const PIXI = {
        live2d: { Live2DModel: class Live2DModel {} },
        Ticker: { shared: makeTicker('shared'), system: makeTicker('system') },
        Application: class Application {
            constructor() { this.ticker = makeTicker('app'); this.stage = {}; this.renderer = {}; }
        },
    };
    const window = {
        PIXI,
        targetFrameRate,
        __LANLAN_IS_ELECTRON_PET__: pet === true,
        innerWidth: 1920,
        innerHeight: 1080,
        screen: { width: 1920, height: 1080 },
        devicePixelRatio: 1,
        location: { hostname: 'localhost', pathname: '/' },
        navigator: { userAgent: 'node' },
        localStorage: { getItem() { return null; }, setItem() {} },
        addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
        removeEventListener() {},
        dispatchEvent(evt) { (listeners[evt.type] || []).forEach((fn) => fn(evt)); return true; },
    };
    const document = {
        hidden: false,
        readyState: 'complete',
        getElementById() { return null; },
        addEventListener() {},
        createElement() { return { style: {} }; },
        body: { classList: { contains() { return false; } } },
    };
    // 沙盒时钟：随 pumpFrames 推进；rAF 时间戳与 performance.now() 同源
    let frameClock = 0;
    const sandbox = {
        window,
        document,
        PIXI,
        console: { log() {}, warn() {}, error() {}, info() {}, debug() {} },
        performance: { now: () => frameClock },
        navigator: window.navigator,
        localStorage: window.localStorage,
        // 走 globalThis 间接调用，让 node:test 的 mock timers 生效
        setTimeout: (...a) => globalThis.setTimeout(...a),
        clearTimeout: (...a) => globalThis.clearTimeout(...a),
        setInterval: (...a) => globalThis.setInterval(...a),
        clearInterval: (...a) => globalThis.clearInterval(...a),
        requestAnimationFrame(fn) { rafId += 1; rafQueue.push({ id: rafId, fn }); return rafId; },
        cancelAnimationFrame(id) { const i = rafQueue.findIndex((e) => e.id === id); if (i >= 0) rafQueue.splice(i, 1); },
        CustomEvent: class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    };
    sandbox.globalThis = sandbox;
    sandbox.self = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(FRAME_PACING_SRC, sandbox, { filename: 'frame-pacing.js' });

    // 手动推 n 个 rAF 帧，时间戳按 hz 递增
    const pumpFrames = (n, hz) => {
        for (let i = 0; i < n; i++) {
            frameClock += 1000 / hz;
            const batch = rafQueue.splice(0, rafQueue.length);
            batch.forEach((e) => e.fn(frameClock));
        }
    };
    // 推进 mock 定时器的同时推进沙盒时钟，让 performance.now() 依赖的保持期判定
    // （_hasRenderActivity 的 900ms 窗口）也随时间失效
    const tick = (ms) => {
        frameClock += ms;
        mock.timers.tick(ms);
    };
    const measureRefresh = (hz) => {
        // frame-pacing：load 后延迟 1500ms 才开始采样，采 24 帧
        tick(1500);
        pumpFrames(30, hz);
    };
    const load = (src, name) => vm.runInContext(src, sandbox, { filename: name });
    return { sandbox, window, PIXI, pumpFrames, measureRefresh, tick, load, rafQueue, listeners };
}

function withMockTimers(fn) {
    return () => {
        mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
        try { fn(); } finally { mock.timers.reset(); }
    };
}

function setupLive2D(sb) {
    sb.load(LIVE2D_CORE_SRC, 'live2d-core.js');
    const mgr = new sb.window.Live2DManager();
    const app = new sb.PIXI.Application();
    mgr.pixi_app = app;
    mgr.isInitialized = true;
    // 复刻 initPIXI 里对 ticker.stop/start 的包装
    const origStop = app.ticker.stop.bind(app.ticker);
    const origStart = app.ticker.start.bind(app.ticker);
    mgr._tickerOrigStop = origStop;
    mgr._tickerOrigStart = origStart;
    app.ticker.stop = function () { mgr._exitIdleTickMode(); return origStop(); };
    app.ticker.start = function () { mgr._exitIdleTickMode(); return origStart(); };
    return { mgr, app, shared: sb.PIXI.Ticker.shared, system: sb.PIXI.Ticker.system };
}

// ─────────────────────────────────────────────────────────────────────────────
// frame-pacing.js
// ─────────────────────────────────────────────────────────────────────────────
test('frame-pacing：非 Pet 页面永远不给定时器驱动帧率', withMockTimers(() => {
    const sb = createSandbox({ pet: false, targetFrameRate: 30 });
    sb.measureRefresh(60);
    assert.equal(sb.window.nekoFramePacing.getDisplayRefreshHz(), null, '非 Pet 不采样刷新率');
    assert.equal(sb.window.nekoFramePacing.activeTimerTickFps(), null);
}));

test('frame-pacing：Pet 窗口先量出刷新率，配置明显低于刷新率才切定时器驱动', withMockTimers(() => {
    const sb = createSandbox({ pet: true, targetFrameRate: 45 });
    const pacing = sb.window.nekoFramePacing;
    assert.equal(pacing.activeTimerTickFps(), null, '刷新率未测出前保守走 rAF');
    sb.measureRefresh(60);
    assert.equal(pacing.getDisplayRefreshHz(), 60);
    assert.equal(pacing.activeTimerTickFps(), 45, '45 < 60×0.9 → 定时器驱动 45fps');
    sb.window.targetFrameRate = 60;
    assert.equal(pacing.activeTimerTickFps(), null, '60fps 配置在 60Hz 屏留在 rAF（vsync 对齐）');
    sb.window.targetFrameRate = 0;
    assert.equal(pacing.activeTimerTickFps(), null, '0=不限帧 留在 rAF');
    sb.window.targetFrameRate = 30;
    assert.equal(pacing.activeTimerTickFps(), 30);
}));

test('frame-pacing：重测超时要作废旧刷新率，回到保守 rAF', withMockTimers(() => {
    const sb = createSandbox({ pet: true, targetFrameRate: 45 });
    sb.measureRefresh(60);
    assert.equal(sb.window.nekoFramePacing.activeTimerTickFps(), 45);
    // 跨屏事件触发重测，但 rAF 一帧都不来 → 3s 超时
    sb.window.dispatchEvent({ type: 'electron-display-changed' });
    // mock timers 不会在同一次 tick 里跑 tick 期间新排的定时器：先到采样起点，再跨过超时
    sb.tick(1500);
    sb.tick(3000);
    assert.equal(sb.window.nekoFramePacing.getDisplayRefreshHz(), null, '超时后旧值作废');
    assert.equal(sb.window.nekoFramePacing.activeTimerTickFps(), null, '未知刷新率 → rAF');
    // 再来一次重测成功 → 恢复
    sb.window.dispatchEvent({ type: 'electron-display-changed' });
    sb.measureRefresh(120);
    assert.equal(sb.window.nekoFramePacing.getDisplayRefreshHz(), 120);
    assert.equal(sb.window.nekoFramePacing.activeTimerTickFps(), 45);
}));

test('frame-pacing：144Hz 屏上 60fps 配置也切定时器驱动', withMockTimers(() => {
    const sb = createSandbox({ pet: true, targetFrameRate: 60 });
    sb.measureRefresh(144);
    assert.equal(sb.window.nekoFramePacing.getDisplayRefreshHz(), 144);
    assert.equal(sb.window.nekoFramePacing.activeTimerTickFps(), 60);
}));

// ─────────────────────────────────────────────────────────────────────────────
// Live2D
// ─────────────────────────────────────────────────────────────────────────────
test('Live2D 非 Pet：行为不变——活动态 rAF + maxFPS，只碰 app.ticker，不碰全局 shared/system', withMockTimers(() => {
    const sb = createSandbox({ pet: false, targetFrameRate: 45 });
    const { mgr, app, shared, system } = setupLive2D(sb);
    shared.maxFPS = 999;
    system.maxFPS = 999;
    mgr.boostInteractiveFPS();
    assert.ok(!mgr._idleTickMode);
    assert.equal(app.ticker.started, true);
    assert.equal(app.ticker.maxFPS, 45);
    assert.equal(shared.maxFPS, 999, '非 Pet 不接管 shared');
    assert.equal(system.maxFPS, 999, '非 Pet 不接管 system');
    sb.tick(1000); // 衰减
    assert.equal(mgr._idleTickMode, true, '无活动衰减到空闲低频 tick');
    assert.equal(mgr._idleTickFps, 30);
    assert.equal(app.ticker.started, false);
    assert.equal(shared.maxFPS, 999);
    assert.equal(system.maxFPS, 999);
}));

test('Live2D Pet + 配置低于刷新率：活动态也走定时器驱动，衰减只换周期、不回 rAF', withMockTimers(() => {
    const sb = createSandbox({ pet: true, targetFrameRate: 45 });
    sb.measureRefresh(60);
    const { mgr, app, shared, system } = setupLive2D(sb);
    mgr.boostInteractiveFPS();
    assert.equal(mgr._idleTickMode, true, '活动态直接进定时器模式');
    assert.equal(mgr._idleTickFps, 45, '周期 = 配置帧率');
    assert.equal(app.ticker.started, false, 'app ticker 的 rAF 链已停');
    assert.equal(shared.started, false, 'shared ticker 已停');
    assert.equal(system.started, false, 'system ticker 已停');
    assert.equal(app.ticker.maxFPS, 0, '定时器模式下 maxFPS 清零，避免抖动丢帧');
    assert.equal(shared.maxFPS, 0);
    assert.equal(system.maxFPS, 0);

    const before = { app: app.ticker.updates, shared: shared.updates, system: system.updates };
    sb.tick(22 * 5);
    assert.ok(app.ticker.updates - before.app >= 4, 'app ticker 按 45fps 手动 update');
    assert.ok(shared.updates - before.shared >= 4, 'shared ticker 一起被手动 update');
    assert.ok(system.updates - before.system >= 4, 'system ticker 一起被手动 update');

    sb.tick(1000); // 衰减
    assert.equal(mgr._idleTickMode, true);
    assert.equal(mgr._idleTickFps, 30, '衰减到地板 30');
    assert.equal(app.ticker.startCalls, 0, '整个过程从未回到 rAF 模式');

    // 空闲态改设置：只换周期
    sb.window.targetFrameRate = 24;
    mgr.setTargetFPS(24);
    assert.equal(mgr._idleTickMode, true);
    assert.equal(mgr._idleTickFps, 24, '地板不超过用户配置');
    assert.equal(app.ticker.startCalls, 0);
}));

test('Live2D Pet + 配置 = 刷新率：rAF 模式接管 shared/system 的 maxFPS，进出定时器模式正确保存/恢复', withMockTimers(() => {
    const sb = createSandbox({ pet: true, targetFrameRate: 60 });
    sb.measureRefresh(60);
    const { mgr, app, shared, system } = setupLive2D(sb);
    mgr.boostInteractiveFPS();
    assert.ok(!mgr._idleTickMode, '60@60Hz 留在 rAF');
    assert.equal(app.ticker.maxFPS, 60);
    assert.equal(shared.maxFPS, 60, 'Pet 下 shared 也被限到配置帧率');
    assert.equal(system.maxFPS, 60);

    sb.tick(1000); // 衰减 → 空闲定时器
    assert.equal(mgr._idleTickMode, true);
    assert.equal(mgr._idleTickFps, 30);
    assert.equal(app.ticker.maxFPS, 0);
    assert.equal(shared.maxFPS, 0);
    assert.equal(system.maxFPS, 0);

    mgr.boostInteractiveFPS(); // 活动 → 回 rAF
    assert.equal(mgr._idleTickMode, false);
    assert.equal(app.ticker.started, true);
    assert.equal(shared.started, true);
    assert.equal(system.started, true);
    assert.equal(app.ticker.maxFPS, 60);
    assert.equal(shared.maxFPS, 60, '退出定时器模式后恢复到配置值而不是旧的地板值');
    assert.equal(system.maxFPS, 60);

    sb.window.targetFrameRate = 0;
    mgr.setTargetFPS(0);
    mgr.boostInteractiveFPS();
    assert.equal(mgr._idleTickMode, false, '0=不限帧 留在 rAF');
    assert.equal(app.ticker.maxFPS, 0);
    assert.equal(shared.maxFPS, 0);
}));

test('Live2D：外部 pauseRendering 时不接管；ticker.start() 让位后 governor 自愈回定时器', withMockTimers(() => {
    const sb = createSandbox({ pet: true, targetFrameRate: 30 });
    sb.measureRefresh(60);
    const { mgr, app } = setupLive2D(sb);
    mgr.pauseRendering();
    mgr.boostInteractiveFPS();
    assert.ok(!mgr._idleTickMode, '外部暂停时不进定时器模式');
    assert.equal(app.ticker.started, false);
    mgr.resumeRendering();
    mgr.boostInteractiveFPS();
    assert.equal(mgr._idleTickMode, true);
    app.ticker.start(); // 外部「确保渲染」路径
    assert.equal(mgr._idleTickMode, false, '外部 start() 让位给 rAF');
    mgr._startIdleFpsGovernor();
    sb.tick(1500);
    assert.equal(mgr._idleTickMode, true, 'governor 自愈回定时器模式');
    mgr._stopIdleFpsGovernor();
}));

// ─────────────────────────────────────────────────────────────────────────────
// VRM
// ─────────────────────────────────────────────────────────────────────────────
function setupVRM(sb) {
    sb.load(VRM_MANAGER_SRC, 'vrm-manager.js');
    const mgr = new sb.window.VRMManager();
    mgr.renderer = {};
    mgr.scene = {};
    mgr.camera = {};
    mgr.renders = 0;
    mgr._renderFrame = () => { mgr.renders++; };
    mgr._rafDriver = () => {};
    mgr._animationFrameId = sb.sandbox.requestAnimationFrame(mgr._rafDriver);
    return mgr;
}

test('VRM Pet + 配置低于刷新率：活动态定时器驱动，衰减只换周期', withMockTimers(() => {
    const sb = createSandbox({ pet: true, targetFrameRate: 45 });
    sb.measureRefresh(60);
    const mgr = setupVRM(sb);
    mgr._boostInteractiveFPS();
    assert.equal(mgr._idleTickMode, true);
    assert.equal(mgr._idleTickFps, 45);
    assert.equal(mgr._isLowTickRate(), false, '活动态 45fps 定时器驱动不算低频：medium 隔帧物理照常');
    assert.equal(mgr._animationFrameId, null, 'rAF 链已停');
    sb.tick(22 * 5);
    assert.ok(mgr.renders >= 4, '按 45fps 定时渲染');
    sb.tick(1000);
    assert.equal(mgr._idleTickMode, true);
    assert.equal(mgr._idleTickFps, 30);
    assert.equal(mgr._isLowTickRate(), true, '30fps 地板才绕过隔帧物理');
    assert.equal(sb.rafQueue.length, 0, '从未重新排 rAF');
    // 设置变更事件驱动换周期
    sb.window.targetFrameRate = 24;
    sb.window.vrmManager = mgr;
    sb.window.dispatchEvent({ type: 'neko-frame-rate-changed', detail: { fps: 24 } });
    assert.equal(mgr._idleTickFps, 24);
}));

test('VRM 非 Pet：行为不变——活动态回 rAF', withMockTimers(() => {
    const sb = createSandbox({ pet: false, targetFrameRate: 45 });
    const mgr = setupVRM(sb);
    sb.tick(0);
    mgr._enterIdleTickMode();
    assert.equal(mgr._idleTickMode, true);
    mgr._boostInteractiveFPS();
    assert.equal(mgr._idleTickMode, false);
    assert.ok(mgr._animationFrameId, '活动态重新排 rAF');
}));

// ─────────────────────────────────────────────────────────────────────────────
// MMD
// ─────────────────────────────────────────────────────────────────────────────
function setupMMD(sb) {
    sb.load(MMD_CORE_SRC, 'mmd-core.js');
    const manager = { _shouldRender: true, _isDisposed: false, renderer: {}, _animationFrameId: null, clock: null, _renderWaiters: [] };
    const core = new sb.window.MMDCore(manager);
    core.renders = 0;
    core._renderFrame = () => { core.renders++; };
    manager._animationFrameId = sb.sandbox.requestAnimationFrame(() => {});
    manager.core = core;
    return { core, manager };
}

test('MMD：目标帧率接到设置里的帧率滑块，0 = 不限帧', withMockTimers(() => {
    const sb = createSandbox({ pet: false, targetFrameRate: 45 });
    const { core } = setupMMD(sb);
    assert.equal(core.targetFPS, 45);
    sb.window.targetFrameRate = 0;
    core._refreshTargetFps();
    assert.equal(core.targetFPS, 0);
    assert.equal(core.frameTime, 0, '不限帧时 rAF 路径不再跳帧');
    assert.equal(core._resolveIdleFps(), 30);
    sb.window.targetFrameRate = 24;
    core._refreshTargetFps();
    assert.equal(core.frameTime, 1000 / 24);
    assert.equal(core._resolveIdleFps(), 24);
    delete sb.window.targetFrameRate;
    core._refreshTargetFps();
    assert.ok([30, 45, 60].includes(core.targetFPS), '没有该配置时退回 performanceMode 档位');
}));

test('MMD rAF 路径：不限帧后切回有限帧率，节流状态不能被 NaN 污染', withMockTimers(() => {
    const sb = createSandbox({ pet: false, targetFrameRate: 0 });
    const { core, manager } = setupMMD(sb);
    manager._animationFrameId = null;
    // 不限帧：连续 rAF 全部渲染
    core._render();
    sb.pumpFrames(1, 60);
    sb.pumpFrames(1, 60);
    assert.equal(core.renders, 3, '不限帧时每个 rAF 都渲染');
    assert.ok(Number.isFinite(core.lastFrameTime), 'lastFrameTime 必须是有限数');
    // 切回 45fps：60Hz 推 60 帧应只渲染约 45 帧
    sb.window.targetFrameRate = 45;
    const before = core.renders;
    sb.pumpFrames(60, 60);
    const rendered = core.renders - before;
    assert.ok(rendered >= 40 && rendered <= 50, `切回 45fps 后应重新节流，实际渲染 ${rendered}/60`);
    assert.ok(Number.isFinite(core.lastFrameTime));
}));

test('MMD：交互升帧的 900ms 保持期到期后活动判定失效', withMockTimers(() => {
    const sb = createSandbox({ pet: false, targetFrameRate: 45 });
    const { core, manager } = setupMMD(sb);
    sb.tick(1000); // 让 performance.now() 离开 0，时间戳判定才不会被当成未设置
    manager._lastInteractionBoostTs = sb.sandbox.performance.now();
    assert.equal(core._hasRenderActivity(), true);
    sb.tick(899);
    assert.equal(core._hasRenderActivity(), true, '保持期内仍算活动');
    sb.tick(2);
    assert.equal(core._hasRenderActivity(), false, '过了 900ms 保持期失效');
    // 光标跟随时间戳同一套窗口
    manager.cursorFollow = { enabled: true, _lastPointerMoveTs: sb.sandbox.performance.now(), _targetYaw: 0, _currentYaw: 0, _targetPitch: 0, _currentPitch: 0 };
    assert.equal(core._hasRenderActivity(), true);
    sb.tick(901);
    assert.equal(core._hasRenderActivity(), false);
}));

test('MMD Pet + 配置低于刷新率：活动态定时器驱动，衰减只换周期', withMockTimers(() => {
    const sb = createSandbox({ pet: true, targetFrameRate: 45 });
    sb.measureRefresh(60);
    const { core, manager } = setupMMD(sb);
    core._boostInteractiveFPS();
    assert.equal(core._idleTickMode, true);
    assert.equal(core._idleTickFps, 45);
    assert.equal(manager._animationFrameId, null);
    sb.tick(22 * 5);
    assert.ok(core.renders >= 4);
    sb.tick(1000);
    assert.equal(core._idleTickMode, true);
    assert.equal(core._idleTickFps, 30);
    assert.equal(sb.rafQueue.length, 0, '从未重新排 rAF');
    sb.window.targetFrameRate = 24;
    sb.window.mmdManager = manager;
    sb.window.dispatchEvent({ type: 'neko-frame-rate-changed', detail: { fps: 24 } });
    assert.equal(core._idleTickFps, 24);
}));

// ─────────────────────────────────────────────────────────────────────────────
// 契约：页面侧对「坐标没变的 pointermove」不再升帧 / 不再算作光标活动
// ─────────────────────────────────────────────────────────────────────────────
test('契约：Live2D 交互层只有坐标真变了才升帧', () => {
    const src = read('static/live2d/live2d-interaction.js');
    const movedDecl = src.indexOf('const pointerMoved = pointer.x !== this._lastMouseX || pointer.y !== this._lastMouseY;');
    const lastAssign = src.indexOf('this._lastMouseX = pointer.x;');
    assert.ok(movedDecl >= 0, 'pointerMoved 判定缺失');
    assert.ok(movedDecl < lastAssign, 'pointerMoved 必须在覆盖 _lastMouseX 之前计算');
    const guarded = src.match(/pointerMoved && typeof this\.boostLinuxX11InteractiveFPS === 'function'/g) || [];
    assert.equal(guarded.length, 2, '悬停范围内 + 全屏跟踪 两处升帧都必须受 pointerMoved 门控');
    assert.doesNotMatch(
        src.slice(src.indexOf('const onPointerMove = '), src.indexOf('const onBlur = ')),
        /\n\s+if \(typeof this\.boostLinuxX11InteractiveFPS === 'function'\) \{\n\s+this\.boostLinuxX11InteractiveFPS\(\);/,
        'onPointerMove 内不允许残留无门控的升帧'
    );
});

test('契约：VRM / MMD 光标跟随对坐标没变的事件不刷新活动时间戳', () => {
    const vrm = read('static/vrm/vrm-cursor-follow.js');
    assert.match(vrm, /const moved = e\.clientX !== this\._rawMouseX \|\| e\.clientY !== this\._rawMouseY;/);
    assert.match(vrm, /if \(moved\) this\._lastPointerMoveAt = now;/);
    assert.doesNotMatch(vrm, /\n\s+this\._lastPointerMoveAt = now;\n/, '不允许无条件刷新');
    const mmd = read('static/mmd/mmd-cursor-follow.js');
    assert.match(mmd, /if \(e\.clientX !== this\._rawMouseX \|\| e\.clientY !== this\._rawMouseY\) \{\n\s+this\._lastPointerMoveTs = now;/);
    assert.equal((mmd.match(/this\._lastPointerMoveTs = now;/g) || []).length, 1, '时间戳只在坐标变化门控内刷新一处');
});

test('契约：frame-pacing.js 只在 index.html（Pet 窗口页面）加载', () => {
    const index = read('templates/index.html');
    assert.match(index, /<script src="\/static\/frame-pacing\.js\?v=\{\{ static_asset_version \}\}"><\/script>/);
    for (const tpl of ['model_manager.html', 'character_card_manager.html', 'card_maker.html', 'chat.html']) {
        assert.doesNotMatch(read('templates/' + tpl), /frame-pacing\.js/, tpl + ' 不应加载帧率总闸');
    }
});
