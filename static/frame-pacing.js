/**
 * 帧率总闸（仅 Electron Pet 窗口生效）
 *
 * 背景：设置里的「帧率」只写 `window.targetFrameRate`，各渲染后端（Live2D / VRM / MMD）
 * 原本各自用 rAF + 跳帧实现节流。跳帧只省掉 draw 本身，rAF 请求仍让 Blink 以显示器
 * 刷新率跑完整主帧生命周期，CPU/GPU 降不下来。
 *
 * 本文件只做两件事：
 *   1. 在 Pet 窗口里用 rAF 时间戳量出显示器刷新率；
 *   2. 回答「活动态是否该改用定时器驱动、用多少帧」——配置帧率明显低于刷新率时，
 *      各后端把 rAF 链整个停掉、改 setInterval 按配置帧率手动 tick（与现有
 *      空闲低频 tick 模式同一套机制，只是频率换成配置值）。
 *
 * 非 Pet 页面（浏览器单窗口、模型管理器、角色卡等）不加载本文件，`window.nekoFramePacing`
 * 不存在，各后端保持原有 rAF + 跳帧行为不变。
 */
(function () {
    'use strict';
    if (window.nekoFramePacing) return;

    const REFRESH_SAMPLE_FRAMES = 24;        // 采样的 rAF 帧数
    const REFRESH_SAMPLE_TIMEOUT_MS = 3000;  // 采样超时：测不到就一律走 rAF（保守）
    const REFRESH_SAMPLE_START_DELAY_MS = 1500; // 页面 load 后延迟再采样，避开加载抖动
    // 配置帧率必须低于「刷新率 × 该比例」才切定时器驱动：60fps 配置在 60Hz 屏上留在
    // rAF（vsync 对齐更平滑，定时器也省不出东西），在 120/144Hz 屏上才切定时器。
    const TIMER_DRIVE_REFRESH_RATIO = 0.9;

    const state = { refreshHz: null, sampling: false };
    let startTimer = null;

    function isElectronPet() {
        return window.__LANLAN_IS_ELECTRON_PET__ === true;
    }

    // 用户配置的目标帧率；0 = 不限帧（跟随 VSync）。与 live2d-core 的解析规则一致。
    function configuredTargetFps() {
        const raw = typeof window.targetFrameRate === 'number' ? Number(window.targetFrameRate) : 60;
        return Number.isFinite(raw) && raw >= 0 ? raw : 60;
    }

    function getDisplayRefreshHz() {
        return state.refreshHz;
    }

    /**
     * 活动态应使用的定时器驱动帧率；返回 null 表示留在 rAF 驱动。
     * 只有 Pet 窗口 + 配置帧率 > 0 + 已测出刷新率 + 配置明显低于刷新率时才给出数值。
     */
    function activeTimerTickFps() {
        if (!isElectronPet()) return null;
        const fps = configuredTargetFps();
        if (!(fps > 0)) return null;
        if (!(state.refreshHz > 0)) return null;
        return fps < state.refreshHz * TIMER_DRIVE_REFRESH_RATIO ? fps : null;
    }

    /**
     * 用连续 rAF 时间戳的中位数间隔估算刷新率。误差方向是保守的：加载期抖动只会让
     * 间隔变长（刷新率被低估 → 更倾向留在 rAF），不会把刷新率估高。
     */
    function measureDisplayRefreshRate(onDone) {
        if (state.sampling || typeof requestAnimationFrame !== 'function') return;
        state.sampling = true;
        const stamps = [];
        let settled = false;
        const finish = (hz) => {
            if (settled) return;
            settled = true;
            state.sampling = false;
            // 测出就更新；测不出（超时/rAF 被挂起）则作废旧值——跨屏后旧显示器的刷新率
            // 不能继续拿来决定驱动方式，回到「未知 → 保守走 rAF」。
            state.refreshHz = hz > 0 ? hz : null;
            if (typeof onDone === 'function') {
                try { onDone(state.refreshHz); } catch (_) {}
            }
        };
        const timeout = setTimeout(() => finish(null), REFRESH_SAMPLE_TIMEOUT_MS);
        const step = (t) => {
            if (settled) return;
            stamps.push(t);
            if (stamps.length <= REFRESH_SAMPLE_FRAMES) {
                requestAnimationFrame(step);
                return;
            }
            clearTimeout(timeout);
            const deltas = [];
            for (let i = 1; i < stamps.length; i++) deltas.push(stamps[i] - stamps[i - 1]);
            deltas.sort((a, b) => a - b);
            const median = deltas[Math.floor(deltas.length / 2)];
            finish(median > 0 ? Math.round(1000 / median) : null);
        };
        requestAnimationFrame(step);
    }

    function scheduleMeasure(delayMs) {
        if (!isElectronPet()) return;
        if (startTimer) clearTimeout(startTimer);
        startTimer = setTimeout(() => {
            startTimer = null;
            measureDisplayRefreshRate();
        }, Math.max(0, Number(delayMs) || 0));
    }

    window.nekoFramePacing = Object.freeze({
        isElectronPet,
        configuredTargetFps,
        getDisplayRefreshHz,
        activeTimerTickFps,
        measureDisplayRefreshRate,
        TIMER_DRIVE_REFRESH_RATIO,
    });

    if (isElectronPet()) {
        if (document.readyState === 'complete') {
            scheduleMeasure(REFRESH_SAMPLE_START_DELAY_MS);
        } else {
            window.addEventListener('load', () => scheduleMeasure(REFRESH_SAMPLE_START_DELAY_MS), { once: true });
        }
        // 跨屏 / 显示器热插拔后刷新率可能变化：重测（沿用各后端已在监听的同名事件）
        window.addEventListener('electron-display-changed', () => scheduleMeasure(REFRESH_SAMPLE_START_DELAY_MS));
    }
})();
