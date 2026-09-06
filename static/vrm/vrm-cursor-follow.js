/**
 * VRM Cursor Follow Controller
 * 实现「眼睛注视 + 头/脖子转动」跟踪鼠标光标
 *
 * 通道 A（眼睛）：高灵敏、低延迟 → vrm.lookAt.target
 * 通道 B（头部）：较慢惯性、小幅平滑 → neck/head 加成旋转
 *
 * 默认始终启用（无 UI 开关）。若 VRM 不支持相关骨骼/LookAt 则自动降级。
 */

// ─── 确保 THREE 可用 ────────────────────────────────────────────────
var THREE = (typeof window !== 'undefined' && window.THREE) ||
    (typeof globalThis !== 'undefined' && globalThis.THREE) || null;

// ─── 默认参数（集中配置，方便调参） ─────────────────────────────────
const CURSOR_FOLLOW_DEFAULTS = Object.freeze({
    // ── 死区 ──────────────────────────────────────────────
    deadzoneDeg: 1.2,                // 小于此角度变化不驱动

    // ── 眼睛通道 ──────────────────────────────────────────
    eyeMaxYawDeg: 30,
    eyeMaxPitchUpDeg: 30,
    eyeMaxPitchDownDeg: 26,
    eyeCenterDeadzoneDeg: 1.2,       // 头部中线附近强制回零，防止左右判定抖动
    eyeSmoothSpeed: 12.0,            // 指数阻尼速度（越大越跟手）
    eyeOneEuroMinCutoff: 1.5,        // One-Euro: 最小截止频率（越大越跟手、越不平滑）
    eyeOneEuroBeta: 0.5,             // One-Euro: 速度系数（越大快速运动越跟手）
    eyeOneEuroDCutoff: 1.0,

    // ── 头部通道 ──────────────────────────────────────────
    headMaxYawDeg: 45,
    headMaxPitchUpDeg: 30,
    headMaxPitchDownDeg: 25,
    headSmoothSpeed: 3.0,            // 比眼睛慢 → 实现"眼快头慢"
    headOneEuroMinCutoff: 0.8,
    headOneEuroBeta: 0.3,
    headOneEuroDCutoff: 1.0,

    // ── 头/颈分配 ─────────────────────────────────────────
    neckContribution: 0.6,           // 脖子承担 60%
    headContribution: 0.4,           // 头部承担 40%
    headBoneMode: 'neckAndHead',     // headOnly: 仅驱动 head；neckAndHead: 同时驱动（更自然）

    // ── 动作权重 ──────────────────────────────────────────
    headWeightIdle: 1.0,             // 无动画时（纯静止）
    headWeightIdleAnim: 0.7,         // 待机动画播放时（加成叠加，保留呼吸协调）
    headWeightAction: 0.0,           // 一次性动作播放时（完全让位）
    weightTransitionSec: 0.2,        // 权重过渡时间

    // ── 拖拽降权 ─────────────────────────────────────────
    reduceWhileDragging: true,       // 拖拽/右键 orbit 时降低 headWeight

    // ── 眼睛目标球面半径 ──────────────────────────────────
    lookAtDistance: 1.6,             // 半径更小可提升眼睛可动范围

    // ── 性能优化：鼠标静止时降频 ─────────────────────────
    pointerIdleMs: 100,              // 超过该时长无鼠标输入视为 idle
    activeTargetSolveIntervalMs: 50, // active 时眼睛目标求解频率（约 20Hz）
    idleTargetSolveIntervalMs: 100,  // idle 时眼睛目标求解频率（约 10Hz）
    activeHeadSolveIntervalMs: 66,   // active 时头部角度求解频率（约 15Hz）
    idleHeadSolveIntervalMs: 100,    // idle 时头部角度求解频率（约 10Hz）
});

// ─── 四档性能预设 ─────────────────────────────────────────────────────
const CURSOR_FOLLOW_PERF_PRESETS = Object.freeze({
    none: Object.freeze({
        enabled: false,              // 无：关闭追踪
    }),
    low: Object.freeze({
        enabled: true,
        // 低档：比当前 medium 更省电
        pointerIdleMs: 160,
        activeTargetSolveIntervalMs: 140,  // ~7Hz
        idleTargetSolveIntervalMs: 260,    // ~4Hz
        activeHeadSolveIntervalMs: 180,    // ~5.5Hz
        idleHeadSolveIntervalMs: 300,      // ~3Hz
        solveTargetOnMoveOnly: false,      // 静止时仍持续求解，避免状态漂移
        solveHeadOnMoveOnly: false,        // 静止时仍持续求解，避免偶发跳变
    }),
    medium: Object.freeze({
        enabled: true,
        // 中档：比 high 更省一点（保持较好观感）
        pointerIdleMs: 140,
        activeTargetSolveIntervalMs: 90,   // ~11Hz
        idleTargetSolveIntervalMs: 180,    // ~5.5Hz
        activeHeadSolveIntervalMs: 120,    // ~8Hz
        idleHeadSolveIntervalMs: 220,      // ~4.5Hz
    }),
    high: Object.freeze({
        enabled: true,
        // 高档：保持当前效果
        pointerIdleMs: CURSOR_FOLLOW_DEFAULTS.pointerIdleMs,
        activeTargetSolveIntervalMs: CURSOR_FOLLOW_DEFAULTS.activeTargetSolveIntervalMs,
        idleTargetSolveIntervalMs: CURSOR_FOLLOW_DEFAULTS.idleTargetSolveIntervalMs,
        activeHeadSolveIntervalMs: CURSOR_FOLLOW_DEFAULTS.activeHeadSolveIntervalMs,
        idleHeadSolveIntervalMs: CURSOR_FOLLOW_DEFAULTS.idleHeadSolveIntervalMs,
    }),
});

// ─── One-Euro 滤波器 ────────────────────────────────────────────────
class OneEuroFilter {
    constructor(minCutoff, beta, dCutoff) {
        this.minCutoff = minCutoff;
        this.beta = beta;
        this.dCutoff = dCutoff;
        this._xPrev = null;
        this._dxPrev = 0;
        this._tPrev = null;
    }

    _alpha(te, cutoff) {
        const r = 2 * Math.PI * cutoff * te;
        return r / (r + 1);
    }

    filter(x, t) {
        if (this._tPrev === null) {
            this._xPrev = x;
            this._dxPrev = 0;
            this._tPrev = t;
            return x;
        }
        const te = t - this._tPrev;
        if (te <= 0) return this._xPrev;

        // 导数
        const ad = this._alpha(te, this.dCutoff);
        const dx = (x - this._xPrev) / te;
        const dxHat = ad * dx + (1 - ad) * this._dxPrev;

        // 自适应截止频率
        const cutoff = this.minCutoff + this.beta * Math.abs(dxHat);
        const a = this._alpha(te, cutoff);

        // 滤波值
        const xHat = a * x + (1 - a) * this._xPrev;

        this._xPrev = xHat;
        this._dxPrev = dxHat;
        this._tPrev = t;
        return xHat;
    }

    reset() {
        this._xPrev = null;
        this._dxPrev = 0;
        this._tPrev = null;
    }
}

// ─── CursorFollowController ─────────────────────────────────────────
class CursorFollowController {
    constructor() {
        this.manager = null;

        // ── 用户可配置参数（从默认值初始化，applyConfig 可覆盖） ──
        this.eyeMaxYawDeg = CURSOR_FOLLOW_DEFAULTS.eyeMaxYawDeg;
        this.eyeMaxPitchUpDeg = CURSOR_FOLLOW_DEFAULTS.eyeMaxPitchUpDeg;
        this.eyeMaxPitchDownDeg = CURSOR_FOLLOW_DEFAULTS.eyeMaxPitchDownDeg;
        this.headMaxYawDeg = CURSOR_FOLLOW_DEFAULTS.headMaxYawDeg;
        this.headMaxPitchUpDeg = CURSOR_FOLLOW_DEFAULTS.headMaxPitchUpDeg;
        this.headMaxPitchDownDeg = CURSOR_FOLLOW_DEFAULTS.headMaxPitchDownDeg;
        this.headSmoothSpeed = CURSOR_FOLLOW_DEFAULTS.headSmoothSpeed;

        // ── 眼睛目标 Object3D ──
        this.eyesTarget = null;

        // ── 鼠标跟踪启用状态 ──
        this._enabled = true;
        this._userDisabled = false;  // 记录用户显式禁用，避免性能档切换覆盖
        this._disabling = false;     // 正在回正过渡中

        // ── 鼠标状态 ──
        this._rawMouseX = 0;
        this._rawMouseY = 0;
        this._hasPointerInput = false;  // 首次 pointermove 前不驱动跟踪
        this._ignoreMouseMoveUntil = 0;
        this._lastCanvasRect = null;
        this._lastCanvasRectReadAt = 0;
        this._lastPointerMoveAt = 0;
        this._lastTargetSolveAt = 0;
        this._lastHeadSolveAt = 0;

        // ── One-Euro 滤波器（NDC 层面） ──
        this._eyeFilterX = null;
        this._eyeFilterY = null;
        this._eyeYaw = 0;
        this._eyePitch = 0;
        this._targetEyeYaw = 0;
        this._targetEyePitch = 0;

        // ── 头部追踪状态 ──
        this._headYaw = 0;
        this._headPitch = 0;
        this._targetHeadYaw = 0;
        this._targetHeadPitch = 0;
        this._headFilterYaw = null;
        this._headFilterPitch = null;

        // ── 权重 ──
        this._headWeight = 1.0;
        this._targetHeadWeight = 1.0;

        // ── 计时 ──
        this._elapsedTime = 0;

        // ── 预分配临时对象（减少 GC） ──
        this._raycaster = null;
        this._ndcVec = null;
        this._desiredTargetPos = null;
        this._headWorldPos = null;
        this._eyeSphere = null;
        this._tempVec3A = null;
        this._tempVec3B = null;
        this._tempVec3C = null;
        this._tempVec3D = null;
        this._tempQuat = null;
        this._tempQuatB = null;
        this._tempQuatC = null;
        this._tempQuatD = null;
        this._tempQuatE = null;
        this._tempQuatF = null;
        this._tempEuler = null;

        // ── 模型前方向符号（由 _detectModelForward() 动态检测） ──
        // VRM 1.0 → -1, VRM 0.x → +1
        this._modelForwardZ = 1;

        // ── 事件处理器引用 ──
        this._onPointerMove = null;

        // ── 初始化标志 ──
        this._initialized = false;

        // ── 性能档位 ──
        this._performanceLevel = 'high';
        this._perfRuntime = { ...CURSOR_FOLLOW_PERF_PRESETS.high };
        this._onPerfLevelChanged = null;

        // ── 局部跟踪 ──
        this._localTrackingEnabled = window.humanoidLocalTrackingEnabled === true;
        this._localTrackingMargin = 50; // 局部跟踪边界扩展（像素）
        this._isWithinLocalBounds = false; // 鼠标是否在局部跟踪范围内
        this._boundsAvailable = false; // 边界是否可用
    }

    // ════════════════════════════════════════════════════════════════
    //  初始化
    // ════════════════════════════════════════════════════════════════
    init(vrmManager) {
        if (!THREE) {
            console.warn('[CursorFollow] THREE.js 未加载，功能不可用');
            return;
        }
        this.manager = vrmManager;

        // 创建眼睛注视目标
        this.eyesTarget = new THREE.Object3D();
        this.eyesTarget.name = 'CursorFollowEyeTarget';
        if (vrmManager.scene) {
            vrmManager.scene.add(this.eyesTarget);
        }

        // 初始位置：头部前方
        const headPos = this._getHeadWorldPos();
        const camDir = new THREE.Vector3();
        if (vrmManager.camera) {
            camDir.subVectors(vrmManager.camera.position, headPos);
            if (camDir.lengthSq() < 1e-8) camDir.set(0, 0, 1);
            else camDir.normalize();
        } else {
            camDir.set(0, 0, 1);
        }
        this.eyesTarget.position.copy(headPos).addScaledVector(camDir, CURSOR_FOLLOW_DEFAULTS.lookAtDistance);

        // 预分配临时对象
        this._raycaster = new THREE.Raycaster();
        this._ndcVec = new THREE.Vector2();
        this._desiredTargetPos = this.eyesTarget.position.clone();
        this._headWorldPos = new THREE.Vector3();
        this._eyeSphere = new THREE.Sphere();
        this._tempVec3A = new THREE.Vector3();
        this._tempVec3B = new THREE.Vector3();
        this._tempVec3C = new THREE.Vector3();
        this._tempVec3D = new THREE.Vector3();
        this._tempQuat = new THREE.Quaternion();
        this._tempQuatB = new THREE.Quaternion();
        this._tempQuatC = new THREE.Quaternion();
        this._tempQuatD = new THREE.Quaternion();
        this._tempQuatE = new THREE.Quaternion();
        this._tempQuatF = new THREE.Quaternion();
        this._tempEuler = new THREE.Euler();

        // 骨骼基准姿态快照（防止 premultiply 累加漂移）
        this._neckBaseQuat = new THREE.Quaternion();
        this._headBaseQuat = new THREE.Quaternion();

        // 骨骼默认姿态（用于禁用跟踪时恢复）
        this._neckDefaultQuat = new THREE.Quaternion();
        this._headDefaultQuat = new THREE.Quaternion();
        this._hasNeckDefault = false;
        this._hasHeadDefault = false;

        // 初始化滤波器
        const D = CURSOR_FOLLOW_DEFAULTS;
        this._eyeFilterX = new OneEuroFilter(D.eyeOneEuroMinCutoff, D.eyeOneEuroBeta, D.eyeOneEuroDCutoff);
        this._eyeFilterY = new OneEuroFilter(D.eyeOneEuroMinCutoff, D.eyeOneEuroBeta, D.eyeOneEuroDCutoff);
        this._headFilterYaw = new OneEuroFilter(D.headOneEuroMinCutoff, D.headOneEuroBeta, D.headOneEuroDCutoff);
        this._headFilterPitch = new OneEuroFilter(D.headOneEuroMinCutoff, D.headOneEuroBeta, D.headOneEuroDCutoff);

        this._bindEvents();
        this._detectModelForward();

        // 支持外部通过 window 变量或事件切换追踪性能档
        const initialLevel = window.cursorFollowPerformanceLevel || 'high';
        this.setPerformanceLevel(initialLevel);
        this._bindPerformanceEvents();

        this._initialized = true;
        console.log('[CursorFollow] 初始化完成');
    }

    // ════════════════════════════════════════════════════════════════
    //  事件绑定（可清理）
    // ════════════════════════════════════════════════════════════════
    _bindEvents() {
        this._onPointerMove = (e) => {
            const now = performance.now();
            if (e.type === 'mousemove' && now < this._ignoreMouseMoveUntil) {
                return;
            }
            if (e.type === 'pointermove') {
                // macOS / Safari 等环境下，pointermove 后常跟随合成 mousemove，短窗口去重即可。
                this._ignoreMouseMoveUntil = now + 40;
            }
            // 坐标没变的重复事件（Electron Pet 的 preload 轮询在光标静止时也会转发）不算
            // 「光标在动」，否则空闲低频 governor 永远看到活动、降不下帧。
            const moved = e.clientX !== this._rawMouseX || e.clientY !== this._rawMouseY;
            this._rawMouseX = e.clientX;
            this._rawMouseY = e.clientY;
            this._hasPointerInput = true;
            if (moved) this._lastPointerMoveAt = now;
        };

        // 同时监听 pointermove + mousemove，绑定到 window（非 document）
        // Electron 透明窗口事件转发可能只产生 mousemove；window 级别确保转发事件可达
        window.addEventListener('pointermove', this._onPointerMove, { passive: true });
        window.addEventListener('mousemove', this._onPointerMove, { passive: true });
    }

    _bindPerformanceEvents() {
        this._onPerfLevelChanged = (event) => {
            const level = event?.detail?.level;
            if (level) this.setPerformanceLevel(level);
        };
        window.addEventListener('neko-cursor-follow-performance-changed', this._onPerfLevelChanged);
    }

    setPerformanceLevel(level) {
        const normalized = (typeof level === 'string' ? level.toLowerCase() : 'high');
        const preset = CURSOR_FOLLOW_PERF_PRESETS[normalized] || CURSOR_FOLLOW_PERF_PRESETS.high;

        this._performanceLevel = CURSOR_FOLLOW_PERF_PRESETS[normalized] ? normalized : 'high';
        this._perfRuntime = { ...preset };
        this._enabled = this._perfRuntime.enabled !== false && !this._userDisabled;

        if (!this._enabled) {
            // 性能档切为 none 时，跳过过渡立即完成禁用
            // 必须走 _completeDisable() 以恢复骨骼默认姿态，
            // 否则 head/neck 残留叠加旋转会导致头部冻结在偏转位置
            this._completeDisable();
        }
    }

    getPerformanceLevel() {
        return this._performanceLevel;
    }

    /**
     * 设置局部跟踪是否启用
     * @param {boolean} enabled - 是否启用局部跟踪
     */
    setLocalTrackingEnabled(enabled) {
        this._localTrackingEnabled = enabled;
        window.humanoidLocalTrackingEnabled = enabled;
        console.log(`[VRM CursorFollow] 局部跟踪已${enabled ? '开启' : '关闭'}`);
    }

    /**
     * 获取局部跟踪是否启用
     * @returns {boolean}
     */
    isLocalTrackingEnabled() {
        return this._localTrackingEnabled === true;
    }

    /**
     * 从UI设置面板应用配置
     * @param {Object} config - { eyeMaxAngle, headMaxAngle, smoothSpeed, enabled }
     */
    applyConfig(config) {
        if (!config) return;
        if (config.enabled === true) {
            this.setEnabled(true);
        } else if (config.enabled === false) {
            this.setEnabled(false);
        }
        if (config.eyeMaxAngle != null) {
            this.eyeMaxYawDeg = config.eyeMaxAngle;
            this.eyeMaxPitchUpDeg = config.eyeMaxAngle;
            this.eyeMaxPitchDownDeg = Math.round(config.eyeMaxAngle * 0.87);
        }
        if (config.headMaxAngle != null) {
            this.headMaxYawDeg = config.headMaxAngle;
            this.headMaxPitchUpDeg = Math.round(config.headMaxAngle * 0.67);
            this.headMaxPitchDownDeg = Math.round(config.headMaxAngle * 0.56);
        }
        if (config.smoothSpeed != null) {
            this.headSmoothSpeed = config.smoothSpeed;
        }
    }

    _getCanvasRect(canvas) {
        const now = performance.now();
        if (!this._lastCanvasRect || (now - this._lastCanvasRectReadAt) > 120) {
            this._lastCanvasRect = canvas.getBoundingClientRect();
            this._lastCanvasRectReadAt = now;
        }
        return this._lastCanvasRect;
    }

    // ════════════════════════════════════════════════════════════════
    //  辅助：检测模型实际前方向
    //  基于 VRM 模型版本（由 vrm-core.js detectVRMVersion 从 GLTF
    //  extensionsUsed / meta 属性检测），不依赖 scene 世界旋转：
    //    VRM 1.0 → three-vrm 内部对 scene 做了 180° Y 翻转，forwardSign = -1
    //    VRM 0.x → forwardSign = +1
    // ════════════════════════════════════════════════════════════════
    _detectModelForward() {
        const vrmVersion = this.manager?.core?.vrmVersion;
        // VRM 1.0: three-vrm 内部已翻转 scene，forwardSign = -1
        // VRM 0.x: forwardSign = +1
        this._modelForwardZ = (vrmVersion === '1.0') ? -1 : 1;
        console.log(`[CursorFollow] 模型前方向检测: vrmVersion=${vrmVersion || 'unknown'}, forwardSign=${this._modelForwardZ}`);
    }

    // ════════════════════════════════════════════════════════════════
    //  辅助：获取头部世界坐标
    // ════════════════════════════════════════════════════════════════
    _getHeadWorldPos() {
        const vrm = this.manager?.currentModel?.vrm;
        if (vrm?.humanoid) {
            const headBone = vrm.humanoid.getRawBoneNode('head');
            if (headBone) {
                headBone.getWorldPosition(this._headWorldPos || (this._headWorldPos = new THREE.Vector3()));
                return this._headWorldPos;
            }
        }
        // 回退：使用 scene 位置 + 偏移
        if (vrm?.scene) {
            vrm.scene.getWorldPosition(this._headWorldPos || (this._headWorldPos = new THREE.Vector3()));
            this._headWorldPos.y += 1.4;
            return this._headWorldPos;
        }
        if (!this._headWorldPos) this._headWorldPos = new THREE.Vector3();
        this._headWorldPos.set(0, 1.4, 0);
        return this._headWorldPos;
    }

    // ════════════════════════════════════════════════════════════════
    //  判断当前是否处于"一次性动作播放中"（用于降权）
    //  待机动画不算"动作"，头部跟踪以较高权重加成叠加
    // ════════════════════════════════════════════════════════════════
    _isActionPlaying() {
        const anim = this.manager?.animation;
        if (!anim) return false;
        // 待机动画不降权
        if (anim.isIdleAnimation) return false;
        // 仅非 idle 的一次性动作才降权
        return anim.vrmaIsPlaying && anim.currentAction && anim.currentAction.isRunning();
    }

    // ════════════════════════════════════════════════════════════════
    //  判断当前是否处于"待机动画播放中"
    // ════════════════════════════════════════════════════════════════
    _isIdleAnimPlaying() {
        const anim = this.manager?.animation;
        if (!anim) return false;
        return anim.isIdleAnimation && anim.vrmaIsPlaying && anim.currentAction && anim.currentAction.isRunning();
    }

    // ════════════════════════════════════════════════════════════════
    //  判断是否正在拖拽/orbit
    // ════════════════════════════════════════════════════════════════
    _isDragging() {
        if (!CURSOR_FOLLOW_DEFAULTS.reduceWhileDragging) return false;
        return this.manager?.interaction?.isDragging === true;
    }

    // ════════════════════════════════════════════════════════════════
    //  updateTarget(delta) — 每帧更新眼睛目标位置
    //  调用时机：在 mixer.update 之前
    // ════════════════════════════════════════════════════════════════
    updateTarget(delta) {
        if (!this._initialized || !this.eyesTarget || !this.manager) return;
        if (!this._enabled && !this._disabling) return;
        // 首次 pointermove 前跳过，避免未知鼠标坐标导致首帧朝向异常
        if (!this._hasPointerInput) return;

        // ── 回正过渡：仅执行眼球阻尼衰减 + eyesTarget 重建，不再求解新目标 ──
        if (this._disabling) {
            this._elapsedTime += delta;
            const D = CURSOR_FOLLOW_DEFAULTS;
            const camera = this.manager.camera;
            if (!camera) return;

            const eyeAlpha = 1 - Math.exp(-delta * D.eyeSmoothSpeed);
            this._eyeYaw += (0 - this._eyeYaw) * eyeAlpha;
            this._eyePitch += (0 - this._eyePitch) * eyeAlpha;

            // 重建 eyesTarget 位置（与正常路径 line 545-556 相同逻辑）
            const headPosForEye = this._getHeadWorldPos();
            const baseRightForEye = this._tempVec3A.set(1, 0, 0).applyQuaternion(camera.quaternion).normalize();
            const baseUpForEye = this._tempVec3D.set(0, 1, 0).applyQuaternion(camera.quaternion).normalize();
            const baseForwardForEye = this._tempVec3C.set(0, 0, -1).applyQuaternion(camera.quaternion).normalize().negate();
            const cosPitch = Math.cos(this._eyePitch);

            this._desiredTargetPos
                .copy(headPosForEye)
                .addScaledVector(baseRightForEye, Math.sin(this._eyeYaw) * cosPitch * D.lookAtDistance)
                .addScaledVector(baseUpForEye, Math.sin(this._eyePitch) * D.lookAtDistance)
                .addScaledVector(baseForwardForEye, Math.cos(this._eyeYaw) * cosPitch * D.lookAtDistance);
            this.eyesTarget.position.copy(this._desiredTargetPos);
            return;
        }

        this._elapsedTime += delta;

        const D = CURSOR_FOLLOW_DEFAULTS;
        const camera = this.manager.camera;
        const canvas = this.manager.renderer?.domElement;
        if (!camera || !canvas) return;

        const now = performance.now();
        const perf = this._perfRuntime;
        const pointerIdle = (now - this._lastPointerMoveAt) >= perf.pointerIdleMs;
        const targetSolveIntervalMs = pointerIdle ? perf.idleTargetSolveIntervalMs : perf.activeTargetSolveIntervalMs;
        const shouldSolveByMovement = !perf.solveTargetOnMoveOnly || !pointerIdle;

        // ③ 屏幕坐标 → NDC
        const rect = this._getCanvasRect(canvas);
        if (!rect.width || !rect.height) return;

        let isWithinLocalBounds = false;
        let localNdcX = null;
        let localNdcY = null;
        let boundsAvailable = false;

        // 局部跟踪：只在鼠标在模型边界范围内时跟随
        if (this._localTrackingEnabled && this.manager) {
            const bounds = this.manager.getModelScreenBounds();
            if (bounds) {
                boundsAvailable = true;
                const margin = this._localTrackingMargin;
                const clampedLeft = bounds.left - margin;
                const clampedRight = bounds.right + margin;
                const clampedTop = bounds.top - margin;
                const clampedBottom = bounds.bottom + margin;

                // 检查鼠标是否在边界范围内
                isWithinLocalBounds = this._rawMouseX >= clampedLeft &&
                                      this._rawMouseX <= clampedRight &&
                                      this._rawMouseY >= clampedTop &&
                                      this._rawMouseY <= clampedBottom;

                if (isWithinLocalBounds) {
                    localNdcX = ((this._rawMouseX - rect.left) / rect.width) * 2 - 1;
                    localNdcY = -((this._rawMouseY - rect.top) / rect.height) * 2 + 1;
                }
            }
        }

        // 保存局部跟踪状态供 applyHead 使用
        // 只有在 bounds 可用时才更新 _isWithinLocalBounds，避免 bounds 不可用时错误地阻止求解
        this._boundsAvailable = boundsAvailable;
        if (boundsAvailable) {
            this._isWithinLocalBounds = isWithinLocalBounds;
        }

        // 局部跟踪时，只有在 bounds 可用且鼠标在边界外才跳过目标更新
        // 如果 bounds 不可用，视为"不可判定"并允许全局跟踪
        // 但仍然执行平滑插值，让眼睛保持当前朝向而不是回正
        const shouldSolveTargetInLocalMode = shouldSolveByMovement && (!this._localTrackingEnabled || !boundsAvailable || isWithinLocalBounds);

        // 如果未启用局部跟踪，或 bounds 不可用，或鼠标在边界外，使用原始坐标
        const rawNdcX = localNdcX !== null ? localNdcX : ((this._rawMouseX - rect.left) / rect.width) * 2 - 1;
        const rawNdcY = localNdcY !== null ? localNdcY : -((this._rawMouseY - rect.top) / rect.height) * 2 + 1;

        // ④ One-Euro 滤波 NDC
        const filteredX = this._eyeFilterX.filter(rawNdcX, this._elapsedTime);
        const filteredY = this._eyeFilterY.filter(rawNdcY, this._elapsedTime);

        const shouldSolveTarget = shouldSolveTargetInLocalMode && (now - this._lastTargetSolveAt) >= targetSolveIntervalMs;
        if (shouldSolveTarget) {
            // ② 获取头部世界坐标（仅在需要求解时执行）
            const headPos = this._getHeadWorldPos();

            // ⑤ 先假定鼠标在“头部球面”上：优先用射线-球面求交得到鼠标点
            this._ndcVec.set(filteredX, filteredY);
            this._raycaster.setFromCamera(this._ndcVec, camera);

            this._eyeSphere.center.copy(headPos);
            this._eyeSphere.radius = D.lookAtDistance;
            const ray = this._raycaster.ray;
            const oc = this._tempVec3C.subVectors(ray.origin, headPos);
            const b = oc.dot(ray.direction);
            const c = oc.lengthSq() - (D.lookAtDistance * D.lookAtDistance);
            const h = b * b - c;
            if (h >= 0) {
                const s = Math.sqrt(h);
                const tNear = -b - s;
                const tFar = -b + s;
                // 选远交点（背侧）避免视线角度被“近侧交点”压缩
                let t = tFar;
                if (t < 0 && tNear >= 0) t = tNear;
                if (t >= 0) {
                    this._tempVec3A.copy(ray.origin).addScaledVector(ray.direction, t);
                } else {
                    ray.closestPointToPoint(headPos, this._tempVec3A);
                }
            } else {
                // 回退：若射线未命中球面，再退化到最近点策略，避免边缘失效
                ray.closestPointToPoint(headPos, this._tempVec3A);
            }

            const dirWorld = this._tempVec3B.subVectors(this._tempVec3A, headPos);
            if (dirWorld.lengthSq() < 1e-8) {
                dirWorld.subVectors(camera.position, headPos);
            }
            if (dirWorld.lengthSq() >= 1e-8) {
                dirWorld.normalize();
                // 使用相机坐标轴作为基准，保证左右/上下与屏幕方向一致
                const baseRight = this._tempVec3A.set(1, 0, 0).applyQuaternion(camera.quaternion).normalize();
                const baseUp = this._tempVec3D.set(0, 1, 0).applyQuaternion(camera.quaternion).normalize();
                // camera forward 指向屏幕内；眼睛基准前方向应朝向相机，所以取反
                const baseForward = this._tempVec3C.set(0, 0, -1).applyQuaternion(camera.quaternion).normalize().negate();

                const dx = dirWorld.dot(baseRight);
                const dy = dirWorld.dot(baseUp);
                const dz = dirWorld.dot(baseForward);

                const rawYaw = Math.atan2(dx, dz);
                const horizLen = Math.sqrt(dx * dx + dz * dz);
                // 屏幕坐标与当前基准存在上下方向差异，这里取反以匹配鼠标直觉
                const rawPitch = Math.atan2(-dy, Math.max(horizLen, 1e-8));

                const maxYaw = this.eyeMaxYawDeg * (Math.PI / 180);
                const maxPitchUp = this.eyeMaxPitchUpDeg * (Math.PI / 180);
                const maxPitchDown = this.eyeMaxPitchDownDeg * (Math.PI / 180);
                const clampedYaw = THREE.MathUtils.clamp(rawYaw, -maxYaw, maxYaw);
                const clampedPitch = THREE.MathUtils.clamp(rawPitch, -maxPitchDown, maxPitchUp);
                const eyeCenterDeadzoneRad = D.eyeCenterDeadzoneDeg * (Math.PI / 180);
                const stableYaw = Math.abs(clampedYaw) < eyeCenterDeadzoneRad ? 0 : clampedYaw;

                // 低频只更新眼睛"目标角度"，每帧用阻尼插值到当前角度，避免瞬移
                this._targetEyeYaw = stableYaw;
                this._targetEyePitch = clampedPitch;
            }
            this._lastTargetSolveAt = now;
        }

        // ⑦ 每帧角度平滑，再重建目标点（连续过渡，不会阶梯跳变）
        const eyeAlpha = 1 - Math.exp(-delta * D.eyeSmoothSpeed);
        this._eyeYaw += (this._targetEyeYaw - this._eyeYaw) * eyeAlpha;
        this._eyePitch += (this._targetEyePitch - this._eyePitch) * eyeAlpha;

        const headPosForEye = this._getHeadWorldPos();
        const baseRightForEye = this._tempVec3A.set(1, 0, 0).applyQuaternion(camera.quaternion).normalize();
        const baseUpForEye = this._tempVec3D.set(0, 1, 0).applyQuaternion(camera.quaternion).normalize();
        const baseForwardForEye = this._tempVec3C.set(0, 0, -1).applyQuaternion(camera.quaternion).normalize().negate();
        const cosPitch = Math.cos(this._eyePitch);

        this._desiredTargetPos
            .copy(headPosForEye)
            .addScaledVector(baseRightForEye, Math.sin(this._eyeYaw) * cosPitch * D.lookAtDistance)
            .addScaledVector(baseUpForEye, Math.sin(this._eyePitch) * D.lookAtDistance)
            .addScaledVector(baseForwardForEye, Math.cos(this._eyeYaw) * cosPitch * D.lookAtDistance);
        this.eyesTarget.position.copy(this._desiredTargetPos);
    }

    // ════════════════════════════════════════════════════════════════
    //  applyHead(delta) — 每帧应用头/颈加成旋转
    //  调用时机：在 vrm.update(delta) 之后
    // ════════════════════════════════════════════════════════════════
    applyHead(delta) {
        if (!this._initialized || !this.manager) return;
        if (!this._enabled && !this._disabling) return;

        const vrm = this.manager?.currentModel?.vrm;
        if (!vrm?.humanoid) return;

        const D = CURSOR_FOLLOW_DEFAULTS;

        // ── 更新权重 ──
        this._updateHeadWeight(delta);
        // 回正过渡中：权重收敛到 0 时完成过渡
        if (this._disabling && this._headWeight < 0.001) {
            this._completeDisable();
            return;
        }
        if (this._headWeight < 0.001) return;

        const now = performance.now();
        const perf = this._perfRuntime;
        const pointerIdle = (now - this._lastPointerMoveAt) >= perf.pointerIdleMs;
        const headSolveIntervalMs = pointerIdle ? perf.idleHeadSolveIntervalMs : perf.activeHeadSolveIntervalMs;
        const shouldSolveByMovement = !perf.solveHeadOnMoveOnly || !pointerIdle;
        const shouldSolveHead = !this._disabling && shouldSolveByMovement && (now - this._lastHeadSolveAt) >= headSolveIntervalMs;

        // 即使低档静止，也不能提前 return，否则会出现“偶发复位感”。
        // 这里只控制是否重算目标角，阻尼和骨骼加成仍每帧应用。

        // ── 获取骨骼 ──
        const useNeckBone = D.headBoneMode === 'neckAndHead';
        const neckBone = useNeckBone ? vrm.humanoid.getRawBoneNode('neck') : null;
        const headBone = vrm.humanoid.getRawBoneNode('head');
        if (!neckBone && !headBone) return; // 降级：仅眼睛

        // ── 快照骨骼基准姿态（vrm.update 后的动画姿态） ──
        // 每帧从快照恢复后再叠加，避免 premultiply 累加漂移
        if (neckBone) this._neckBaseQuat.copy(neckBone.quaternion);
        if (headBone) this._headBaseQuat.copy(headBone.quaternion);

        // sceneQuat 每帧都需要用于应用旋转
        vrm.scene.getWorldQuaternion(this._tempQuat);

        // 低频求解目标角度，高频插值应用，避免阶梯感抽动
        // 局部跟踪时，只有在 bounds 可用且鼠标在边界外才跳过求解
        // 如果 bounds 不可用，视为"不可判定"并允许全局跟踪
        const shouldSolveHeadInLocalMode = shouldSolveHead && (!this._localTrackingEnabled || !this._boundsAvailable || this._isWithinLocalBounds);
        if (shouldSolveHeadInLocalMode) {
            // ── 参考位置 ──
            const refBone = headBone || neckBone;
            refBone.getWorldPosition(this._headWorldPos);

            // ── 目标方向（世界空间） ──
            const targetPos = this.eyesTarget.position;
            const dirWorld = this._tempVec3A.subVectors(targetPos, this._headWorldPos);

            if (dirWorld.lengthSq() >= 0.001) {
                dirWorld.normalize();

                // modelForward / modelUp / modelRight
                // 使用 _modelForwardZ 适配 VRM 0.x(+Z) 和 1.0(-Z) 的前方向差异
                const modelForward = this._tempVec3B.set(0, 0, this._modelForwardZ).applyQuaternion(this._tempQuat);
                const modelUp = this._tempVec3C.set(0, 1, 0).applyQuaternion(this._tempQuat);
                const modelRight = this._tempVec3D.crossVectors(modelUp, modelForward).normalize();

                // ── 分解方向到模型坐标系 ──
                const dx = dirWorld.dot(modelRight);
                const dy = dirWorld.dot(modelUp);
                const dz = dirWorld.dot(modelForward);

                // ── 原始 yaw / pitch ──
                const rawYaw = Math.atan2(-dx, Math.max(dz, 0.001));
                const horizLen = Math.sqrt(dx * dx + dz * dz);
                const rawPitch = Math.atan2(dy, Math.max(horizLen, 0.001));

                // ── One-Euro 滤波 ──
                const filteredYaw = this._headFilterYaw.filter(rawYaw, this._elapsedTime);
                const filteredPitch = this._headFilterPitch.filter(rawPitch, this._elapsedTime);

                // ── Clamp ──
                const maxYaw = this.headMaxYawDeg * (Math.PI / 180);
                const maxPitchUp = this.headMaxPitchUpDeg * (Math.PI / 180);
                const maxPitchDown = this.headMaxPitchDownDeg * (Math.PI / 180);

                const clampedYaw = THREE.MathUtils.clamp(filteredYaw, -maxYaw, maxYaw);
                const clampedPitch = THREE.MathUtils.clamp(filteredPitch, -maxPitchDown, maxPitchUp);

                // 死区：抑制小幅抖动
                const deadzoneRad = D.deadzoneDeg * (Math.PI / 180);
                if (Math.abs(clampedYaw - this._targetHeadYaw) >= deadzoneRad) {
                    this._targetHeadYaw = clampedYaw;
                }
                if (Math.abs(clampedPitch - this._targetHeadPitch) >= deadzoneRad) {
                    this._targetHeadPitch = clampedPitch;
                }
            }
            this._lastHeadSolveAt = now;
        }

        // 局部跟踪时，如果鼠标不在边界范围内，跳过头部更新（保持当前朝向）

        // ── 指数阻尼平滑（每帧） ──
        const headAlpha = 1 - Math.exp(-delta * this.headSmoothSpeed);
        this._headYaw += (this._targetHeadYaw - this._headYaw) * headAlpha;
        this._headPitch += (this._targetHeadPitch - this._headPitch) * headAlpha;

        // sceneQuat 始终指向 this._tempQuat（无论是否进入 if 分支都已赋值）
        const sceneQuat = this._tempQuat;
        const sceneQuatInv = this._tempQuatF.copy(sceneQuat).invert();

        const w = this._headWeight;
        const neckYaw = this._headYaw * D.neckContribution * w;
        const neckPitch = this._headPitch * D.neckContribution * w;
        const headYaw = this._headYaw * D.headContribution * w;
        const headPitch = this._headPitch * D.headContribution * w;

        // 旋转量极小则跳过本帧加成计算（保持当前视觉，减少四元数运算）
        if (
            Math.abs(neckYaw) < 1e-6 &&
            Math.abs(neckPitch) < 1e-6 &&
            Math.abs(headYaw) < 1e-6 &&
            Math.abs(headPitch) < 1e-6
        ) {
            return;
        }

        // ── 对 neck 应用加成旋转 ──
        if (neckBone) {
            neckBone.quaternion.copy(this._neckBaseQuat); // 恢复基准姿态
            this._applyAdditiveRotation(
                neckBone, sceneQuat, sceneQuatInv,
                neckYaw, neckPitch
            );
        }

        // ── 对 head 应用加成旋转 ──
        if (headBone) {
            headBone.quaternion.copy(this._headBaseQuat); // 恢复基准姿态
            this._applyAdditiveRotation(
                headBone, sceneQuat, sceneQuatInv,
                headYaw, headPitch
            );
        }
    }

    // ════════════════════════════════════════════════════════════════
    //  核心：将 yaw/pitch 转换为骨骼本地空间偏移并 premultiply
    // ════════════════════════════════════════════════════════════════
    _applyAdditiveRotation(bone, sceneWorldQuat, sceneWorldQuatInv, yaw, pitch) {
        if (Math.abs(yaw) < 1e-6 && Math.abs(pitch) < 1e-6) return;

        // 构造模型空间偏移四元数（先 yaw 后 pitch → YXZ 顺序）
        this._tempEuler.set(pitch, yaw, 0, 'YXZ');
        const modelOffset = this._tempQuatB.setFromEuler(this._tempEuler);

        // 模型空间 → 世界空间
        //   worldOffset = sceneQuat * modelOffset * sceneQuat^-1
        const worldOffset = this._tempQuatD
            .copy(sceneWorldQuat)
            .multiply(modelOffset)
            .multiply(sceneWorldQuatInv);

        // 世界空间 → 骨骼父级本地空间
        //   localOffset = parentWorldQuat^-1 * worldOffset * parentWorldQuat
        const parentQuat = this._tempQuatE;
        if (bone.parent) {
            bone.parent.getWorldQuaternion(parentQuat);
        } else {
            parentQuat.identity();
        }
        // 计算: parentQuat^-1 * worldOffset * parentQuat
        // 注意：不能 in-place invert parentQuat，因为后面还要用
        const parentQuatInv = this._tempQuatC.copy(parentQuat).invert();
        const localOffset = parentQuatInv.multiply(worldOffset).multiply(parentQuat);

        // 加成旋转（premultiply = 在父空间叠加）
        bone.quaternion.premultiply(localOffset);
    }

    // ════════════════════════════════════════════════════════════════
    //  动画/拖拽感知权重
    // ════════════════════════════════════════════════════════════════
    _updateHeadWeight(delta) {
        const D = CURSOR_FOLLOW_DEFAULTS;

        // 回正过渡中：强制目标权重为 0，不受动画/拖拽状态覆盖
        if (this._disabling) {
            this._targetHeadWeight = 0;
        } else if (this._isActionPlaying()) {
            this._targetHeadWeight = D.headWeightAction;       // 一次性动作 → 0
        } else if (this._isDragging()) {
            this._targetHeadWeight = 0.15;
        } else if (this._isIdleAnimPlaying()) {
            this._targetHeadWeight = D.headWeightIdleAnim;     // 待机动画 → 0.7（加成叠加）
        } else {
            // 无动画运行时（T-pose / 静止态），不驱动头部跟踪，
            // 避免身体静止但头部跟随鼠标转动的不自然效果
            this._targetHeadWeight = 0;
        }

        // 平滑过渡
        const speed = 1.0 / Math.max(0.01, D.weightTransitionSec);
        const alpha = 1 - Math.exp(-delta * speed);
        this._headWeight += (this._targetHeadWeight - this._headWeight) * alpha;
    }

    // ════════════════════════════════════════════════════════════════
    //  重置（模型切换时调用）
    // ════════════════════════════════════════════════════════════════
    reset() {
        // 取消回正过渡状态，根据用户偏好恢复启用状态
        this._disabling = false;
        this._enabled = this._perfRuntime.enabled !== false && !this._userDisabled;
        this._headYaw = 0;
        this._headPitch = 0;
        this._eyeYaw = 0;
        this._eyePitch = 0;
        this._targetEyeYaw = 0;
        this._targetEyePitch = 0;
        this._targetHeadYaw = 0;
        this._targetHeadPitch = 0;
        this._headWeight = 1.0;
        this._targetHeadWeight = 1.0;
        this._elapsedTime = 0;
        this._lastPointerMoveAt = 0;
        this._lastTargetSolveAt = 0;
        this._lastHeadSolveAt = 0;

        if (this._eyeFilterX) this._eyeFilterX.reset();
        if (this._eyeFilterY) this._eyeFilterY.reset();
        if (this._headFilterYaw) this._headFilterYaw.reset();
        if (this._headFilterPitch) this._headFilterPitch.reset();

        // 重新检测新模型的前方向
        this._detectModelForward();

        // 重置眼睛目标到头部前方
        if (this.eyesTarget && this.manager?.camera) {
            const headPos = this._getHeadWorldPos();
            const camDir = new THREE.Vector3();
            camDir.subVectors(this.manager.camera.position, headPos);
            if (camDir.lengthSq() < 1e-8) camDir.set(0, 0, 1);
            else camDir.normalize();
            this.eyesTarget.position.copy(headPos).addScaledVector(camDir, CURSOR_FOLLOW_DEFAULTS.lookAtDistance);
            this._desiredTargetPos.copy(this.eyesTarget.position);
        }
    }

    // ════════════════════════════════════════════════════════════════
    //  启用/禁用鼠标跟踪
    // ════════════════════════════════════════════════════════════════
    setEnabled(enabled) {
        this._userDisabled = !enabled;
        if (!enabled && this._enabled && !this._disabling) {
            // 进入"回正"过渡状态，不立即禁用
            this._disabling = true;
            // 将所有目标角度清零 → 已有的阻尼插值会自动平滑回正
            this._targetHeadYaw = 0;
            this._targetHeadPitch = 0;
            this._targetEyeYaw = 0;
            this._targetEyePitch = 0;
            this._targetHeadWeight = 0;
            console.log('[CursorFollow] 鼠标跟踪开始回正过渡');
        } else if (enabled) {
            this._disabling = false;
            this._enabled = this._perfRuntime.enabled !== false && !this._userDisabled;
            console.log('[CursorFollow] 鼠标跟踪已启用');
        }
    }

    isEnabled() {
        return this._enabled;
    }

    // ════════════════════════════════════════════════════════════════
    //  回正过渡完成：真正禁用鼠标跟踪
    // ════════════════════════════════════════════════════════════════
    _completeDisable() {
        this._disabling = false;
        this._enabled = false;
        this.reset();
        this._restoreBonesToDefault();
        console.log('[CursorFollow] 回正过渡完成，鼠标跟踪已禁用');
    }

    // ════════════════════════════════════════════════════════════════
    //  恢复骨骼到默认姿态
    // ════════════════════════════════════════════════════════════════
    _restoreBonesToDefault() {
        const vrm = this.manager?.currentModel?.vrm;
        if (!vrm?.humanoid) return;

        const neckBone = vrm.humanoid.getRawBoneNode('neck');
        const headBone = vrm.humanoid.getRawBoneNode('head');

        // 恢复到保存的默认姿态（仅在有效快照存在时）
        if (neckBone && this._neckDefaultQuat && this._hasNeckDefault) {
            neckBone.quaternion.copy(this._neckDefaultQuat);
        }
        if (headBone && this._headDefaultQuat && this._hasHeadDefault) {
            headBone.quaternion.copy(this._headDefaultQuat);
        }
    }

    // ════════════════════════════════════════════════════════════════
    //  保存骨骼默认姿态（在动画更新后调用）
    // ════════════════════════════════════════════════════════════════
    _saveBonesDefaultQuat() {
        const vrm = this.manager?.currentModel?.vrm;
        if (!vrm?.humanoid) return;

        const neckBone = vrm.humanoid.getRawBoneNode('neck');
        const headBone = vrm.humanoid.getRawBoneNode('head');

        if (neckBone && this._neckDefaultQuat) {
            this._neckDefaultQuat.copy(neckBone.quaternion);
            this._hasNeckDefault = true;
        }
        if (headBone && this._headDefaultQuat) {
            this._headDefaultQuat.copy(headBone.quaternion);
            this._hasHeadDefault = true;
        }
    }

    // ════════════════════════════════════════════════════════════════
    //  销毁
    // ════════════════════════════════════════════════════════════════
    destroy() {
        // 移除事件监听（与 _bindEvents 对称）
        if (this._onPointerMove) {
            window.removeEventListener('pointermove', this._onPointerMove);
            window.removeEventListener('mousemove', this._onPointerMove);
            this._onPointerMove = null;
        }
        if (this._onPerfLevelChanged) {
            window.removeEventListener('neko-cursor-follow-performance-changed', this._onPerfLevelChanged);
            this._onPerfLevelChanged = null;
        }

        // 从场景移除目标对象
        if (this.eyesTarget?.parent) {
            this.eyesTarget.parent.remove(this.eyesTarget);
        }
        this.eyesTarget = null;

        // 清理预分配的 THREE.js 对象
        this._raycaster = null;
        this._ndcVec = null;
        this._desiredTargetPos = null;
        this._lastCanvasRect = null;
        this._headWorldPos = null;
        this._eyeSphere = null;
        this._tempVec3A = null;
        this._tempVec3B = null;
        this._tempVec3C = null;
        this._tempVec3D = null;
        this._tempQuat = null;
        this._tempQuatB = null;
        this._tempQuatC = null;
        this._tempQuatD = null;
        this._tempQuatE = null;
        this._tempQuatF = null;
        this._tempEuler = null;
        this._neckBaseQuat = null;
        this._headBaseQuat = null;

        // 清理 One-Euro 滤波器实例
        this._eyeFilterX = null;
        this._eyeFilterY = null;
        this._headFilterYaw = null;
        this._headFilterPitch = null;

        this._initialized = false;
        this._hasPointerInput = false;
        this.manager = null;

        console.log('[CursorFollow] 已销毁');
    }
}

// ─── 全局导出 ───────────────────────────────────────────────────────
window.CursorFollowController = CursorFollowController;
