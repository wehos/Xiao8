/**
 * Frame pacing gate (Electron Pet window only).
 *
 * Background: the "frame rate" setting only writes `window.targetFrameRate`, and each
 * renderer backend (Live2D / VRM / MMD) used to throttle on its own with rAF + frame
 * skipping. Skipping only saves the draw itself; the pending rAF request still makes
 * Blink run a full main-frame lifecycle at the display refresh rate, so CPU/GPU never
 * actually go down.
 *
 * This file does two things:
 *   1. measures the display refresh rate from rAF timestamps inside the Pet window;
 *   2. answers "should the active state switch to timer driving, and at what rate" —
 *      when the configured frame rate is clearly below the refresh rate, backends stop
 *      their rAF chain entirely and tick manually via setInterval at the configured rate
 *      (same mechanism as the existing idle low-rate tick mode, just at a different rate).
 *
 * Non-Pet pages (browser single window, model manager, character cards, ...) do not load
 * this file, `window.nekoFramePacing` does not exist there, and every backend keeps its
 * original rAF + frame-skipping behaviour.
 */
(function () {
    'use strict';
    if (window.nekoFramePacing) return;

    const REFRESH_SAMPLE_FRAMES = 24;        // number of rAF frames to sample
    const REFRESH_SAMPLE_TIMEOUT_MS = 3000;  // sampling timeout: unmeasurable -> stay on rAF (conservative)
    const REFRESH_SAMPLE_START_DELAY_MS = 1500; // delay after load before sampling, to skip load-time jank
    // The configured rate must be below "refresh rate x this ratio" to switch to timer
    // driving: a 60fps setting stays on rAF on a 60Hz display (vsync-aligned is smoother
    // and a timer would save nothing) and only switches on 120/144Hz displays.
    const TIMER_DRIVE_REFRESH_RATIO = 0.9;

    const state = { refreshHz: null, sampling: false, resamplePending: false, pendingCallbacks: [] };
    let startTimer = null;

    function isElectronPet() {
        return window.__LANLAN_IS_ELECTRON_PET__ === true;
    }

    // User-configured target frame rate; 0 = uncapped (follow VSync). Same parsing rules as live2d-core.
    function configuredTargetFps() {
        const raw = typeof window.targetFrameRate === 'number' ? Number(window.targetFrameRate) : 60;
        return Number.isFinite(raw) && raw >= 0 ? raw : 60;
    }

    function getDisplayRefreshHz() {
        return state.refreshHz;
    }

    /**
     * Timer-driving frame rate the active state should use; null means stay on rAF.
     * A value is returned only for Pet window + configured rate > 0 + refresh rate measured
     * + configured rate clearly below the refresh rate.
     * Callers may pass their own resolved target rate (e.g. MMD falls back to performanceMode
     * when window.targetFrameRate is absent) so the gate and the backend agree on "target rate".
     */
    function activeTimerTickFps(fpsOverride) {
        if (!isElectronPet()) return null;
        const override = Number(fpsOverride);
        const fps = Number.isFinite(override) && override >= 0 ? override : configuredTargetFps();
        if (!(fps > 0)) return null;
        if (!(state.refreshHz > 0)) return null;
        return fps < state.refreshHz * TIMER_DRIVE_REFRESH_RATIO ? fps : null;
    }

    /**
     * Estimate the refresh rate from the median interval of consecutive rAF timestamps.
     * The error is conservative: load-time jank only lengthens intervals (refresh rate
     * underestimated -> more likely to stay on rAF), it never overestimates it.
     */
    function measureDisplayRefreshRate(onDone) {
        if (typeof requestAnimationFrame !== 'function') return;
        if (state.sampling) {
            // Another request while sampling (back-to-back display changes): must not be
            // dropped -- the in-flight sample may hold timestamps from the previous display,
            // so run one more round right after it settles; queued callbacks fire after that round.
            state.resamplePending = true;
            if (typeof onDone === 'function') state.pendingCallbacks.push(onDone);
            return;
        }
        state.sampling = true;
        const stamps = [];
        let settled = false;
        const finish = (hz) => {
            if (settled) return;
            settled = true;
            state.sampling = false;
            // Measured -> update; unmeasurable (timeout / rAF suspended) -> invalidate the old
            // value: after a display change the previous display's refresh rate must not keep
            // deciding the driving mode, fall back to "unknown -> conservative rAF".
            state.refreshHz = hz > 0 ? hz : null;
            if (typeof onDone === 'function') {
                try { onDone(state.refreshHz); } catch (_) {}
            }
            if (state.resamplePending) {
                state.resamplePending = false;
                const queued = state.pendingCallbacks.splice(0);
                measureDisplayRefreshRate(queued.length === 0 ? undefined : (hz) => {
                    queued.forEach((cb) => { try { cb(hz); } catch (_) {} });
                });
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

    /**
     * Whether a renderer backend is currently timer-driven; if so returns its tick rate,
     * otherwise null (rAF-driven). Lets the page's other per-frame loops (mic level monitor,
     * lip-sync, ...) follow along: once rendering no longer schedules rAF, any of these loops
     * scheduling rAF on their own would push Blink's main frame back to the display refresh
     * rate and cancel the gain.
     */
    function currentTimerTickFps() {
        if (!isElectronPet()) return null;
        const candidates = [
            window.live2dManager,
            window.vrmManager,
            window.mmdManager && window.mmdManager.core,
        ];
        for (const backend of candidates) {
            if (!backend || backend._idleTickMode !== true) continue;
            const fps = Number(backend._idleTickFps);
            if (fps > 0) return fps;
        }
        return null;
    }

    /**
     * Schedule one frame: setTimeout (same period as the render tick) while a backend is
     * timer-driven, otherwise rAF. Returns a cancel function.
     */
    function requestPacedFrame(callback) {
        const fps = currentTimerTickFps();
        if (fps != null) {
            const id = setTimeout(() => callback(performance.now()), Math.max(4, Math.round(1000 / fps)));
            return () => clearTimeout(id);
        }
        const id = requestAnimationFrame(callback);
        return () => cancelAnimationFrame(id);
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
        currentTimerTickFps,
        requestPacedFrame,
        measureDisplayRefreshRate,
        TIMER_DRIVE_REFRESH_RATIO,
    });

    if (isElectronPet()) {
        if (document.readyState === 'complete') {
            scheduleMeasure(REFRESH_SAMPLE_START_DELAY_MS);
        } else {
            window.addEventListener('load', () => scheduleMeasure(REFRESH_SAMPLE_START_DELAY_MS), { once: true });
        }
        // Refresh rate may change after moving displays / hotplug: re-measure (same event the
        // backends already listen to)
        window.addEventListener('electron-display-changed', () => scheduleMeasure(REFRESH_SAMPLE_START_DELAY_MS));
    }
})();
