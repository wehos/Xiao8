/**
 * Live2D Core - 核心类结构和基础功能
 * 功能包括:
 * - PIXI 应用初始化和管理
 * - Live2D 模型加载和管理
 * - 表情映射和转换
 * - 动作和表情控制
 * - 模型偏好设置
 * - 模型偏好验证
 * - 口型同步参数列表
 * - 全局状态管理（如锁定状态、按钮状态等）
 * - 事件监听（如帧率变更、画质变更等）
 * - 触摸事件处理（如点击、拖动等）
 */

window.PIXI = PIXI;
const { Live2DModel } = PIXI.live2d;

// 全局变量
let currentModel = null;
let emotionMapping = null;
let currentEmotion = 'neutral';
let pixi_app = null;
let isInitialized = false;

let motionTimer = null; // 动作持续时间定时器
let isEmotionChanging = false; // 防止快速连续点击的标志

// 全局：判断是否为移动端宽度
// Electron Pet 窗口永不进入手机模式（本文件早于 common_ui.js 加载，故用 flag 内联判断）。
const isMobileWidth = () => !window.__LANLAN_IS_ELECTRON_PET__ && window.innerWidth <= 768;

// 口型同步参数列表常量
// 这些参数用于控制模型的嘴部动作，在处理表情和常驻表情时需要跳过，以避免覆盖实时的口型同步
window.LIPSYNC_PARAMS = [
    'ParamMouthOpenY',
    'ParamMouthForm',
    'ParamMouthOpen',
    'ParamA',
    'ParamI',
    'ParamU',
    'ParamE',
    'ParamO'
];

// 模型偏好验证常量
const MODEL_PREFERENCES = {
    SCALE_MIN: 0.005,
    SCALE_MAX: 10,
    POSITION_MAX: 100000
};

const LIVE2D_BUBBLE_GEOMETRY_OVERRIDES = Object.freeze({});
// 模型刚加载完成时，物理/动作还可能在收敛，首帧头框容易偏离。
// 在稳定窗口后允许缓存自动刷新一次，避免“早期误识别被长期锁死”。
const LIVE2D_BUBBLE_GEOMETRY_SETTLE_REFRESH_MS = 1800;
const LIVE2D_LINUX_X11_DEFAULT_QUALITY = 'low';
// 自适应帧率：无动作/说话/交互时降到地板省 CPU，活动时升回配置帧率（默认 60，全平台生效）。
const LIVE2D_IDLE_FPS = 30;                         // 静止地板帧率
const LIVE2D_INTERACTIVE_FPS_HOLD_MS = 900;         // 活动后维持满帧的窗口，过后衰减回地板
const LIVE2D_IDLE_FPS_GOVERNOR_INTERVAL_MS = 300;   // 活动探测轮询间隔
const LIVE2D_RETURN_BALL_VIEWPORT_MAX_SIZE = 200;

function isDesktopLinuxX11Runtime() {
    return !!(window.__NEKO_DESKTOP_RUNTIME__ && window.__NEKO_DESKTOP_RUNTIME__.isLinuxX11);
}

function getEffectiveLive2DRenderQuality(quality) {
    if (quality) return quality;
    return isDesktopLinuxX11Runtime() ? LIVE2D_LINUX_X11_DEFAULT_QUALITY : 'medium';
}

function getLive2DNiriPetVirtualViewportSize() {
    try {
        const api = window.__nekoNiriPetPhysicalCrop;
        if (!api || typeof api.getState !== 'function') return null;
        const state = api.getState();
        const virtualBounds = state && state.enabled === true ? state.virtualBounds : null;
        const width = Number(virtualBounds && virtualBounds.width);
        const height = Number(virtualBounds && virtualBounds.height);
        if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
            return null;
        }
        return { width, height };
    } catch (_) {
        return null;
    }
}

// 验证模型偏好是否有效
function isValidModelPreferences(scale, position) {
    if (!scale || !position) return false;
    const scaleX = scale.x;
    const scaleY = scale.y;
    const posX = position.x;
    const posY = position.y;
    const isValidScale = Number.isFinite(scaleX) && scaleX >= MODEL_PREFERENCES.SCALE_MIN && scaleX < MODEL_PREFERENCES.SCALE_MAX &&
                        Number.isFinite(scaleY) && scaleY >= MODEL_PREFERENCES.SCALE_MIN && scaleY < MODEL_PREFERENCES.SCALE_MAX;
    const isValidPosition = Number.isFinite(posX) && Number.isFinite(posY) &&
                           Math.abs(posX) < MODEL_PREFERENCES.POSITION_MAX && Math.abs(posY) < MODEL_PREFERENCES.POSITION_MAX;
    return isValidScale && isValidPosition;
}

function isLive2DReturnBallViewportSize(width, height) {
    if (!window.__LANLAN_IS_ELECTRON_PET__) return false;
    const w = Number(width);
    const h = Number(height);
    return Number.isFinite(w) && Number.isFinite(h) &&
        w > 0 && h > 0 &&
        w <= LIVE2D_RETURN_BALL_VIEWPORT_MAX_SIZE &&
        h <= LIVE2D_RETURN_BALL_VIEWPORT_MAX_SIZE;
}

// Live2D 管理器类
class Live2DManager {
    constructor() {
        this.currentModel = null;
        this.emotionMapping = null; // { motions: {emotion: [string]}, expressions: {emotion: [string]} }
        this.fileReferences = null; // 保存原始 FileReferences（含 Motions/Expressions）
        this.currentEmotion = 'neutral';
        this.currentExpressionFile = null; // 当前使用的表情文件（用于精确比较）
        this.pixi_app = null;
        this.isInitialized = false;
        this.motionTimer = null;
        this._motionTimerGeneration = 0;
        this._actionMotionRequestPendingModel = null;
        this._simpleMotionActive = false;
        this._transientExpressionGeneration = 0;
        this._transientExpressionTask = null;
        this._activeTransientExpression = false;
        this._touchSetExpressionRestoreGeneration = 0;
        this._touchSetExpressionRestoreState = null;
        this.isEmotionChanging = false;
        this.dragEnabled = false;
        this.isFocusing = false;
        this.isLocked = false;
        this.onModelLoaded = null;
        this.onStatusUpdate = null;
        this.touchSetFilter = {};       // 点击/触摸滤波计数器（touch-config.js 会覆盖）
        this.touchSetHitEventLock = false; // hit 事件锁（touch-config.js 会覆盖）
        this.modelName = null; // 记录当前模型目录名
        this.modelRootPath = null; // 记录当前模型根路径，如 /static/<modelName>
        this.savedModelParameters = null; // 保存的模型参数（从parameters.json加载），供定时器定期应用
        this._shouldApplySavedParams = false; // 是否应该应用保存的参数
        this.appearanceBaselineParameters = {}; // 用户保存后的外观基准，用于表情/motion 重置
        this._savedParamsTimer = null; // 保存参数应用的定时器
        this._mouseTrackingEnabled = window.mouseTrackingEnabled !== false; // 鼠标跟踪启用状态
        this._fullscreenTrackingEnabled = window.live2dFullscreenTrackingEnabled === true; // 全屏跟踪启用状态
        
        // 模型加载锁，防止并发加载导致重复模型叠加
        this._isLoadingModel = false;
        this._activeLoadToken = 0;
        this._modelLoadState = 'idle';
        this._isModelReadyForInteraction = false;
        this._initPIXIPromise = null;
        this._initPIXIContext = null;
        this._lastPIXIContext = { canvasId: null, containerId: null };
        this._displayInfo = null;
        this._autoNamedHitAreaIds = new Set();
        this._bubbleGeometryCache = null;
        this._bubbleGeometrySettleRefreshMs = LIVE2D_BUBBLE_GEOMETRY_SETTLE_REFRESH_MS;
        this._bubbleGeometryModelReadyAt = 0;
        this._bubbleGeometryRefreshPass = 0;
        this._linuxX11RendererProfileOptimized = false;
        this._idleFpsRestoreTimer = null;
        this._idleFpsGovernorTimer = null;

        // 常驻表情：使用官方 expression 播放并在清理后自动重放
        this.persistentExpressionNames = [];
        this.persistentExpressionParamsByName = {};
        this.motionBaselineParameters = {};
        this._activeExpressionParamIds = null;
        this._activeMotionParamIds = null;
        this._motionParameterTrackGeneration = 0;

        // UI/Ticker 资源句柄（便于在切换模型时清理）
        this._lockIconTicker = null;
        this._lockIconElement = null;

        // 口型同步
        this.mouthValue = 0; // 0~1 (嘴巴开合值)
        this.mouthParameterId = null; // 例如 'ParamMouthOpenY' 或 'ParamO'
        this._mouthOverrideInstalled = false;
        this._origMotionManagerUpdate = null; // 保存原始的 motionManager.update 方法
        this._origCoreModelUpdate = null; // 保存原始的 coreModel.update 方法
        this._mouthTicker = null;
        this._temporaryMotionSuspendToken = null;
        this._idleMotionFinishHandler = null;
        this._idleMotionFinishModel = null;

        // 记录最后一次加载模型的原始路径（用于保存偏好时使用）
        this._lastLoadedModelPath = null;

        // 防抖定时器（用于滚轮缩放等连续操作后保存位置）
        this._savePositionDebounceTimer = null;

        // 口型覆盖重新安装标志（防止重复安装）
        this._reinstallScheduled = false;

        // 记录已确认不存在的 expression 文件，避免重复 404 请求
        this._missingExpressionFiles = new Set();
        
        
    }

    // 从 FileReferences 推导 EmotionMapping（用于兼容历史数据）
    deriveEmotionMappingFromFileRefs(fileRefs) {
        const result = { motions: {}, expressions: {} };

        try {
            // 推导 motions
            const motions = (fileRefs && fileRefs.Motions) || {};
            Object.keys(motions).forEach(group => {
                const items = motions[group] || [];
                const files = items
                    .map(item => (item && item.File) ? String(item.File) : null)
                    .filter(Boolean);
                result.motions[group] = files;
            });

            // 推导 expressions（按 Name 前缀分组）
            const expressions = (fileRefs && Array.isArray(fileRefs.Expressions)) ? fileRefs.Expressions : [];
            expressions.forEach(item => {
                if (!item || typeof item !== 'object') return;
                const name = String(item.Name || '');
                const file = String(item.File || '');
                if (!file) return;
                const group = name.includes('_') ? name.split('_', 1)[0] : 'neutral';
                if (!result.expressions[group]) result.expressions[group] = [];
                result.expressions[group].push(file);
            });
        } catch (e) {
            console.warn('从 FileReferences 推导 EmotionMapping 失败:', e);
        }

        return result;
    }

    // 初始化 PIXI 应用
    async initPIXI(canvasId, containerId, options = {}) {
        const resizeMode = String(options.resizeMode || 'host-window');
        if (!['fixed', 'host-window'].includes(resizeMode)) {
            throw new Error(`不支持的 Live2D resizeMode: ${resizeMode}`);
        }
        if (resizeMode === 'fixed' && (!(Number(options.width) > 0) || !(Number(options.height) > 0))) {
            throw new Error('Live2D fixed resizeMode 需要有效的 width 和 height');
        }
        const requestedInitContext = {
            canvasId,
            containerId,
            resizeMode,
            width: resizeMode === 'fixed' ? Number(options.width) : null,
            height: resizeMode === 'fixed' ? Number(options.height) : null,
        };
        if (this._initPIXIPromise) {
            const activeContext = this._initPIXIContext || {};
            const contextMatches = (
                activeContext.canvasId === requestedInitContext.canvasId
                && activeContext.containerId === requestedInitContext.containerId
                && activeContext.resizeMode === requestedInitContext.resizeMode
                && activeContext.width === requestedInitContext.width
                && activeContext.height === requestedInitContext.height
            );
            if (!contextMatches) {
                throw new Error(
                    `Live2D 正在按 ${activeContext.resizeMode || 'unknown'} 初始化，不能并发复用为 ${resizeMode}`
                );
            }
            return await this._initPIXIPromise;
        }

        if (this.isInitialized && this.pixi_app && this.pixi_app.stage) {
            const initializedContext = this._lastPIXIContext || {};
            const contextMatches = (
                initializedContext.canvasId === requestedInitContext.canvasId
                && initializedContext.containerId === requestedInitContext.containerId
                && (initializedContext.resizeMode || 'host-window') === requestedInitContext.resizeMode
                && (initializedContext.width ?? null) === requestedInitContext.width
                && (initializedContext.height ?? null) === requestedInitContext.height
            );
            if (!contextMatches) {
                throw new Error(`Live2D 已按 ${initializedContext.resizeMode || 'host-window'} 初始化，不能复用不同上下文`);
            }
            console.warn('Live2D 管理器已经初始化');
            return this.pixi_app;
        }

        // 如果已初始化但 stage 不存在，重置状态
        if (this.isInitialized && (!this.pixi_app || !this.pixi_app.stage)) {
            console.warn('Live2D 管理器标记为已初始化，但 pixi_app 或 stage 不存在，重置状态');
            if (this.pixi_app && this.pixi_app.destroy) {
                this._stopIdleFpsGovernor();
                if (this._screenChangeHandler) {
                    window.removeEventListener('resize', this._screenChangeHandler);
                    this._screenChangeHandler = null;
                }
                if (this._displayChangeHandler) {
                    window.removeEventListener('electron-display-changed', this._displayChangeHandler);
                    this._displayChangeHandler = null;
                }
                try {
                    this.pixi_app.destroy(true);
                } catch (e) {
                    console.warn('销毁旧的 pixi_app 时出错:', e);
                }
            }
            this.pixi_app = null;
            this.isInitialized = false;
        }

        const canvas = document.getElementById(canvasId);
        const container = document.getElementById(containerId);
        
        if (!canvas) {
            throw new Error(`找不到 canvas 元素: ${canvasId}`);
        }
        if (!container) {
            throw new Error(`找不到容器元素: ${containerId}`);
        }

        const defaultOptions = {
            autoStart: true,
            transparent: true,
            backgroundAlpha: 0,
            resolution: this._getRenderResolutionForQuality(getEffectiveLive2DRenderQuality(window.renderQuality)),
            autoDensity: true
        };
        const pixiOptions = { ...options };
        delete pixiOptions.resizeMode;

        this._initPIXIContext = requestedInitContext;
        this._initPIXIPromise = (async () => {
            try {
                // 桌宠窗口继续按物理屏幕初始化；手机网页必须按真实视口初始化。
                // 否则窄窗口/停靠 DevTools 下 renderer 仍是整屏宽度，移动端默认位置和
                // 首次边界检查都会误以为屏外模型仍在“可视范围”内，直到 resize 才回正。
                const useViewportSize = isMobileWidth();
                const initW = Math.max((useViewportSize ? window.innerWidth : window.screen.width) || 1, 1);
                const initH = Math.max((useViewportSize ? window.innerHeight : window.screen.height) || 1, 1);
                this.pixi_app = new PIXI.Application({
                    view: canvas,
                    width: initW,
                    height: initH,
                    ...defaultOptions,
                    ...pixiOptions
                });

                if (!this.pixi_app) {
                    throw new Error('PIXI.Application 创建失败：返回值为 null 或 undefined');
                }

                try {
                    canvas.style.background = 'transparent';
                    canvas.style.backgroundColor = 'transparent';
                    container.style.background = 'transparent';
                    container.style.backgroundColor = 'transparent';
                    if (this.pixi_app.renderer) {
                        this.pixi_app.renderer.backgroundAlpha = 0;
                        if (this.pixi_app.renderer.background) {
                            this.pixi_app.renderer.background.alpha = 0;
                        }
                    }
                } catch (_) {}

                if (!this.pixi_app.stage) {
                    throw new Error('PIXI.Application 创建失败：stage 属性不存在');
                }

                this.isInitialized = true;
                this._lastPIXIContext = { ...requestedInitContext };
                if (typeof window.targetFrameRate === 'number' && this.pixi_app.ticker) {
                    // 经归一化再写：已存的负数/非法配置不能直接进 PIXI maxFPS
                    this.pixi_app.ticker.maxFPS = this._resolveConfiguredTargetFps();
                }
                // 包装 ticker.stop/start：外部代码（app-character / live2d-model 等）直接
                // 操作 ticker 时先退出空闲低频 tick 模式，避免空闲定时器与外部暂停意图打架。
                // 空闲模式自身通过 _tickerOrigStop/Start 绕过包装，不会自触发。
                {
                    const ticker = this.pixi_app.ticker;
                    const mgr = this;
                    // 闭包捕获本 ticker 自己的原始方法：PIXI 重建后旧 ticker 上残留的
                    // 包装函数只会作用于旧 ticker 本身，不会透过实例属性误操作新 ticker。
                    const origStop = ticker.stop.bind(ticker);
                    const origStart = ticker.start.bind(ticker);
                    this._tickerOrigStop = origStop;
                    this._tickerOrigStart = origStart;
                    // stop：退出定时器模式但不拉起全局 ticker（Live2D 停了就不该让 PIXI 的
                    // rAF 链继续跑）；start：退出定时器模式并把全局 ticker 一并拉回来。
                    // 身份检查：PIXI 重建后外部残留的旧 ticker 再调包装，不能去动当前实例
                    // 与全局 ticker 的调度状态，只操作旧 ticker 自己。
                    // stop 在 Pet 窗口下无论此前是否在定时器模式都扣住全局 ticker：切角色的真实
                    // 序列是 stop → removeModel 里 start（放开）→ 最终 stop，最后这一下不在
                    // 定时器模式，只靠 _exitIdleTickMode 不会再把全局 ticker 停回去。
                    ticker.stop = function () {
                        if (mgr.pixi_app && mgr.pixi_app.ticker === ticker) {
                            mgr._exitIdleTickMode({ restartGlobals: false });
                            if (mgr._resolveGlobalTickers()) mgr._holdGlobalTickers();
                        }
                        return origStop();
                    };
                    ticker.start = function () {
                        if (mgr.pixi_app && mgr.pixi_app.ticker === ticker) {
                            mgr._exitIdleTickMode();
                            mgr._releaseGlobalTickers();
                        }
                        return origStart();
                    };
                }
                // 启动自适应帧率守护：静止时降到地板（LIVE2D_IDLE_FPS），活动时升回配置帧率。
                this._startIdleFpsGovernor();

                // 嵌入小游戏的固定画布由 SDK 的 viewport/fit 契约管理，不能跟随宿主
                // window resize。否则 200x300 renderer 会被放大到整个游戏窗口，并把
                // 模型 scale 再乘一次面积比，造成窗口尺寸变化后角色巨大化。
                if (resizeMode === 'fixed') {
                    console.log('[Live2D Core] PIXI.Application 以固定视口初始化:', {
                        width: this.pixi_app.renderer?.screen?.width,
                        height: this.pixi_app.renderer?.screen?.height,
                    });
                    return this.pixi_app;
                }

                // Resize 渲染器并等比调整模型坐标/尺寸
                // 触发时机：
                //  1) 系统屏幕分辨率变化（window.screen.width/height 变化）—— 原有逻辑
                //  2) Electron 跨屏切换 / 显示器 hotplug —— 通过 'electron-display-changed' 事件触发
                //     （在 Electron 里 window.screen.width/height 不会随 BrowserWindow 跨屏而变，
                //      所以单靠 screen 比较无法感知跨屏，canvas 会保持主屏初始尺寸被窗口边界裁切）
                // 任务栏、DevTools、输入法等视口变化不会触发（幂等判定跳过）
                let lastScreenW = window.screen.width;
                let lastScreenH = window.screen.height;
                let lastViewportW = window.innerWidth;
                let lastViewportH = window.innerHeight;
                let lastDevicePixelRatio = window.devicePixelRatio || 1;

                const doResize = (reason) => {
                    if (!this.pixi_app || !this.pixi_app.renderer) return;
                    const renderer = this.pixi_app.renderer;
                    const prevW = this.pixi_app.renderer.screen.width;
                    const prevH = this.pixi_app.renderer.screen.height;
                    // Niri 会把透明 Pet 的 BrowserWindow 物理裁剪到人物附近，但 Live2D
                    // 始终工作在完整虚拟桌面坐标系。若把物理裁剪尺寸当成 viewport，
                    // 每轮 setBounds 都会再次缩放人物并反过来改变下一轮裁剪范围。
                    const niriVirtualViewport = getLive2DNiriPetVirtualViewportSize();
                    const niriPhysicalCropActive = !!niriVirtualViewport;
                    const newW = Math.max(
                        niriVirtualViewport?.width || window.innerWidth || window.screen.width || 1,
                        1
                    );
                    const newH = Math.max(
                        niriVirtualViewport?.height || window.innerHeight || window.screen.height || 1,
                        1
                    );
                    const prevResolution = renderer.resolution || 1;
                    const nextResolution = this._getRenderResolutionForQuality(getEffectiveLive2DRenderQuality(window.renderQuality));
                    if (isLive2DReturnBallViewportSize(newW, newH)) {
                        return;
                    }
                    const restoringFromReturnBallViewport =
                        isLive2DReturnBallViewportSize(prevW, prevH) &&
                        !isLive2DReturnBallViewportSize(newW, newH);
                    const sizeChanged = prevW !== newW || prevH !== newH;
                    const resolutionChanged = Math.abs(prevResolution - nextResolution) >= 0.001;
                    if (!sizeChanged && !resolutionChanged) return;

                    if (!niriPhysicalCropActive &&
                        typeof this.isLive2DPeekActive === 'function' &&
                        this.isLive2DPeekActive() &&
                        typeof this.clearLive2DPeek === 'function') {
                        this.clearLive2DPeek(`viewport-changed:${reason}`);
                    }

                    if (resolutionChanged) {
                        renderer.resolution = nextResolution;
                    }
                    renderer.resize(newW, newH);

                    if (!sizeChanged) {
                        console.log('[Live2D Core] renderer resolution 已刷新:', { reason, prevResolution, nextResolution, newW, newH });
                        return;
                    }

                    // 跨屏切换路径（Live2DManager._checkAndSwitchDisplay）已在 moveWindowToDisplay 之后
                    // 主动把 model.x/y 设置为新屏窗口坐标。若这里再按 (newW/prevW, newH/prevH) 缩放，
                    // 会对同一个值双重作用，导致模型偏移。通过 _pendingDisplaySwitch 跳过缩放，
                    // 仅 resize renderer（renderer 尺寸必须更新，否则 canvas 仍是旧尺寸裁切模型）。
                    if (this._pendingDisplaySwitch || restoringFromReturnBallViewport || niriPhysicalCropActive) {
                        if (restoringFromReturnBallViewport && !this._pendingDisplaySwitch) return;
                        console.log('[Live2D Core] renderer 已 resize（跳过模型缩放）:', {
                            reason,
                            prevW,
                            prevH,
                            newW,
                            newH,
                            pendingDisplaySwitch: !!this._pendingDisplaySwitch,
                            niriPhysicalCropActive
                        });
                        return;
                    }

                    if (this.currentModel && prevW > 0 && prevH > 0) {
                        const wRatio = newW / prevW;
                        const hRatio = newH / prevH;
                        this.currentModel.x *= wRatio;
                        this.currentModel.y *= hRatio;
                        const areaRatio = Math.sqrt(wRatio * hRatio);
                        this.currentModel.scale.x *= areaRatio;
                        this.currentModel.scale.y *= areaRatio;
                    }
                    console.log('[Live2D Core] renderer 已 resize:', { reason, prevW, prevH, newW, newH });
                };

                this._screenChangeHandler = () => {
                    const sw = window.screen.width;
                    const sh = window.screen.height;
                    const vw = window.innerWidth;
                    const vh = window.innerHeight;
                    const dpr = window.devicePixelRatio || 1;
                    const renderer = this.pixi_app && this.pixi_app.renderer;
                    const shouldRecoverReturnBallRenderer = !!(renderer && renderer.screen &&
                        isLive2DReturnBallViewportSize(renderer.screen.width, renderer.screen.height) &&
                        !isLive2DReturnBallViewportSize(window.innerWidth, window.innerHeight));
                    if (sw === lastScreenW && sh === lastScreenH &&
                        vw === lastViewportW && vh === lastViewportH &&
                        Math.abs(dpr - lastDevicePixelRatio) < 0.001 &&
                        !shouldRecoverReturnBallRenderer) return;
                    lastScreenW = sw;
                    lastScreenH = sh;
                    lastViewportW = vw;
                    lastViewportH = vh;
                    lastDevicePixelRatio = dpr;
                    doResize(shouldRecoverReturnBallRenderer
                        ? 'window.resize:return-ball-renderer-recovery'
                        : 'window.screen/devicePixelRatio changed');
                };
                // 跨屏切换信号：主进程 setBounds 后广播；这里等一帧让 innerWidth/Height 落地再 resize
                this._displayChangeHandler = () => {
                    requestAnimationFrame(() => {
                        lastDevicePixelRatio = window.devicePixelRatio || 1;
                        doResize('electron-display-changed');
                        requestAnimationFrame(() => doResize('electron-display-changed:settled'));
                        setTimeout(() => doResize('electron-display-changed:delayed'), 120);
                    });
                };

                window.addEventListener('resize', this._screenChangeHandler);
                window.addEventListener('electron-display-changed', this._displayChangeHandler);

                console.log('[Live2D Core] PIXI.Application 初始化成功，stage 已创建');
                return this.pixi_app;
            } catch (error) {
                console.error('[Live2D Core] PIXI.Application 初始化失败:', error);
                this.pixi_app = null;
                this.isInitialized = false;
                throw error;
            }
        })();

        try {
            return await this._initPIXIPromise;
        } finally {
            this._initPIXIPromise = null;
            this._initPIXIContext = null;
        }
    }

    async ensurePIXIReady(canvasId, containerId, options = {}) {
        const lastContext = this._lastPIXIContext || {};
        const resizeMode = String(options.resizeMode || 'host-window');
        const requestedWidth = resizeMode === 'fixed' ? Number(options.width) : null;
        const requestedHeight = resizeMode === 'fixed' ? Number(options.height) : null;
        const contextMatches = (
            lastContext.canvasId === canvasId &&
            lastContext.containerId === containerId &&
            (lastContext.resizeMode || 'host-window') === resizeMode &&
            (lastContext.width ?? null) === requestedWidth &&
            (lastContext.height ?? null) === requestedHeight
        );

        if (this.isInitialized && this.pixi_app && this.pixi_app.stage && contextMatches) {
            return this.pixi_app;
        }
        if (this.isInitialized && !contextMatches) {
            if (this._screenChangeHandler) {
                window.removeEventListener('resize', this._screenChangeHandler);
                this._screenChangeHandler = null;
            }
            if (this._displayChangeHandler) {
                window.removeEventListener('electron-display-changed', this._displayChangeHandler);
                this._displayChangeHandler = null;
            }
            if (this.pixi_app && this.pixi_app.destroy) {
                this._stopIdleFpsGovernor();
                try {
                    this.pixi_app.destroy(true);
                } catch (e) {
                    console.warn('[Live2D Core] ensurePIXIReady 销毁旧 PIXI 失败:', e);
                }
            }
            this.pixi_app = null;
            this.isInitialized = false;
        }
        const app = await this.initPIXI(canvasId, containerId, options);
        if (app && app.stage) {
            this._lastPIXIContext = {
                canvasId,
                containerId,
                resizeMode,
                width: requestedWidth,
                height: requestedHeight,
            };
        }
        return app;
    }

    async rebuildPIXI(canvasId, containerId, options = {}) {
        if (this._initPIXIPromise) {
            try {
                await this._initPIXIPromise;
            } catch (e) {
                console.warn('[Live2D Core] 忽略旧初始化失败，继续重建 PIXI:', e);
            }
        }
        if (this._screenChangeHandler) {
            window.removeEventListener('resize', this._screenChangeHandler);
            this._screenChangeHandler = null;
        }
        if (this._displayChangeHandler) {
            window.removeEventListener('electron-display-changed', this._displayChangeHandler);
            this._displayChangeHandler = null;
        }
        if (this.pixi_app && this.pixi_app.destroy) {
            this._stopIdleFpsGovernor();
            try {
                this.pixi_app.destroy(true);
            } catch (e) {
                console.warn('[Live2D Core] 重建时销毁旧 PIXI 失败:', e);
            }
        }
        this.pixi_app = null;
        this.isInitialized = false;
        return await this.initPIXI(canvasId, containerId, options);
    }

    _isModelInputRegionInteractive() {
        if (!this._isModelReadyForInteraction) {
            return false;
        }

        const bodyClassList = document.body?.classList;
        if (bodyClassList &&
            (bodyClassList.contains('neko-main-ui-hidden-by-model-manager') ||
                bodyClassList.contains('neko-model-hidden-by-manager-overlap'))) {
            return false;
        }

        const getStyleValueFrom = (style, propertyName, camelName) => {
            if (!style) return '';
            const directValue = style[camelName];
            if (directValue !== undefined && directValue !== null && directValue !== '') {
                return String(directValue).trim().toLowerCase();
            }
            if (typeof style.getPropertyValue === 'function') {
                return String(style.getPropertyValue(propertyName) || '').trim().toLowerCase();
            }
            return '';
        };
        const getStyleValue = (element, propertyName, camelName) => {
            if (!element) return '';
            if (typeof window.getComputedStyle === 'function') {
                const computedValue = getStyleValueFrom(
                    window.getComputedStyle(element),
                    propertyName,
                    camelName
                );
                if (computedValue !== '') {
                    return computedValue;
                }
            }
            return getStyleValueFrom(element.style, propertyName, camelName);
        };
        const isVisuallyHidden = (element, checkPointerEvents = false) => {
            if (!element) return false;
            const classList = element.classList;
            if (classList && (classList.contains('hidden') || classList.contains('minimized'))) {
                return true;
            }
            if (typeof element.getAttribute === 'function' &&
                element.getAttribute('data-neko-model-goodbye-exiting') === 'true') {
                return true;
            }

            const display = getStyleValue(element, 'display', 'display');
            const visibility = getStyleValue(element, 'visibility', 'visibility');
            const opacity = getStyleValue(element, 'opacity', 'opacity');
            if (display === 'none' || visibility === 'hidden' ||
                (opacity !== '' && Number.isFinite(Number(opacity)) && Number(opacity) <= 0)) {
                return true;
            }
            return checkPointerEvents &&
                getStyleValue(element, 'pointer-events', 'pointerEvents') === 'none';
        };

        const container = document.getElementById('live2d-container');
        const canvas = this.pixi_app?.view || document.getElementById('live2d-canvas');
        if (window._nekoModelReturnEnterContainer === container) {
            return false;
        }
        if (window._nekoAvatarPerformanceFrameContainer === container) {
            // Avatar performance frames are written to the inline transform.
            // The stylesheet baseline uses translateZ(0), whose computed matrix
            // is identity and must not keep stale suppression alive.
            const inlineTransform = getStyleValueFrom(
                container?.style,
                'transform',
                'transform'
            );
            if (inlineTransform !== '' && inlineTransform !== 'none') {
                return false;
            }
            window._nekoAvatarPerformanceFrameContainer = null;
        }
        // The Electron pet root intentionally stays pointer-transparent in normal
        // operation, so only canvas pointer-events are an interaction signal.
        return !isVisuallyHidden(container) && !isVisuallyHidden(canvas, true);
    }

    /**
     * 暂停渲染循环（用于节省资源，例如进入模型管理界面时）
     */
    pauseRendering() {
        if (this.pixi_app && this.pixi_app.ticker) {
            this.pixi_app.ticker.stop();
            console.log('[Live2D Core] 渲染循环已暂停');
        }
    }

    /**
     * 恢复渲染循环（从暂停状态恢复）
     */
    resumeRendering() {
        if (this.pixi_app && this.pixi_app.ticker) {
            this.pixi_app.ticker.start();
            console.log('[Live2D Core] 渲染循环已恢复');
        }
    }

    /**
     * 设置目标帧率
     * @param {number} fps - 目标帧率，0 表示不限帧（跟随 VSync）
     */
    setTargetFPS(fps) {
        if (!this.pixi_app || !this.pixi_app.ticker) return;
        // setTargetFPS 是「目标帧率」配置的权威应用点：先同步配置源，确保 governor 的
        // boost/restore 读到的是新值而非旧值（调用方一般已设过 window.targetFrameRate，这里兜底自洽）。
        const resolved = Number(fps);
        // 负数/非法值一律按 60：否则豁免分支会把负数直接写进 PIXI maxFPS，被钳到 minFPS=10
        window.targetFrameRate = Number.isFinite(resolved) && resolved >= 0 ? resolved : 60;
        if (this._idleFpsRestoreTimer) {
            clearTimeout(this._idleFpsRestoreTimer);
            this._idleFpsRestoreTimer = null;
        }
        // 立即按 governor 语义落地，不必等下一次活动周期：有渲染活动升回配置帧率，
        // 否则直接压到静止地板，避免改完设置后空闲态停在未节流的值。
        if (window.__NEKO_DISABLE_AVATAR_IDLE_THROTTLE__ === true) {
            // 页面级豁免：直接落配置帧率，不进入空闲地板/低频模式
            this._exitIdleTickMode();
            this._applyRafMaxFps(this._resolveConfiguredTargetFps());
        } else if (this._hasRenderActivity()) {
            this.boostInteractiveFPS();
        } else {
            // 无活动：直接按新地板落到空闲低频 tick。已在定时器模式则只换周期，
            // 让 interval 周期与新的地板帧率一致。
            const idleFps = this._resolveIdleFps();
            this._applyRafMaxFps(idleFps);
            this._enterIdleTickMode(idleFps);
        }
        console.log(`[Live2D Core] 目标帧率设置为 ${window.targetFrameRate === 0 ? 'VSync (无限制)' : window.targetFrameRate + 'fps'}`);
    }

    // 用户配置的目标帧率（活动时的上限），默认 60；0 表示不限帧（跟随 VSync）。
    _resolveConfiguredTargetFps() {
        const configured = typeof window.targetFrameRate === 'number' ? Number(window.targetFrameRate) : 60;
        // 负数/非法值一律按 60（否则地板会变成负数、定时器周期算出秒级）
        return Number.isFinite(configured) && configured >= 0 ? configured : 60;
    }

    // 静止地板帧率：不超过用户配置；配置为 0（不限帧）时仍压到地板省 CPU。
    _resolveIdleFps() {
        const configured = this._resolveConfiguredTargetFps();
        return configured === 0 ? LIVE2D_IDLE_FPS : Math.min(LIVE2D_IDLE_FPS, configured);
    }

    // 活动态应改用定时器驱动的帧率（Pet 窗口且配置帧率明显低于显示器刷新率时由
    // frame-pacing.js 给出）；null = 留在 rAF 驱动。非 Pet 页面没有 nekoFramePacing，恒为 null。
    _resolveActiveTimerTickFps() {
        const pacing = window.nekoFramePacing;
        if (!pacing || typeof pacing.activeTimerTickFps !== 'function') return null;
        const fps = Number(pacing.activeTimerTickFps(this._resolveConfiguredTargetFps()));
        return Number.isFinite(fps) && fps > 0 ? fps : null;
    }

    // Pet 窗口进程里只有一个 Live2DManager，可以安全接管全局 PIXI.Ticker.shared/system
    // 的帧率上限（模型 motion/physics 的 autoUpdate 挂在 shared 上，不接管的话它仍按
    // 显示器刷新率跑）。其它页面（模型管理器/角色卡预览可能多个 manager 共存）不碰。
    _resolveGlobalTickers() {
        const pacing = window.nekoFramePacing;
        if (!pacing || typeof pacing.isElectronPet !== 'function' || !pacing.isElectronPet()) return null;
        const PixiTicker = (typeof PIXI !== 'undefined' && PIXI.Ticker) ? PIXI.Ticker : null;
        if (!PixiTicker || !PixiTicker.shared || !PixiTicker.system) return null;
        return { shared: PixiTicker.shared, system: PixiTicker.system };
    }

    // rAF 驱动下的帧率上限：写到 app.ticker，Pet 窗口还一并写到 shared/system。
    // 定时器驱动模式下三个 ticker 的 maxFPS 必须保持 0（定时器周期本身就是节流器，
    // interval 抖动会被 maxFPS 丢帧、实际帧率减半），此时只更新退出定时器模式时要恢复的值。
    _applyRafMaxFps(fps) {
        const resolved = Number.isFinite(Number(fps)) ? Number(fps) : 0;
        const ticker = this.pixi_app && this.pixi_app.ticker;
        if (!ticker) return;
        const globals = this._resolveGlobalTickers();
        if (this._idleTickMode) {
            this._idleTickSavedMaxFPS = resolved;
        } else if (ticker.maxFPS !== resolved) {
            ticker.maxFPS = resolved;
        }
        if (!globals) return;
        // 全局 ticker 被扣住（定时器模式 / 外部 stop 期间）时只更新待恢复值，放开时才生效；
        // 否则直接写——两者分开存，虽然本 manager 总是给同一个值，但恢复时不互相串
        if (this._globalTickersHeld) {
            this._idleTickSavedSharedMaxFPS = resolved;
            this._idleTickSavedSystemMaxFPS = resolved;
            return;
        }
        if (globals.shared.maxFPS !== resolved) globals.shared.maxFPS = resolved;
        if (globals.system.maxFPS !== resolved) globals.system.maxFPS = resolved;
    }

    // 有渲染活动时升回配置帧率，并安排在 durationMs 后衰减回静止地板（全平台）。
    boostInteractiveFPS(durationMs = LIVE2D_INTERACTIVE_FPS_HOLD_MS) {
        if (!this.pixi_app || !this.pixi_app.ticker) return;
        const ticker = this.pixi_app.ticker;
        const configured = this._resolveConfiguredTargetFps();
        // 活动时升回用户配置上限（0=不限帧）。不再 Math.max(IDLE,...)，否则会把刻意设到
        // 低于地板的配置（如低端机 24fps）反而抬到 30，超过用户上限。
        const activeFps = configured === 0 ? 0 : configured;
        const timerFps = this._resolveActiveTimerTickFps();
        if (timerFps != null) {
            // Pet 窗口且配置帧率低于刷新率：活动态也留在定时器驱动，只把周期换成配置帧率。
            // 回到 rAF 只会让 Blink 按刷新率跑主帧再跳掉一半，省不出 CPU/GPU。
            this._applyRafMaxFps(activeFps);
            this._enterIdleTickMode(timerFps);
        } else {
            // 有活动：退出定时器驱动，恢复 rAF 驱动的满帧管线。
            this._exitIdleTickMode();
            this._applyRafMaxFps(activeFps);
        }
        const originalTicker = ticker;
        if (this._idleFpsRestoreTimer) {
            clearTimeout(this._idleFpsRestoreTimer);
            this._idleFpsRestoreTimer = null;
        }
        // 页面级豁免：只升频、不安排衰减——衰减会把预览页压回空闲地板帧率
        if (window.__NEKO_DISABLE_AVATAR_IDLE_THROTTLE__ === true) return;
        this._idleFpsRestoreTimer = setTimeout(() => {
            this._idleFpsRestoreTimer = null;
            if (this.pixi_app && this.pixi_app.ticker === originalTicker) {
                const idleFps = this._resolveIdleFps();
                this._applyRafMaxFps(idleFps);
                // 无活动衰减到地板后，进一步切换到定时器驱动的低频 tick：
                // rAF 驱动下即使 maxFPS 已限 30，三个 ticker 的 rAF 请求仍会让
                // Blink 以显示器刷新率（如 120Hz）跑完整主帧生命周期，空耗 CPU/GPU。
                // 已在定时器模式（活动态定时器驱动）则只把周期换成地板帧率。
                this._enterIdleTickMode(idleFps);
            }
        }, Math.max(100, Number(durationMs) || LIVE2D_INTERACTIVE_FPS_HOLD_MS));
    }

    /**
     * 空闲低频 tick 模式：停掉 rAF 驱动的 ticker（app + 全局 shared/system），
     * 改用 setInterval 以静止地板帧率手动 update()。效果等同 30fps 渲染，
     * 但页面不再以显示器刷新率调度主帧。任何 boost / 外部 ticker.start()/stop()
     * 都会立即退出该模式。
     *
     * 注意：PIXI.Ticker.shared/system 是全局单例。当前没有任何页面会同时运行
     * 两个持有活跃 pixi_app 的 Live2DManager（预览管理器等都是互斥激活），
     * 若未来出现共存场景，先进入空闲模式的实例会把另一个实例的 shared 驱动
     * （模型 motion/physics 的 autoUpdate）降到自己的地板频率——届时需要把
     * shared/system 的接管改成跨实例引用计数。
     */
    _enterIdleTickMode(fps) {
        const requested = Number(fps);
        const tickFps = Number.isFinite(requested) && requested > 0 ? requested : this._resolveIdleFps();
        if (this._idleTickMode) {
            // 已在定时器模式（空闲地板 ↔ 活动态定时器驱动之间切换）：只换周期，不经过 rAF
            if (this._idleTickFps === tickFps) return;
            if (this._idleTickTimer) clearInterval(this._idleTickTimer);
            this._idleTickTimer = this._startTimerTick(tickFps);
            return;
        }
        if (!this.pixi_app || !this.pixi_app.ticker || !this._tickerOrigStop) return;
        const ticker = this.pixi_app.ticker;
        // 外部已显式暂停（pauseRendering / 角色切换）：不接管
        if (ticker.started === false) return;
        this._idleTickMode = true;
        // 定时器本身就是节流器：清掉 maxFPS 限制，避免 interval 抖动（33ms < minElapsed
        // 33.33ms）导致 update() 被 maxFPS 丢帧、实际帧率减半。
        this._idleTickSavedMaxFPS = ticker.maxFPS;
        ticker.maxFPS = 0;
        this._tickerOrigStop();
        this._holdGlobalTickers();
        this._idleTickTimer = this._startTimerTick(tickFps);
    }

    /**
     * 扣住全局 PIXI.Ticker.shared/system：记录它们原本是否在跑并停掉（幂等，已扣住则不动）。
     * 定时器模式下由 _startTimerTick 手动 update 它们；Pet 窗口外部 ticker.stop()（切
     * VRM/MMD、pauseRendering）也扣住——Live2D 不渲染时没人需要 PIXI 的 rAF 链继续跑。
     * Pet 窗口下 shared/system 的 maxFPS 也由本 manager 接管（见 _applyRafMaxFps），
     * 手动 update() 同样不能被它们的 maxFPS 丢帧：一并清零并记住恢复值。
     */
    _holdGlobalTickers() {
        if (this._globalTickersHeld) return;
        const PixiTicker = (typeof PIXI !== 'undefined' && PIXI.Ticker) ? PIXI.Ticker : null;
        if (!PixiTicker || !PixiTicker.shared || !PixiTicker.system) return;
        this._globalTickersHeld = true;
        this._idleTickSharedWasStarted = !!PixiTicker.shared.started;
        this._idleTickSystemWasStarted = !!PixiTicker.system.started;
        const globals = this._resolveGlobalTickers();
        if (globals) {
            this._idleTickSavedSharedMaxFPS = globals.shared.maxFPS;
            this._idleTickSavedSystemMaxFPS = globals.system.maxFPS;
            globals.shared.maxFPS = 0;
            globals.system.maxFPS = 0;
        }
        try {
            if (this._idleTickSharedWasStarted) PixiTicker.shared.stop();
            if (this._idleTickSystemWasStarted) PixiTicker.system.stop();
        } catch (_) {}
    }

    // 定时器驱动的 tick 循环：按 fps 手动 update() app + shared/system 三个 ticker。
    // 空闲地板（30）和活动态定时器驱动（配置帧率）共用，只有周期不同。
    _startTimerTick(fps) {
        this._idleTickFps = fps;
        const PixiTicker = (typeof PIXI !== 'undefined' && PIXI.Ticker) ? PIXI.Ticker : null;
        const intervalMs = Math.max(4, Math.round(1000 / Math.max(1, fps)));
        return setInterval(() => {
            if (!this._idleTickMode) return;
            const app = this.pixi_app;
            if (!app || !app.ticker) { this._exitIdleTickMode(); return; }
            // 外部代码绕过包装直接把 ticker 拉起来了：让位给 rAF 模式
            if (app.ticker.started) { this._exitIdleTickMode(); return; }
            // 纯浏览器标签页隐藏时不渲染（对齐 rAF 模式下的完全暂停语义；
            // Electron 宠物窗禁用 backgroundThrottling，不受影响）
            if (typeof document !== 'undefined' && document.hidden) return;
            const now = performance.now();
            // 三个 update 各自捕获：一个 ticker 的异常不应拖累其余（对齐 rAF
            // 模式下三条独立 rAF 链的失败隔离），且打印首个异常保住可诊断性。
            const guardedUpdate = (t) => {
                try {
                    t.update(now);
                } catch (e) {
                    if (!this._idleTickErrorLogged) {
                        this._idleTickErrorLogged = true;
                        console.warn('[Live2D Core] 空闲低频 tick 渲染异常（同类后续异常不再打印）:', e);
                    }
                }
            };
            if (PixiTicker) {
                if (this._idleTickSystemWasStarted) guardedUpdate(PixiTicker.system);
                if (this._idleTickSharedWasStarted) guardedUpdate(PixiTicker.shared);
            }
            guardedUpdate(app.ticker);
        }, intervalMs);
    }

    /**
     * 退出定时器驱动模式。默认把定时器模式停掉的全局 shared/system ticker 一并拉起
     * （回到 rAF 驱动）；`restartGlobals: false` 用于外部 ticker.stop() 路径（切到
     * VRM/MMD、pauseRendering）：Live2D 本身都不渲染了，全局 ticker 拉起来只会让
     * PIXI 的 rAF 链继续按刷新率空跑，之后由 ticker.start() 包装再 _releaseGlobalTickers。
     */
    _exitIdleTickMode(options) {
        if (!this._idleTickMode) return;
        const restartGlobals = !(options && options.restartGlobals === false);
        this._idleTickMode = false;
        if (this._idleTickTimer) {
            clearInterval(this._idleTickTimer);
            this._idleTickTimer = null;
        }
        const app = this.pixi_app;
        if (app && app.ticker && typeof this._idleTickSavedMaxFPS === 'number') {
            app.ticker.maxFPS = this._idleTickSavedMaxFPS;
        }
        if (restartGlobals) this._releaseGlobalTickers();
        if (app && app.ticker && app.ticker.started === false && this._tickerOrigStart) {
            try { this._tickerOrigStart(); } catch (_) {}
        }
        this._idleTickSavedMaxFPS = null;
        this._idleTickFps = null;
    }

    // 把定时器模式接管（停掉）的全局 shared/system ticker 拉回来并恢复它们的帧率上限。
    // 幂等：没接管过就什么都不做。
    _releaseGlobalTickers() {
        if (!this._globalTickersHeld) return;
        this._globalTickersHeld = false;
        const PixiTicker = (typeof PIXI !== 'undefined' && PIXI.Ticker) ? PIXI.Ticker : null;
        if (!PixiTicker) return;
        try {
            if (typeof this._idleTickSavedSharedMaxFPS === 'number') PixiTicker.shared.maxFPS = this._idleTickSavedSharedMaxFPS;
            if (typeof this._idleTickSavedSystemMaxFPS === 'number') PixiTicker.system.maxFPS = this._idleTickSavedSystemMaxFPS;
            if (this._idleTickSharedWasStarted && !PixiTicker.shared.started) PixiTicker.shared.start();
            if (this._idleTickSystemWasStarted && !PixiTicker.system.started) PixiTicker.system.start();
        } catch (_) {}
        this._idleTickSharedWasStarted = false;
        this._idleTickSystemWasStarted = false;
        this._idleTickSavedSharedMaxFPS = null;
        this._idleTickSavedSystemMaxFPS = null;
    }

    // 向后兼容旧调用名（live2d-interaction.js 的交互升帧），现已推广到全平台。
    boostLinuxX11InteractiveFPS(durationMs) {
        this.boostInteractiveFPS(durationMs);
    }

    // 是否有需要满帧的渲染活动：动作/表情播放、拖拽、光标聚焦跟踪、口型同步说话。
    _hasRenderActivity() {
        try {
            if (typeof this.hasActiveMotionPlayback === 'function' && this.hasActiveMotionPlayback()) {
                return true;
            }
        } catch (_) {}
        if (this._isDraggingModel) return true;
        // 光标停在模型悬停范围内时 isFocusing 会一直为 true；只有光标最近真的动过才算活动，
        // 否则视线目标已收敛、却永远进不了空闲低频 tick。
        if (this.isFocusing && Number.isFinite(this._lastPointerMoveAt) &&
            (performance.now() - this._lastPointerMoveAt) < LIVE2D_INTERACTIVE_FPS_HOLD_MS) return true;
        const appState = window.appState;
        if (appState && appState.lipSyncActive) return true;
        return false;
    }

    // 自适应帧率守护：周期性探测活动状态，有活动就续命满帧，无活动时由衰减计时器回落到地板。
    _startIdleFpsGovernor() {
        this._stopIdleFpsGovernor();
        // 页面级豁免（demo/模型管理器等预览页）：期望满帧，不启动空闲治理
        if (window.__NEKO_DISABLE_AVATAR_IDLE_THROTTLE__ === true) return;
        // 启动即视为活动（加载/入场动画期间保持满帧），随后自动衰减。
        this.boostInteractiveFPS();
        this._idleFpsGovernorTimer = setInterval(() => {
            // 自终止：任何 teardown 路径（切 VRM/MMD、model_manager 销毁、manager.destroy 等）
            // 销毁/置空 pixi_app 后，governor 在下一拍自动停掉并释放对 manager 的闭包引用——
            // 不必每条销毁路径都手动清，避免遗漏与内存泄漏。
            if (!this.pixi_app || !this.pixi_app.ticker) {
                this._stopIdleFpsGovernor();
                return;
            }
            // 暂停态（pauseRendering / 切到非 Live2D 角色时直接 ticker.stop() 但保留 pixi_app 复用）：
            // ticker 已 stop，跳过升/降帧工作。不在此自终止——有多处直接 ticker.start() 的恢复路径
            // （app-character / model_manager / app-ui / live2d-model 等）不走 resumeRendering，
            // 自终止后无人重启会让 Live2D 失去 idle 节流；ticker 恢复 started 后本守护自动继续治理。
            // 用 === false 安全降级：万一某 pixi 版本无 started 属性，守卫不触发即维持原行为。
            // 空闲低频 tick 模式下 started 恒为 false（由定时器驱动），但活动探测必须继续，
            // 否则动作/拖拽/口型同步永远无法把帧率拉回来。
            if (!this._idleTickMode && this.pixi_app.ticker.started === false) return;
            if (this._hasRenderActivity()) {
                this.boostInteractiveFPS();
            } else if (!this._idleTickMode && !this._idleFpsRestoreTimer) {
                // 自愈：外部「确保渲染」路径（model-display / universal-manager 等的
                // `!started && start()`）会把 ticker 拉回 rAF 模式并解除空闲低频 tick，
                // 且不会再有 boost 衰减计时器带我们回来。无活动、无待衰减时直接重新进入。
                const idleFps = this._resolveIdleFps();
                this._applyRafMaxFps(idleFps);
                this._enterIdleTickMode(idleFps);
            }
        }, LIVE2D_IDLE_FPS_GOVERNOR_INTERVAL_MS);
    }

    _stopIdleFpsGovernor() {
        if (this._idleFpsGovernorTimer) {
            clearInterval(this._idleFpsGovernorTimer);
            this._idleFpsGovernorTimer = null;
        }
        // 一并清掉待触发的衰减计时器，避免销毁/重建失败路径留下后台 timer。
        if (this._idleFpsRestoreTimer) {
            clearTimeout(this._idleFpsRestoreTimer);
            this._idleFpsRestoreTimer = null;
        }
        // teardown 时必须退出空闲低频 tick 模式：全局 PIXI.Ticker.shared/system 是
        // 我们停的，不恢复的话销毁重建后模型 autoUpdate（挂在 shared 上）会被冻住。
        this._exitIdleTickMode();
        // 先 ticker.stop()（不拉起全局 ticker）再销毁的路径：上面已不在定时器模式，
        // 全局 ticker 仍被本 manager 扣着——销毁时必须无条件释放（幂等）。
        this._releaseGlobalTickers();
    }

    /**
     * Electron/X11 的透明全屏窗口里，WebGL getter 会造成明显同步阻塞。
     * pixi-live2d-display 的 Cubism renderer 每帧都 save/restore WebGL profile，
     * 其中包含一批 gl.getParameter/getVertexAttrib 调用；当前页面只有这一个
     * Live2D WebGL pipeline，PIXI 会在同一帧内重新设置所需状态，所以桌面 X11 下
     * 跳过这段 profile 保存可以避免每帧 GPU/CPU 同步等待。
     */
    _optimizeLinuxX11RendererProfile(model) {
        if (!isDesktopLinuxX11Runtime()) return;
        const renderer = model && model.internalModel && model.internalModel.renderer;
        if (!renderer || typeof renderer.saveProfile !== 'function' || typeof renderer.restoreProfile !== 'function') return;
        if (renderer.__nekoLinuxX11ProfileOptimized) return;

        renderer.__nekoOriginalSaveProfile = renderer.saveProfile;
        renderer.__nekoOriginalRestoreProfile = renderer.restoreProfile;
        renderer.saveProfile = function() {};
        renderer.restoreProfile = function() {};
        renderer.__nekoLinuxX11ProfileOptimized = true;
        this._linuxX11RendererProfileOptimized = true;
        console.log('[Live2D Core] Linux X11 renderer profile optimization enabled');
    }

    /**
     * 根据画质设置计算 Live2D 渲染分辨率。
     * 只调整 canvas 后备缓冲尺寸，不改模型贴图本体，避免破坏 Live2D 图集和裁剪蒙版。
     */
    _getRenderResolutionForQuality(quality) {
        quality = getEffectiveLive2DRenderQuality(quality);
        const deviceRatio = Math.max(1, window.devicePixelRatio || 1);
        if (quality === 'low') {
            return Math.max(0.75, Math.min(deviceRatio * 0.75, 1));
        }
        if (quality === 'high') {
            return deviceRatio;
        }
        return Math.max(1, Math.min(deviceRatio, 1.5));
    }

    /**
     * 应用 Live2D 画质设置。
     * PIXI 的 resize 使用逻辑尺寸，resolution 决定实际像素密度，因此不会改变模型坐标。
     */
    applyRenderQuality(quality) {
        if (!this.pixi_app || !this.pixi_app.renderer) return;
        const renderer = this.pixi_app.renderer;
        const effectiveQuality = getEffectiveLive2DRenderQuality(quality);
        const resolution = this._getRenderResolutionForQuality(effectiveQuality);
        if (Math.abs((renderer.resolution || 1) - resolution) < 0.001) return;

        const width = Math.max(renderer.screen?.width || window.innerWidth || window.screen.width || 1, 1);
        const height = Math.max(renderer.screen?.height || window.innerHeight || window.screen.height || 1, 1);
        renderer.resolution = resolution;
        renderer.resize(width, height);
        console.log('[Live2D Core] 画质已应用:', { quality: effectiveQuality, requestedQuality: quality, resolution, width, height });
    }

    recoverRendererFromReturnBallViewport(reason = 'manual') {
        if (!this.pixi_app || !this.pixi_app.renderer) return false;
        const renderer = this.pixi_app.renderer;
        const currentW = Math.max(window.innerWidth || 0, 0);
        const currentH = Math.max(window.innerHeight || 0, 0);
        if (isLive2DReturnBallViewportSize(currentW, currentH)) return false;
        if (!renderer.screen ||
            !isLive2DReturnBallViewportSize(renderer.screen.width, renderer.screen.height)) {
            return false;
        }
        const targetW = Math.max(currentW || window.screen.width || 1, 1);
        const targetH = Math.max(currentH || window.screen.height || 1, 1);
        renderer.resize(targetW, targetH);
        return true;
    }

    // 加载用户偏好
    async loadUserPreferences() {
        try {
            const response = await fetch('/api/config/preferences');
            if (response.ok) {
                return await response.json();
            }
        } catch (error) {
            console.warn('加载用户偏好失败:', error);
        }
        return [];
    }

    // 保存用户偏好
    async saveUserPreferences(modelPath, position, scale, parameters, display, viewport) {
        try {
            // 观看模式只读：viewer 不应把本地拖动覆盖到全局模型布局（也避免向 monitor 的只读端点 POST 触发 405）
            if (window.isViewerMode) {
                return false;
            }
            // 验证位置和缩放值是否为有效的有限数值
            if (!isValidModelPreferences(scale, position)) {
                console.error('位置或缩放值无效:', { scale, position });
                return false;
            }

            const preferences = {
                model_path: modelPath,
                position: position,
                scale: scale
            };

            // 如果有参数，添加到偏好中
            if (parameters && typeof parameters === 'object') {
                preferences.parameters = parameters;
            }

            // 如果有显示器信息，添加到偏好中（用于多屏幕位置恢复）
            if (display && typeof display === 'object' &&
                Number.isFinite(display.screenX) && Number.isFinite(display.screenY)) {
                preferences.display = {
                    screenX: display.screenX,
                    screenY: display.screenY
                };
            }

            // 如果有视口信息，添加到偏好中（用于跨分辨率位置和缩放归一化）
            if (viewport && typeof viewport === 'object' &&
                Number.isFinite(viewport.width) && Number.isFinite(viewport.height) &&
                viewport.width > 0 && viewport.height > 0) {
                preferences.viewport = {
                    width: viewport.width,
                    height: viewport.height
                };
            }

            const response = await fetch('/api/config/preferences', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(preferences)
            });
            const result = await response.json();
            return result.success;
        } catch (error) {
            console.error("保存偏好失败:", error);
            return false;
        }
    }

    // 随机选择数组中的一个元素
    getRandomElement(array) {
        if (!array || array.length === 0) return null;
        return array[Math.floor(Math.random() * array.length)];
    }

    // 解析资源相对路径（基于当前模型根目录）
    resolveAssetPath(relativePath) {
        if (!relativePath) return '';
        let rel = String(relativePath).replace(/^[\\/]+/, '');
        if (rel.startsWith('static/')) {
            return `/${rel}`;
        }
        if (rel.startsWith('/static/')) {
            return rel;
        }
        return `${this.modelRootPath}/${rel}`;
    }

    // 规范化资源路径，用于宽松比较（忽略斜杠差异与大小写）
    normalizeAssetPathForCompare(assetPath) {
        if (!assetPath) return '';
        const decoded = String(assetPath).trim();
        const unified = decoded.replace(/\\/g, '/').replace(/^\/+/, '').replace(/^\.\//, '');
        return unified.toLowerCase();
    }

    // 通过表达文件路径解析 expression name（兼容 "expressions/a.exp3.json" 与 "a.exp3.json"）
    resolveExpressionNameByFile(expressionFile) {
        const ref = this.resolveExpressionReferenceByFile(expressionFile);
        return ref ? ref.name : null;
    }

    normalizeExpressionFileKey(expressionFile) {
        if (!expressionFile || typeof expressionFile !== 'string') return '';
        return expressionFile.replace(/\\/g, '/').trim().toLowerCase();
    }

    markExpressionFileMissing(expressionFile) {
        const key = this.normalizeExpressionFileKey(expressionFile);
        if (!key) return;
        if (!this._missingExpressionFiles) this._missingExpressionFiles = new Set();
        this._missingExpressionFiles.add(key);
        const base = key.split('/').pop();
        if (base) this._missingExpressionFiles.add(base);
    }

    isExpressionFileMissing(expressionFile) {
        const key = this.normalizeExpressionFileKey(expressionFile);
        if (!key || !this._missingExpressionFiles) return false;
        if (this._missingExpressionFiles.has(key)) return true;
        const base = key.split('/').pop();
        return !!base && this._missingExpressionFiles.has(base);
    }

    clearMissingExpressionFiles() {
        if (this._missingExpressionFiles) this._missingExpressionFiles.clear();
    }

    // 通过 expression 文件路径解析出标准引用（Name + File）
    resolveExpressionReferenceByFile(expressionFile) {
        if (!expressionFile || !this.fileReferences || !Array.isArray(this.fileReferences.Expressions)) {
            return null;
        }

        const targetNorm = this.normalizeAssetPathForCompare(expressionFile);
        const targetBase = targetNorm.split('/').pop() || '';

        // 1) 优先精确匹配规范化后的 File 路径
        for (const expr of this.fileReferences.Expressions) {
            if (!expr || typeof expr !== 'object' || !expr.Name || !expr.File) continue;
            const fileNorm = this.normalizeAssetPathForCompare(expr.File);
            if (fileNorm === targetNorm) {
                return { name: expr.Name, file: expr.File };
            }
        }

        // 2) 兜底按文件名匹配（处理映射只给 basename 的情况）
        if (targetBase) {
            for (const expr of this.fileReferences.Expressions) {
                if (!expr || typeof expr !== 'object' || !expr.Name || !expr.File) continue;
                const fileBase = this.normalizeAssetPathForCompare(expr.File).split('/').pop() || '';
                if (fileBase === targetBase) {
                    return { name: expr.Name, file: expr.File };
                }
            }
        }

        return null;
    }

    // 获取当前模型
    getCurrentModel() {
        return this.currentModel;
    }

    // 获取当前情感映射
    getEmotionMapping() {
        return this.emotionMapping;
    }

    // 获取 PIXI 应用
    getPIXIApp() {
        return this.pixi_app;
    }

    _isFiniteMatrix2D(matrix) {
        return !!(matrix &&
            Number.isFinite(matrix.a) &&
            Number.isFinite(matrix.b) &&
            Number.isFinite(matrix.c) &&
            Number.isFinite(matrix.d) &&
            Number.isFinite(matrix.tx) &&
            Number.isFinite(matrix.ty));
    }

    _applyMatrixToPoint(matrix, x, y) {
        if (!this._isFiniteMatrix2D(matrix) || !Number.isFinite(x) || !Number.isFinite(y)) {
            return null;
        }

        return {
            x: matrix.a * x + matrix.c * y + matrix.tx,
            y: matrix.b * x + matrix.d * y + matrix.ty
        };
    }

    _ensureModelWorldTransform(model = this.currentModel) {
        if (!model) {
            return;
        }

        try {
            if (typeof model._recursivePostUpdateTransform === 'function') {
                model._recursivePostUpdateTransform();
            }

            if (typeof model.displayObjectUpdateTransform === 'function') {
                if (model.parent) {
                    model.displayObjectUpdateTransform();
                } else if (model._tempDisplayObjectParent) {
                    const originalParent = model.parent;
                    model.parent = model._tempDisplayObjectParent;
                    model.displayObjectUpdateTransform();
                    model.parent = originalParent || null;
                }
            }
        } catch (_) {}
    }

    _getDrawableVertexSequence(drawableIndex) {
        const internalModel = this.currentModel?.internalModel;
        if (!internalModel || typeof internalModel.getDrawableVertices !== 'function') {
            return null;
        }

        let vertices = null;
        try {
            vertices = internalModel.getDrawableVertices(drawableIndex);
        } catch (_) {
            return null;
        }

        return vertices && typeof vertices.length === 'number' && vertices.length >= 4
            ? vertices
            : null;
    }

    _getCubism2DrawableOpacity(internalModel, drawableIndex) {
        if (!Number.isInteger(internalModel?.drawDataCount)) {
            return null;
        }

        try {
            const modelContext = internalModel.coreModel?.getModelContext?.();
            const drawData = modelContext?.getDrawData?.(drawableIndex);
            // Cubism 2 stores the evaluated draw contexts in this runtime-owned
            // array. The bundled renderer composes these same three factors.
            const drawContext = modelContext?._$8b?.[drawableIndex];
            if (!drawData || !drawContext || typeof drawData.getOpacity !== 'function') {
                return null;
            }
            if (typeof drawContext._$yo === 'function' && drawContext._$yo() !== true) {
                return 0;
            }

            const drawableOpacity = drawData.getOpacity(modelContext, drawContext);
            const partContext = modelContext?._$Hr?.[drawContext._$IP];
            const parentOpacity = typeof partContext?.getPartsOpacity === 'function'
                ? partContext.getPartsOpacity()
                : drawContext._$VS;
            const baseOpacity = drawContext.baseOpacity;
            if (!Number.isFinite(drawableOpacity) ||
                !Number.isFinite(parentOpacity) ||
                !Number.isFinite(baseOpacity)) {
                return null;
            }
            return drawableOpacity * parentOpacity * baseOpacity;
        } catch (_) {
            return null;
        }
    }

    _isDrawableRenderable(coreModel, drawableIndex) {
        if (!coreModel || !Number.isInteger(drawableIndex) || drawableIndex < 0) {
            return false;
        }

        try {
            const visible = coreModel.getDrawableDynamicFlagIsVisible?.(drawableIndex);
            if (typeof visible === 'boolean' && !visible) {
                return false;
            }
        } catch (_) {}

        let opacity = null;
        try {
            opacity = coreModel.getDrawableOpacity?.(drawableIndex);
        } catch (_) {}
        if (!Number.isFinite(opacity)) {
            const internalModel = this.currentModel?.internalModel;
            opacity = this._getCubism2DrawableOpacity(internalModel, drawableIndex);
            if (Number.isInteger(internalModel?.drawDataCount) && !Number.isFinite(opacity)) {
                return false;
            }
        }
        if (Number.isFinite(opacity) && opacity <= 0.01) {
            return false;
        }

        return true;
    }

    _getDrawableLogicalRect(drawableIndex) {
        const internalModel = this.currentModel?.internalModel;
        if (!internalModel || typeof internalModel.getDrawableBounds !== 'function') {
            return null;
        }

        const rect = internalModel.getDrawableBounds(drawableIndex, {});
        if (!rect || !Number.isFinite(rect.x) || !Number.isFinite(rect.y) ||
            !Number.isFinite(rect.width) || !Number.isFinite(rect.height)) {
            return null;
        }

        return {
            x: rect.x,
            y: rect.y,
            width: Math.max(1, rect.width),
            height: Math.max(1, rect.height)
        };
    }

    _getDrawableDirectScreenRect(drawableIndex, skipTransformSync = false) {
        const model = this.currentModel;
        const internalModel = model?.internalModel;
        const vertices = this._getDrawableVertexSequence(drawableIndex);
        const localTransform = internalModel?.localTransform;
        const worldTransform = model?.worldTransform;
        if (!model || !internalModel || !vertices ||
            !this._isFiniteMatrix2D(localTransform) ||
            !this._isFiniteMatrix2D(worldTransform)) {
            return null;
        }

        if (!skipTransformSync) {
            this._ensureModelWorldTransform(model);
        }

        let minX = Infinity;
        let maxX = -Infinity;
        let minY = Infinity;
        let maxY = -Infinity;

        for (let index = 0; index < vertices.length; index += 2) {
            const vx = Number(vertices[index]);
            const vy = Number(vertices[index + 1]);
            const localPoint = this._applyMatrixToPoint(localTransform, vx, vy);
            const screenPoint = localPoint
                ? this._applyMatrixToPoint(worldTransform, localPoint.x, localPoint.y)
                : null;
            if (!screenPoint) {
                continue;
            }

            minX = Math.min(minX, screenPoint.x);
            maxX = Math.max(maxX, screenPoint.x);
            minY = Math.min(minY, screenPoint.y);
            maxY = Math.max(maxY, screenPoint.y);
        }

        return this._createScreenRect(minX, minY, maxX, maxY);
    }

    _getTransformedLogicalScreenRect(logicalRect, skipTransformSync = false) {
        const model = this.currentModel;
        if (!model || !logicalRect) {
            return null;
        }

        if (!skipTransformSync) {
            this._ensureModelWorldTransform(model);
        }

        const localTransform = model.internalModel?.localTransform;
        const worldTransform = model.worldTransform;
        const left = Number(logicalRect.x);
        const top = Number(logicalRect.y);
        const right = left + Number(logicalRect.width);
        const bottom = top + Number(logicalRect.height);
        if (!this._isFiniteMatrix2D(localTransform) ||
            !this._isFiniteMatrix2D(worldTransform) ||
            !Number.isFinite(left) || !Number.isFinite(top) ||
            !Number.isFinite(right) || !Number.isFinite(bottom)) {
            return null;
        }

        let minX = Infinity;
        let maxX = -Infinity;
        let minY = Infinity;
        let maxY = -Infinity;
        for (const [x, y] of [
            [left, top],
            [right, top],
            [left, bottom],
            [right, bottom]
        ]) {
            const localPoint = this._applyMatrixToPoint(localTransform, x, y);
            const screenPoint = localPoint
                ? this._applyMatrixToPoint(worldTransform, localPoint.x, localPoint.y)
                : null;
            if (!screenPoint) {
                continue;
            }
            minX = Math.min(minX, screenPoint.x);
            maxX = Math.max(maxX, screenPoint.x);
            minY = Math.min(minY, screenPoint.y);
            maxY = Math.max(maxY, screenPoint.y);
        }

        return this._createScreenRect(minX, minY, maxX, maxY);
    }

    _getDrawableCount(internalModel = this.currentModel?.internalModel) {
        const coreDrawableCount = internalModel?.coreModel?.getDrawableCount?.();
        return Number.isInteger(coreDrawableCount)
            ? coreDrawableCount
            : internalModel?.drawDataCount;
    }

    _getModelLogicalRect() {
        const internalModel = this.currentModel?.internalModel;
        const coreModel = internalModel?.coreModel;
        const drawableCount = this._getDrawableCount(internalModel);
        if (!internalModel || !coreModel || !Number.isInteger(drawableCount) || drawableCount <= 0) {
            return null;
        }
        let minX = Infinity;
        let maxX = -Infinity;
        let minY = Infinity;
        let maxY = -Infinity;

        for (let index = 0; index < drawableCount; index += 1) {
            const rect = this._getDrawableLogicalRect(index);
            if (!rect) continue;
            minX = Math.min(minX, rect.x);
            maxX = Math.max(maxX, rect.x + rect.width);
            minY = Math.min(minY, rect.y);
            maxY = Math.max(maxY, rect.y + rect.height);
        }

        if (!Number.isFinite(minX) || !Number.isFinite(maxX) ||
            !Number.isFinite(minY) || !Number.isFinite(maxY)) {
            return null;
        }

        return {
            x: minX,
            y: minY,
            width: Math.max(1, maxX - minX),
            height: Math.max(1, maxY - minY)
        };
    }

    _mapLogicalRectToScreen(logicalRect, modelLogicalRect, modelBounds) {
        if (!logicalRect || !modelLogicalRect || !modelBounds) {
            return null;
        }

        const logicalWidth = Math.max(1, modelLogicalRect.width);
        const logicalHeight = Math.max(1, modelLogicalRect.height);

        const relLeft = (logicalRect.x - modelLogicalRect.x) / logicalWidth;
        const relTop = (logicalRect.y - modelLogicalRect.y) / logicalHeight;
        const relWidth = logicalRect.width / logicalWidth;
        const relHeight = logicalRect.height / logicalHeight;

        return {
            left: modelBounds.left + modelBounds.width * relLeft,
            top: modelBounds.top + modelBounds.height * relTop,
            width: modelBounds.width * relWidth,
            height: modelBounds.height * relHeight
        };
    }

    _screenRectToLogical(screenRect, modelBounds, modelLogicalRect) {
        if (!screenRect || !modelBounds || !modelLogicalRect) { return null; }
        const bw = Math.max(1, modelBounds.width);
        const bh = Math.max(1, modelBounds.height);
        const lw = Math.max(1, modelLogicalRect.width);
        const lh = Math.max(1, modelLogicalRect.height);
        return {
            x: modelLogicalRect.x + (screenRect.left - modelBounds.left) / bw * lw,
            y: modelLogicalRect.y + (screenRect.top - modelBounds.top) / bh * lh,
            width: screenRect.width / bw * lw,
            height: screenRect.height / bh * lh
        };
    }

    _logicalRectToScreenRect(logicalRect, modelLogicalRect, modelBounds) {
        const mapped = this._mapLogicalRectToScreen(logicalRect, modelLogicalRect, modelBounds);
        if (!mapped) { return null; }
        return this._createScreenRect(mapped.left, mapped.top, mapped.left + mapped.width, mapped.top + mapped.height);
    }

    _screenPointToLogical(point, modelBounds, modelLogicalRect) {
        if (!point || !modelBounds || !modelLogicalRect) { return null; }
        const bw = Math.max(1, modelBounds.width);
        const bh = Math.max(1, modelBounds.height);
        return {
            x: modelLogicalRect.x + (point.x - modelBounds.left) / bw * modelLogicalRect.width,
            y: modelLogicalRect.y + (point.y - modelBounds.top) / bh * modelLogicalRect.height
        };
    }

    _logicalPointToScreen(logicalPoint, modelLogicalRect, modelBounds) {
        if (!logicalPoint || !modelLogicalRect || !modelBounds) { return null; }
        const lw = Math.max(1, modelLogicalRect.width);
        const lh = Math.max(1, modelLogicalRect.height);
        return {
            x: modelBounds.left + (logicalPoint.x - modelLogicalRect.x) / lw * modelBounds.width,
            y: modelBounds.top + (logicalPoint.y - modelLogicalRect.y) / lh * modelBounds.height
        };
    }

    _createScreenRect(left, top, right, bottom) {
        const width = right - left;
        const height = bottom - top;
        if (!Number.isFinite(left) || !Number.isFinite(top) ||
            !Number.isFinite(right) || !Number.isFinite(bottom) ||
            width <= 0 || height <= 0) {
            return null;
        }

        return {
            left,
            right,
            top,
            bottom,
            width,
            height,
            centerX: left + width * 0.5,
            centerY: top + height * 0.5
        };
    }

    _createRectInfoFromScreenRect(screenRect, mode, source = null) {
        if (!screenRect) {
            return null;
        }

        return {
            rect: {
                left: screenRect.left,
                right: screenRect.right,
                top: screenRect.top,
                bottom: screenRect.bottom,
                width: screenRect.width,
                height: screenRect.height,
                centerX: screenRect.centerX,
                centerY: screenRect.centerY
            },
            mode,
            source
        };
    }

    _cacheBubbleGeometryResult(result, modelBounds, modelLogicalRect) {
        if (!result || !result.reliableHeadRect || !modelBounds || !modelLogicalRect) { return; }
        const now = (typeof performance !== 'undefined' && typeof performance.now === 'function')
            ? performance.now()
            : Date.now();
        const settleRefreshMs = this._getBubbleGeometrySettleRefreshMs();
        const modelReadyAt = Number(this._bubbleGeometryModelReadyAt);
        const modelHasSettled = Number.isFinite(modelReadyAt) &&
            modelReadyAt > 0 &&
            now - modelReadyAt >= settleRefreshMs;
        const currentPass = Number.isInteger(this._bubbleGeometryRefreshPass)
            ? this._bubbleGeometryRefreshPass
            : 0;
        const refreshPass = modelHasSettled
            ? Math.max(currentPass, 1)
            : Math.max(currentPass, 0);
        this._bubbleGeometryRefreshPass = refreshPass;

        const headRect = result.headRect ? this._screenRectToLogical(result.headRect, modelBounds, modelLogicalRect) : null;
        const bubbleHeadRect = result.bubbleHeadRect ? this._screenRectToLogical(result.bubbleHeadRect, modelBounds, modelLogicalRect) : null;
        const bodyRect = result.bodyRect ? this._screenRectToLogical(result.bodyRect, modelBounds, modelLogicalRect) : null;
        const rawHeadAnchor = result.rawHeadAnchor ? this._screenPointToLogical(result.rawHeadAnchor, modelBounds, modelLogicalRect) : null;
        const headAnchor = result.headAnchor ? this._screenPointToLogical(result.headAnchor, modelBounds, modelLogicalRect) : null;
        if (!bubbleHeadRect && !headRect) { return; }
        this._bubbleGeometryCache = {
            modelPath: this.modelRootPath,
            modelLogicalRect: { x: modelLogicalRect.x, y: modelLogicalRect.y, width: modelLogicalRect.width, height: modelLogicalRect.height },
            headMode: result.headMode,
            headSource: result.headSource,
            bodySource: result.bodySource,
            reliableHeadRect: result.reliableHeadRect,
            preciseDisplayInfoRect: result.preciseDisplayInfoRect || false,
            coarseHitAreaHeadRect: result.coarseHitAreaHeadRect || false,
            cachedAtMs: now,
            refreshPass,
            overrideSignature: this._getBubbleGeometryOverrideSignature(),
            headRect,
            bubbleHeadRect,
            bodyRect,
            rawHeadAnchor,
            headAnchor
        };
    }

    _getCachedBubbleGeometryResult() {
        const cache = this._bubbleGeometryCache;
        if (!cache) { return null; }
        if (cache.modelPath !== this.modelRootPath) {
            this._bubbleGeometryCache = null;
            return null;
        }
        if (cache.overrideSignature !== this._getBubbleGeometryOverrideSignature()) {
            this._bubbleGeometryCache = null;
            return null;
        }
        const now = (typeof performance !== 'undefined' && typeof performance.now === 'function')
            ? performance.now()
            : Date.now();
        const settleRefreshMs = this._getBubbleGeometrySettleRefreshMs();
        const modelReadyAt = Number(this._bubbleGeometryModelReadyAt);
        const cacheNeedsSettleRefresh = Number(cache.refreshPass || 0) < 1 &&
            Number.isFinite(modelReadyAt) &&
            modelReadyAt > 0 &&
            now - modelReadyAt >= settleRefreshMs;
        if (cacheNeedsSettleRefresh) {
            this._bubbleGeometryCache = null;
            this._bubbleGeometryRefreshPass = 1;
            return null;
        }
        const bounds = this.getModelScreenBounds();
        if (!bounds) { return null; }
        const mlr = cache.modelLogicalRect;
        const headRect = cache.headRect ? this._logicalRectToScreenRect(cache.headRect, mlr, bounds) : null;
        const bubbleHeadRect = cache.bubbleHeadRect ? this._logicalRectToScreenRect(cache.bubbleHeadRect, mlr, bounds) : null;
        const bodyRect = cache.bodyRect ? this._logicalRectToScreenRect(cache.bodyRect, mlr, bounds) : null;
        const rawHeadAnchor = cache.rawHeadAnchor ? this._logicalPointToScreen(cache.rawHeadAnchor, mlr, bounds) : null;
        const headAnchor = cache.headAnchor ? this._logicalPointToScreen(cache.headAnchor, mlr, bounds) : null;
        return {
            bounds,
            rawHeadAnchor,
            headAnchor,
            headRect,
            bubbleHeadRect: bubbleHeadRect || headRect,
            headMode: cache.headMode,
            headSource: cache.headSource,
            bodyRect,
            bodySource: cache.bodySource,
            reliableHeadRect: cache.reliableHeadRect,
            preciseDisplayInfoRect: cache.preciseDisplayInfoRect,
            coarseHitAreaHeadRect: cache.coarseHitAreaHeadRect
        };
    }

    _getBubbleGeometrySettleRefreshMs() {
        const settleRefreshMs = Number(this._bubbleGeometrySettleRefreshMs);
        if (Number.isFinite(settleRefreshMs) && settleRefreshMs > 0) {
            return settleRefreshMs;
        }
        return LIVE2D_BUBBLE_GEOMETRY_SETTLE_REFRESH_MS;
    }

    _invalidateBubbleGeometryCache() {
        this._bubbleGeometryCache = null;
    }

    _getDrawableScreenRect(drawableIndex, modelLogicalRect = null, modelBounds = null, skipTransformSync = false) {
        const directScreenRect = this._getDrawableDirectScreenRect(drawableIndex, skipTransformSync);
        if (directScreenRect) {
            return directScreenRect;
        }

        const logicalRect = this._getDrawableLogicalRect(drawableIndex);
        const transformedLogicalRect = this._getTransformedLogicalScreenRect(logicalRect, skipTransformSync);
        if (transformedLogicalRect) {
            return transformedLogicalRect;
        }

        const resolvedModelLogicalRect = modelLogicalRect || this._getModelLogicalRect();
        const resolvedModelBounds = modelBounds || this.getModelScreenBounds();
        const mappedRect = this._mapLogicalRectToScreen(logicalRect, resolvedModelLogicalRect, resolvedModelBounds);
        if (!mappedRect) {
            return null;
        }

        return this._createScreenRect(
            mappedRect.left,
            mappedRect.top,
            mappedRect.left + mappedRect.width,
            mappedRect.top + mappedRect.height
        );
    }

    _mergeScreenRects(rects) {
        if (!Array.isArray(rects) || rects.length === 0) {
            return null;
        }

        let minX = Infinity;
        let maxX = -Infinity;
        let minY = Infinity;
        let maxY = -Infinity;

        for (const rect of rects) {
            if (!rect) continue;
            minX = Math.min(minX, rect.left);
            maxX = Math.max(maxX, rect.right);
            minY = Math.min(minY, rect.top);
            maxY = Math.max(maxY, rect.bottom);
        }

        if (!Number.isFinite(minX) || !Number.isFinite(maxX) ||
            !Number.isFinite(minY) || !Number.isFinite(maxY)) {
            return null;
        }

        return this._createScreenRect(minX, minY, maxX, maxY);
    }

    _getRenderableDrawableScreenRects(modelBounds = null, modelLogicalRect = null, includeIndex = false) {
        const internalModel = this.currentModel?.internalModel;
        const coreModel = internalModel?.coreModel;
        const drawableCount = this._getDrawableCount(internalModel);
        const resolvedModelBounds = modelBounds || this.getModelScreenBounds();
        const resolvedModelLogicalRect = modelLogicalRect || this._getModelLogicalRect();
        if (!internalModel || !coreModel || !Number.isInteger(drawableCount) || drawableCount <= 0) {
            return [];
        }

        this._ensureModelWorldTransform();

        const rects = [];
        for (let index = 0; index < drawableCount; index += 1) {
            if (!this._isDrawableRenderable(coreModel, index)) {
                continue;
            }

            const rect = this._getDrawableScreenRect(
                index,
                resolvedModelLogicalRect,
                resolvedModelBounds,
                true
            );
            if (rect) {
                rects.push(includeIndex ? { rect, index } : rect);
            }
        }

        return rects;
    }

    /**
     * 解析并限制 drawable 矩形的 padding。
     *
     * @param {number|string} rawPadding 原始 padding
     * @param {number} fallback 无效输入时的默认值
     * @returns {number} 0-32 范围内的 padding
     */
    _resolveRectPadding(rawPadding, fallback = 0) {
        const requestedPadding =
            (typeof rawPadding === 'number' ||
                (typeof rawPadding === 'string' && rawPadding.trim() !== ''))
                ? Number(rawPadding)
                : NaN;
        return Number.isFinite(requestedPadding)
            ? Math.max(0, Math.min(32, requestedPadding))
            : fallback;
    }

    /**
     * 获取当前实际可渲染 drawable 的未裁剪屏幕区域。
     *
     * 该公开入口仅供贴边拖拽链读取未裁剪画面几何。既有桌面输入区继续
     * 走 getModelInputRegionRects()，本功能不能改写普通点击穿透合同。
     */
    getModelDrawableScreenRects(options = {}, modelOverride = null) {
        const model = modelOverride || this.currentModel;
        if ((model && model.destroyed) ||
            (modelOverride && this.currentModel && modelOverride !== this.currentModel)) {
            return [];
        }

        const padding = this._resolveRectPadding(options?.padding, 0);
        const modelBounds = this._getUnclippedModelScreenBounds();
        const drawableRects = this._getRenderableDrawableScreenRects(
            modelBounds,
            null,
            false
        );
        if (!Array.isArray(drawableRects)) return [];

        return drawableRects.map((rect) => {
            if (!rect) return null;
            const left = Number(rect.left) - padding;
            const top = Number(rect.top) - padding;
            const right = Number(rect.right) + padding;
            const bottom = Number(rect.bottom) + padding;
            return this._createScreenRect(left, top, right, bottom);
        }).filter(Boolean);
    }

    /**
     * 获取当前实际可渲染 drawable 的屏幕输入区域。
     *
     * 桌面宿主使用这些区域构造 Native Wayland compositor input region。
     * 这里必须先从 drawable 顶点得到屏幕几何，再裁剪到 renderer viewport；
     * 不能从已裁剪的模型外接矩形重新猜一个居中椭圆，否则靠边模型会出现
     * “透明处可点击、人物本体穿透”。
     *
     * @param {Object} options
     * @param {number} options.padding 每个 drawable 周围的拖拽容差（CSS 像素）
     * @returns {Array<Object>} 屏幕坐标矩形
     */
    getModelInputRegionRects(options = {}) {
        if (!this._isModelInputRegionInteractive()) {
            return [];
        }

        const edgePeekState = this._live2DPeekState;
        if (edgePeekState && edgePeekState.active &&
            (edgePeekState.phase === 'hidden' || edgePeekState.phase === 'hiding')) {
            return [];
        }

        const padding = this._resolveRectPadding(options?.padding, 8);
        // Drawable vertices already carry their real screen coordinates. When
        // vertices are temporarily unavailable, logical fallback mapping must
        // use the model's full (unclipped) bounds as its scale basis. Edge peek
        // deliberately makes getModelScreenBounds() return only the visible
        // viewport intersection, which would otherwise compress every fallback
        // drawable into the narrow revealed strip.
        const modelBounds = this._getUnclippedModelScreenBounds();
        const drawableRects = this._getRenderableDrawableScreenRects(
            modelBounds,
            null,
            false
        );
        if (!Array.isArray(drawableRects) || drawableRects.length === 0) {
            return [];
        }

        const rendererScreen = this.pixi_app?.renderer?.screen;
        const rendererWidth = Number(rendererScreen?.width);
        const rendererHeight = Number(rendererScreen?.height);
        const windowWidth = Number(window.innerWidth);
        const windowHeight = Number(window.innerHeight);
        const viewportWidth = Number.isFinite(rendererWidth) && rendererWidth > 0
            ? Math.min(rendererWidth, Number.isFinite(windowWidth) && windowWidth > 0 ? windowWidth : rendererWidth)
            : windowWidth;
        const viewportHeight = Number.isFinite(rendererHeight) && rendererHeight > 0
            ? Math.min(rendererHeight, Number.isFinite(windowHeight) && windowHeight > 0 ? windowHeight : rendererHeight)
            : windowHeight;
        if (!Number.isFinite(viewportWidth) || viewportWidth <= 0 ||
            !Number.isFinite(viewportHeight) || viewportHeight <= 0) {
            return [];
        }

        const inputRects = [];
        for (const drawableRect of drawableRects) {
            if (!drawableRect) continue;
            const rawLeft = Number(drawableRect.left);
            const rawTop = Number(drawableRect.top);
            const rawRight = Number(drawableRect.right);
            const rawBottom = Number(drawableRect.bottom);
            if (!Number.isFinite(rawLeft) || !Number.isFinite(rawTop) ||
                !Number.isFinite(rawRight) || !Number.isFinite(rawBottom)) {
                continue;
            }

            const left = Math.max(0, rawLeft - padding);
            const top = Math.max(0, rawTop - padding);
            const right = Math.min(viewportWidth, rawRight + padding);
            const bottom = Math.min(viewportHeight, rawBottom + padding);
            const rect = this._createScreenRect(left, top, right, bottom);
            if (rect) inputRects.push(rect);
        }
        return inputRects;
    }

    _expandScreenRect(rect, paddingX = 0, paddingY = 0) {
        if (!rect) {
            return null;
        }

        return this._createScreenRect(
            rect.left - paddingX,
            rect.top - paddingY,
            rect.right + paddingX,
            rect.bottom + paddingY
        );
    }

    _getScreenRectArea(rect) {
        if (!rect) {
            return 0;
        }

        const width = Number(rect.width);
        const height = Number(rect.height);
        if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
            return 0;
        }

        return width * height;
    }

    _getWeightedValueQuantile(samples, quantile = 0.5) {
        if (!Array.isArray(samples) || samples.length === 0) {
            return null;
        }

        const resolvedQuantile = Math.max(0, Math.min(1, Number.isFinite(quantile) ? quantile : 0.5));
        const validSamples = samples
            .map((sample) => ({
                value: Number(sample?.value),
                weight: Number(sample?.weight)
            }))
            .filter((sample) => Number.isFinite(sample.value) && Number.isFinite(sample.weight) && sample.weight > 0)
            .sort((left, right) => left.value - right.value);
        if (validSamples.length === 0) {
            return null;
        }

        const totalWeight = validSamples.reduce((sum, sample) => sum + sample.weight, 0);
        if (!(totalWeight > 0)) {
            return validSamples[Math.min(
                validSamples.length - 1,
                Math.max(0, Math.round((validSamples.length - 1) * resolvedQuantile))
            )].value;
        }

        const targetWeight = totalWeight * resolvedQuantile;
        let accumulatedWeight = 0;
        for (const sample of validSamples) {
            accumulatedWeight += sample.weight;
            if (accumulatedWeight >= targetWeight) {
                return sample.value;
            }
        }

        return validSamples[validSamples.length - 1].value;
    }

    _createContributorCoreScreenRect(contributors, quantiles = {}) {
        if (!Array.isArray(contributors) || contributors.length === 0) {
            return null;
        }

        const resolvedContributors = contributors
            .map((contributor) => {
                const rect = contributor?.rect || contributor;
                const area = this._getScreenRectArea(rect);
                const scoreWeight = Number.isFinite(contributor?.score) ? contributor.score : 1;
                const weight = area * Math.max(0.25, scoreWeight);
                return rect && weight > 0
                    ? { rect, weight }
                    : null;
            })
            .filter(Boolean);
        if (resolvedContributors.length === 0) {
            return null;
        }

        const left = this._getWeightedValueQuantile(
            resolvedContributors.map((contributor) => ({
                value: contributor.rect.left,
                weight: contributor.weight
            })),
            quantiles.left ?? 0.1
        );
        const top = this._getWeightedValueQuantile(
            resolvedContributors.map((contributor) => ({
                value: contributor.rect.top,
                weight: contributor.weight
            })),
            quantiles.top ?? 0.1
        );
        const right = this._getWeightedValueQuantile(
            resolvedContributors.map((contributor) => ({
                value: contributor.rect.right,
                weight: contributor.weight
            })),
            quantiles.right ?? 0.9
        );
        const bottom = this._getWeightedValueQuantile(
            resolvedContributors.map((contributor) => ({
                value: contributor.rect.bottom,
                weight: contributor.weight
            })),
            quantiles.bottom ?? 0.9
        );

        return this._createScreenRect(left, top, right, bottom);
    }

    _extractDrawableHeadContributorCoreScreenRect(rect, modelBounds, bodyRectHint, contributors = null) {
        const bodyRect = bodyRectHint?.rect || bodyRectHint;
        if (!rect || !modelBounds || !bodyRect ||
            !Array.isArray(contributors) || contributors.length < 6) {
            return rect;
        }

        const headLooksAccessoryInflated = rect.width >= modelBounds.width * 0.42 &&
            rect.height >= modelBounds.height * 0.2 &&
            rect.top <= modelBounds.top + modelBounds.height * 0.18 &&
            rect.bottom <= modelBounds.top + modelBounds.height * 0.58 &&
            (rect.width / Math.max(1, rect.height)) >= 1.32 &&
            Math.abs(rect.centerX - bodyRect.centerX) >= Math.max(40, modelBounds.width * 0.08) &&
            (
                bodyRect.top >= rect.top + rect.height * 0.38 ||
                (bodyRect.width >= modelBounds.width * 0.68 &&
                    bodyRect.height >= modelBounds.height * 0.68)
            );
        if (!headLooksAccessoryInflated) {
            return rect;
        }

        const contributorCoreRect = this._createContributorCoreScreenRect(contributors, {
            // Later left trimming helps reject tiny top-left accessories/hair
            // fragments without disturbing the main head mass.
            left: 0.18,
            top: 0.1,
            right: 0.94,
            bottom: 0.96
        });
        if (!contributorCoreRect) {
            return rect;
        }

        const widthRatio = contributorCoreRect.width / Math.max(1, rect.width);
        const heightRatio = contributorCoreRect.height / Math.max(1, rect.height);
        const bodyDeltaBefore = Math.abs(rect.centerX - bodyRect.centerX);
        const bodyDeltaAfter = Math.abs(contributorCoreRect.centerX - bodyRect.centerX);
        const staysInUpperHeadBand = contributorCoreRect.top <= modelBounds.top + modelBounds.height * 0.24 &&
            contributorCoreRect.bottom <= modelBounds.top + modelBounds.height * 0.62;
        if (!staysInUpperHeadBand ||
            widthRatio < 0.6 ||
            heightRatio < 0.68 ||
            bodyDeltaAfter > bodyDeltaBefore * 0.86) {
            return rect;
        }

        return contributorCoreRect;
    }

    _extractDrawableBodyContributorCoreScreenRect(rect, modelBounds, headRectHint, contributors = null) {
        const headRect = headRectHint?.rect || headRectHint;
        if (!rect || !modelBounds || !headRect ||
            !Array.isArray(contributors) || contributors.length < 9) {
            return rect;
        }

        const bodyLooksAccessoryInflated = rect.width >= modelBounds.width * 0.72 &&
            rect.height >= modelBounds.height * 0.48 &&
            rect.width >= headRect.width * 1.48 &&
            rect.top <= headRect.bottom + Math.max(36, headRect.height * 0.42);
        if (!bodyLooksAccessoryInflated) {
            return rect;
        }

        const contributorCoreRect = this._createContributorCoreScreenRect(contributors, {
            left: 0.12,
            top: 0.08,
            right: 0.93,
            bottom: 0.96
        });
        if (!contributorCoreRect) {
            return rect;
        }

        const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
        const minWidth = Math.max(headRect.width * 1.18, modelBounds.width * 0.24);
        const minHeight = Math.max(headRect.height * 1.15, modelBounds.height * 0.24);
        let left = Math.max(rect.left, contributorCoreRect.left);
        let right = Math.min(rect.right, contributorCoreRect.right);
        let bottom = Math.min(rect.bottom, Math.max(rect.top + minHeight, contributorCoreRect.bottom));

        if (right - left < minWidth) {
            const preferredCenterX = Number.isFinite(contributorCoreRect.centerX)
                ? contributorCoreRect.centerX
                : rect.centerX;
            left = clamp(preferredCenterX - minWidth * 0.5, rect.left, rect.right - minWidth);
            right = left + minWidth;
        }

        if (bottom - rect.top < minHeight) {
            bottom = Math.min(rect.bottom, rect.top + minHeight);
        }

        const normalizedRect = this._createScreenRect(
            left,
            rect.top,
            right,
            bottom
        );
        if (!normalizedRect) {
            return rect;
        }

        const widthRatio = normalizedRect.width / Math.max(1, rect.width);
        const heightRatio = normalizedRect.height / Math.max(1, rect.height);
        if (widthRatio < 0.55 || heightRatio < 0.68) {
            return rect;
        }

        return normalizedRect;
    }

    _shouldIgnoreBodyRectHintForHeadNormalization(rect, modelBounds, bodyRectHint) {
        if (!rect || !modelBounds || !bodyRectHint) {
            return false;
        }

        const bodyStartsNearHeadTop = bodyRectHint.top <= rect.top + Math.max(24, rect.height * 0.16);
        const bodyHintCoversMostModel = bodyRectHint.width >= modelBounds.width * 0.68 &&
            bodyRectHint.height >= modelBounds.height * 0.68;
        const headClusterAlreadyLarge = rect.width >= modelBounds.width * 0.34 &&
            rect.height >= modelBounds.height * 0.24;
        const headClusterLivesInUpperBand = rect.top <= modelBounds.top + modelBounds.height * 0.16 &&
            rect.bottom <= modelBounds.top + modelBounds.height * 0.54;
        const bodyHintIsMuchLargerThanHead = bodyRectHint.width >= rect.width * 1.45 &&
            bodyRectHint.height >= rect.height * 1.6;

        return bodyStartsNearHeadTop &&
            bodyHintCoversMostModel &&
            headClusterAlreadyLarge &&
            headClusterLivesInUpperBand &&
            bodyHintIsMuchLargerThanHead;
    }

    _normalizeDrawableHeadScreenRect(rect, modelBounds, bodyRectHint = null, headRectHint = null, contributors = null) {
        if (!rect || !modelBounds) {
            return rect;
        }

        const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
        const effectiveBodyRectHint = this._shouldIgnoreBodyRectHintForHeadNormalization(
            rect,
            modelBounds,
            bodyRectHint
        )
            ? null
            : bodyRectHint;
        let normalizedRect = rect;

        let bottomCap = modelBounds.top + modelBounds.height * 0.56;
        if (effectiveBodyRectHint) {
            bottomCap = Math.min(bottomCap, effectiveBodyRectHint.top + effectiveBodyRectHint.height * 0.36);
        }

        const minHeadHeight = Math.max(24, modelBounds.height * 0.08);
        if (normalizedRect.bottom > bottomCap && bottomCap > normalizedRect.top + minHeadHeight) {
            normalizedRect = this._createScreenRect(
                normalizedRect.left,
                normalizedRect.top,
                normalizedRect.right,
                bottomCap
            ) || normalizedRect;
        }

        const finalWidthRatio = normalizedRect.width / Math.max(1, modelBounds.width);
        const finalHeightRatio = normalizedRect.height / Math.max(1, modelBounds.height);
        const finalAspectRatio = normalizedRect.width / Math.max(1, normalizedRect.height);
        const stillLooksLikeWideBand = finalWidthRatio >= 0.44 &&
            finalHeightRatio <= 0.24 &&
            finalAspectRatio >= 2.4;
        if (stillLooksLikeWideBand && effectiveBodyRectHint) {
            const normalizedHeight = clamp(
                Math.max(
                    normalizedRect.height,
                    effectiveBodyRectHint.height * 0.18
                ),
                Math.max(32, modelBounds.height * 0.1),
                effectiveBodyRectHint.height * 0.34
            );
            const normalizedWidth = clamp(
                Math.max(
                    normalizedHeight * 1.05,
                    effectiveBodyRectHint.width * 0.26
                ),
                Math.max(56, modelBounds.width * 0.14),
                effectiveBodyRectHint.width * 0.42
            );
            const clampNormalizedCenterX = (value) => clamp(
                value,
                effectiveBodyRectHint.left + normalizedWidth * 0.5,
                effectiveBodyRectHint.right - normalizedWidth * 0.5
            );
            const hintedCenterX = Number.isFinite(headRectHint?.centerX)
                ? clampNormalizedCenterX(headRectHint.centerX)
                : null;
            const bodyBiasThreshold = modelBounds.width * 0.04;
            const bodyBias = effectiveBodyRectHint.centerX >= (
                (Number.isFinite(modelBounds.centerX) ? modelBounds.centerX : modelBounds.left + modelBounds.width * 0.5) +
                bodyBiasThreshold
            )
                ? 'right'
                : effectiveBodyRectHint.centerX <= (
                    (Number.isFinite(modelBounds.centerX) ? modelBounds.centerX : modelBounds.left + modelBounds.width * 0.5) -
                    bodyBiasThreshold
                )
                    ? 'left'
                    : 'center';
            let normalizedCenterX = clampNormalizedCenterX(normalizedRect.centerX);
            const shouldUseHeadHint = Number.isFinite(hintedCenterX) &&
                Math.abs(hintedCenterX - normalizedRect.centerX) >= normalizedWidth * 0.38;
            if (shouldUseHeadHint) {
                normalizedCenterX = hintedCenterX;
            } else if (bodyBias === 'right') {
                normalizedCenterX = clampNormalizedCenterX(
                    Math.min(
                        normalizedRect.right - normalizedWidth * 0.45,
                        effectiveBodyRectHint.right - normalizedWidth * 0.48
                    )
                );
            } else if (bodyBias === 'left') {
                normalizedCenterX = clampNormalizedCenterX(
                    Math.max(
                        normalizedRect.left + normalizedWidth * 0.45,
                        effectiveBodyRectHint.left + normalizedWidth * 0.48
                    )
                );
            }

            const normalizedTop = Math.min(
                normalizedRect.top,
                effectiveBodyRectHint.top + effectiveBodyRectHint.height * 0.16
            );
            normalizedRect = this._createScreenRect(
                normalizedCenterX - normalizedWidth * 0.5,
                normalizedTop,
                normalizedCenterX + normalizedWidth * 0.5,
                normalizedTop + normalizedHeight
            ) || normalizedRect;
        }

        if (effectiveBodyRectHint) {
            const bodyWidthRatio = normalizedRect.width / Math.max(1, effectiveBodyRectHint.width);
            const bodyHeightRatio = normalizedRect.height / Math.max(1, effectiveBodyRectHint.height);
            const bodyBottomProgress = (normalizedRect.bottom - effectiveBodyRectHint.top) / Math.max(1, effectiveBodyRectHint.height);
            const boundsWidthRatio = normalizedRect.width / Math.max(1, modelBounds.width);
            const aspectRatio = normalizedRect.width / Math.max(1, normalizedRect.height);
            const looksLikeOversizedBodySlice = bodyBottomProgress >= 0.3 && (
                bodyWidthRatio >= 0.56 ||
                bodyHeightRatio >= 0.38 ||
                boundsWidthRatio >= 0.46 ||
                (aspectRatio >= 1.55 && bodyWidthRatio >= 0.44)
            );

            if (looksLikeOversizedBodySlice) {
                const minNormalizedHeight = Math.max(
                    64,
                    modelBounds.height * 0.1,
                    effectiveBodyRectHint.height * 0.18
                );
                const maxNormalizedHeight = Math.max(
                    minNormalizedHeight + 8,
                    effectiveBodyRectHint.height * 0.32
                );
                const normalizedHeight = clamp(
                    Math.max(
                        effectiveBodyRectHint.height * 0.2,
                        normalizedRect.height * 0.58
                    ),
                    minNormalizedHeight,
                    maxNormalizedHeight
                );
                const minNormalizedWidth = Math.max(
                    76,
                    modelBounds.width * 0.12,
                    effectiveBodyRectHint.width * 0.22
                );
                const maxNormalizedWidth = Math.max(
                    minNormalizedWidth + 12,
                    effectiveBodyRectHint.width * 0.44
                );
                let normalizedWidth = Math.max(
                    effectiveBodyRectHint.width * 0.28,
                    normalizedRect.width * 0.4,
                    normalizedHeight * 0.82
                );
                if (aspectRatio >= 1.55) {
                    normalizedWidth = Math.min(normalizedWidth, normalizedHeight * 1.22);
                }
                normalizedWidth = clamp(
                    normalizedWidth,
                    minNormalizedWidth,
                    maxNormalizedWidth
                );

                const normalizedCenterX = clamp(
                    Number.isFinite(headRectHint?.centerX) ? headRectHint.centerX : normalizedRect.centerX,
                    effectiveBodyRectHint.left + normalizedWidth * 0.5,
                    effectiveBodyRectHint.right - normalizedWidth * 0.5
                );
                const normalizedTop = clamp(
                    Math.min(
                        normalizedRect.top,
                        effectiveBodyRectHint.top + effectiveBodyRectHint.height * 0.08
                    ),
                    modelBounds.top,
                    effectiveBodyRectHint.top + effectiveBodyRectHint.height * 0.14
                );

                normalizedRect = this._createScreenRect(
                    normalizedCenterX - normalizedWidth * 0.5,
                    normalizedTop,
                    normalizedCenterX + normalizedWidth * 0.5,
                    normalizedTop + normalizedHeight
                ) || normalizedRect;
            }
        }

        const looksLikeTinyFragment = effectiveBodyRectHint &&
            Number.isFinite(headRectHint?.centerX) &&
            Number.isFinite(headRectHint?.centerY) &&
            (
                normalizedRect.width <= Math.max(40, effectiveBodyRectHint.width * 0.14) ||
                normalizedRect.height <= Math.max(40, effectiveBodyRectHint.height * 0.14)
            ) &&
            headRectHint.centerY >= normalizedRect.bottom + Math.max(28, normalizedRect.height * 0.55);
        if (looksLikeTinyFragment) {
            const normalizedHeight = clamp(
                Math.max(
                    normalizedRect.height * 2.2,
                    effectiveBodyRectHint.height * 0.18
                ),
                Math.max(56, modelBounds.height * 0.11),
                effectiveBodyRectHint.height * 0.32
            );
            const normalizedWidth = clamp(
                Math.max(
                    normalizedHeight * 0.9,
                    normalizedRect.width * 2.4,
                    effectiveBodyRectHint.width * 0.16
                ),
                Math.max(64, modelBounds.width * 0.12),
                effectiveBodyRectHint.width * 0.28
            );
            const normalizedCenterX = clamp(
                headRectHint.centerX,
                effectiveBodyRectHint.left + normalizedWidth * 0.5,
                effectiveBodyRectHint.right - normalizedWidth * 0.5
            );
            const normalizedCenterY = clamp(
                headRectHint.centerY - normalizedHeight * 0.18,
                modelBounds.top + normalizedHeight * 0.5,
                modelBounds.bottom - normalizedHeight * 0.5
            );
            normalizedRect = this._createScreenRect(
                normalizedCenterX - normalizedWidth * 0.5,
                normalizedCenterY - normalizedHeight * 0.5,
                normalizedCenterX + normalizedWidth * 0.5,
                normalizedCenterY + normalizedHeight * 0.5
            ) || normalizedRect;
        }

        const looksLikeTinyFragmentWithoutHeadHint = effectiveBodyRectHint &&
            !Number.isFinite(headRectHint?.centerX) &&
            !Number.isFinite(headRectHint?.centerY) &&
            (
                normalizedRect.width <= Math.max(56, effectiveBodyRectHint.width * 0.18) ||
                normalizedRect.height <= Math.max(56, effectiveBodyRectHint.height * 0.18)
            ) &&
            normalizedRect.centerY <= effectiveBodyRectHint.top + effectiveBodyRectHint.height * 0.14;
        if (looksLikeTinyFragmentWithoutHeadHint) {
            const normalizedHeight = clamp(
                Math.max(
                    normalizedRect.height * 1.7,
                    effectiveBodyRectHint.height * 0.18
                ),
                Math.max(64, modelBounds.height * 0.1),
                effectiveBodyRectHint.height * 0.34
            );
            const normalizedWidth = clamp(
                Math.max(
                    normalizedHeight * 0.88,
                    normalizedRect.width * 1.9,
                    effectiveBodyRectHint.width * 0.16
                ),
                Math.max(72, modelBounds.width * 0.12),
                effectiveBodyRectHint.width * 0.32
            );
            const blendedCenterX = normalizedRect.centerX * 0.35 + effectiveBodyRectHint.centerX * 0.65;
            const normalizedCenterX = clamp(
                blendedCenterX,
                effectiveBodyRectHint.left + normalizedWidth * 0.5,
                effectiveBodyRectHint.right - normalizedWidth * 0.5
            );
            const normalizedTop = clamp(
                effectiveBodyRectHint.top - normalizedHeight * 0.78,
                modelBounds.top,
                effectiveBodyRectHint.top + effectiveBodyRectHint.height * 0.12
            );
            normalizedRect = this._createScreenRect(
                normalizedCenterX - normalizedWidth * 0.5,
                normalizedTop,
                normalizedCenterX + normalizedWidth * 0.5,
                normalizedTop + normalizedHeight
            ) || normalizedRect;
        }

        normalizedRect = this._extractDrawableHeadContributorCoreScreenRect(
            normalizedRect,
            modelBounds,
            bodyRectHint || effectiveBodyRectHint,
            contributors
        );

        return normalizedRect;
    }

    _normalizeDrawableBodyScreenRect(rect, modelBounds, headRectHint = null, contributors = null) {
        if (!rect || !modelBounds || !headRectHint) {
            return rect;
        }

        const headRect = headRectHint?.rect || headRectHint;
        if (!headRect) {
            return rect;
        }

        const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
        let normalizedRect = rect;
        const bodyStartsInsideHead = rect.top <= headRect.top + Math.max(24, headRect.height * 0.18);
        const bodySpansMostOfModel = rect.width >= modelBounds.width * 0.58 &&
            rect.height >= modelBounds.height * 0.62;
        const bodyClearlyLargerThanHead = rect.width >= headRect.width * 1.18 &&
            rect.height >= headRect.height * 1.45;

        if (bodyStartsInsideHead && bodySpansMostOfModel && bodyClearlyLargerThanHead) {
            const minHeight = Math.max(72, rect.height * 0.24, headRect.height * 0.9);
            const nextTop = clamp(
                headRect.top + headRect.height * 0.58,
                rect.top,
                rect.bottom - minHeight
            );

            if (nextTop > rect.top + Math.max(20, headRect.height * 0.12)) {
                normalizedRect = this._createScreenRect(
                    rect.left,
                    nextTop,
                    rect.right,
                    rect.bottom
                ) || rect;
            }
        }

        normalizedRect = this._extractDrawableBodyContributorCoreScreenRect(
            normalizedRect,
            modelBounds,
            headRect,
            contributors
        );

        return normalizedRect;
    }

    _inferDrawableRegionScreenRectInfo(kind, modelBounds = null, modelLogicalRect = null, bodyRectHint = null, headRectHint = null) {
        const resolvedModelBounds = modelBounds || this.getModelScreenBounds();
        const resolvedModelLogicalRect = modelLogicalRect || this._getModelLogicalRect();
        const drawableEntries = this._getRenderableDrawableScreenRects(
            resolvedModelBounds,
            resolvedModelLogicalRect,
            true
        );
        if (!resolvedModelBounds || drawableEntries.length === 0) {
            return null;
        }

        const boundsCenterX = Number.isFinite(resolvedModelBounds.centerX)
            ? resolvedModelBounds.centerX
            : resolvedModelBounds.left + resolvedModelBounds.width * 0.5;
        const modelArea = Math.max(1, Number(resolvedModelBounds.width) * Number(resolvedModelBounds.height));
        const targetRect = kind === 'head'
            ? this._createScreenRect(
                boundsCenterX - resolvedModelBounds.width * 0.34,
                resolvedModelBounds.top - resolvedModelBounds.height * 0.02,
                boundsCenterX + resolvedModelBounds.width * 0.34,
                resolvedModelBounds.top + resolvedModelBounds.height * 0.52
            )
            : this._createScreenRect(
                boundsCenterX - resolvedModelBounds.width * 0.38,
                resolvedModelBounds.top + resolvedModelBounds.height * 0.16,
                boundsCenterX + resolvedModelBounds.width * 0.38,
                resolvedModelBounds.top + resolvedModelBounds.height * 0.88
            );
        if (!targetRect) {
            return null;
        }
        const rectArea = (rect) => Math.max(1, Number(rect?.width) * Number(rect?.height));
        const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
        const clamp01 = (value) => clamp(value, 0, 1);

        const candidates = drawableEntries
            .map((entry) => {
                const rect = entry?.rect || entry;
                if (!rect) {
                    return null;
                }
                const drawableIndex = Number.isInteger(entry?.index) ? entry.index : null;
                const area = rectArea(rect);
                const overlapArea = this._getRectIntersectionArea(rect, targetRect);
                const overlapRatio = overlapArea / area;
                const widthRatio = rect.width / Math.max(1, resolvedModelBounds.width);
                const heightRatio = rect.height / Math.max(1, resolvedModelBounds.height);
                const aspectRatio = rect.width / Math.max(1, rect.height);
                const centerBias = clamp01(1 - Math.abs(rect.centerX - boundsCenterX) / Math.max(1, resolvedModelBounds.width * 0.48));
                const verticalTargetY = kind === 'head'
                    ? resolvedModelBounds.top + resolvedModelBounds.height * 0.24
                    : resolvedModelBounds.top + resolvedModelBounds.height * 0.53;
                const verticalBand = kind === 'head'
                    ? resolvedModelBounds.height * 0.26
                    : resolvedModelBounds.height * 0.34;
                const verticalBias = clamp01(1 - Math.abs(rect.centerY - verticalTargetY) / Math.max(1, verticalBand));
                const areaRatio = area / modelArea;

                if (kind === 'head') {
                    const wideShallowBand = widthRatio >= 0.44 &&
                        heightRatio <= 0.24 &&
                        aspectRatio >= 2.4;
                    if (areaRatio < 0.001 || areaRatio > 0.26 ||
                        overlapRatio < 0.16 ||
                        rect.top > resolvedModelBounds.top + resolvedModelBounds.height * 0.36 ||
                        rect.centerY > resolvedModelBounds.top + resolvedModelBounds.height * 0.54 ||
                        rect.bottom > resolvedModelBounds.top + resolvedModelBounds.height * 0.68 ||
                        rect.height > resolvedModelBounds.height * 0.44 ||
                        rect.width > resolvedModelBounds.width * 0.78 ||
                        wideShallowBand) {
                        return null;
                    }
                } else if (areaRatio < 0.002 || areaRatio > 0.7 ||
                    overlapRatio < 0.08 ||
                    rect.centerY < resolvedModelBounds.top + resolvedModelBounds.height * 0.22 ||
                    rect.centerY > resolvedModelBounds.top + resolvedModelBounds.height * 0.88 ||
                    rect.height > resolvedModelBounds.height * 0.82) {
                    return null;
                }

                const widthBias = kind === 'head'
                    ? clamp01(1 - Math.max(0, widthRatio - 0.32) / 0.34)
                    : 1;
                const aspectBias = kind === 'head'
                    ? clamp01(1 - Math.max(0, aspectRatio - 1.9) / 1.4)
                    : 1;

                let score = overlapRatio * 4.2 +
                    centerBias * 1.8 +
                    verticalBias * 1.9 +
                    widthBias * (kind === 'head' ? 1.4 : 0) +
                    aspectBias * (kind === 'head' ? 1.3 : 0);

                return { rect, score, drawableIndex };
            })
            .filter(Boolean);

        candidates.sort((left, right) => right.score - left.score);
        if (candidates.length === 0) {
            return null;
        }

        let orderedCandidates = candidates;
        if (kind === 'head' && candidates.length > 1) {
            const boundsTop = resolvedModelBounds.top;
            const boundsHeight = Math.max(1, resolvedModelBounds.height);
            const bestScore = Math.max(0.01, candidates[0].score);
            const isPrimaryHeadBand = (candidateRect) =>
                candidateRect.centerY >= boundsTop + boundsHeight * 0.14 &&
                candidateRect.centerY <= boundsTop + boundsHeight * 0.62;

            let stableAnchor = null;
            let stableAnchorScore = -Infinity;

            const topCandidate = candidates[0];
            const topAreaRatio = rectArea(topCandidate.rect) / modelArea;
            const topLooksLikeAccessoryFragment = topCandidate.rect.centerY <= boundsTop + boundsHeight * 0.2 &&
                topCandidate.rect.height <= boundsHeight * 0.2 &&
                topCandidate.rect.width <= resolvedModelBounds.width * 0.38;
            const topLooksLikeTinyFragment = topAreaRatio <= 0.0105 ||
                (topCandidate.rect.width <= resolvedModelBounds.width * 0.078 &&
                    topCandidate.rect.height <= resolvedModelBounds.height * 0.22) ||
                topCandidate.rect.height <= resolvedModelBounds.height * 0.075;

            if (!stableAnchor && (topLooksLikeAccessoryFragment || topLooksLikeTinyFragment)) {
                const minStableAreaRatio = Math.max(0.003, topAreaRatio * 1.8);
                for (const candidate of candidates) {
                    if (!isPrimaryHeadBand(candidate.rect) || candidate.score < bestScore * 0.84) {
                        continue;
                    }

                    const candidateAreaRatio = rectArea(candidate.rect) / modelArea;
                    if (candidateAreaRatio < minStableAreaRatio) {
                        continue;
                    }

                    const areaSupport = clamp01((candidateAreaRatio - minStableAreaRatio) / 0.042);
                    const candidateStabilityScore = candidate.score + areaSupport * 0.85;
                    if (!stableAnchor ||
                        candidateStabilityScore > stableAnchorScore ||
                        (candidateStabilityScore === stableAnchorScore &&
                            rectArea(candidate.rect) > rectArea(stableAnchor.rect))) {
                        stableAnchor = candidate;
                        stableAnchorScore = candidateStabilityScore;
                    }
                }
            }

            if (stableAnchor && stableAnchor !== candidates[0]) {
                orderedCandidates = [stableAnchor, ...candidates.filter((candidate) => candidate !== stableAnchor)];
            }
        }

        let mergedRect = orderedCandidates[0].rect;
        const mergedCandidates = [orderedCandidates[0]];
        const bestScore = Math.max(0.01, orderedCandidates[0].score);
        const mergePaddingX = resolvedModelBounds.width * (kind === 'head' ? 0.05 : 0.1);
        const mergePaddingY = resolvedModelBounds.height * (kind === 'head' ? 0.03 : 0.08);
        const anchorCenterX = kind === 'head' ? orderedCandidates[0].rect.centerX : null;
        const anchorCenterY = kind === 'head' ? orderedCandidates[0].rect.centerY : null;

        for (const candidate of orderedCandidates.slice(1)) {
            if (kind === 'head' && candidate.score < bestScore * 0.72) {
                continue;
            }

            if (kind === 'head' &&
                Number.isFinite(anchorCenterX) &&
                Number.isFinite(anchorCenterY)) {
                const deltaXFromAnchor = Math.abs(candidate.rect.centerX - anchorCenterX);
                const deltaYFromAnchor = Math.abs(candidate.rect.centerY - anchorCenterY);
                if (deltaXFromAnchor > resolvedModelBounds.width * 0.16 ||
                    deltaYFromAnchor > resolvedModelBounds.height * 0.14) {
                    continue;
                }
            }

            const expandedMergedRect = this._expandScreenRect(mergedRect, mergePaddingX, mergePaddingY);
            const overlapsMerged = this._getRectIntersectionArea(candidate.rect, expandedMergedRect) > 0;
            const verticallyAdjacent = candidate.rect.top <= mergedRect.bottom + mergePaddingY &&
                candidate.rect.bottom >= mergedRect.top - mergePaddingY;
            const centerReferenceX = kind === 'head' && Number.isFinite(anchorCenterX)
                ? anchorCenterX
                : mergedRect.centerX;
            const centeredEnough = Math.abs(candidate.rect.centerX - centerReferenceX) <=
                resolvedModelBounds.width * (kind === 'head' ? 0.14 : 0.28);
            if (!overlapsMerged && !(verticallyAdjacent && centeredEnough)) {
                continue;
            }

            const nextMergedRect = this._mergeScreenRects([mergedRect, candidate.rect]);
            if (!nextMergedRect) {
                continue;
            }

            if (kind === 'head') {
                const mergedAreaRatio = rectArea(mergedRect) / modelArea;
                const candidateAreaRatio = rectArea(candidate.rect) / modelArea;
                const candidateDominatesArea = candidateAreaRatio >= Math.max(
                    mergedAreaRatio * 2.35,
                    mergedAreaRatio + 0.012
                );
                const candidateClearlyLowerThanMerged = candidate.rect.centerY >=
                    mergedRect.centerY + resolvedModelBounds.height * 0.065;
                if (candidateDominatesArea && candidateClearlyLowerThanMerged) {
                    continue;
                }

                if (nextMergedRect.width > resolvedModelBounds.width * 0.56 ||
                    nextMergedRect.height > resolvedModelBounds.height * 0.36 ||
                    nextMergedRect.bottom > resolvedModelBounds.top + resolvedModelBounds.height * 0.6) {
                    continue;
                }
            } else if (nextMergedRect.width > resolvedModelBounds.width * 0.82 ||
                nextMergedRect.height > resolvedModelBounds.height * 0.86) {
                continue;
            }

            mergedRect = nextMergedRect;
            mergedCandidates.push(candidate);
        }

        if (kind === 'head') {
            mergedRect = this._normalizeDrawableHeadScreenRect(
                mergedRect,
                resolvedModelBounds,
                bodyRectHint,
                headRectHint,
                mergedCandidates
            );
        } else if (kind === 'body') {
            mergedRect = this._normalizeDrawableBodyScreenRect(
                mergedRect,
                resolvedModelBounds,
                headRectHint,
                mergedCandidates
            );
        }

        return this._createRectInfoFromScreenRect(
            mergedRect,
            kind === 'head' ? 'face' : 'body',
            'drawableHeuristic'
        );
    }

    _getCoreModelSequence(coreModel, methodNames = [], propertyNames = []) {
        if (!coreModel) {
            return [];
        }

        for (const methodName of methodNames) {
            const getter = coreModel?.[methodName];
            if (typeof getter !== 'function') {
                continue;
            }

            try {
                const value = getter.call(coreModel);
                if (value && typeof value.length === 'number') {
                    return value;
                }
            } catch (_) {}
        }

        for (const propertyName of propertyNames) {
            const value = coreModel?.[propertyName];
            if (value && typeof value.length === 'number') {
                return value;
            }
        }

        return [];
    }

    _getCoreModelSequenceFromIndexedGetter(coreModel, countMethodNames = [], countPropertyNames = [], itemMethodNames = []) {
        if (!coreModel || !Array.isArray(itemMethodNames) || itemMethodNames.length === 0) {
            return [];
        }

        let count = null;

        for (const methodName of countMethodNames) {
            const getter = coreModel?.[methodName];
            if (typeof getter !== 'function') {
                continue;
            }

            try {
                const value = Number(getter.call(coreModel));
                if (Number.isInteger(value) && value > 0) {
                    count = value;
                    break;
                }
            } catch (_) {}
        }

        if (!Number.isInteger(count) || count <= 0) {
            for (const propertyName of countPropertyNames) {
                const value = Number(coreModel?.[propertyName]);
                if (Number.isInteger(value) && value > 0) {
                    count = value;
                    break;
                }
            }
        }

        if (!Number.isInteger(count) || count <= 0) {
            return [];
        }

        for (const methodName of itemMethodNames) {
            const getter = coreModel?.[methodName];
            if (typeof getter !== 'function') {
                continue;
            }

            const values = [];
            let succeeded = true;

            for (let index = 0; index < count; index += 1) {
                try {
                    values.push(getter.call(coreModel, index));
                } catch (_) {
                    succeeded = false;
                    break;
                }
            }

            if (succeeded && values.length === count) {
                return values;
            }
        }

        return [];
    }

    _getCoreModelPartIds(coreModel) {
        const directPartIds = this._getCoreModelSequence(coreModel, ['getPartIds'], [
            '_partIds',
            'partIds'
        ]);
        if (directPartIds.length > 0) {
            return directPartIds;
        }

        const indexedPartIds = this._getCoreModelSequenceFromIndexedGetter(
            coreModel,
            ['getPartCount'],
            [],
            ['getPartId']
        );
        if (indexedPartIds.length > 0) {
            return indexedPartIds;
        }

        const nestedPartIds = coreModel?._model?.parts?.ids;
        return nestedPartIds && typeof nestedPartIds.length === 'number'
            ? nestedPartIds
            : [];
    }

    _getCoreModelPartParentPartIndices(coreModel) {
        const directParentIndices = this._getCoreModelSequence(coreModel, ['getPartParentPartIndices'], [
            '_partParentPartIndices',
            'partParentPartIndices'
        ]);
        if (directParentIndices.length > 0) {
            return directParentIndices;
        }

        const indexedParentIndices = this._getCoreModelSequenceFromIndexedGetter(
            coreModel,
            ['getPartCount'],
            [],
            ['getPartParentPartIndex']
        );
        if (indexedParentIndices.length > 0) {
            return indexedParentIndices;
        }

        const nestedParentIndices = coreModel?._model?.parts?.parentPartIndices;
        return nestedParentIndices && typeof nestedParentIndices.length === 'number'
            ? nestedParentIndices
            : [];
    }

    _getCoreModelDrawableParentPartIndices(coreModel) {
        const directParentIndices = this._getCoreModelSequence(coreModel, ['getDrawableParentPartIndices'], [
            '_drawableParentPartIndices',
            'drawableParentPartIndices'
        ]);
        if (directParentIndices.length > 0) {
            return directParentIndices;
        }

        const indexedParentIndices = this._getCoreModelSequenceFromIndexedGetter(
            coreModel,
            ['getDrawableCount'],
            [],
            ['getDrawableParentPartIndex']
        );
        if (indexedParentIndices.length > 0) {
            return indexedParentIndices;
        }

        const nestedParentIndices = coreModel?._model?.drawables?.parentPartIndices;
        return nestedParentIndices && typeof nestedParentIndices.length === 'number'
            ? nestedParentIndices
            : [];
    }

    _getDrawableParentPartIdLookup(coreModel) {
        if (!coreModel) {
            return null;
        }

        const partIds = this._getCoreModelPartIds(coreModel);
        const drawableParentPartIndices = this._getCoreModelDrawableParentPartIndices(coreModel);
        if (partIds.length === 0 || drawableParentPartIndices.length === 0) {
            return null;
        }

        const normalizedPartIds = partIds.map((partId) => String(partId || '').toLowerCase());
        const lookup = new Array(drawableParentPartIndices.length);
        for (let drawableIndex = 0; drawableIndex < drawableParentPartIndices.length; drawableIndex += 1) {
            const parentPartIndex = Number(drawableParentPartIndices[drawableIndex]);
            lookup[drawableIndex] = Number.isInteger(parentPartIndex) &&
                parentPartIndex >= 0 &&
                parentPartIndex < normalizedPartIds.length
                ? normalizedPartIds[parentPartIndex]
                : '';
        }

        return lookup;
    }

    _partIndexMatchesTargetIds(partIndex, partIds, partParentIndices, targetPartIdSet) {
        if (!Number.isInteger(partIndex) || partIndex < 0 || partIndex >= partIds.length || !(targetPartIdSet instanceof Set)) {
            return false;
        }

        let currentPartIndex = partIndex;
        let depth = 0;

        while (Number.isInteger(currentPartIndex) &&
            currentPartIndex >= 0 &&
            currentPartIndex < partIds.length &&
            depth <= partIds.length) {
            const currentPartId = String(partIds[currentPartIndex] || '');
            if (currentPartId && targetPartIdSet.has(currentPartId)) {
                return true;
            }

            const nextPartIndex = Number(partParentIndices?.[currentPartIndex]);
            if (!Number.isInteger(nextPartIndex) || nextPartIndex < 0 || nextPartIndex === currentPartIndex) {
                break;
            }

            currentPartIndex = nextPartIndex;
            depth += 1;
        }

        return false;
    }

    _findDisplayInfoPartIds(patterns) {
        const displayParts = this._displayInfo?.Parts;
        if (!Array.isArray(displayParts) || !Array.isArray(patterns) || patterns.length === 0) {
            return [];
        }

        return displayParts
            .filter((part) => {
                const label = String(part?.Name || part?.Id || '');
                return patterns.some((pattern) => pattern.test(label));
            })
            .map((part) => String(part?.Id || ''))
            .filter(Boolean);
    }

    _collectDisplayInfoPartScreenRectInfo(targetPartIds, mode) {
        const internalModel = this.currentModel?.internalModel;
        const coreModel = internalModel?.coreModel;
        const drawableCount = this._getDrawableCount(internalModel);
        const modelBounds = this.getModelScreenBounds();
        const modelLogicalRect = this._getModelLogicalRect();
        if (!internalModel || !coreModel || !Number.isInteger(drawableCount) || drawableCount <= 0 ||
            !modelBounds || !modelLogicalRect || !Array.isArray(targetPartIds) || targetPartIds.length === 0) {
            return null;
        }

        const partIds = this._getCoreModelPartIds(coreModel);
        const drawableParentPartIndices = this._getCoreModelDrawableParentPartIndices(coreModel);
        const partParentPartIndices = this._getCoreModelPartParentPartIndices(coreModel);
        if (partIds.length === 0 || drawableParentPartIndices.length === 0) {
            return null;
        }

        const targetPartIdSet = new Set(targetPartIds);
        const rects = [];

        for (let index = 0; index < drawableCount; index += 1) {
            const parentPartIndex = Number(drawableParentPartIndices[index]);
            if (!this._partIndexMatchesTargetIds(parentPartIndex, partIds, partParentPartIndices, targetPartIdSet)) {
                continue;
            }

            if (!this._isDrawableRenderable(coreModel, index)) {
                continue;
            }

            const rect = this._getDrawableScreenRect(index, modelLogicalRect, modelBounds);
            if (rect) {
                rects.push(rect);
            }
        }

        return this._createRectInfoFromScreenRect(this._mergeScreenRects(rects), mode, 'displayInfo');
    }

    _buildDisplayInfoEyeFaceRectInfo(eyeInfo, modelBounds = null) {
        const eyeRect = eyeInfo?.rect;
        const bounds = modelBounds || this.getModelScreenBounds();
        if (!eyeRect || !bounds) {
            return null;
        }

        const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
        const boundsRight = Number.isFinite(bounds.right)
            ? bounds.right
            : bounds.left + bounds.width;
        const boundsBottom = Number.isFinite(bounds.bottom)
            ? bounds.bottom
            : bounds.top + bounds.height;
        const expandedWidth = clamp(
            Math.max(
                eyeRect.width * 2.35,
                bounds.width * 0.22
            ),
            Math.max(48, eyeRect.width * 1.8),
            bounds.width * 0.56
        );
        const expandedHeight = clamp(
            Math.max(
                eyeRect.height * 3.25,
                expandedWidth * 0.86,
                bounds.height * 0.18
            ),
            Math.max(64, eyeRect.height * 2.6),
            bounds.height * 0.46
        );
        const centerX = clamp(
            Number.isFinite(eyeRect.centerX) ? eyeRect.centerX : eyeRect.left + eyeRect.width * 0.5,
            bounds.left + expandedWidth * 0.5,
            boundsRight - expandedWidth * 0.5
        );
        const top = clamp(
            eyeRect.top - Math.max(
                eyeRect.height * 1.85,
                expandedHeight * 0.38
            ),
            bounds.top,
            boundsBottom - expandedHeight
        );

        return Object.assign(
            this._createRectInfoFromScreenRect(
                this._createScreenRect(
                    centerX - expandedWidth * 0.5,
                    top,
                    centerX + expandedWidth * 0.5,
                    top + expandedHeight
                ),
                'face',
                'displayInfo'
            ),
            {
                derivedFromEyes: true,
                displayInfoSynthetic: true
            }
        );
    }

    _getDisplayInfoPartScreenRectInfo(kind) {
        if (kind === 'head') {
            const facePartIds = this._findDisplayInfoPartIds([/(^|[^a-z])face([^a-z]|$)|顔|脸/i]);
            const neckPartIds = this._findDisplayInfoPartIds([/(^|[^a-z])neck([^a-z]|$)|首/i]);
            const headPartIds = this._findDisplayInfoPartIds([/(^|[^a-z])head([^a-z]|$)|頭|头/i]);

            const faceInfo = this._collectDisplayInfoPartScreenRectInfo(
                [...new Set([...facePartIds, ...neckPartIds])],
                'face'
            );
            if (faceInfo) {
                return faceInfo;
            }

            const headInfo = this._collectDisplayInfoPartScreenRectInfo(
                [...new Set([...headPartIds, ...neckPartIds])],
                'head'
            );
            if (headInfo) {
                return headInfo;
            }

            const eyePartIds = this._findDisplayInfoPartIds([/(^|[^a-z])eye([^a-z]|$)|目|眼|瞳/i]);
            const eyeInfo = this._collectDisplayInfoPartScreenRectInfo(eyePartIds, 'face');
            if (eyeInfo) {
                return this._buildDisplayInfoEyeFaceRectInfo(
                    eyeInfo,
                    this.getModelScreenBounds()
                );
            }

            return null;
        }

        if (kind === 'body') {
            const bodyPartIds = this._findDisplayInfoPartIds([
                /(^|[^a-z])body([^a-z]|$)|身体|身體|体|胴|胴体|胸|torso|chest|upperbody|upper_body|bust/i
            ]);
            const bodyInfo = this._collectDisplayInfoPartScreenRectInfo(bodyPartIds, 'body');
            if (bodyInfo) {
                return bodyInfo;
            }

            // Some models leave "body" parts empty and attach visible torso meshes to
            // outfit parts instead. Use upper-body clothing as a fallback body proxy.
            const outfitPartIds = this._findDisplayInfoPartIds([
                /(^|[^a-z])dress([^a-z]|$)|(^|[^a-z])clothes([^a-z]|$)|(^|[^a-z])costume([^a-z]|$)|(^|[^a-z])coat([^a-z]|$)|(^|[^a-z])jacket([^a-z]|$)|(^|[^a-z])shirt([^a-z]|$)|(^|[^a-z])uniform([^a-z]|$)|(^|[^a-z])hoodie([^a-z]|$)|(^|[^a-z])jersey([^a-z]|$)|(^|[^a-z])onepiece([^a-z]|$)|(^|[^a-z])one_piece([^a-z]|$)|ワンピース|ジャージ|服|衣|上着/i
            ]);
            return this._collectDisplayInfoPartScreenRectInfo(
                [...new Set([...bodyPartIds, ...outfitPartIds])],
                'body'
            );
        }

        return null;
    }

    _normalizeHitAreaMatchKey(value) {
        return String(value || '')
            .toLowerCase()
            .replace(/[^a-z0-9\u3040-\u30ff\u3400-\u9fff]/g, '');
    }

    _getHitAreaLogicalBoundsRect(modelLogicalRect = null) {
        const resolvedModelLogicalRect = modelLogicalRect || this._getModelLogicalRect();
        if (!resolvedModelLogicalRect) {
            return null;
        }

        return this._createScreenRect(
            resolvedModelLogicalRect.x,
            resolvedModelLogicalRect.y,
            resolvedModelLogicalRect.x + resolvedModelLogicalRect.width,
            resolvedModelLogicalRect.y + resolvedModelLogicalRect.height
        );
    }

    _collectHitAreaLogicalRectCandidates(modelLogicalRect = null) {
        const model = this.currentModel;
        const internalModel = model?.internalModel;
        const hitAreaDefs = internalModel?.settings?.hitAreas;
        const hitAreas = internalModel?.hitAreas;
        const logicalBoundsRect = this._getHitAreaLogicalBoundsRect(modelLogicalRect);
        if (!Array.isArray(hitAreaDefs) || !hitAreas || !logicalBoundsRect) {
            return [];
        }

        const logicalArea = Math.max(1, logicalBoundsRect.width * logicalBoundsRect.height);
        const candidates = [];

        for (const hitAreaDef of hitAreaDefs) {
            if (!hitAreaDef) {
                continue;
            }

            const id = String(hitAreaDef.Id || '');
            const name = String(hitAreaDef.Name || '');
            const hitArea = hitAreas[name] || hitAreas[id];
            const drawableIndex = Number.isInteger(hitArea?.index)
                ? hitArea.index
                : internalModel.coreModel?.getDrawableIndex?.(id);
            if (!Number.isInteger(drawableIndex) || drawableIndex < 0) {
                continue;
            }

            const logicalRect = this._getDrawableLogicalRect(drawableIndex);
            if (!logicalRect) {
                continue;
            }

            const left = Number.isFinite(logicalRect.x)
                ? logicalRect.x
                : Number(logicalRect.left);
            const top = Number.isFinite(logicalRect.y)
                ? logicalRect.y
                : Number(logicalRect.top);
            const width = Number.isFinite(logicalRect.width)
                ? logicalRect.width
                : (Number(logicalRect.right) - left);
            const height = Number.isFinite(logicalRect.height)
                ? logicalRect.height
                : (Number(logicalRect.bottom) - top);
            const rect = this._createScreenRect(
                left,
                top,
                left + width,
                top + height
            );
            if (!rect) {
                continue;
            }

            const area = Math.max(1, rect.width * rect.height);
            const areaRatio = area / logicalArea;
            if (areaRatio < 0.0004 || areaRatio > 0.98) {
                continue;
            }

            candidates.push({
                id,
                name,
                rect,
                area,
                areaRatio,
                drawableIndex,
                autoNamed: this._autoNamedHitAreaIds instanceof Set && this._autoNamedHitAreaIds.has(id)
            });
        }

        return candidates;
    }

    _resolveHitAreaBodyProxyLogicalCandidate(hitAreaCandidates, logicalBoundsRect) {
        if (!Array.isArray(hitAreaCandidates) || hitAreaCandidates.length === 0 || !logicalBoundsRect) {
            return null;
        }

        const minCenterY = logicalBoundsRect.top + logicalBoundsRect.height * 0.26;
        const minAreaRatio = 0.006;
        const bodyCandidates = hitAreaCandidates.filter((candidate) =>
            candidate.rect.centerY >= minCenterY &&
            candidate.areaRatio >= minAreaRatio &&
            candidate.rect.width <= logicalBoundsRect.width * 0.96 &&
            candidate.rect.height <= logicalBoundsRect.height * 0.96
        );
        if (bodyCandidates.length === 0) {
            return null;
        }

        const sorted = [...bodyCandidates].sort((left, right) => {
            const leftScore = left.area * (0.48 + (left.rect.centerY - logicalBoundsRect.top) / Math.max(1, logicalBoundsRect.height));
            const rightScore = right.area * (0.48 + (right.rect.centerY - logicalBoundsRect.top) / Math.max(1, logicalBoundsRect.height));
            return rightScore - leftScore;
        });
        return sorted[0] || null;
    }

    _resolveHeadHitAreaLogicalRectInfoByGeometry(hitAreaCandidates, logicalBoundsRect) {
        if (!Array.isArray(hitAreaCandidates) || hitAreaCandidates.length === 0 || !logicalBoundsRect) {
            return null;
        }

        const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
        const clamp01 = (value) => clamp(value, 0, 1);
        const modelTop = logicalBoundsRect.top;
        const modelHeight = Math.max(1, logicalBoundsRect.height);
        const modelWidth = Math.max(1, logicalBoundsRect.width);
        const bodyProxy = this._resolveHitAreaBodyProxyLogicalCandidate(hitAreaCandidates, logicalBoundsRect);

        const headCandidates = hitAreaCandidates
            .filter((candidate) =>
                candidate.areaRatio >= 0.001 &&
                candidate.areaRatio <= 0.62 &&
                candidate.rect.width <= modelWidth * 0.92 &&
                candidate.rect.height <= modelHeight * 0.86
            )
            .map((candidate) => {
                const rect = candidate.rect;
                const centerYNorm = (rect.centerY - modelTop) / modelHeight;
                const topNorm = (rect.top - modelTop) / modelHeight;
                const widthRatio = rect.width / modelWidth;
                const aspectRatio = rect.width / Math.max(1, rect.height);

                const upperBandScore = clamp01(1 - Math.abs(centerYNorm - 0.24) / 0.34);
                const topBandScore = clamp01(1 - Math.max(0, topNorm - 0.48) / 0.22);
                const sizeScore = clamp01(1 - Math.abs(candidate.areaRatio - 0.08) / 0.17);
                const shapeScore = clamp01(1 - Math.max(0, aspectRatio - 2.45) / 1.7) *
                    clamp01(1 - Math.max(0, widthRatio - 0.72) / 0.22);

                let score = upperBandScore * 3.1 +
                    topBandScore * 1.4 +
                    sizeScore * 1.15 +
                    shapeScore * 1.2;

                if (centerYNorm > 0.72 || topNorm > 0.64) {
                    score -= 2.6;
                }
                if (candidate.areaRatio < 0.003) {
                    score -= 1.1;
                }

                if (bodyProxy) {
                    const deltaY = bodyProxy.rect.centerY - rect.centerY;
                    const deltaX = Math.abs(rect.centerX - bodyProxy.rect.centerX);
                    const aboveBodyScore = clamp01((deltaY / modelHeight - 0.01) / 0.36);
                    const bodyAlignScore = clamp01(1 - deltaX / Math.max(1, modelWidth * 0.46));
                    score += aboveBodyScore * 1.8 + bodyAlignScore * 0.82;

                    if (deltaY < modelHeight * 0.01) {
                        score -= 1.9;
                    }
                }

                return Object.assign({}, candidate, { score });
            })
            .sort((left, right) => right.score - left.score);

        if (headCandidates.length === 0 || headCandidates[0].score < 1.85) {
            return null;
        }

        const anchor = headCandidates[0];
        const cluster = [anchor];
        for (const candidate of headCandidates.slice(1)) {
            if (candidate.score < Math.max(anchor.score * 0.56, anchor.score - 1.2)) {
                continue;
            }

            const deltaX = Math.abs(candidate.rect.centerX - anchor.rect.centerX);
            const deltaY = Math.abs(candidate.rect.centerY - anchor.rect.centerY);
            if (deltaX > modelWidth * 0.24 || deltaY > modelHeight * 0.24) {
                continue;
            }

            const expandedAnchorRect = this._expandScreenRect(anchor.rect, modelWidth * 0.035, modelHeight * 0.04);
            const overlapsAnchor = this._getRectIntersectionArea(candidate.rect, expandedAnchorRect) > 0;
            const verticallyAdjacent = candidate.rect.top <= anchor.rect.bottom + modelHeight * 0.06 &&
                candidate.rect.bottom >= anchor.rect.top - modelHeight * 0.06;
            if (!overlapsAnchor && !verticallyAdjacent) {
                continue;
            }

            cluster.push(candidate);
        }

        let mergedRect = this._mergeScreenRects(cluster.map((candidate) => candidate.rect));
        if (!mergedRect) {
            return null;
        }

        if (bodyProxy) {
            const maxBottomByBody = bodyProxy.rect.top + bodyProxy.rect.height * 0.64;
            const minRetainedHeight = Math.max(modelHeight * 0.06, mergedRect.height * 0.45);
            if (mergedRect.bottom > maxBottomByBody &&
                maxBottomByBody > mergedRect.top + minRetainedHeight) {
                mergedRect = this._createScreenRect(
                    mergedRect.left,
                    mergedRect.top,
                    mergedRect.right,
                    maxBottomByBody
                ) || mergedRect;
            }
        }

        const centerYNorm = (mergedRect.centerY - modelTop) / modelHeight;
        if (mergedRect.width > modelWidth * 0.94 ||
            mergedRect.height > modelHeight * 0.84 ||
            mergedRect.top > modelTop + modelHeight * 0.66 ||
            centerYNorm > 0.72) {
            return null;
        }

        return {
            rect: {
                x: mergedRect.left,
                y: mergedRect.top,
                width: mergedRect.width,
                height: mergedRect.height
            },
            id: anchor.id || null,
            name: anchor.name || null,
            autoNamed: cluster.length === 1 ? !!anchor.autoNamed : false,
            derivedFromHitAreaGeometry: true,
            hitAreaClusterSize: cluster.length
        };
    }

    _resolveBodyHitAreaLogicalRectInfoByGeometry(hitAreaCandidates, logicalBoundsRect, headLogicalInfo = null) {
        if (!Array.isArray(hitAreaCandidates) || hitAreaCandidates.length === 0 || !logicalBoundsRect) {
            return null;
        }

        const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
        const clamp01 = (value) => clamp(value, 0, 1);
        const modelTop = logicalBoundsRect.top;
        const modelHeight = Math.max(1, logicalBoundsRect.height);
        const modelWidth = Math.max(1, logicalBoundsRect.width);
        const modelCenterX = logicalBoundsRect.centerX;
        const headRect = headLogicalInfo?.rect
            ? this._createScreenRect(
                headLogicalInfo.rect.x,
                headLogicalInfo.rect.y,
                headLogicalInfo.rect.x + headLogicalInfo.rect.width,
                headLogicalInfo.rect.y + headLogicalInfo.rect.height
            )
            : null;

        const bodyCandidates = hitAreaCandidates
            .filter((candidate) => {
                const rect = candidate.rect;
                const centerYNorm = (rect.centerY - modelTop) / modelHeight;
                return candidate.areaRatio >= 0.003 &&
                    candidate.areaRatio <= 0.92 &&
                    centerYNorm >= 0.2 &&
                    rect.width <= modelWidth * 0.98 &&
                    rect.height <= modelHeight * 0.98;
            })
            .map((candidate) => {
                const rect = candidate.rect;
                const centerYNorm = (rect.centerY - modelTop) / modelHeight;
                const lowerScore = clamp01((centerYNorm - 0.2) / 0.58);
                const widthScore = clamp01(rect.width / Math.max(1, modelWidth * 0.45));
                const areaScore = clamp01(candidate.areaRatio / 0.26);

                let score = areaScore * 2.2 + lowerScore * 1.9 + widthScore * 0.9;
                if (headRect) {
                    const deltaY = rect.centerY - headRect.centerY;
                    const deltaX = Math.abs(rect.centerX - headRect.centerX);
                    score += clamp01((deltaY / modelHeight - 0.04) / 0.4) * 1.4;
                    score += clamp01(1 - deltaX / Math.max(1, modelWidth * 0.5)) * 0.65;
                    if (deltaY < modelHeight * 0.04) {
                        score -= 1.8;
                    }
                } else {
                    const deltaXToModelCenter = Math.abs(rect.centerX - modelCenterX);
                    score += clamp01(1 - deltaXToModelCenter / Math.max(1, modelWidth * 0.52)) * 0.45;
                }

                return Object.assign({}, candidate, { score });
            })
            .sort((left, right) => right.score - left.score);

        const best = bodyCandidates[0];
        if (!best || best.score < 1.2) {
            return null;
        }

        return {
            rect: {
                x: best.rect.left,
                y: best.rect.top,
                width: best.rect.width,
                height: best.rect.height
            },
            id: best.id || null,
            name: best.name || null,
            autoNamed: !!best.autoNamed,
            derivedFromHitAreaGeometry: true
        };
    }

    _isCanonicalHeadHitAreaKey(key) {
        return key === 'head' ||
            key === 'face' ||
            key === 'touchhead' ||
            key === 'touchface' ||
            key === 'hitareahead' ||
            key === 'hitareaface';
    }

    _isCanonicalBodyHitAreaKey(key) {
        return key === 'body' ||
            key === 'torso' ||
            key === 'touchbody' ||
            key === 'hitareabody';
    }

    _findBestHitAreaLogicalRectInfo(matchInfoFn) {
        const model = this.currentModel;
        const internalModel = model?.internalModel;
        const hitAreaDefs = internalModel?.settings?.hitAreas;
        const hitAreas = internalModel?.hitAreas;
        if (!Array.isArray(hitAreaDefs) || !hitAreas || typeof matchInfoFn !== 'function') {
            return null;
        }

        let bestRect = null;
        let bestScore = -1;
        let bestHitArea = null;

        for (const hitAreaDef of hitAreaDefs) {
            if (!hitAreaDef) continue;

            const name = String(hitAreaDef.Name || '');
            const id = String(hitAreaDef.Id || '');
            const nameMatch = matchInfoFn(name) || {};
            const idMatch = matchInfoFn(id) || {};
            const matchInfo = Number(nameMatch.score) >= Number(idMatch.score) ? nameMatch : idMatch;
            const score = Number(matchInfo.score);
            if (!Number.isFinite(score) || score < 0) continue;

            const hitArea = hitAreas[name] || hitAreas[id];
            const drawableIndex = Number.isInteger(hitArea?.index)
                ? hitArea.index
                : internalModel.coreModel?.getDrawableIndex?.(id);
            if (!Number.isInteger(drawableIndex) || drawableIndex < 0) {
                continue;
            }

            const rect = this._getDrawableLogicalRect(drawableIndex);
            if (!rect) continue;

            if (!bestRect || score > bestScore) {
                bestRect = rect;
                bestScore = score;
                bestHitArea = {
                    id,
                    name,
                    autoNamed: this._autoNamedHitAreaIds instanceof Set && this._autoNamedHitAreaIds.has(id)
                };
            }
        }

        if (!bestRect) {
            return null;
        }

        return {
            rect: bestRect,
            id: bestHitArea?.id || null,
            name: bestHitArea?.name || null,
            autoNamed: !!bestHitArea?.autoNamed
        };
    }

    _getHeadHitAreaLogicalRectInfo() {
        const modelLogicalRect = this._getModelLogicalRect();
        const logicalBoundsRect = this._getHitAreaLogicalBoundsRect(modelLogicalRect);
        const hitAreaCandidates = this._collectHitAreaLogicalRectCandidates(modelLogicalRect);
        const geometryInfo = this._resolveHeadHitAreaLogicalRectInfoByGeometry(
            hitAreaCandidates,
            logicalBoundsRect
        );
        if (geometryInfo) {
            return geometryInfo;
        }

        return this._findBestHitAreaLogicalRectInfo((value) => {
            const key = this._normalizeHitAreaMatchKey(value);
            return this._isCanonicalHeadHitAreaKey(key)
                ? { score: 1 }
                : { score: -1 };
        });
    }

    _getBodyHitAreaLogicalRectInfo() {
        const modelLogicalRect = this._getModelLogicalRect();
        const logicalBoundsRect = this._getHitAreaLogicalBoundsRect(modelLogicalRect);
        const hitAreaCandidates = this._collectHitAreaLogicalRectCandidates(modelLogicalRect);
        const headLogicalInfo = this._resolveHeadHitAreaLogicalRectInfoByGeometry(
            hitAreaCandidates,
            logicalBoundsRect
        );
        const geometryInfo = this._resolveBodyHitAreaLogicalRectInfoByGeometry(
            hitAreaCandidates,
            logicalBoundsRect,
            headLogicalInfo
        );
        if (geometryInfo) {
            return geometryInfo;
        }

        return this._findBestHitAreaLogicalRectInfo((value) => {
            const key = this._normalizeHitAreaMatchKey(value);
            return this._isCanonicalBodyHitAreaKey(key)
                ? { score: 1 }
                : { score: -1 };
        });
    }

    _createHitAreaScreenRectInfo(logicalInfo, mode, modelBounds = null, modelLogicalRect = null) {
        const resolvedModelBounds = modelBounds || this.getModelScreenBounds();
        const resolvedModelLogicalRect = modelLogicalRect || this._getModelLogicalRect();
        const screenRect = this._mapLogicalRectToScreen(logicalInfo?.rect, resolvedModelLogicalRect, resolvedModelBounds);
        if (!screenRect) {
            return null;
        }

        return Object.assign(
            this._createRectInfoFromScreenRect(
                this._createScreenRect(
                    screenRect.left,
                    screenRect.top,
                    screenRect.left + screenRect.width,
                    screenRect.top + screenRect.height
                ),
                mode,
                'hitArea'
            ),
            {
                hitAreaId: logicalInfo?.id || null,
                hitAreaName: logicalInfo?.name || null,
                autoNamed: !!logicalInfo?.autoNamed
            }
        );
    }

    _getHeadHitAreaScreenRectInfo(modelBounds = null, modelLogicalRect = null) {
        return this._createHitAreaScreenRectInfo(
            this._getHeadHitAreaLogicalRectInfo(),
            'face',
            modelBounds,
            modelLogicalRect
        );
    }

    _getBodyHitAreaScreenRectInfo(modelBounds = null, modelLogicalRect = null) {
        return this._createHitAreaScreenRectInfo(
            this._getBodyHitAreaLogicalRectInfo(),
            'body',
            modelBounds,
            modelLogicalRect
        );
    }

    _getRectArea(rectInfo) {
        const rect = rectInfo?.rect || rectInfo;
        const width = Number(rect?.width);
        const height = Number(rect?.height);
        if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
            return 0;
        }
        return width * height;
    }

    _getRectIntersectionArea(rectAInfo, rectBInfo) {
        const rectA = rectAInfo?.rect || rectAInfo;
        const rectB = rectBInfo?.rect || rectBInfo;
        if (!rectA || !rectB) {
            return 0;
        }

        const left = Math.max(Number(rectA.left), Number(rectB.left));
        const top = Math.max(Number(rectA.top), Number(rectB.top));
        const right = Math.min(Number(rectA.right), Number(rectB.right));
        const bottom = Math.min(Number(rectA.bottom), Number(rectB.bottom));
        const width = right - left;
        const height = bottom - top;
        if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
            return 0;
        }

        return width * height;
    }

    _isRectInfoPlausibleWithinModel(rectInfo, modelBounds, options = {}) {
        const rect = rectInfo?.rect;
        if (!rect || !modelBounds) {
            return false;
        }

        const boundsRight = Number.isFinite(modelBounds.right)
            ? modelBounds.right
            : Number(modelBounds.left) + Number(modelBounds.width);
        const boundsBottom = Number.isFinite(modelBounds.bottom)
            ? modelBounds.bottom
            : Number(modelBounds.top) + Number(modelBounds.height);
        const toleranceX = Number.isFinite(options.toleranceX)
            ? options.toleranceX
            : Math.max(18, Number(modelBounds.width) * 0.12);
        const toleranceY = Number.isFinite(options.toleranceY)
            ? options.toleranceY
            : Math.max(18, Number(modelBounds.height) * 0.12);
        const maxWidthRatio = Number.isFinite(options.maxWidthRatio) ? options.maxWidthRatio : 1.02;
        const maxHeightRatio = Number.isFinite(options.maxHeightRatio) ? options.maxHeightRatio : 1.02;

        return rect.left >= Number(modelBounds.left) - toleranceX &&
            rect.right <= boundsRight + toleranceX &&
            rect.top >= Number(modelBounds.top) - toleranceY &&
            rect.bottom <= boundsBottom + toleranceY &&
            rect.width <= Number(modelBounds.width) * maxWidthRatio &&
            rect.height <= Number(modelBounds.height) * maxHeightRatio;
    }

    _shouldPreferDisplayInfoRect(kind, hitAreaInfo, displayInfoInfo, modelBounds) {
        if (!displayInfoInfo) {
            return false;
        }

        if (!hitAreaInfo) {
            return true;
        }

        const displayPlausible = this._isRectInfoPlausibleWithinModel(
            displayInfoInfo,
            modelBounds,
            kind === 'head'
                ? { maxWidthRatio: 0.98, maxHeightRatio: 0.88 }
                : { maxWidthRatio: 1.04, maxHeightRatio: 1.02 }
        );
        if (!displayPlausible) {
            return false;
        }

        if (hitAreaInfo.autoNamed) {
            return true;
        }

        const hitAreaArea = this._getRectArea(hitAreaInfo);
        const displayArea = this._getRectArea(displayInfoInfo);
        if (!(hitAreaArea > 0 && displayArea > 0)) {
            return false;
        }

        const overlapArea = this._getRectIntersectionArea(hitAreaInfo, displayInfoInfo);
        const overlapRatio = overlapArea / Math.max(1, Math.min(hitAreaArea, displayArea));
        const areaOversizeRatio = hitAreaArea / Math.max(displayArea, 1);
        const hitRect = hitAreaInfo.rect;
        const displayRect = displayInfoInfo.rect;
        const widthOversizeRatio = hitRect.width / Math.max(displayRect.width, 1);
        const heightOversizeRatio = hitRect.height / Math.max(displayRect.height, 1);
        const displayDerivedFromEyes = kind === 'head' && displayInfoInfo.derivedFromEyes === true;

        if (kind === 'head') {
            if (displayDerivedFromEyes) {
                const displayContainsHitAreaCenter = Number.isFinite(hitRect.centerX) &&
                    Number.isFinite(hitRect.centerY) &&
                    hitRect.centerX >= displayRect.left - 12 &&
                    hitRect.centerX <= displayRect.right + 12 &&
                    hitRect.centerY >= displayRect.top - 12 &&
                    hitRect.centerY <= displayRect.bottom + 12;
                const hitAreaClearlySmallerThanFace = hitAreaArea <= displayArea * 0.72 ||
                    hitRect.width <= displayRect.width * 0.82 ||
                    hitRect.height <= displayRect.height * 0.82;
                const hitAreaLivesInUpperFaceBand = hitRect.centerY <= displayRect.top + displayRect.height * 0.56 ||
                    hitRect.bottom <= displayRect.top + displayRect.height * 0.72;
                if (displayContainsHitAreaCenter &&
                    hitAreaClearlySmallerThanFace &&
                    hitAreaLivesInUpperFaceBand) {
                    return true;
                }
            }

            const displayClearlyInsideHitArea = overlapRatio >= 0.68 ||
                (displayRect.left >= hitRect.left - 12 &&
                    displayRect.right <= hitRect.right + 12 &&
                    displayRect.top >= hitRect.top - 12 &&
                    displayRect.bottom <= hitRect.bottom + 12);
            const hitAreaLooksCoarse = areaOversizeRatio >= 1.5 ||
                widthOversizeRatio >= 1.3 ||
                heightOversizeRatio >= 1.3 ||
                displayRect.top >= hitRect.top + Math.max(18, displayRect.height * 0.16);
            return displayClearlyInsideHitArea && hitAreaLooksCoarse;
        }

        return overlapRatio >= 0.5 && (
            areaOversizeRatio >= 1.45 ||
            widthOversizeRatio >= 1.25 ||
            heightOversizeRatio >= 1.25
        );
    }

    _isLikelyChibiRectPair(headRect, bodyRect, modelBounds) {
        const head = headRect?.rect || headRect;
        const body = bodyRect?.rect || bodyRect;
        const bounds = modelBounds?.rect || modelBounds;
        if (!head || !body || !bounds) {
            return false;
        }

        const rectsLookValid = [head, body, bounds].every((rect) =>
            Number.isFinite(rect.left) &&
            Number.isFinite(rect.top) &&
            Number.isFinite(rect.width) &&
            Number.isFinite(rect.height) &&
            rect.width > 0 &&
            rect.height > 0
        );
        if (!rectsLookValid) {
            return false;
        }

        const headCenterY = Number.isFinite(head.centerY) ? head.centerY : head.top + head.height * 0.5;
        const bodyCenterY = Number.isFinite(body.centerY) ? body.centerY : body.top + body.height * 0.5;
        if (headCenterY >= bodyCenterY) {
            return false;
        }

        const headWidthRatio = head.width / Math.max(1, bounds.width);
        const headHeightRatio = head.height / Math.max(1, bounds.height);
        const bodyWidthRatio = body.width / Math.max(1, bounds.width);
        const bodyHeightRatio = body.height / Math.max(1, bounds.height);
        const headToBodyWidthRatio = head.width / Math.max(1, body.width);
        const headToBodyHeightRatio = head.height / Math.max(1, body.height);
        const verticalGap = body.top - head.bottom;
        const maxVerticalGap = Math.max(72, head.height * 1.05, body.height * 0.34);
        const unionRect = this._mergeScreenRects([head, body]);

        return headWidthRatio >= 0.16 &&
            headWidthRatio <= 0.76 &&
            headHeightRatio >= 0.1 &&
            headHeightRatio <= 0.54 &&
            bodyWidthRatio >= 0.1 &&
            bodyWidthRatio <= 0.72 &&
            bodyHeightRatio >= 0.12 &&
            bodyHeightRatio <= 0.68 &&
            headToBodyWidthRatio >= 0.42 &&
            headToBodyHeightRatio >= 0.26 &&
            body.width <= head.width * 2.18 &&
            body.height <= head.height * 3.5 &&
            verticalGap <= maxVerticalGap &&
            body.top <= head.top + head.height * 1.55 &&
            unionRect &&
            unionRect.height <= bounds.height * 0.84;
    }

    _shouldPreferOversizedHitAreaRect(kind, hitAreaInfo, inferredInfo, modelBounds, counterpartInfo = null) {
        if (!hitAreaInfo || !inferredInfo || !modelBounds) {
            return false;
        }

        const hitRect = hitAreaInfo.rect;
        const inferredRect = inferredInfo.rect;
        if (!hitRect || !inferredRect) {
            return false;
        }

        const counterpartRect = counterpartInfo?.rect || null;
        const inferredLooksChibi = kind === 'head'
            ? this._isLikelyChibiRectPair(inferredRect, counterpartRect, modelBounds)
            : this._isLikelyChibiRectPair(counterpartRect, inferredRect, modelBounds);
        const overlapArea = this._getRectIntersectionArea(hitAreaInfo, inferredInfo);
        const overlapRatio = overlapArea / Math.max(1, Math.min(
            this._getRectArea(hitAreaInfo),
            this._getRectArea(inferredInfo)
        ));
        if (overlapRatio < (inferredLooksChibi ? 0.42 : 0.54)) {
            return false;
        }

        const areaCoverageRatio = this._getRectArea(hitAreaInfo) / Math.max(this._getRectArea(inferredInfo), 1);
        const widthCoverageRatio = hitRect.width / Math.max(inferredRect.width, 1);
        const heightCoverageRatio = hitRect.height / Math.max(inferredRect.height, 1);
        const coarseAreaThreshold = inferredLooksChibi || hitAreaInfo.autoNamed ? 1.42 : 1.9;
        const coarseWidthThreshold = inferredLooksChibi || hitAreaInfo.autoNamed ? 1.16 : 1.34;
        const coarseHeightThreshold = inferredLooksChibi || hitAreaInfo.autoNamed ? 1.16 : 1.34;
        const hitAreaClearlyTooLarge = areaCoverageRatio >= coarseAreaThreshold ||
            widthCoverageRatio >= coarseWidthThreshold ||
            heightCoverageRatio >= coarseHeightThreshold;
        if (!hitAreaClearlyTooLarge) {
            return false;
        }

        if (kind === 'head') {
            const hitAreaStartsTooHigh = hitRect.top <= inferredRect.top - Math.max(16, inferredRect.height * 0.14);
            const hitAreaEndsTooLow = hitRect.bottom >= inferredRect.bottom + Math.max(20, inferredRect.height * 0.18);
            const hitAreaCenterTooLow = hitRect.centerY >= inferredRect.centerY + Math.max(14, inferredRect.height * 0.12);
            return hitAreaStartsTooHigh || hitAreaEndsTooLow || hitAreaCenterTooLow;
        }

        const hitAreaStartsTooHigh = hitRect.top <= inferredRect.top - Math.max(20, inferredRect.height * 0.16);
        const hitAreaEndsTooLow = hitRect.bottom >= inferredRect.bottom + Math.max(24, inferredRect.height * 0.18);
        const bodyCenterTooHigh = hitRect.centerY <= inferredRect.centerY - Math.max(16, inferredRect.height * 0.12);
        const hitAreaAbsorbsHead = counterpartRect &&
            hitRect.top <= counterpartRect.top + counterpartRect.height * 0.18;
        return hitAreaStartsTooHigh || hitAreaEndsTooLow || bodyCenterTooHigh || hitAreaAbsorbsHead;
    }

    _shouldPreferInferredRect(kind, hitAreaInfo, inferredInfo, modelBounds, counterpartInfo = null) {
        if (!inferredInfo) {
            return false;
        }

        if (!hitAreaInfo) {
            return true;
        }

        const inferredPlausible = this._isRectInfoPlausibleWithinModel(
            inferredInfo,
            modelBounds,
            kind === 'head'
                ? { maxWidthRatio: 0.86, maxHeightRatio: 0.64 }
                : { maxWidthRatio: 0.9, maxHeightRatio: 0.92 }
        );
        if (!inferredPlausible) {
            return false;
        }

        const hitAreaArea = this._getRectArea(hitAreaInfo);
        const inferredArea = this._getRectArea(inferredInfo);
        if (!(hitAreaArea > 0 && inferredArea > 0)) {
            return false;
        }

        const hitRect = hitAreaInfo.rect;
        const inferredRect = inferredInfo.rect;
        const areaCoverageRatio = hitAreaArea / Math.max(inferredArea, 1);
        const widthCoverageRatio = hitRect.width / Math.max(inferredRect.width, 1);
        const heightCoverageRatio = hitRect.height / Math.max(inferredRect.height, 1);
        const hitName = String(hitAreaInfo.hitAreaName || hitAreaInfo.name || '').toLowerCase();
        const hitId = String(hitAreaInfo.hitAreaId || hitAreaInfo.id || '').toLowerCase();
        const looksLikeTouchHotspot = /touch|tap|click/.test(hitName) || /touch|tap|click/.test(hitId);

        if (kind === 'head') {
            const hitAreaClearlyTooSmall = areaCoverageRatio <= 0.26 ||
                widthCoverageRatio <= 0.48 ||
                heightCoverageRatio <= 0.42;
            const hitAreaSitsTooLow = hitRect.top >= inferredRect.top + Math.max(18, inferredRect.height * 0.28) ||
                hitRect.centerY >= inferredRect.top + inferredRect.height * 0.72;
            return hitAreaClearlyTooSmall ||
                (looksLikeTouchHotspot && hitAreaSitsTooLow) ||
                this._shouldPreferOversizedHitAreaRect(
                    kind,
                    hitAreaInfo,
                    inferredInfo,
                    modelBounds,
                    counterpartInfo
                );
        }

        const hitAreaClearlyTooSmall = areaCoverageRatio <= 0.22 ||
            widthCoverageRatio <= 0.42 ||
            heightCoverageRatio <= 0.34;
        return hitAreaClearlyTooSmall ||
            looksLikeTouchHotspot ||
            this._shouldPreferOversizedHitAreaRect(
                kind,
                hitAreaInfo,
                inferredInfo,
                modelBounds,
                counterpartInfo
            );
    }

    _shouldPreferInferredBodyRectOverDisplayInfo(displayInfoInfo, inferredInfo, headInfo, modelBounds) {
        if (!displayInfoInfo || !inferredInfo || !headInfo || !modelBounds) {
            return false;
        }

        const displayPlausible = this._isRectInfoPlausibleWithinModel(
            displayInfoInfo,
            modelBounds,
            { maxWidthRatio: 1.04, maxHeightRatio: 1.02 }
        );
        const inferredPlausible = this._isRectInfoPlausibleWithinModel(
            inferredInfo,
            modelBounds,
            { maxWidthRatio: 0.9, maxHeightRatio: 0.92 }
        );
        if (!displayPlausible || !inferredPlausible) {
            return false;
        }

        const displayRect = displayInfoInfo.rect;
        const inferredRect = inferredInfo.rect;
        const headRect = headInfo.rect;
        if (!displayRect || !inferredRect || !headRect) {
            return false;
        }

        const displayTinyVsBounds = displayRect.width <= modelBounds.width * 0.24 &&
            displayRect.height <= modelBounds.height * 0.18;
        const displaySmallerThanHead = displayRect.width <= headRect.width * 0.96 &&
            displayRect.height <= headRect.height * 0.9;
        const inferredClearlyLarger = inferredRect.width >= displayRect.width * 2.1 &&
            inferredRect.height >= displayRect.height * 2.4;
        const displaySitsNearHead = displayRect.top <= headRect.bottom + Math.max(24, headRect.height * 1.15);

        return displayTinyVsBounds &&
            displaySmallerThanHead &&
            inferredClearlyLarger &&
            displaySitsNearHead;
    }

    _hasValidBubbleScreenRect(rect) {
        return !!(rect &&
            Number.isFinite(rect.left) &&
            Number.isFinite(rect.top) &&
            Number.isFinite(rect.width) &&
            Number.isFinite(rect.height) &&
            rect.width > 0 &&
            rect.height > 0);
    }

    _getBubbleHeadAnchorFromRect(headRect, headMode, headSource) {
        if (!this._hasValidBubbleScreenRect(headRect)) {
            return null;
        }

        const faceAnchorRatio = headSource === 'displayInfo' ? 0.36 : 0.39;
        const headAnchorRatio = headSource === 'displayInfo' ? 0.42 : 0.48;

        return {
            x: Number.isFinite(headRect.centerX) ? headRect.centerX : headRect.left + headRect.width * 0.5,
            y: headRect.top + headRect.height * (headMode === 'face' ? faceAnchorRatio : headAnchorRatio)
        };
    }

    _isReliableBubbleHeadRect(headRect, bounds, bodyRect, headSource) {
        if (!this._hasValidBubbleScreenRect(headRect) || !bounds) {
            return false;
        }

        const headCenterY = Number.isFinite(headRect.centerY)
            ? headRect.centerY
            : headRect.top + headRect.height * 0.5;
        const boundsRight = Number.isFinite(bounds.right) ? bounds.right : bounds.left + bounds.width;
        const boundsBottom = Number.isFinite(bounds.bottom) ? bounds.bottom : bounds.top + bounds.height;

        if (headSource === 'displayInfo') {
            const toleranceX = Math.max(18, bounds.width * 0.08);
            const toleranceY = Math.max(18, bounds.height * 0.08);
            if (headRect.left < bounds.left - toleranceX ||
                headRect.right > boundsRight + toleranceX ||
                headRect.top < bounds.top - toleranceY ||
                headRect.bottom > boundsBottom + toleranceY ||
                headRect.width > bounds.width * 0.98 ||
                headRect.height > bounds.height * 0.88) {
                return false;
            }

            if (!this._hasValidBubbleScreenRect(bodyRect)) {
                return true;
            }

            const bodyCenterY = Number.isFinite(bodyRect.centerY)
                ? bodyRect.centerY
                : bodyRect.top + bodyRect.height * 0.5;
            return headCenterY <= bodyRect.bottom &&
                headRect.top <= bodyCenterY &&
                headRect.height <= bodyRect.height * 1.12;
        }

        const maxHeadTop = bounds.top + bounds.height * 0.54;
        const maxHeadCenterY = bounds.top + bounds.height * 0.52;
        if (headRect.width > bounds.width * 0.76 ||
            headRect.height > bounds.height * 0.62 ||
            headRect.top > maxHeadTop ||
            headCenterY > maxHeadCenterY) {
            return false;
        }

        if (!this._hasValidBubbleScreenRect(bodyRect)) {
            return true;
        }

        return headRect.width <= bodyRect.width * 1.52 &&
            headRect.height <= bodyRect.height * 0.94 &&
            headCenterY <= bodyRect.top + bodyRect.height * 0.42;
    }

    _createBubbleBodyProxyRect(headRect, bounds, bodyRect = null) {
        if (!this._hasValidBubbleScreenRect(headRect) || !bounds) {
            return null;
        }

        const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
        const boundsRight = Number.isFinite(bounds.right) ? bounds.right : bounds.left + bounds.width;
        const boundsBottom = Number.isFinite(bounds.bottom) ? bounds.bottom : bounds.top + bounds.height;
        const headCenterX = Number.isFinite(headRect.centerX)
            ? headRect.centerX
            : headRect.left + headRect.width * 0.5;
        const bodyWidthHint = this._hasValidBubbleScreenRect(bodyRect) ? bodyRect.width : 0;
        const bodyHeightHint = this._hasValidBubbleScreenRect(bodyRect) ? bodyRect.height : 0;
        const width = clamp(
            Math.max(
                headRect.width * 1.28,
                bodyWidthHint * 0.42,
                bounds.width * 0.16
            ),
            Math.max(56, headRect.width * 1.12),
            Math.max(72, Math.min(bounds.width * 0.44, headRect.width * 2.05))
        );
        const height = clamp(
            Math.max(
                headRect.height * 1.6,
                bodyHeightHint * 0.36,
                bounds.height * 0.18
            ),
            Math.max(72, headRect.height * 1.24),
            Math.max(96, Math.min(bounds.height * 0.42, headRect.height * 3.0))
        );
        const centerX = clamp(
            headCenterX,
            bounds.left + width * 0.5,
            boundsRight - width * 0.5
        );
        const top = clamp(
            headRect.bottom - Math.min(headRect.height * 0.12, height * 0.1),
            bounds.top,
            boundsBottom - height
        );

        return this._createScreenRect(
            centerX - width * 0.5,
            top,
            centerX + width * 0.5,
            top + height
        );
    }

    _isReliableBubbleBodyRect(bodyRect, bounds, headRect, reliableHeadRect, bodySource) {
        if (!this._hasValidBubbleScreenRect(bodyRect) || !bounds) {
            return false;
        }

        const boundsRight = Number.isFinite(bounds.right) ? bounds.right : bounds.left + bounds.width;
        const boundsBottom = Number.isFinite(bounds.bottom) ? bounds.bottom : bounds.top + bounds.height;
        const toleranceX = Math.max(24, bounds.width * 0.1);
        const toleranceY = Math.max(24, bounds.height * 0.1);
        if (bodyRect.left < bounds.left - toleranceX ||
            bodyRect.right > boundsRight + toleranceX ||
            bodyRect.top < bounds.top - toleranceY ||
            bodyRect.bottom > boundsBottom + toleranceY ||
            bodyRect.width > bounds.width * 1.04 ||
            bodyRect.height > bounds.height * 1.04) {
            return false;
        }

        const bodyCenterY = Number.isFinite(bodyRect.centerY)
            ? bodyRect.centerY
            : bodyRect.top + bodyRect.height * 0.5;
        if (!reliableHeadRect || !this._hasValidBubbleScreenRect(headRect)) {
            return bodyRect.width >= bounds.width * 0.08 &&
                bodyRect.height >= bounds.height * 0.12 &&
                bodyCenterY >= bounds.top + bounds.height * 0.2 &&
                bodyCenterY <= bounds.top + bounds.height * 0.9;
        }

        const headCenterX = Number.isFinite(headRect.centerX)
            ? headRect.centerX
            : headRect.left + headRect.width * 0.5;
        const bodyCenterX = Number.isFinite(bodyRect.centerX)
            ? bodyRect.centerX
            : bodyRect.left + bodyRect.width * 0.5;
        const widthRatio = bodyRect.width / Math.max(1, headRect.width);
        const heightRatio = bodyRect.height / Math.max(1, headRect.height);
        const centerDrift = Math.abs(bodyCenterX - headCenterX);
        const maxCenterDrift = Math.max(
            32,
            headRect.width * 0.9,
            bodyRect.width * 0.16,
            bounds.width * 0.08
        );
        const gapFromHeadBottom = bodyRect.top - headRect.bottom;
        const bodyStartsTooHigh = bodyRect.top < headRect.top - Math.max(24, headRect.height * 0.24);
        const bodyStartsTooLow = gapFromHeadBottom > Math.max(64, headRect.height * 0.95);
        const bodyTooTiny = widthRatio < 0.6 || heightRatio < 0.56;
        const bodyTooWide = widthRatio > 2.7 || bodyRect.width > bounds.width * 0.88;
        const bodyTooTall = heightRatio > 3.4 || bodyRect.height > bounds.height * 0.88;
        const bodyEndsTooHigh = bodyRect.bottom < headRect.bottom + Math.max(32, headRect.height * 0.32);
        const bodyCenterNotBelowHead = bodyCenterY <= (
            (Number.isFinite(headRect.centerY) ? headRect.centerY : headRect.top + headRect.height * 0.5) +
            Math.max(18, headRect.height * 0.12)
        );

        if (bodySource === 'drawableHeuristic') {
            return !bodyStartsTooHigh &&
                !bodyStartsTooLow &&
                !bodyTooTiny &&
                !bodyTooWide &&
                !bodyTooTall &&
                !bodyEndsTooHigh &&
                !bodyCenterNotBelowHead &&
                centerDrift <= maxCenterDrift;
        }

        return !bodyStartsTooLow &&
            !bodyTooTiny &&
            !bodyTooWide &&
            !bodyTooTall &&
            !bodyEndsTooHigh &&
            !bodyCenterNotBelowHead &&
            centerDrift <= maxCenterDrift * 1.1;
    }

    _normalizeBubbleBodyRect(bodyRect, bounds, headRect, reliableHeadRect, bodySource) {
        if (bodySource === 'bubbleBodyProxy') {
            return this._createBubbleBodyProxyRect(headRect, bounds, bodyRect) || bodyRect;
        }

        if (!this._hasValidBubbleScreenRect(bodyRect)) {
            return reliableHeadRect
                ? this._createBubbleBodyProxyRect(headRect, bounds, null)
                : bodyRect;
        }

        if (!reliableHeadRect || !this._hasValidBubbleScreenRect(headRect)) {
            return this._isReliableBubbleBodyRect(bodyRect, bounds, headRect, false, bodySource)
                ? bodyRect
                : null;
        }

        return this._isReliableBubbleBodyRect(bodyRect, bounds, headRect, true, bodySource)
            ? bodyRect
            : this._createBubbleBodyProxyRect(headRect, bounds, bodyRect);
    }

    _normalizeBubbleHeadRect(headRect, bounds, bodyRect, headSource, headMode) {
        if (!this._hasValidBubbleScreenRect(headRect) || !bounds) {
            return headRect;
        }

        if (headSource === 'drawableHeuristic') {
            return this._normalizeDrawableHeadScreenRect(
                headRect,
                bounds,
                this._hasValidBubbleScreenRect(bodyRect) ? bodyRect : null,
                null
            ) || headRect;
        }

        if (headMode === 'face') {
            return this._expandFaceModeRectToFullHead(
                headRect, bounds, bodyRect
            ) || headRect;
        }

        return headRect;
    }

    _expandFaceModeRectToFullHead(rect, bounds, bodyRect) {
        if (!rect || !bounds) {
            return rect;
        }

        const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
        const widthRatio = rect.width / Math.max(1, bounds.width);
        const heightRatio = rect.height / Math.max(1, bounds.height);

        if (widthRatio >= 0.48 || heightRatio >= 0.38) {
            return rect;
        }

        const expandedWidth = clamp(
            rect.width * 1.3,
            rect.width * 1.2,
            Math.min(bounds.width * 0.58, rect.width * 1.45)
        );
        const topExtension = rect.height * 0.3;
        let newTop = rect.top - topExtension;
        let newBottom = rect.bottom;

        if (this._hasValidBubbleScreenRect(bodyRect)) {
            const bodyCap = bodyRect.top + bodyRect.height * 0.15;
            if (newBottom > bodyCap && bodyCap > newTop + expandedWidth * 0.4) {
                newBottom = bodyCap;
            }
        }

        newTop = Math.max(bounds.top, newTop);
        newBottom = Math.min(bounds.top + bounds.height, newBottom);
        if (newBottom - newTop < rect.height * 1.15) {
            newBottom = newTop + rect.height * 1.25;
            newBottom = Math.min(bounds.top + bounds.height, newBottom);
        }

        const centerX = Number.isFinite(rect.centerX)
            ? rect.centerX
            : rect.left + rect.width * 0.5;

        return this._createScreenRect(
            centerX - expandedWidth * 0.5,
            newTop,
            centerX + expandedWidth * 0.5,
            newBottom
        ) || rect;
    }

    _shouldUseBubbleDrawableHeadProxy(headRect, bounds, bodyRect, headSource) {
        if (headSource !== 'drawableHeuristic' ||
            !this._hasValidBubbleScreenRect(headRect) ||
            !bounds) {
            return false;
        }

        const headCenterX = Number.isFinite(headRect.centerX)
            ? headRect.centerX
            : headRect.left + headRect.width * 0.5;
        const boundsCenterX = Number.isFinite(bounds.centerX)
            ? bounds.centerX
            : bounds.left + bounds.width * 0.5;
        const headOccupiesLargeUpperBand = headRect.top <= bounds.top + bounds.height * 0.16 &&
            headRect.bottom <= bounds.top + bounds.height * 0.58 &&
            headRect.width >= bounds.width * 0.34 &&
            headRect.height >= bounds.height * 0.22;
        const headLooksWideForFaceAnchor = headRect.width >= bounds.width * 0.28 &&
            (headRect.width / Math.max(1, headRect.height)) >= 1.25;
        const bodyStartsBelowHeadCore = !this._hasValidBubbleScreenRect(bodyRect) ||
            bodyRect.top >= headRect.top + headRect.height * 0.38;
        const headBiasesAwayFromBoundsCenter = Math.abs(headCenterX - boundsCenterX) >= bounds.width * 0.04;

        return (headOccupiesLargeUpperBand || headLooksWideForFaceAnchor) &&
            (bodyStartsBelowHeadCore || headBiasesAwayFromBoundsCenter);
    }

    _createBubbleDrawableHeadProxyRect(headRect, bounds, bodyRect = null) {
        if (!this._hasValidBubbleScreenRect(headRect) || !bounds) {
            return null;
        }

        const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
        const minVisibleCoverageRatio = 0.1;
        const boundsRight = Number.isFinite(bounds.right) ? bounds.right : bounds.left + bounds.width;
        const boundsBottom = Number.isFinite(bounds.bottom) ? bounds.bottom : bounds.top + bounds.height;
        const visibleHeadLeft = Math.max(bounds.left, headRect.left);
        const visibleHeadRight = Math.min(boundsRight, headRect.right);
        const visibleHeadTop = Math.max(bounds.top, headRect.top);
        const visibleHeadBottom = Math.min(boundsBottom, headRect.bottom);
        const rawVisibleHeadWidth = Math.max(0, visibleHeadRight - visibleHeadLeft);
        const rawVisibleHeadHeight = Math.max(0, visibleHeadBottom - visibleHeadTop);
        const visibleHeadWidth = Math.max(1, rawVisibleHeadWidth);
        const visibleHeadHeight = Math.max(1, rawVisibleHeadHeight);
        const visibleHeadWidthCoverage = rawVisibleHeadWidth / Math.max(1, headRect.width);
        const visibleHeadHeightCoverage = rawVisibleHeadHeight / Math.max(1, headRect.height);
        const hasReliableVisibleWidthClip = visibleHeadWidthCoverage >= minVisibleCoverageRatio;
        const hasReliableVisibleHeightClip = visibleHeadHeightCoverage >= minVisibleCoverageRatio;
        const widthClipLimit = hasReliableVisibleWidthClip ? visibleHeadWidth : headRect.width;
        const heightClipLimit = hasReliableVisibleHeightClip ? visibleHeadHeight : headRect.height;
        const headCenterX = Number.isFinite(headRect.centerX)
            ? headRect.centerX
            : headRect.left + headRect.width * 0.5;
        const hasBodyRect = this._hasValidBubbleScreenRect(bodyRect);
        const bodyCenterX = hasBodyRect && Number.isFinite(bodyRect.centerX)
            ? bodyRect.centerX
            : headCenterX;
        const bodyCenterDeltaX = bodyCenterX - headCenterX;
        let horizontalShift = clamp(
            (bodyCenterX - headCenterX) * 0.82,
            -headRect.width * 0.18,
            headRect.width * 0.18
        );
        if (hasBodyRect) {
            const bodyStartsBelowHeadCore = Number.isFinite(bodyRect.top) &&
                bodyRect.top >= headRect.top + headRect.height * 0.42;
            const bodyMuchWiderThanHead = bodyRect.width >= headRect.width * 1.45;
            const bodyCenterOffsetRatio = Math.abs(bodyCenterDeltaX) / Math.max(1, headRect.width);
            const requiresBodyBiasDamping = bodyStartsBelowHeadCore && (
                bodyMuchWiderThanHead ||
                bodyCenterOffsetRatio >= 0.16
            );
            if (requiresBodyBiasDamping) {
                const dampedShiftRatio = bodyCenterOffsetRatio >= 0.2 ? 0.06 : 0.08;
                const dampedShiftAbs = headRect.width * dampedShiftRatio;
                horizontalShift = clamp(
                    horizontalShift,
                    -dampedShiftAbs,
                    dampedShiftAbs
                );
            }
        }
        const widthTarget = Math.max(
            headRect.width * 0.4,
            hasBodyRect ? bodyRect.width * 0.24 : 0,
            bounds.width * 0.17
        );
        const widthMax = Math.max(
            1,
            Math.min(
                bounds.width * 0.42,
                headRect.width * 0.58,
                widthClipLimit
            )
        );
        const widthMin = Math.min(
            Math.max(
                headRect.width * 0.28,
                bounds.width * 0.1,
                48
            ),
            widthMax
        );
        const width = clamp(widthTarget, widthMin, widthMax);
        const heightTarget = Math.max(
            headRect.height * 0.48,
            width * 0.68,
            bounds.height * 0.16
        );
        const heightMax = Math.max(
            1,
            Math.min(
                bounds.height * 0.34,
                headRect.height * 0.7,
                heightClipLimit
            )
        );
        const heightMin = Math.min(
            Math.max(
                headRect.height * 0.32,
                bounds.height * 0.11,
                40
            ),
            heightMax
        );
        const height = clamp(heightTarget, heightMin, heightMax);
        const centerClampLeft = hasReliableVisibleWidthClip ? visibleHeadLeft : bounds.left;
        const centerClampRight = hasReliableVisibleWidthClip ? visibleHeadRight : boundsRight;
        const centerXMin = centerClampLeft + width * 0.5;
        const centerXMax = centerClampRight - width * 0.5;
        const centerX = centerXMin <= centerXMax
            ? clamp(headCenterX + horizontalShift, centerXMin, centerXMax)
            : clamp(headCenterX, bounds.left, boundsRight);
        const topClampTop = hasReliableVisibleHeightClip ? visibleHeadTop : bounds.top;
        const topClampBottom = hasReliableVisibleHeightClip ? visibleHeadBottom : boundsBottom;
        const topMin = topClampTop;
        const topMax = topClampBottom - height;
        const top = topMin <= topMax
            ? clamp(headRect.top + headRect.height * 0.1, topMin, topMax)
            : clamp(headRect.top, bounds.top, Math.max(bounds.top, boundsBottom - height));

        return this._createScreenRect(
            centerX - width * 0.5,
            top,
            centerX + width * 0.5,
            top + height
        );
    }

    _getBubbleGeometryOverride() {
        const runtimeOverrides = window.NEKO_LIVE2D_BUBBLE_OVERRIDES;
        const overrideMap = (runtimeOverrides && typeof runtimeOverrides === 'object')
            ? runtimeOverrides
            : LIVE2D_BUBBLE_GEOMETRY_OVERRIDES;
        if (!overrideMap || typeof overrideMap !== 'object') {
            return null;
        }

        return overrideMap[this.modelRootPath] ||
            overrideMap[this.modelName] ||
            null;
    }

    _getBubbleGeometryOverrideSignature() {
        const override = this._getBubbleGeometryOverride();
        if (!override || typeof override !== 'object') {
            return 'none';
        }

        const readFinite = (key) => {
            const value = Number(override[key]);
            return Number.isFinite(value) ? value : null;
        };

        return JSON.stringify({
            headScaleX: readFinite('headScaleX'),
            headScaleY: readFinite('headScaleY'),
            headOffsetX: readFinite('headOffsetX'),
            headOffsetY: readFinite('headOffsetY'),
            anchorOffsetX: readFinite('anchorOffsetX'),
            anchorOffsetY: readFinite('anchorOffsetY')
        });
    }

    _applyBubbleGeometryOverride(geometryInfo) {
        const override = this._getBubbleGeometryOverride();
        if (!override || typeof override !== 'object' || !geometryInfo) {
            return geometryInfo;
        }

        const nextGeometryInfo = Object.assign({}, geometryInfo);
        const rawHeadRect = geometryInfo.headRect;
        const bubbleHeadRect = geometryInfo.bubbleHeadRect || geometryInfo.headRect || null;
        if (this._hasValidBubbleScreenRect(bubbleHeadRect)) {
            let left = bubbleHeadRect.left;
            let top = bubbleHeadRect.top;
            let width = bubbleHeadRect.width;
            let height = bubbleHeadRect.height;
            const centerX = Number.isFinite(bubbleHeadRect.centerX) ? bubbleHeadRect.centerX : bubbleHeadRect.left + bubbleHeadRect.width * 0.5;
            const centerY = Number.isFinite(bubbleHeadRect.centerY) ? bubbleHeadRect.centerY : bubbleHeadRect.top + bubbleHeadRect.height * 0.5;
            const widthScale = Number.isFinite(override.headScaleX) ? override.headScaleX : 1;
            const heightScale = Number.isFinite(override.headScaleY) ? override.headScaleY : 1;
            const offsetX = Number.isFinite(override.headOffsetX) ? override.headOffsetX : 0;
            const offsetY = Number.isFinite(override.headOffsetY) ? override.headOffsetY : 0;

            width = Math.max(1, width * widthScale);
            height = Math.max(1, height * heightScale);
            left = centerX - width * 0.5 + offsetX;
            top = centerY - height * 0.5 + offsetY;
            nextGeometryInfo.bubbleHeadRect = {
                left,
                top,
                right: left + width,
                bottom: top + height,
                width,
                height,
                centerX: left + width * 0.5,
                centerY: top + height * 0.5
            };
        }

        const resolvedHeadRect = nextGeometryInfo.bubbleHeadRect || bubbleHeadRect || null;
        const resolvedHeadSource = nextGeometryInfo.headSource || geometryInfo.headSource || null;
        const rawBodyRect = nextGeometryInfo.bodyRect || geometryInfo.bodyRect || null;
        const headPlausibleWithoutBody = this._isReliableBubbleHeadRect(
            resolvedHeadRect,
            nextGeometryInfo.bounds || geometryInfo.bounds || null,
            null,
            resolvedHeadSource
        );
        nextGeometryInfo.bodyRect = this._normalizeBubbleBodyRect(
            rawBodyRect,
            nextGeometryInfo.bounds || geometryInfo.bounds || null,
            resolvedHeadRect,
            headPlausibleWithoutBody,
            nextGeometryInfo.bodySource || geometryInfo.bodySource || null
        ) || null;
        if (nextGeometryInfo.bodyRect !== rawBodyRect && nextGeometryInfo.bodyRect) {
            nextGeometryInfo.bodySource = 'bubbleBodyProxy';
        }
        const reliableHeadRect = this._isReliableBubbleHeadRect(
            resolvedHeadRect,
            nextGeometryInfo.bounds || geometryInfo.bounds || null,
            nextGeometryInfo.bodyRect || null,
            resolvedHeadSource
        );
        const preciseDisplayInfoRect = reliableHeadRect && resolvedHeadSource === 'displayInfo';
        const coarseHitAreaHeadRect = resolvedHeadSource === 'hitArea' &&
            this._hasValidBubbleScreenRect(resolvedHeadRect) &&
            geometryInfo.rawHeadAnchor &&
            Number.isFinite(geometryInfo.rawHeadAnchor.y) &&
            geometryInfo.rawHeadAnchor.y >= resolvedHeadRect.top + resolvedHeadRect.height * 0.82;
        let baseAnchor = reliableHeadRect
            ? (this._getBubbleHeadAnchorFromRect(
                resolvedHeadRect,
                nextGeometryInfo.headMode || geometryInfo.headMode || null,
                resolvedHeadSource
            ) || geometryInfo.rawHeadAnchor)
            : null;

        if (coarseHitAreaHeadRect && geometryInfo.rawHeadAnchor) {
            baseAnchor = geometryInfo.rawHeadAnchor;
        }

        if (baseAnchor) {
            nextGeometryInfo.headAnchor = {
                x: baseAnchor.x + (Number.isFinite(override.anchorOffsetX) ? override.anchorOffsetX : 0),
                y: baseAnchor.y + (Number.isFinite(override.anchorOffsetY) ? override.anchorOffsetY : 0)
            };
        } else {
            nextGeometryInfo.headAnchor = geometryInfo.rawHeadAnchor || null;
        }

        nextGeometryInfo.reliableHeadRect = reliableHeadRect;
        nextGeometryInfo.preciseDisplayInfoRect = preciseDisplayInfoRect;
        nextGeometryInfo.coarseHitAreaHeadRect = coarseHitAreaHeadRect;
        nextGeometryInfo.headRect = rawHeadRect || null;
        nextGeometryInfo.bubbleHeadRect = resolvedHeadRect || null;

        return nextGeometryInfo;
    }

    _isTinyHeadHitAreaHint(hitAreaInfo, modelBounds) {
        const rect = hitAreaInfo?.rect;
        if (!rect || !modelBounds) {
            return false;
        }

        const normalizedHitAreaId = this._normalizeHitAreaMatchKey(
            hitAreaInfo?.hitAreaId || hitAreaInfo?.id || ''
        );
        const normalizedHitAreaName = this._normalizeHitAreaMatchKey(
            hitAreaInfo?.hitAreaName || hitAreaInfo?.name || ''
        );
        if (this._isCanonicalBodyHitAreaKey(normalizedHitAreaId) ||
            this._isCanonicalBodyHitAreaKey(normalizedHitAreaName)) {
            return false;
        }

        const modelArea = Math.max(1, modelBounds.width * modelBounds.height);
        const areaRatio = (rect.width * rect.height) / modelArea;
        const widthRatio = rect.width / Math.max(1, modelBounds.width);
        const heightRatio = rect.height / Math.max(1, modelBounds.height);
        return areaRatio <= 0.012 || (widthRatio <= 0.14 && heightRatio <= 0.14);
    }

    _nudgeInferredHeadRectWithTinyHitArea(inferredInfo, hitAreaInfo, inferredBodyInfo, modelBounds) {
        const rect = inferredInfo?.rect;
        const hintRect = hitAreaInfo?.rect;
        if (!rect || !hintRect || !modelBounds || !this._isTinyHeadHitAreaHint(hitAreaInfo, modelBounds)) {
            return inferredInfo;
        }

        const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
        const boundsRight = Number.isFinite(modelBounds.right) ? modelBounds.right : modelBounds.left + modelBounds.width;
        const boundsBottom = Number.isFinite(modelBounds.bottom) ? modelBounds.bottom : modelBounds.top + modelBounds.height;
        const bodyRect = inferredBodyInfo?.rect || null;
        const headCenterX = Number.isFinite(rect.centerX) ? rect.centerX : rect.left + rect.width * 0.5;
        const headCenterY = Number.isFinite(rect.centerY) ? rect.centerY : rect.top + rect.height * 0.5;
        const hintCenterX = Number.isFinite(hintRect.centerX) ? hintRect.centerX : hintRect.left + hintRect.width * 0.5;
        const hintCenterY = Number.isFinite(hintRect.centerY) ? hintRect.centerY : hintRect.top + hintRect.height * 0.5;
        const bodyCenterX = Number.isFinite(bodyRect?.centerX)
            ? bodyRect.centerX
            : null;
        const maxHintCenterY = Math.min(
            modelBounds.top + modelBounds.height * 0.52,
            rect.bottom + Math.max(20, rect.height * 0.2)
        );
        const minHintCenterY = rect.top - Math.max(36, rect.height * 0.55);
        if (Number.isFinite(hintCenterY) &&
            (hintCenterY > maxHintCenterY || hintCenterY < minHintCenterY)) {
            return inferredInfo;
        }
        if (bodyRect &&
            Number.isFinite(bodyRect.top) &&
            Number.isFinite(bodyRect.height) &&
            bodyRect.height > 0 &&
            Number.isFinite(hintCenterY)) {
            const bodyUpperBandCap = bodyRect.top + bodyRect.height * 0.24;
            if (hintCenterY > bodyUpperBandCap) {
                return inferredInfo;
            }
        }

        const hintDeltaX = Number.isFinite(hintCenterX) ? (hintCenterX - headCenterX) : 0;
        const hintDeltaY = Number.isFinite(hintCenterY) ? (hintCenterY - headCenterY) : 0;
        const bodyDeltaX = Number.isFinite(bodyCenterX) ? (bodyCenterX - headCenterX) : 0;

        const hintDirection = Math.abs(hintDeltaX) >= rect.width * 0.05
            ? Math.sign(hintDeltaX)
            : 0;
        const bodyDirection = Number.isFinite(bodyCenterX) && Math.abs(bodyDeltaX) >= rect.width * 0.18
            ? Math.sign(bodyDeltaX)
            : 0;

        let shiftX = 0;
        if (Number.isFinite(hintCenterX)) {
            shiftX += clamp(hintDeltaX * 0.36, -rect.width * 0.18, rect.width * 0.18);
        }
        if (Number.isFinite(bodyCenterX)) {
            const strongBodyBias = Math.abs(bodyDeltaX) >= rect.width * 0.75;
            const bodyBiasConsistent = hintDirection === 0 ||
                bodyDirection === 0 ||
                hintDirection === bodyDirection;
            const bodyWeight = bodyBiasConsistent
                ? (strongBodyBias ? 0.2 : 0.06)
                : (strongBodyBias ? 0.05 : 0.03);
            const bodyClampAbs = bodyBiasConsistent
                ? (strongBodyBias ? rect.width * 0.34 : rect.width * 0.1)
                : (strongBodyBias ? rect.width * 0.08 : rect.width * 0.05);
            const bodyShift = clamp(
                bodyDeltaX * bodyWeight,
                -bodyClampAbs,
                bodyClampAbs
            );
            shiftX += bodyShift;
        }
        if (hintDirection < 0 && shiftX > 0) {
            shiftX = Math.min(shiftX, rect.width * 0.04);
        } else if (hintDirection > 0 && shiftX < 0) {
            shiftX = Math.max(shiftX, -rect.width * 0.04);
        }
        shiftX = clamp(shiftX, -rect.width * 0.34, rect.width * 0.34);

        let shiftY = 0;
        if (Number.isFinite(hintCenterY)) {
            const verticalPull = hintDeltaY >= 0
                ? clamp(
                    0.16 + (hintDeltaY / Math.max(1, rect.height * 1.8)),
                    0.16,
                    0.55
                )
                : clamp(
                    0.16 + (Math.abs(hintDeltaY) / Math.max(1, rect.height * 2.2)),
                    0.16,
                    0.32
                );
            shiftY = hintDeltaY * verticalPull;
        }
        shiftY = clamp(shiftY, -rect.height * 0.18, rect.height * 0.48);

        if (Math.abs(shiftX) < 0.35 && Math.abs(shiftY) < 0.35) {
            return inferredInfo;
        }

        const nextLeft = clamp(rect.left + shiftX, modelBounds.left, boundsRight - rect.width);
        let nextTop = clamp(rect.top + shiftY, modelBounds.top, boundsBottom - rect.height);
        if (bodyRect &&
            Number.isFinite(bodyRect.top) &&
            Number.isFinite(bodyRect.height) &&
            bodyRect.height > 0) {
            const maxBottomByBody = bodyRect.top + bodyRect.height * 0.36;
            const maxTopByBody = maxBottomByBody - rect.height;
            if (Number.isFinite(maxTopByBody)) {
                nextTop = Math.min(nextTop, maxTopByBody);
                nextTop = clamp(nextTop, modelBounds.top, boundsBottom - rect.height);
            }
        }
        const nudgedRect = this._createScreenRect(
            nextLeft,
            nextTop,
            nextLeft + rect.width,
            nextTop + rect.height
        );
        if (!nudgedRect) {
            return inferredInfo;
        }

        return Object.assign({}, inferredInfo, {
            rect: nudgedRect
        });
    }

    _applyTinyHitAreaBubbleHeadRectHint(bubbleHeadRect, tinyHeadHitPoint, bodyRect, bounds) {
        if (!this._hasValidBubbleScreenRect(bubbleHeadRect) ||
            !tinyHeadHitPoint ||
            !bounds ||
            !Number.isFinite(tinyHeadHitPoint.x) ||
            !Number.isFinite(tinyHeadHitPoint.y)) {
            return bubbleHeadRect;
        }

        const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
        const boundsRight = Number.isFinite(bounds.right) ? bounds.right : bounds.left + bounds.width;
        const boundsBottom = Number.isFinite(bounds.bottom) ? bounds.bottom : bounds.top + bounds.height;
        const centerX = Number.isFinite(bubbleHeadRect.centerX)
            ? bubbleHeadRect.centerX
            : bubbleHeadRect.left + bubbleHeadRect.width * 0.5;
        const centerY = Number.isFinite(bubbleHeadRect.centerY)
            ? bubbleHeadRect.centerY
            : bubbleHeadRect.top + bubbleHeadRect.height * 0.5;
        const bodyCenterX = this._hasValidBubbleScreenRect(bodyRect) && Number.isFinite(bodyRect.centerX)
            ? bodyRect.centerX
            : null;
        const hitDeltaX = tinyHeadHitPoint.x - centerX;
        const hitDeltaY = tinyHeadHitPoint.y - centerY;
        const bodyDeltaX = Number.isFinite(bodyCenterX) ? (bodyCenterX - centerX) : 0;
        const maxHitY = Math.min(
            bounds.top + bounds.height * 0.52,
            bubbleHeadRect.bottom + Math.max(18, bubbleHeadRect.height * 0.22)
        );
        const minHitY = bubbleHeadRect.top - Math.max(30, bubbleHeadRect.height * 0.55);
        if (tinyHeadHitPoint.y > maxHitY || tinyHeadHitPoint.y < minHitY) {
            return bubbleHeadRect;
        }
        if (this._hasValidBubbleScreenRect(bodyRect)) {
            const bodyUpperBandCap = bodyRect.top + bodyRect.height * 0.24;
            if (tinyHeadHitPoint.y > bodyUpperBandCap) {
                return bubbleHeadRect;
            }
        }

        const hitDirection = Math.abs(hitDeltaX) >= bubbleHeadRect.width * 0.05
            ? Math.sign(hitDeltaX)
            : 0;
        const bodyDirection = Number.isFinite(bodyCenterX) && Math.abs(bodyDeltaX) >= bubbleHeadRect.width * 0.2
            ? Math.sign(bodyDeltaX)
            : 0;

        let shiftX = clamp(hitDeltaX * 0.45, -bubbleHeadRect.width * 0.2, bubbleHeadRect.width * 0.2);
        if (Number.isFinite(bodyCenterX) && Math.abs(bodyDeltaX) >= bubbleHeadRect.width * 0.8) {
            const bodyBiasConsistent = hitDirection === 0 ||
                bodyDirection === 0 ||
                hitDirection === bodyDirection;
            const bodyWeight = bodyBiasConsistent ? 0.2 : 0.05;
            const bodyNegClamp = bodyBiasConsistent
                ? bubbleHeadRect.width * 0.15
                : bubbleHeadRect.width * 0.06;
            const bodyPosClamp = bodyBiasConsistent
                ? bubbleHeadRect.width * 0.35
                : bubbleHeadRect.width * 0.08;
            shiftX += clamp(
                bodyDeltaX * bodyWeight,
                -bodyNegClamp,
                bodyPosClamp
            );
        }
        if (hitDirection < 0 && shiftX > 0) {
            shiftX = Math.min(shiftX, bubbleHeadRect.width * 0.04);
        } else if (hitDirection > 0 && shiftX < 0) {
            shiftX = Math.max(shiftX, -bubbleHeadRect.width * 0.04);
        }
        if (hitDirection !== 0 && Math.sign(shiftX) === hitDirection) {
            const maxTowardHit = Math.abs(hitDeltaX) * 0.95;
            if (maxTowardHit > 0 && Math.abs(shiftX) > maxTowardHit) {
                shiftX = hitDirection * maxTowardHit;
            }
        }
        shiftX = clamp(shiftX, -bubbleHeadRect.width * 0.34, bubbleHeadRect.width * 0.34);

        let shiftY = clamp(
            hitDeltaY * 0.62,
            -bubbleHeadRect.height * 0.14,
            bubbleHeadRect.height * 0.42
        );
        shiftY = clamp(shiftY, -bubbleHeadRect.height * 0.16, bubbleHeadRect.height * 0.46);

        if (Math.abs(shiftX) < 0.35 && Math.abs(shiftY) < 0.35) {
            return bubbleHeadRect;
        }

        const nextLeft = clamp(
            bubbleHeadRect.left + shiftX,
            bounds.left,
            boundsRight - bubbleHeadRect.width
        );
        const nextTop = clamp(
            bubbleHeadRect.top + shiftY,
            bounds.top,
            boundsBottom - bubbleHeadRect.height
        );
        let resolvedTop = nextTop;
        if (this._hasValidBubbleScreenRect(bodyRect)) {
            const maxBottomByBody = bodyRect.top + bodyRect.height * 0.34;
            const maxTopByBody = maxBottomByBody - bubbleHeadRect.height;
            if (Number.isFinite(maxTopByBody)) {
                resolvedTop = Math.min(resolvedTop, maxTopByBody);
                resolvedTop = clamp(resolvedTop, bounds.top, boundsBottom - bubbleHeadRect.height);
            }
        }

        return this._createScreenRect(
            nextLeft,
            resolvedTop,
            nextLeft + bubbleHeadRect.width,
            resolvedTop + bubbleHeadRect.height
        ) || bubbleHeadRect;
    }

    getHeadScreenRectInfo() {
        const modelBounds = this.getModelScreenBounds();
        const modelLogicalRect = this._getModelLogicalRect();
        const hitAreaInfo = this._getHeadHitAreaScreenRectInfo(modelBounds, modelLogicalRect);
        const displayInfoInfo = this._getDisplayInfoPartScreenRectInfo('head');
        const inferredBodyInfo = this._inferDrawableRegionScreenRectInfo('body', modelBounds, modelLogicalRect);
        const useHitAreaAsHeadHint = this._isRectInfoPlausibleWithinModel(
            hitAreaInfo,
            modelBounds,
            { maxWidthRatio: 0.78, maxHeightRatio: 0.56 }
        );
        let inferredInfo = this._inferDrawableRegionScreenRectInfo(
            'head',
            modelBounds,
            modelLogicalRect,
            inferredBodyInfo?.rect || null,
            useHitAreaAsHeadHint ? (hitAreaInfo?.rect || null) : null
        );
        inferredInfo = this._nudgeInferredHeadRectWithTinyHitArea(
            inferredInfo,
            hitAreaInfo,
            inferredBodyInfo,
            modelBounds
        );
        const headHitRect = hitAreaInfo?.rect || null;
        const hitAreaLooksCoarseAgainstModel = !!(
            headHitRect &&
            modelBounds &&
            headHitRect.width >= modelBounds.width * 0.8 &&
            headHitRect.height >= modelBounds.height * 0.46 &&
            headHitRect.top <= modelBounds.top + modelBounds.height * 0.12
        );
        if (this._shouldPreferDisplayInfoRect('head', hitAreaInfo, displayInfoInfo, modelBounds)) {
            return displayInfoInfo;
        }

        if (hitAreaLooksCoarseAgainstModel &&
            this._isRectInfoPlausibleWithinModel(
                inferredInfo,
                modelBounds,
                { maxWidthRatio: 0.86, maxHeightRatio: 0.64 }
            )) {
            return inferredInfo;
        }

        if (this._shouldPreferInferredRect('head', hitAreaInfo, inferredInfo, modelBounds, inferredBodyInfo)) {
            return inferredInfo;
        }

        return hitAreaInfo || displayInfoInfo || inferredInfo;
    }

    getBodyScreenRectInfo(headInfo = undefined) {
        const modelBounds = this.getModelScreenBounds();
        const modelLogicalRect = this._getModelLogicalRect();
        const hitAreaInfo = this._getBodyHitAreaScreenRectInfo(modelBounds, modelLogicalRect);
        const displayInfoInfo = this._getDisplayInfoPartScreenRectInfo('body');
        const resolvedHeadInfo = headInfo === undefined
            ? this.getHeadScreenRectInfo()
            : headInfo;
        const inferredInfo = this._inferDrawableRegionScreenRectInfo(
            'body',
            modelBounds,
            modelLogicalRect,
            null,
            resolvedHeadInfo?.rect || null
        );
        if (this._shouldPreferInferredBodyRectOverDisplayInfo(displayInfoInfo, inferredInfo, resolvedHeadInfo, modelBounds)) {
            return inferredInfo;
        }
        if (this._shouldPreferDisplayInfoRect('body', hitAreaInfo, displayInfoInfo, modelBounds)) {
            return displayInfoInfo;
        }

        if (this._shouldPreferInferredRect('body', hitAreaInfo, inferredInfo, modelBounds, resolvedHeadInfo)) {
            return inferredInfo;
        }

        return hitAreaInfo || displayInfoInfo || inferredInfo;
    }

    getHeadDetectionGeometryInfo() {
        const bounds = this.getModelScreenBounds();
        if (!bounds) {
            return null;
        }

        const modelLogicalRect = this._getModelLogicalRect();
        const headHitAreaInfo = this._getHeadHitAreaScreenRectInfo(bounds, modelLogicalRect);
        const tinyHeadHitAreaInfo = this._isTinyHeadHitAreaHint(headHitAreaInfo, bounds)
            ? headHitAreaInfo
            : null;
        const tinyHeadHitRect = tinyHeadHitAreaInfo?.rect || null;
        const tinyHeadHitPoint = tinyHeadHitRect
            ? {
                x: Number.isFinite(tinyHeadHitRect.centerX)
                    ? tinyHeadHitRect.centerX
                    : tinyHeadHitRect.left + tinyHeadHitRect.width * 0.5,
                y: Number.isFinite(tinyHeadHitRect.centerY)
                    ? tinyHeadHitRect.centerY
                    : tinyHeadHitRect.top + tinyHeadHitRect.height * 0.5
            }
            : null;

        const headInfo = this.getHeadScreenRectInfo();
        const bodyInfo = this.getBodyScreenRectInfo(headInfo);
        const rawHeadAnchor = this.getHeadScreenAnchor(headInfo);
        const headRect = this._normalizeBubbleHeadRect(
            headInfo?.rect || null,
            bounds,
            bodyInfo?.rect || null,
            headInfo?.source || null,
            headInfo?.mode || null
        );
        const rawBodyRect = bodyInfo?.rect || null;
        const headMode = headInfo?.mode || null;
        const headSource = headInfo?.source || null;
        let bodySource = bodyInfo?.source || null;

        const headPlausibleWithoutBody = this._isReliableBubbleHeadRect(headRect, bounds, null, headSource);
        const bodyRect = this._normalizeBubbleBodyRect(
            rawBodyRect,
            bounds,
            headRect,
            headPlausibleWithoutBody,
            bodySource
        ) || null;
        if (bodyRect && bodyRect !== rawBodyRect) {
            bodySource = 'bubbleBodyProxy';
        }

        const reliableHeadRect = this._isReliableBubbleHeadRect(headRect, bounds, bodyRect, headSource);
        const preciseDisplayInfoRect = reliableHeadRect && headSource === 'displayInfo';
        const coarseHitAreaHeadRect = headSource === 'hitArea' &&
            this._hasValidBubbleScreenRect(headRect) &&
            rawHeadAnchor &&
            Number.isFinite(rawHeadAnchor.y) &&
            rawHeadAnchor.y >= headRect.top + headRect.height * 0.82;
        let headAnchor = reliableHeadRect
            ? (this._getBubbleHeadAnchorFromRect(headRect, headMode, headSource) || rawHeadAnchor)
            : null;

        if (coarseHitAreaHeadRect && rawHeadAnchor) {
            headAnchor = rawHeadAnchor;
        }

        return {
            type: 'live2d',
            bounds,
            rawHeadAnchor: rawHeadAnchor || null,
            headAnchor: headAnchor || rawHeadAnchor || null,
            headRect: headRect || null,
            headMode,
            headSource,
            bodyRect,
            bodySource,
            reliableHeadRect,
            preciseDisplayInfoRect,
            coarseHitAreaHeadRect,
            tinyHeadHitPoint
        };
    }

    getBubbleAnchorGeometryInfo() {
        const cached = this._getCachedBubbleGeometryResult();
        if (cached) {
            return cached;
        }

        const detectionInfo = this.getHeadDetectionGeometryInfo();
        if (!detectionInfo || !detectionInfo.bounds) {
            return null;
        }

        const bounds = detectionInfo.bounds;
        const rawHeadAnchor = detectionInfo.rawHeadAnchor || null;
        const headRect = detectionInfo.headRect || null;
        const headMode = detectionInfo.headMode || null;
        const headSource = detectionInfo.headSource || null;
        const bodyRect = detectionInfo.bodyRect || null;
        const bodySource = detectionInfo.bodySource || null;
        let bubbleHeadRect = this._shouldUseBubbleDrawableHeadProxy(headRect, bounds, bodyRect, headSource)
            ? (this._createBubbleDrawableHeadProxyRect(headRect, bounds, bodyRect) || headRect)
            : headRect;
        bubbleHeadRect = this._applyTinyHitAreaBubbleHeadRectHint(
            bubbleHeadRect,
            detectionInfo.tinyHeadHitPoint || null,
            bodyRect,
            bounds
        );
        const reliableHeadRect = this._isReliableBubbleHeadRect(bubbleHeadRect, bounds, bodyRect, headSource);
        const preciseDisplayInfoRect = reliableHeadRect && headSource === 'displayInfo';
        const coarseHitAreaHeadRect = headSource === 'hitArea' &&
            this._hasValidBubbleScreenRect(bubbleHeadRect) &&
            rawHeadAnchor &&
            Number.isFinite(rawHeadAnchor.y) &&
            rawHeadAnchor.y >= bubbleHeadRect.top + bubbleHeadRect.height * 0.82;
        let headAnchor = reliableHeadRect
            ? (this._getBubbleHeadAnchorFromRect(bubbleHeadRect, headMode, headSource) || rawHeadAnchor)
            : null;

        if (coarseHitAreaHeadRect && rawHeadAnchor) {
            headAnchor = rawHeadAnchor;
        }

        const result = this._applyBubbleGeometryOverride({
            bounds,
            rawHeadAnchor: rawHeadAnchor || null,
            headAnchor: headAnchor || rawHeadAnchor || null,
            headRect: headRect || null,
            bubbleHeadRect: bubbleHeadRect || headRect || null,
            headMode,
            headSource,
            bodyRect,
            bodySource,
            reliableHeadRect,
            preciseDisplayInfoRect,
            coarseHitAreaHeadRect
        });

        if (result.reliableHeadRect) {
            const modelLogicalRect = this._getModelLogicalRect();
            if (modelLogicalRect) {
                this._cacheBubbleGeometryResult(result, bounds, modelLogicalRect);
            }
        }

        return result;
    }

    getHeadScreenAnchor(headScreenInfo = undefined) {
        const resolvedHeadScreenInfo = headScreenInfo === undefined
            ? this.getHeadScreenRectInfo()
            : headScreenInfo;
        return this.getHeadScreenAnchorFromInfo(resolvedHeadScreenInfo);
    }

    getHeadScreenAnchorFromInfo(headScreenInfo) {
        const headScreenRect = headScreenInfo?.rect;
        if (!headScreenRect) {
            return null;
        }

        return {
            x: headScreenRect.centerX,
            y: headScreenRect.top + headScreenRect.height * (headScreenInfo.mode === 'face' ? 0.42 : 0.5)
        };
    }

    getBubbleAnchorDebugInfo() {
        const settings = this.currentModel?.internalModel?.settings;
        const settingsJson = settings?.json;
        const hitAreaDefs = settings?.hitAreas;
        const headInfo = this.getHeadScreenRectInfo();
        const bodyInfo = this.getBodyScreenRectInfo(headInfo);
        const geometryInfo = this.getBubbleAnchorGeometryInfo();

        return {
            modelName: this.modelName || null,
            modelRootPath: this.modelRootPath || null,
            displayInfoLoaded: !!this._displayInfo,
            displayInfoPath: settingsJson?.FileReferences?.DisplayInfo || null,
            hitAreas: Array.isArray(hitAreaDefs)
                ? hitAreaDefs.map((hitArea) => ({
                    id: String(hitArea?.Id || ''),
                    name: String(hitArea?.Name || '')
                }))
                : [],
            bounds: geometryInfo?.bounds || this.getModelScreenBounds(),
            headInfo: headInfo || null,
            bodyInfo: bodyInfo || null,
            geometryInfo
        };
    }

    _getUnclippedModelScreenBounds(model = this.currentModel) {
        if (!model || model.destroyed || typeof model.getBounds !== 'function') {
            return null;
        }

        let bounds = null;
        try {
            bounds = model.getBounds();
        } catch (error) {
            console.warn('[Live2D] 获取模型屏幕边界失败:', error);
            return null;
        }
        if (!bounds) {
            return null;
        }

        const left = Number(bounds.left ?? bounds.x);
        const top = Number(bounds.top ?? bounds.y);
        const right = Number(bounds.right ?? (left + Number(bounds.width)));
        const bottom = Number(bounds.bottom ?? (top + Number(bounds.height)));
        if (!Number.isFinite(left) || !Number.isFinite(right) ||
            !Number.isFinite(top) || !Number.isFinite(bottom)) {
            return null;
        }

        const width = right - left;
        const height = bottom - top;
        if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
            return null;
        }

        return {
            left: left,
            right: right,
            top: top,
            bottom: bottom,
            width: width,
            height: height,
            centerX: left + width / 2,
            centerY: top + height / 2
        };
    }

    /**
     * 获取 Live2D 模型在屏幕上的边界
     * @returns {Object|null} 边界对象 { left, right, top, bottom, width, height, centerX, centerY } 或 null
     */
    getModelScreenBounds() {
        const edgePeekState = this._live2DPeekState;
        if (edgePeekState && edgePeekState.active) {
            if (edgePeekState.phase === 'hidden' || edgePeekState.phase === 'hiding') {
                return null;
            }
            const model = edgePeekState.model || this.currentModel;
            if (model && !model.destroyed && typeof model.getBounds === 'function') {
                const bounds = model.getBounds();
                const left = Number(bounds.left ?? bounds.x);
                const top = Number(bounds.top ?? bounds.y);
                const right = Number(bounds.right ?? (left + Number(bounds.width)));
                const bottom = Number(bounds.bottom ?? (top + Number(bounds.height)));
                const renderer = this.pixi_app && this.pixi_app.renderer;
                const screen = renderer && renderer.screen;
                const rendererW = Number(screen && screen.width);
                const rendererH = Number(screen && screen.height);
                const niriVirtualViewport = getLive2DNiriPetVirtualViewportSize();
                const viewportLeft = 0;
                const viewportTop = 0;
                const viewportRight = Math.max(0, niriVirtualViewport
                    ? niriVirtualViewport.width
                    : (Number.isFinite(rendererW) && rendererW > 0
                        ? Math.min(rendererW, Number(window.innerWidth) || rendererW)
                        : Number(window.innerWidth) || 0));
                const viewportBottom = Math.max(0, niriVirtualViewport
                    ? niriVirtualViewport.height
                    : (Number.isFinite(rendererH) && rendererH > 0
                        ? Math.min(rendererH, Number(window.innerHeight) || rendererH)
                        : Number(window.innerHeight) || 0));
                const visibleLeft = Math.max(left, viewportLeft);
                const visibleRight = Math.min(right, viewportRight);
                const visibleTop = Math.max(top, viewportTop);
                const visibleBottom = Math.min(bottom, viewportBottom);
                const width = visibleRight - visibleLeft;
                const height = visibleBottom - visibleTop;
                if (Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0) {
                    return {
                        left: visibleLeft,
                        right: visibleRight,
                        top: visibleTop,
                        bottom: visibleBottom,
                        width: width,
                        height: height,
                        centerX: visibleLeft + width / 2,
                        centerY: visibleTop + height / 2
                    };
                }
            }
        }

        const model = this.currentModel;
        if (!model) {
            return null;
        }

        if (typeof model.getBounds !== 'function') {
            return null;
        }

        let bounds = null;
        try {
            bounds = model.getBounds();
        } catch (error) {
            console.warn('[Live2D] 获取模型屏幕边界失败:', error);
            return null;
        }

        if (!bounds) {
            return null;
        }

        const left = Number(bounds.left);
        const right = Number(bounds.right);
        const top = Number(bounds.top);
        const bottom = Number(bounds.bottom);

        if (!Number.isFinite(left) || !Number.isFinite(right) ||
            !Number.isFinite(top) || !Number.isFinite(bottom)) {
            return null;
        }

        const width = right - left;
        const height = bottom - top;
        if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
            return null;
        }

        const stableBounds = {
            left: left,
            right: right,
            top: top,
            bottom: bottom,
            width: width,
            height: height,
            centerX: left + width / 2,
            centerY: top + height / 2
        };

        return stableBounds;
    }

    // 复位模型位置和缩放到初始状态
    async resetModelPosition() {
        if (typeof this.clearLive2DPeek === 'function') {
            this.clearLive2DPeek('reset-model-position');
        }
        if (!this.currentModel || !this.pixi_app) {
            console.warn('无法复位：模型或PIXI应用未初始化');
            return;
        }

        try {
            let resetViewport = null;
            if (isMobileWidth()) {
                this.currentModel.anchor.set(0.5, 0.1);
                const viewportWidth = Math.max(window.innerWidth || this.pixi_app.renderer.screen.width || 1, 1);
                const viewportHeight = Math.max(window.innerHeight || this.pixi_app.renderer.screen.height || 1, 1);
                resetViewport = { width: viewportWidth, height: viewportHeight };
                const scale = Math.min(
                    0.5,
                    viewportHeight * 1.3 / 4000,
                    viewportWidth * 1.2 / 2000
                );
                this.currentModel.scale.set(scale);
                this.currentModel.x = viewportWidth * 0.5;
                this.currentModel.y = viewportHeight * 0.28;
            } else {
                this.currentModel.anchor.set(0.65, 0.75);
                const scale = Math.min(
                    0.5,
                    (window.innerHeight * 0.75) / 7000,
                    (window.innerWidth * 0.6) / 7000
                );
                this.currentModel.scale.set(scale);
                this.currentModel.x = this.pixi_app.renderer.screen.width;
                this.currentModel.y = this.pixi_app.renderer.screen.height;
            }

            console.log('模型位置已复位到初始状态');

            // 复位后自动保存位置；手机端沿用本次复位的 viewport，避免坐标与元数据基准不一致。
            if (this._lastLoadedModelPath) {
                const viewport = resetViewport || {
                    width: this.pixi_app.renderer.screen.width,
                    height: this.pixi_app.renderer.screen.height
                };
                const saveSuccess = await this.saveUserPreferences(
                    this._lastLoadedModelPath,
                    { x: this.currentModel.x, y: this.currentModel.y },
                    { x: this.currentModel.scale.x, y: this.currentModel.scale.y },
                    null, null, viewport
                );
                if (saveSuccess) {
                    console.log('模型位置已保存');
                } else {
                    console.warn('模型位置保存失败');
                }
            }

        } catch (error) {
            console.error('复位模型位置时出错:', error);
        }
    }

    /**
     * 【统一状态管理】设置锁定状态并同步更新所有相关 UI
     * @param {boolean} locked - 是否锁定
     * @param {Object} options - 可选配置
     * @param {boolean} options.updateFloatingButtons - 是否同时控制浮动按钮显示（默认 true）
     */
    setLocked(locked, options = {}) {
        const { updateFloatingButtons = true } = options;

        // 1. 更新状态
        this.isLocked = locked;

        // 2. 更新锁图标样式（使用存储的引用，避免每次 querySelector）
        if (this._lockIconImages) {
            const { locked: imgLocked, unlocked: imgUnlocked } = this._lockIconImages;
            if (imgLocked) imgLocked.style.opacity = locked ? '1' : '0';
            if (imgUnlocked) imgUnlocked.style.opacity = locked ? '0' : '1';
        }

        // 3. 更新 canvas 的 pointerEvents
        const container = document.getElementById('live2d-canvas');
        if (container) {
            container.style.pointerEvents = locked ? 'none' : 'auto';
        }

        if (!locked) {
            const live2dContainer = document.getElementById('live2d-container');
            if (live2dContainer) {
                live2dContainer.classList.remove('locked-hover-fade');
            }
        }

        // 4. 控制浮动按钮显示（可选）
        if (updateFloatingButtons) {
            const floatingButtons = document.getElementById('live2d-floating-buttons');
            if (floatingButtons) {
                floatingButtons.style.display = locked ? 'none' : 'flex';
            }
        }
    }

    /**
     * 【统一状态管理】更新浮动按钮的激活状态和图标
     * @param {string} buttonId - 按钮ID（如 'mic', 'screen', 'agent' 等）
     * @param {boolean} active - 是否激活
     */
    setButtonActive(buttonId, active) {
        const buttonData = this._floatingButtons && this._floatingButtons[buttonId];
        if (!buttonData || !buttonData.button) return;

        // 更新 dataset
        buttonData.button.dataset.active = active ? 'true' : 'false';

        // 更新背景色（使用 CSS 变量，确保暗色模式正确）
        buttonData.button.style.background = active
            ? 'var(--neko-btn-bg-active, rgba(255, 255, 255, 0.75))'
            : 'var(--neko-btn-bg, rgba(255, 255, 255, 0.65))';

        // 更新图标
        if (buttonData.imgOff) {
            buttonData.imgOff.style.opacity = active ? '0' : '0.75';
        }
        if (buttonData.imgOn) {
            buttonData.imgOn.style.opacity = active ? '1' : '0';
        }
    }

    /**
     * 【统一状态管理】重置所有浮动按钮到默认状态
     */
    resetAllButtons() {
        if (!this._floatingButtons) return;

        Object.keys(this._floatingButtons).forEach(btnId => {
            this.setButtonActive(btnId, false);
        });
    }

    /**
     * 【统一状态管理】根据全局状态同步浮动按钮状态
     * 用于模型切换或浮动按钮重建后恢复按钮状态
     */
    _syncButtonStatesWithGlobalState() {
        if (!this._floatingButtons) return;

        // 同步语音按钮状态
        const isRecording = window.isRecording || false;
        if (this._floatingButtons.mic) {
            this.setButtonActive('mic', isRecording);
        }

        // 同步屏幕分享按钮状态
        // 屏幕分享状态通过 DOM 元素判断（screenButton 的 active class 或 stopButton 的 disabled 状态）
        let isScreenSharing = false;
        const screenButton = document.getElementById('screenButton');
        const stopButton = document.getElementById('stopButton');
        if (screenButton && screenButton.classList.contains('active')) {
            isScreenSharing = true;
        } else if (stopButton && !stopButton.disabled) {
            isScreenSharing = true;
        }
        if (this._floatingButtons.screen) {
            this.setButtonActive('screen', isScreenSharing);
        }
    }

    /**
     * 设置鼠标跟踪是否启用
     * @param {boolean} enabled - 是否启用鼠标跟踪
     */
    setMouseTrackingEnabled(enabled) {
        this._mouseTrackingEnabled = enabled;
        window.mouseTrackingEnabled = enabled;
        const effectiveEnabled = enabled && (
            window.nekoYuiGuideFaceForwardLock !== true
            || window.nekoYuiGuideIntroVoiceLookAtActive === true
        );

        if (effectiveEnabled) {
            // 重新启用时，如果模型存在且没有鼠标跟踪监听器，则启用
            if (this.currentModel && !this._mouseTrackingListener) {
                this.enableMouseTracking(this.currentModel);
            }
        } else {
            this.isFocusing = false;
            // 清除 focusController 的外部输入，使头部不受鼠标/拖拽等外部因素影响
            // 自主运动（updateNaturalMovements：呼吸、轻微摆动）通过独立管线叠加，不受影响
            // 注意：不能用 model.focus(center) — 它经过 toModelPosition + atan2 + 单位圆投影，
            // 永远产生非零值（如 targetX=1），无法真正归零
            if (this.currentModel && this.currentModel.internalModel && this.currentModel.internalModel.focusController) {
                const fc = this.currentModel.internalModel.focusController;
                fc.targetX = 0;
                fc.targetY = 0;
            }
        }
    }

    /**
     * 获取鼠标跟踪是否启用
     * @returns {boolean}
     */
    isMouseTrackingEnabled() {
        if (
            window.nekoYuiGuideFaceForwardLock === true
            && window.nekoYuiGuideIntroVoiceLookAtActive !== true
        ) {
            return false;
        }
        return this._mouseTrackingEnabled !== false;
    }

    /**
     * 设置全屏跟踪是否启用
     * @param {boolean} enabled - 是否启用全屏跟踪
     */
    setFullscreenTrackingEnabled(enabled) {
        this._fullscreenTrackingEnabled = enabled;
        window.live2dFullscreenTrackingEnabled = enabled;
        console.log(`[Live2D] 全屏跟踪已${enabled ? '开启' : '关闭'}`);
    }

    /**
     * 获取全屏跟踪是否启用
     * @returns {boolean}
     */
    isFullscreenTrackingEnabled() {
        return this._fullscreenTrackingEnabled === true;
    }
}

// 导出
window.Live2DModel = Live2DModel;
window.Live2DManager = Live2DManager;
window.isMobileWidth = isMobileWidth;

// 监听帧率变更事件
window.addEventListener('neko-frame-rate-changed', (e) => {
    const fps = e.detail?.fps;
    if (fps != null && window.live2dManager) {
        window.live2dManager.setTargetFPS(fps);
    }
});

// 监听画质变更事件：只调整 renderer 分辨率，不重载模型。
window.addEventListener('neko-render-quality-changed', (e) => {
    const quality = e.detail?.quality;
    if (!quality || !window.live2dManager) return;
    try {
        window.live2dManager.applyRenderQuality(quality);
    } catch (err) {
        console.warn('[Live2D] 应用画质设置失败:', err);
    }
});
