/**
 * MMD 核心模块 - 负责场景初始化、模型加载、渲染循环
 * 基于 @moeru/three-mmd 框架，参考 hime-display MmdManager 和 vrm-core.js 结构
 */

// 空闲低频 tick 模式常量（对齐 live2d-core.js 的同名机制）：
// 无活动时把 rAF 驱动的渲染链切到 setInterval，避免 120Hz 屏上 rAF 请求
// 让 Blink 以显示器刷新率空跑主帧生命周期。物理安全性：Bullet 以固定 1/60
// 子步积分（motion-state 插值余量），33ms 步长等价于已上线的 low 档路径。
const MMD_IDLE_FPS = 30;
const MMD_INTERACTIVE_FPS_HOLD_MS = 900;
const MMD_IDLE_GOVERNOR_INTERVAL_MS = 300;

class MMDCore {
    constructor(manager) {
        this.manager = manager;
        this.performanceMode = this.detectPerformanceMode();
        this.targetFPS = 60;
        this.frameTime = 1000 / this.targetFPS;
        this._refreshTargetFps();
        this.lastFrameTime = 0;

        // 缓存的模块引用
        this._mmdModuleCache = null;
        this._physicsModuleCache = null;
        this._outlineEffectCache = null;
    }

    // ═══════════════════ 性能检测 ═══════════════════

    detectPerformanceMode() {
        let savedMode = null;
        try {
            savedMode = localStorage.getItem('mmd_performance_mode');
            if (savedMode && ['low', 'medium', 'high'].includes(savedMode)) {
                return savedMode;
            }
            // 也尝试读取 VRM 的性能设置作为共享配置
            savedMode = localStorage.getItem('vrm_performance_mode');
            if (savedMode && ['low', 'medium', 'high'].includes(savedMode)) {
                return savedMode;
            }
        } catch (e) {
            console.debug('[MMD Core] localStorage 访问失败:', e);
        }

        try {
            const canvas = document.createElement('canvas');
            const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
            if (!gl) return 'low';

            const debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
            if (debugInfo) {
                const renderer = gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL);
                const isLowEndGPU =
                    renderer.includes('Intel') &&
                    (renderer.includes('HD Graphics') || renderer.includes('Iris') || renderer.includes('UHD'));
                const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
                const isLowEndMobile = isMobile && navigator.hardwareConcurrency <= 4;
                if (isLowEndGPU || isLowEndMobile) return 'low';
            }

            const cores = navigator.hardwareConcurrency || 4;
            if (cores <= 2) return 'low';
            if (cores <= 4) return 'medium';
            return 'high';
        } catch (e) {
            return 'medium';
        }
    }

    _getPerformancePixelRatio() {
        const devicePixelRatio = window.devicePixelRatio || 1;
        let pixelRatio;
        if (this.performanceMode === 'low') {
            pixelRatio = Math.min(1.0, devicePixelRatio);
        } else if (this.performanceMode === 'medium') {
            pixelRatio = Math.min(1.5, devicePixelRatio);
        } else {
            pixelRatio = devicePixelRatio;
        }
        pixelRatio = Math.max(1.0, pixelRatio);
        return pixelRatio;
    }

    _getQualityPixelRatio(quality) {
        const devicePixelRatio = window.devicePixelRatio || 1;
        if (quality === 'low') return Math.min(0.8, devicePixelRatio);
        if (quality === 'medium') return Math.min(1.0, devicePixelRatio);
        return this._getPerformancePixelRatio();
    }

    applyPerformanceSettings() {
        if (!this.manager.renderer) return;
        const pixelRatio = this._getPerformancePixelRatio();
        this.manager.renderer.setPixelRatio(pixelRatio);
    }

    syncRendererPixelRatio(reason = 'resize') {
        if (!this.manager.renderer) return;
        const renderer = this.manager.renderer;
        const prevPixelRatio = typeof renderer.getPixelRatio === 'function'
            ? renderer.getPixelRatio()
            : null;

        renderer.setPixelRatio(this._getQualityPixelRatio(window.renderQuality || 'medium'));

        const nextPixelRatio = typeof renderer.getPixelRatio === 'function'
            ? renderer.getPixelRatio()
            : null;
        if (Number.isFinite(prevPixelRatio) && Number.isFinite(nextPixelRatio) &&
            Math.abs(prevPixelRatio - nextPixelRatio) >= 0.001) {
            console.log('[MMD Core] renderer pixelRatio 已刷新:', {
                reason,
                prevPixelRatio,
                nextPixelRatio,
                devicePixelRatio: window.devicePixelRatio || 1
            });
        }
    }

    /**
     * 应用画质设置（模仿 VRM 的画质分级系统）
     * low:    pixelRatio=0.8, 物理关
     * medium: pixelRatio=1.0, 物理开
     * high:   pixelRatio=auto, 物理开, 描边开
     * 注意：描边设置由用户手动控制，画质设置不会覆盖用户的选择
     */
    applyQualitySettings(quality) {
        if (!this.manager.renderer) return;

        const devicePixelRatio = window.devicePixelRatio || 1;

        if (quality === 'low') {
            this.manager.renderer.setPixelRatio(Math.min(0.8, devicePixelRatio));
            this.manager.enablePhysics = false;
        } else if (quality === 'medium') {
            this.manager.renderer.setPixelRatio(Math.min(1.0, devicePixelRatio));
            this.manager.enablePhysics = true;
        } else {
            this.applyPerformanceSettings();
            this.manager.enablePhysics = true;
            if (!this.manager._userForcedOutline) {
                this.manager.useOutlineEffect = true;
            }
        }

        if (!this.manager._userForcedOutline) {
            if (quality === 'low' || quality === 'medium') {
                this.manager.useOutlineEffect = false;
            }
        }

        console.log(`[MMD] 画质设置: ${quality}, physics=${this.manager.enablePhysics}, outline=${this.manager.useOutlineEffect}, userForcedOutline=${this.manager._userForcedOutline}`);
    }

    // ═══════════════════ 模块动态导入 ═══════════════════

    async _getMMDModule() {
        if (this._mmdModuleCache) return this._mmdModuleCache;
        try {
            this._mmdModuleCache = await import('@moeru/three-mmd');
            return this._mmdModuleCache;
        } catch (error) {
            console.error('[MMD Core] 无法导入 @moeru/three-mmd:', error);
            return null;
        }
    }

    async _getPhysicsModule() {
        if (this._physicsModuleCache) return this._physicsModuleCache;
        try {
            this._physicsModuleCache = await import('@moeru/three-mmd-physics-ammo');
            return this._physicsModuleCache;
        } catch (error) {
            console.error('[MMD Core] 无法导入 @moeru/three-mmd-physics-ammo:', error);
            return null;
        }
    }

    async _getOutlineEffect() {
        if (this._outlineEffectCache) return this._outlineEffectCache;
        try {
            const module = await import('three/addons/effects/OutlineEffect.js');
            this._outlineEffectCache = module.OutlineEffect;
            return this._outlineEffectCache;
        } catch (error) {
            console.warn('[MMD Core] OutlineEffect 导入失败，将使用普通渲染:', error);
            return null;
        }
    }

    _updateLoadingOverlay(sessionId, stage, detail = '') {
        if (!sessionId || !window.MMDLoadingOverlay || typeof window.MMDLoadingOverlay.update !== 'function') {
            return;
        }
        window.MMDLoadingOverlay.update(sessionId, { stage, detail });
    }

    _formatProgressDetail(progress) {
        if (!progress || typeof progress.loaded !== 'number' || !Number.isFinite(progress.loaded)) {
            return '';
        }
        if (typeof progress.total === 'number' && Number.isFinite(progress.total) && progress.total > 0) {
            const percent = Math.max(0, Math.min(100, Math.round((progress.loaded / progress.total) * 100)));
            return `${percent}%`;
        }
        const kb = Math.max(1, Math.round(progress.loaded / 1024));
        return `${kb} KB`;
    }

    // ═══════════════════ Three.js 依赖检查 ═══════════════════

    _ensureThreeReady() {
        const THREE = window.THREE;
        if (!THREE) {
            throw new Error('Three.js 库未加载，请确保已引入 three.js');
        }
        const required = [
            { name: 'WebGLRenderer', obj: THREE.WebGLRenderer },
            { name: 'Clock', obj: THREE.Clock },
            { name: 'Scene', obj: THREE.Scene },
            { name: 'PerspectiveCamera', obj: THREE.PerspectiveCamera }
        ];
        const missing = required.filter(item => !item.obj);
        if (missing.length > 0) {
            throw new Error(`Three.js 依赖不完整，缺少: ${missing.map(item => item.name).join(', ')}`);
        }
    }

    // ═══════════════════ 场景初始化 ═══════════════════

    async init(canvasId, containerId) {
        this._ensureThreeReady();
        const THREE = window.THREE;

        this.manager._isDisposed = false;

        this.manager.container = document.getElementById(containerId);
        this.manager.canvas = document.getElementById(canvasId);

        if (!this.manager.container) {
            throw new Error(`找不到容器元素: ${containerId}`);
        }
        if (!this.manager.canvas) {
            throw new Error(`找不到 canvas 元素: ${canvasId}`);
        }

        // 配置容器样式
        const container = this.manager.container;
        container.style.display = 'block';
        container.style.visibility = 'visible';
        container.style.opacity = '1';
        container.style.width = '100%';
        container.style.height = '100%';
        container.style.position = 'fixed';
        container.style.top = '0';
        container.style.left = '0';
        container.style.setProperty('pointer-events', 'auto', 'important');
        // 【修复】确保 z-index 与 vrm-container 一致，作为 CSS 缺失时的后备
        container.style.zIndex = '10';

        this.manager.clock = new THREE.Clock();
        this.manager.scene = new THREE.Scene();
        this.manager.scene.background = null; // 透明背景

        let width = container.clientWidth || container.offsetWidth;
        let height = container.clientHeight || container.offsetHeight;
        if (width === 0 || height === 0) {
            width = window.innerWidth;
            height = window.innerHeight;
        }

        // 相机：FOV=30° (参考 hime-display，减少边缘畸变)
        this.manager.camera = new THREE.PerspectiveCamera(30, width / height, 1, 2000);
        this.manager.camera.position.set(0, 10, 70);
        this.manager.camera.lookAt(0, 10, 0);

        // WebGL 可用性检查
        const webglAvailable = (() => {
            try {
                const testCanvas = document.createElement('canvas');
                return !!(testCanvas.getContext('webgl2') || testCanvas.getContext('webgl'));
            } catch (e) {
                return false;
            }
        })();

        if (!webglAvailable) {
            console.error('[MMD Core] WebGL 不可用');
            this.manager.renderer = null;
            throw new Error('[MMD Core] WebGL 不可用，无法初始化渲染器');
        }

        // 显式启用颜色管理（确保线性工作流）
        if (THREE.ColorManagement) {
            THREE.ColorManagement.enabled = true;
        }

        try {
            this.manager.renderer = new THREE.WebGLRenderer({
                canvas: this.manager.canvas,
                alpha: true,
                antialias: true,
                powerPreference: 'high-performance',
                precision: 'highp',
                preserveDrawingBuffer: false,
                depth: true
            });
        } catch (e) {
            console.error('[MMD Core] 创建 WebGLRenderer 失败:', e);
            this.manager.renderer = null;
            throw new Error('[MMD Core] 创建 WebGLRenderer 失败: ' + (e.message || e));
        }

        this.manager.renderer.setSize(width, height);
        this.manager.renderer.setClearColor(0x000000, 0); // 透明背景
        this.manager.renderer.debug.checkShaderErrors = true;
        this.applyPerformanceSettings();
        this.applyQualitySettings(window.renderQuality || 'medium');

        // 颜色空间
        if (THREE.SRGBColorSpace !== undefined) {
            this.manager.renderer.outputColorSpace = THREE.SRGBColorSpace;
        } else if (THREE.sRGBEncoding !== undefined) {
            this.manager.renderer.outputEncoding = THREE.sRGBEncoding;
        }

        // 默认使用 NeutralToneMapping（色调平衡、对比度适中）
        this.manager.renderer.toneMapping = THREE.NeutralToneMapping;
        this.manager.renderer.toneMappingExposure = 1.0;

        // Canvas 样式
        const canvas = this.manager.renderer.domElement;
        canvas.style.setProperty('pointer-events', 'auto', 'important');
        canvas.style.setProperty('touch-action', 'none', 'important');
        canvas.style.setProperty('user-select', 'none', 'important');
        canvas.style.cursor = 'default';
        canvas.style.width = '100%';
        canvas.style.height = '100%';
        canvas.style.display = 'block';

        // OutlineEffect（MMD 描边）
        const OutlineEffect = await this._getOutlineEffect();
        if (OutlineEffect) {
            this.manager.effect = new OutlineEffect(this.manager.renderer);
            // Three.js r180 的 OutlineEffect 未初始化 autoClear 属性（值为 undefined/falsy），
            // 导致每帧正常渲染 pass 前不清除深度缓冲区，上一帧描边 pass 写入的深度值
            // 会遮挡下一帧模型的正常渲染，使模型表面永久透明，只剩黑色描边可见。
            // 显式设为 true 确保每帧正常 pass 前清除颜色和深度缓冲区。
            this.manager.effect.autoClear = true;
            this.manager.useOutlineEffect = true;
        } else {
            this.manager.effect = null;
            this.manager.useOutlineEffect = false;
        }

        this.manager.scene.add(this.manager.camera);

        // 光照设置（参考 hime-display: AmbientLight intensity=3, DirectionalLight intensity=2）
        const ambientLight = new THREE.AmbientLight(0xaaaaaa, 3);
        this.manager.scene.add(ambientLight);
        this.manager.ambientLight = ambientLight;

        const directionalLight = new THREE.DirectionalLight(0xffffff, 2);
        directionalLight.position.set(-1, 1, 1).normalize();
        directionalLight.castShadow = false;
        this.manager.scene.add(directionalLight);
        this.manager.directionalLight = directionalLight;

        // 窗口 resize 监听
        if (!this.manager._coreWindowHandlers) {
            this.manager._coreWindowHandlers = [];
        }

        if (!this.manager._resizeHandler) {
            this.manager._resizeHandler = () => {
                this.onWindowResize();
            };
        }

        const alreadyRegistered = this.manager._coreWindowHandlers.some(
            h => h.event === 'resize' && h.handler === this.manager._resizeHandler
        );
        if (!alreadyRegistered) {
            this.manager._coreWindowHandlers.push({ event: 'resize', handler: this.manager._resizeHandler });
            window.addEventListener('resize', this.manager._resizeHandler);
        }

        if (!this.manager._displayChangeHandler) {
            this.manager._displayChangeHandler = () => {
                requestAnimationFrame(() => {
                    this.onWindowResize('electron-display-changed');
                    requestAnimationFrame(() => this.onWindowResize('electron-display-changed:settled'));
                    setTimeout(() => this.onWindowResize('electron-display-changed:delayed'), 120);
                });
            };
        }

        const alreadyDisplayRegistered = this.manager._coreWindowHandlers.some(
            h => h.event === 'electron-display-changed' && h.handler === this.manager._displayChangeHandler
        );
        if (!alreadyDisplayRegistered) {
            this.manager._coreWindowHandlers.push({ event: 'electron-display-changed', handler: this.manager._displayChangeHandler });
            window.addEventListener('electron-display-changed', this.manager._displayChangeHandler);
        }

        // 监听画质变更事件
        const qualityChangeHandler = (e) => {
            const quality = e.detail?.quality;
            if (quality && window.mmdManager?.core) {
                window.mmdManager.core.applyQualitySettings(quality);
                window.mmdManager.core.onWindowResize('neko-render-quality-changed');
            }
        };
        const alreadyQualityRegistered = this.manager._coreWindowHandlers.some(
            h => h.event === 'neko-render-quality-changed' && h.handler === qualityChangeHandler
        );
        if (!alreadyQualityRegistered) {
            this.manager._coreWindowHandlers.push({ event: 'neko-render-quality-changed', handler: qualityChangeHandler });
            window.addEventListener('neko-render-quality-changed', qualityChangeHandler);
        }

        // 应用已保存的调试渲染设置
        if (typeof window.applyMMDSavedDebugSettings === 'function') {
            try {
                window.applyMMDSavedDebugSettings();
                console.log('[MMD Core] 已应用保存的调试渲染设置');
            } catch (e) {
                console.warn('[MMD Core] 应用调试设置失败:', e);
            }
        }

        console.log('[MMD Core] 场景初始化完成');
    }

    // ═══════════════════ 模型加载 ═══════════════════

    async loadModel(modelUrl, options = {}, managerLoadToken = null) {
        const THREE = window.THREE;
        const embed = options.embed === true;
        if (!THREE) {
            throw new Error('Three.js 库未加载，无法加载 MMD 模型');
        }

        if (!modelUrl || typeof modelUrl !== 'string' || modelUrl.trim() === '') {
            throw new Error(`MMD 模型路径无效: ${modelUrl}`);
        }

        // 加载令牌由 mmd-manager.js 分配并传入；未传时回退读当前值（等价旧行为）。
        // 必须在首个 await 之前确定：旧实现在模块导入 await 之后才读当前值，悬挂期间
        // 若有新调用进入会误拿新调用的 token，使下方的取代检查失效（幽灵模型叠加）。
        const loadToken = managerLoadToken !== null ? managerLoadToken : this.manager._activeLoadToken;

        // 已被更新的加载取代（如失败回退路径迟到）：在触碰 currentModel 之前退出，
        // 避免把新调用刚装好的模型 _clearModel 掉
        if (this.manager._activeLoadToken !== loadToken) {
            console.log('[MMD Core] 模型加载已被取代（进入时），跳过');
            return null;
        }

        const mmdModule = await this._getMMDModule();
        if (!mmdModule) {
            throw new Error('three-mmd 模块加载失败');
        }

        // 模块导入悬挂期间同样可能被取代：同上，先于 _clearModel 检查
        if (this.manager._activeLoadToken !== loadToken) {
            console.log('[MMD Core] 模型加载已被取代（模块导入后），跳过');
            return null;
        }

        const { MMDLoader } = mmdModule;
        const loadingSessionId = options.loadingSessionId || '';

        // 自定义 LoadingManager: 追踪加载失败的纹理 URL
        const loadManager = new THREE.LoadingManager();
        loadManager._failedUrls = [];
        loadManager._allResourcesLoaded = false;
        loadManager.onError = (url) => {
            console.warn(`[MMD Core] 资源加载失败: ${url}`);
            loadManager._failedUrls.push(url);
        };
        // 所有资源（含失败的）加载完成后，检查并修复缺失纹理
        // 使用 _pendingMmd 捕获模型引用，避免 onLoad 在 currentModel 赋值前触发时跳过检查
        this._pendingMmd = null;
        loadManager.onLoad = () => {
            loadManager._allResourcesLoaded = true;
            console.log('[MMD Core] 所有资源加载完成，检查纹理状态');
            const mmd = this._pendingMmd || this.manager.currentModel;
            if (mmd) {
                if (this._fixMissingTexturesTimer) {
                    clearTimeout(this._fixMissingTexturesTimer);
                    this._fixMissingTexturesTimer = null;
                }
                this._fixMissingTextures(mmd);
            }
        };
        this.manager._loadManager = loadManager;

        const loader = new MMDLoader([], loadManager);

        // 清理旧模型
        if (this.manager.currentModel) {
            this._clearModel();
        }

        try {
            // MMDLoader.load 返回 MMD 对象
            this._updateLoadingOverlay(loadingSessionId, 'model');
            const mmd = await new Promise((resolve, reject) => {
                loader.load(
                    modelUrl,
                    (mmd) => {
                        this._pendingMmd = mmd;
                        resolve(mmd);
                    },
                    (progress) => {
                        this._updateLoadingOverlay(
                            loadingSessionId,
                            'model',
                            this._formatProgressDetail(progress)
                        );
                        if (options.onProgress) {
                            options.onProgress(progress);
                        }
                    },
                    (error) => reject(error)
                );
            });

            // 异步竞态检查：如果在加载期间又触发了新的加载，丢弃此结果
            if (this.manager._activeLoadToken !== loadToken) {
                console.log('[MMD Core] 模型加载已被取代，丢弃旧模型');
                // 释放已加载但不需要的模型资源
                if (mmd.mesh) {
                    if (mmd.mesh.geometry) mmd.mesh.geometry.dispose();
                    if (mmd.mesh.material) {
                        const mats = Array.isArray(mmd.mesh.material) ? mmd.mesh.material : [mmd.mesh.material];
                        mats.forEach(m => {
                            if (m.map) m.map.dispose();
                            if (m.gradientMap) m.gradientMap.dispose();
                            if (m.matcap) m.matcap.dispose();
                            if (m.normalMap) m.normalMap.dispose();
                            if (m.emissiveMap) m.emissiveMap.dispose();
                            if (m.specularMap) m.specularMap.dispose();
                            m.dispose();
                        });
                    }
                    if (mmd.mesh.skeleton) mmd.mesh.skeleton.dispose();
                }
                return null;
            }

            // 存储模型引用
            this.manager.currentModel = mmd;
            this.manager.currentModel.url = modelUrl;
            this.manager.scene.add(mmd.mesh);

            // 材质后处理：修正纹理颜色空间 + 强制材质更新
            this._postProcessMaterials(mmd);

            // 初始化物理引擎
            if (this.manager.enablePhysics) {
                this._updateLoadingOverlay(loadingSessionId, 'physics');
                await this._initPhysics(mmd, loadingSessionId);
                // 如果有存储的物理强度设置，应用到刚初始化的物理引擎
                const strength = this.manager.physicsStrength;
                if (strength !== 1.0 && mmd.physics && typeof mmd.physics.setGravity === 'function') {
                    const THREE = window.THREE;
                    if (THREE) {
                        mmd.physics.setGravity(new THREE.Vector3(0, this.manager._baseGravityY * strength, 0));
                    }
                }
            }

            // macOS WebGL 崩溃修复：首次加载延迟渲染
            if (!this.manager._shouldRender) {
                this.manager._shouldRender = true;
                const isMacOS = /macintosh|mac os x/i.test(navigator.userAgent);
                if (isMacOS) {
                    console.log('[MMD Core] macOS 检测到，延迟 1s 启动渲染（防止 WebGL 崩溃）');
                    setTimeout(() => this._startRenderLoop(), 1000);
                } else {
                    this._startRenderLoop();
                }
            }

            // 构建模型信息
            const modelInfo = this._buildModelInfo(mmd);
            console.log('[MMD Core] 模型加载完成:', modelInfo.name);

            // 材质诊断日志
            this._logMaterialDiagnostics(mmd);

            // The forge embed owns its own camera and model coordinate system.
            // Desktop position/scale/camera preferences must never leak into it.
            if (!embed) {
                await this._restoreUserPreferences(mmd, modelUrl);
            }

            return modelInfo;
        } catch (error) {
            console.error('[MMD Core] 模型加载失败:', error);
            throw error;
        }
    }

    // ═══════════════════ 材质后处理 ═══════════════════

    /**
     * 模型加载后修复 MMDToonMaterial（ShaderMaterial 子类）的渲染问题。
     *
     * 核心问题：Three.js r180 的 refreshMaterialUniforms 对 isShaderMaterial 不调用
     * refreshUniformsCommon，因此 mapTransform / gradientMapTransform 等 texture transform
     * uniform 永远不会被从纹理矩阵同步到 uniform，保持为 UniformsLib 的初始 Matrix3()。
     * 对 MMDToonMaterial 这不是问题（纹理变换默认就是 identity），但 r180 prefix 中
     * 使用 mapTransform 来做 uv 变换，uniform 必须存在且可上传。
     *
     * 真正的兼容性修复：
     * 1. 确保 colorSpace 正确
     * 2. 标记 material.needsUpdate = true，强制以当前材质属性重编译 shader
     *    （MMDToonMaterial 的 uniform 通过 property descriptor 直接挂在 uniforms 上，
     *     重编译后 WebGLUniforms.upload 直接读取 material.uniforms，不依赖 refreshUniformsCommon）
     */
    _postProcessMaterials(mmd) {
        const THREE = window.THREE;
        if (!mmd || !mmd.mesh) return;

        const srgb = THREE.SRGBColorSpace;
        let textureFixed = 0;
        let materialCount = 0;

        const materials = Array.isArray(mmd.mesh.material) ? mmd.mesh.material : [mmd.mesh.material];
        materials.forEach(mat => {
            if (!mat) return;
            materialCount++;

            let matChanged = false;

            // 遍历纹理属性，修正 colorSpace
            const textureProps = ['map', 'matcap', 'gradientMap', 'emissiveMap', 'specularMap'];
            textureProps.forEach(prop => {
                const tex = mat.uniforms?.[prop]?.value;
                if (tex && tex.isTexture && srgb && tex.colorSpace !== srgb) {
                    // gradientMap 不需要 sRGB（它是 data 纹理）
                    if (prop === 'gradientMap') return;
                    tex.colorSpace = srgb;
                    if (tex.image) {
                        tex.needsUpdate = true;
                    }
                    textureFixed++;
                    matChanged = true;
                }
            });

            // 仅在实际修改了纹理属性时才强制 shader 重编译，
            // 避免对未加载纹理 (version=0) 触发 texSubImage2D 错误
            if (matChanged) {
                mat.needsUpdate = true;
            }
        });

        if (textureFixed > 0) {
            console.log(`[MMD Core] 材质后处理: 修正了 ${textureFixed} 个纹理的 colorSpace (${materialCount} 个材质)`);
        }
        console.log(`[MMD Core] 材质后处理完成: ${materialCount} 个材质`);

        // 兜底：如果 LoadingManager.onLoad 30 秒内未触发，强制检查纹理状态
        // （正常情况下 onLoad 会在所有资源加载完后触发，此定时器仅防极端情况）
        this._fixMissingTexturesTimer = setTimeout(() => {
            this._fixMissingTexturesTimer = null;
            const loadManager = this.manager._loadManager;
            if (loadManager && !loadManager._allResourcesLoaded) {
                console.warn('[MMD Core] 纹理加载兜底超时（30s），强制检查纹理状态');
                this._fixMissingTextures(mmd);
            }
        }, 30000);
    }

    /**
     * 修复加载失败的纹理：替换为 1x1 白色 fallback 纹理。
     * 当纹理文件不存在（404）时，map 采样结果为 (0,0,0,0) 导致模型完全透明。
     * 替换为白色纹理后，模型至少能以平面 diffuse 颜色渲染。
     */
    _fixMissingTextures(mmd) {
        const THREE = window.THREE;
        if (!THREE || !mmd?.mesh) return;

        const materials = Array.isArray(mmd.mesh.material) ? mmd.mesh.material : [mmd.mesh.material];
        // 只检查影响渲染可见性的纹理属性（不含 gradientMap）
        const textureProps = ['map', 'matcap', 'emissiveMap', 'specularMap'];
        let fixedCount = 0;
        const missingList = [];

        // 懒创建 1x1 白色 fallback 纹理
        let fallback = null;
        const getFallback = () => {
            if (!fallback) {
                const data = new Uint8Array([255, 255, 255, 255]);
                fallback = new THREE.DataTexture(data, 1, 1, THREE.RGBAFormat);
                fallback.needsUpdate = true;
                fallback.colorSpace = THREE.SRGBColorSpace;
            }
            return fallback;
        };

        materials.forEach((mat, i) => {
            if (!mat?.uniforms) return;
            let matFixed = false;

            textureProps.forEach(prop => {
                const tex = mat.uniforms[prop]?.value;
                if (!tex?.isTexture) return;

                // version === 0 且 source.data 为空 = 纹理从未成功加载
                if (tex.version === 0 && tex.source?.data == null) {
                    missingList.push(`mat[${i}] "${mat.name || ''}" .${prop}`);
                    mat.uniforms[prop].value = getFallback();
                    matFixed = true;
                    fixedCount++;
                }
            });

            if (matFixed) {
                mat.needsUpdate = true;
            }
        });

        if (fixedCount > 0) {
            console.warn(`[MMD Core] ⚠️ ${fixedCount} 个纹理加载失败（文件不存在），已替换为白色 fallback:`);
            missingList.forEach(p => console.warn(`  - ${p}`));

            // 输出 LoadingManager 记录的失败 URL
            const failedUrls = this.manager._loadManager?._failedUrls;
            if (failedUrls && failedUrls.length > 0) {
                console.warn('[MMD Core] 加载失败的纹理 URL:');
                failedUrls.forEach(url => console.warn(`  ✗ ${url}`));
            }

            console.warn('[MMD Core] 请将模型的完整纹理文件放到与 PMX 文件相同的目录结构中。');
        } else {
            console.log('[MMD Core] 纹理加载状态检查: 全部正常');
        }
    }

    /**
     * 输出材质诊断信息到控制台 + 渲染像素检查
     */
    _logMaterialDiagnostics(mmd) {
        if (!mmd || !mmd.mesh) return;
        const THREE = window.THREE;

        const renderer = this.manager.renderer;
        const materials = Array.isArray(mmd.mesh.material) ? mmd.mesh.material : [mmd.mesh.material];

        console.group('[MMD Core] 材质诊断');
        console.log('渲染器:', {
            outputColorSpace: renderer?.outputColorSpace,
            toneMapping: renderer?.toneMapping,
            toneMappingExposure: renderer?.toneMappingExposure,
            pixelRatio: renderer?.getPixelRatio(),
            colorManagement: THREE.ColorManagement?.enabled,
            checkShaderErrors: renderer?.debug?.checkShaderErrors
        });
        console.log(`材质总数: ${materials.length}`);

        // 详细输出前 8 个材质
        materials.slice(0, 8).forEach((mat, i) => {
            if (!mat) return;
            const mapTex = mat.uniforms?.map?.value;
            const gradTex = mat.uniforms?.gradientMap?.value;
            const info = {
                type: mat.type,
                isShaderMaterial: mat.isShaderMaterial,
                visible: mat.visible,
                lights: mat.lights,
                opacity: mat.uniforms?.opacity?.value,
                transparent: mat.transparent,
                blending: mat.blending,
                side: mat.side,
                depthTest: mat.depthTest,
                depthWrite: mat.depthWrite,
                diffuse: mat.uniforms?.diffuse?.value
                    ? `rgb(${mat.uniforms.diffuse.value.r.toFixed(3)}, ${mat.uniforms.diffuse.value.g.toFixed(3)}, ${mat.uniforms.diffuse.value.b.toFixed(3)})`
                    : 'undefined',
                emissive: mat.uniforms?.emissive?.value
                    ? `rgb(${mat.uniforms.emissive.value.r.toFixed(3)}, ${mat.uniforms.emissive.value.g.toFixed(3)}, ${mat.uniforms.emissive.value.b.toFixed(3)})`
                    : 'undefined',
                map: mapTex ? `cs=${mapTex.colorSpace}, img=${mapTex.image?.width}x${mapTex.image?.height}, flipY=${mapTex.flipY}` : 'none',
                gradientMap: gradTex ? `cs=${gradTex.colorSpace}, img=${gradTex.image?.width}x${gradTex.image?.height}` : 'none',
                defines: mat.defines ? Object.keys(mat.defines).join(', ') : 'none',
                version: mat.version,
                vertexShaderLen: mat.vertexShader?.length || 0,
                fragmentShaderLen: mat.fragmentShader?.length || 0,
                uniformKeys: mat.uniforms ? Object.keys(mat.uniforms).length : 0
            };
            console.log(`  材质[${i}] ${mat.name || ''}:`, info);
        });

        if (materials.length > 8) {
            console.log(`  ... 还有 ${materials.length - 8} 个材质`);
        }

        console.groupEnd();
    }

    // ═══════════════════ 物理引擎 ═══════════════════

    async _initPhysics(mmd, loadingSessionId = '') {
        const physicsModule = await this._getPhysicsModule();
        if (!physicsModule) {
            console.warn('[MMD Core] 物理模块不可用，跳过物理初始化');
            return;
        }

        const { MMDAmmoPhysics, initAmmo } = physicsModule;

        try {
            this._updateLoadingOverlay(loadingSessionId, 'physics');
            // 初始化 Ammo.js
            await initAmmo();
            console.log('[MMD Core] Ammo.js 初始化完成');

            // 为模型设置物理
            mmd.setPhysics(MMDAmmoPhysics);
            console.log('[MMD Core] 物理引擎已绑定');

            // 物理 warmup：预计算 60 帧以稳定物理状态
            // 关键修复：warmup 期间运行 Grant 求解器（付与変換），
            // 确保 kinematic 骨骼在 warmup 时处于与渲染时一致的位置。
            // 大模型（>200 刚体）需要更多 warmup 帧让复杂约束链稳定
            const bodyCount = mmd.physics && typeof mmd.physics.getPhysics === 'function'
                ? mmd.physics.getPhysics().bodies.length : 0;
            const warmupFrames = bodyCount > 200 ? 180 : 60;
            const warmupDelta = 1 / 60;

            let warmupGrantSolver = null;
            try {
                const mmdModule = await this._getMMDModule();
                if (mmdModule && mmdModule.GrantSolver && mmd.grants && mmd.grants.length > 0) {
                    warmupGrantSolver = new mmdModule.GrantSolver(mmd.mesh, mmd.grants);
                    console.log(`[MMD Physics] Warmup Grant solver created (${mmd.grants.length} grants)`);
                }
            } catch (e) {
                console.warn('[MMD Physics] Failed to create warmup Grant solver:', e);
            }

            if (mmd.mesh) mmd.mesh.updateMatrixWorld(true);
            if (warmupGrantSolver) warmupGrantSolver.update();

            for (let i = 0; i < warmupFrames; i++) {
                if (warmupGrantSolver) warmupGrantSolver.update();
                mmd.update(warmupDelta);
            }

            // Refresh stability baselines after the full warmup so frozen-body
            // restorations use the settled (post-warmup) pose, not the pre-warmup one.
            if (typeof mmd.physics.getPhysics === 'function') {
                const inner = mmd.physics.getPhysics();
                if (typeof inner.refreshStabilityBaseline === 'function') {
                    inner.refreshStabilityBaseline();
                }
            }
            console.log(`[MMD Core] 物理 warmup 完成 (${warmupFrames} 帧, Grant: ${!!warmupGrantSolver})`);
        } catch (error) {
            console.warn('[MMD Core] 物理初始化失败:', error);
        }
    }

    // ═══════════════════ 模型信息 ═══════════════════

    _buildModelInfo(mmd) {
        const mesh = mmd.mesh;
        const pmx = mmd.pmx;
        const geometry = mesh.geometry;

        return {
            name: pmx?.header?.modelName || '未知模型',
            comment: pmx?.header?.comment || '',
            vertexCount: geometry?.attributes?.position?.count || 0,
            triangleCount: geometry?.index ? geometry.index.count / 3 : 0,
            boneCount: mesh.skeleton?.bones?.length || 0,
            ikCount: mmd.iks?.length || 0,
            grantCount: mmd.grants?.length || 0,
            morphCount: Object.keys(geometry?.morphAttributes?.position || {}).length,
            morphNames: geometry?.morphTargetDictionary ? Object.keys(geometry.morphTargetDictionary) : [],
            rigidBodyCount: pmx?.rigidBodies?.length || 0,
            constraintCount: pmx?.joints?.length || 0,
            hasPhysics: !!mmd.physics
        };
    }

    // ═══════════════════ 模型清理 ═══════════════════

    _clearModel() {
        // 清理纹理修复兜底定时器（必须在早退之前，防止 currentModel 被外部提前置空时 timer 泄漏）
        if (this._fixMissingTexturesTimer) {
            clearTimeout(this._fixMissingTexturesTimer);
            this._fixMissingTexturesTimer = null;
        }

        const mmd = this.manager.currentModel;
        if (!mmd) return;

        // 停止动画
        if (this.manager.animationModule) {
            this.manager.animationModule.dispose();
        }

        // 清理物理
        if (mmd.physics && mmd.physics.dispose) {
            mmd.physics.dispose();
        }

        // 从场景移除（优先通过 parent，回退到 scene.remove）
        if (mmd.mesh) {
            if (mmd.mesh.parent) {
                mmd.mesh.parent.remove(mmd.mesh);
            } else if (this.manager.scene) {
                this.manager.scene.remove(mmd.mesh);
            }
        }

        // 清理 SkinnedMesh 资源
        if (mmd.mesh) {
            if (mmd.mesh.geometry) {
                mmd.mesh.geometry.dispose();
            }
            if (mmd.mesh.material) {
                const materials = Array.isArray(mmd.mesh.material) ? mmd.mesh.material : [mmd.mesh.material];
                materials.forEach(mat => {
                    if (mat.map) mat.map.dispose();
                    if (mat.gradientMap) mat.gradientMap.dispose();
                    if (mat.matcap) mat.matcap.dispose();
                    if (mat.normalMap) mat.normalMap.dispose();
                    if (mat.emissiveMap) mat.emissiveMap.dispose();
                    if (mat.specularMap) mat.specularMap.dispose();
                    mat.dispose();
                });
            }
            if (mmd.mesh.skeleton) {
                mmd.mesh.skeleton.dispose();
            }
        }

        this.manager.currentModel = null;
        console.log('[MMD Core] 模型资源已清理');
    }

    // ═══════════════════ 渲染循环 ═══════════════════

    _startRenderLoop() {
        // 退出空闲低频模式但不让它复跑 rAF（下面自建新链），避免双驱动
        this._exitIdleTickMode(false);
        if (this.manager._animationFrameId) {
            cancelAnimationFrame(this.manager._animationFrameId);
        }
        this._render();
        this._startIdleGovernor();
    }

    // ═══════════════════ 空闲低频 tick 模式（对齐 live2d-core.js round-1 模式）═══════════════════

    // 用户配置的目标帧率（设置里的「帧率」滑块，与 Live2D/VRM 同源）；0 = 不限帧。
    // 没有该配置（独立预览页等）时退回 performanceMode 的档位值。
    _resolveConfiguredTargetFps() {
        const raw = typeof window.targetFrameRate === 'number' ? Number(window.targetFrameRate) : NaN;
        if (Number.isFinite(raw) && raw >= 0) return raw;
        return this.performanceMode === 'low' ? 30 : (this.performanceMode === 'medium' ? 45 : 60);
    }

    // 同步 targetFPS/frameTime 到当前配置；rAF 路径每帧调用，配置变更立即生效。
    _refreshTargetFps() {
        const fps = this._resolveConfiguredTargetFps();
        if (fps === this.targetFPS) return;
        this.targetFPS = fps;
        this.frameTime = fps > 0 ? 1000 / fps : 0;
    }

    // 静止地板帧率：不超过用户配置；配置为 0（不限帧）时仍压到地板省 CPU。
    _resolveIdleFps() {
        const configured = this._resolveConfiguredTargetFps();
        return configured === 0 ? MMD_IDLE_FPS : Math.min(MMD_IDLE_FPS, configured);
    }

    // 活动态应改用定时器驱动的帧率（Pet 窗口且配置帧率明显低于显示器刷新率时由
    // frame-pacing.js 给出）；null = 留在 rAF 驱动。非 Pet 页面没有 nekoFramePacing，恒为 null。
    _resolveActiveTimerTickFps() {
        const pacing = window.nekoFramePacing;
        if (!pacing || typeof pacing.activeTimerTickFps !== 'function') return null;
        // 传入本后端解析出的目标帧率：没有 window.targetFrameRate 时 MMD 按 performanceMode
        // 回退 30/45/60，总闸不能自己按 60 算
        const fps = Number(pacing.activeTimerTickFps(this._resolveConfiguredTargetFps()));
        return Number.isFinite(fps) && fps > 0 ? fps : null;
    }

    // 帧率设置变更：有活动按新配置重新升帧；空闲定时器模式按新地板换周期。
    applyTargetFrameRate() {
        this._refreshTargetFps();
        const m = this.manager;
        if (!m || !m._shouldRender || m._isDisposed || !m.renderer) return;
        if (this._hasRenderActivity()) {
            this._boostInteractiveFPS();
        } else if (this._idleTickMode) {
            this._enterIdleTickMode(this._resolveIdleFps());
        }
    }

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
        const manager = this.manager;
        if (!manager || !manager._shouldRender || manager._isDisposed || !manager.renderer) return;
        this._idleTickMode = true;
        if (manager._animationFrameId) {
            cancelAnimationFrame(manager._animationFrameId);
            manager._animationFrameId = null;
        }
        this._idleTickTimer = this._startTimerTick(tickFps);
    }

    // 定时器驱动的 tick 循环：空闲地板（30）和活动态定时器驱动（配置帧率）共用，只有周期不同。
    _startTimerTick(fps) {
        this._idleTickFps = fps;
        const intervalMs = Math.max(4, Math.round(1000 / Math.max(1, fps)));
        return setInterval(() => {
            if (!this._idleTickMode) return;
            const m = this.manager;
            if (!m || !m._shouldRender || m._isDisposed || !m.renderer) { this._exitIdleTickMode(false); return; }
            // 外部代码绕过 _startRenderLoop 直接拉起了 rAF 链：让位
            if (m._animationFrameId) { this._exitIdleTickMode(false); return; }
            // 纯浏览器隐藏标签页对齐 rAF 暂停语义（Electron 宠物窗关了节流，不受影响）
            if (typeof document !== 'undefined' && document.hidden) return;
            try {
                this._renderFrame(performance.now());
            } catch (e) {
                if (!this._idleTickErrorLogged) {
                    this._idleTickErrorLogged = true;
                    console.warn('[MMD Core] 空闲低频 tick 渲染异常（同类后续不再打印）:', e);
                }
            }
        }, intervalMs);
    }

    _exitIdleTickMode(restartRaf = true) {
        if (!this._idleTickMode) return;
        this._idleTickMode = false;
        this._idleTickFps = null;
        if (this._idleTickTimer) {
            clearInterval(this._idleTickTimer);
            this._idleTickTimer = null;
        }
        const manager = this.manager;
        if (restartRaf && manager && manager._shouldRender && !manager._isDisposed && !manager._animationFrameId) {
            this.lastFrameTime = 0;
            manager._animationFrameId = requestAnimationFrame(() => this._render());
        }
    }

    // 有交互/活动时升回 rAF 满帧，并安排在 durationMs 后无活动则回落空闲模式
    _boostInteractiveFPS(durationMs = MMD_INTERACTIVE_FPS_HOLD_MS) {
        const timerFps = this._resolveActiveTimerTickFps();
        if (timerFps != null) {
            // Pet 窗口且配置帧率低于刷新率：活动态也留在定时器驱动，只把周期换成配置帧率。
            // 回到 rAF 只会让 Blink 按刷新率跑主帧再跳掉一半，省不出 CPU/GPU。
            this._enterIdleTickMode(timerFps);
        } else {
            this._exitIdleTickMode();
        }
        if (this._idleDecayTimer) clearTimeout(this._idleDecayTimer);
        // 豁免页：只升频、不安排衰减——衰减会在 governor 缺席时把渲染拖回空闲模式
        if (window.__NEKO_DISABLE_AVATAR_IDLE_THROTTLE__ === true) {
            this._idleDecayTimer = null;
            return;
        }
        this._idleDecayTimer = setTimeout(() => {
            this._idleDecayTimer = null;
            const m = this.manager;
            if (m && m._shouldRender && !m._isDisposed && !this._hasRenderActivity()) {
                this._enterIdleTickMode();
            }
        }, Math.max(100, Number(durationMs) || MMD_INTERACTIVE_FPS_HOLD_MS));
    }

    // 需要满帧的活动：非 idle 动画播放、口型同步、拖拽、光标跟踪进行中、滚轮缩放余温
    _hasRenderActivity() {
        const m = this.manager;
        if (!m) return false;
        try {
            if (m.animationModule && m.animationModule.isPlaying && m._currentAnimationMode !== 'idle') return true;
        } catch (_) {}
        try { if (m.animationModule && m.animationModule._lipSyncActive) return true; } catch (_) {}
        try { if (window.appState && window.appState.lipSyncActive) return true; } catch (_) {}
        try { if (m.interaction && m.interaction.isDragging) return true; } catch (_) {}
        try {
            const cf = m.cursorFollow;
            if (cf && cf.enabled) {
                // 时间戳 0 是合法值（performance.now() 起点），不能用真值判断
                if (Number.isFinite(cf._lastPointerMoveTs) && (performance.now() - cf._lastPointerMoveTs) < MMD_INTERACTIVE_FPS_HOLD_MS) return true;
                // 头部朝向还没收敛完：保持满帧到位
                if (Math.abs((cf._targetYaw || 0) - (cf._currentYaw || 0)) > 0.02 ||
                    Math.abs((cf._targetPitch || 0) - (cf._currentPitch || 0)) > 0.02) return true;
            }
        } catch (_) {}
        try {
            if (Number.isFinite(m._lastInteractionBoostTs) && (performance.now() - m._lastInteractionBoostTs) < MMD_INTERACTIVE_FPS_HOLD_MS) return true;
        } catch (_) {}
        return false;
    }

    _startIdleGovernor() {
        this._stopIdleGovernor();
        // 页面级豁免：小游戏 demo 等用自有 rAF 循环驱动骨骼姿态的页面，
        // 活动探测看不见它们的动画，节流会造成可见卡顿——由页面声明退出
        if (window.__NEKO_DISABLE_AVATAR_IDLE_THROTTLE__ === true) return;
        // 启动/加载视为活动：先满帧，随后自动衰减
        this._boostInteractiveFPS();
        this._idleGovernorTimer = setInterval(() => {
            const m = this.manager;
            // 自终止：dispose / renderer 销毁后自动停摆并释放闭包引用
            if (!m || m._isDisposed || !m.renderer) { this._stopIdleGovernor(); return; }
            // 外部暂停（pauseRendering）：不接管；resume 走 _startRenderLoop 会重启本 governor
            if (!m._shouldRender) return;
            if (this._hasRenderActivity()) {
                this._boostInteractiveFPS();
            } else if (!this._idleTickMode && !this._idleDecayTimer) {
                // 自愈：外部直接重启 rAF 链（resumeRendering 等）后无人再带我们回空闲模式
                this._enterIdleTickMode();
            }
        }, MMD_IDLE_GOVERNOR_INTERVAL_MS);
    }

    _stopIdleGovernor() {
        if (this._idleGovernorTimer) {
            clearInterval(this._idleGovernorTimer);
            this._idleGovernorTimer = null;
        }
        if (this._idleDecayTimer) {
            clearTimeout(this._idleDecayTimer);
            this._idleDecayTimer = null;
        }
        this._exitIdleTickMode(false);
    }

    _flushRenderWaiters() {
        const waiters = this.manager._renderWaiters;
        if (!Array.isArray(waiters) || waiters.length === 0) return;
        this.manager._renderWaiters = [];
        waiters.forEach((resolve) => {
            try {
                resolve();
            } catch (_) { /* ignore waiter errors */ }
        });
    }

    waitForRenderFrame(timeoutMs = 2000) {
        if (!this.manager.renderer || this.manager._isDisposed) {
            return Promise.resolve();
        }
        return new Promise((resolve) => {
            const waiters = this.manager._renderWaiters || (this.manager._renderWaiters = []);
            let settled = false;
            const finish = () => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                const idx = waiters.indexOf(finish);
                if (idx >= 0) waiters.splice(idx, 1);
                resolve();
            };
            const timer = setTimeout(finish, timeoutMs);
            waiters.push(finish);
        });
    }

    _render() {
        if (!this.manager._shouldRender || this.manager._isDisposed) {
            this._flushRenderWaiters();
            return;
        }

        this.manager._animationFrameId = requestAnimationFrame(() => this._render());

        // 帧率限制（仅 rAF 驱动路径；空闲低频模式下 interval 周期本身就是节流器，
        // 由 interval 直接调用 _renderFrame，不经过这里）
        this._refreshTargetFps();
        const now = performance.now();
        if (!(this.frameTime > 0)) {
            // 不限帧：每个 rAF 都渲染。不能走下面的取模（elapsed % 0 = NaN 会把
            // lastFrameTime 污染成 NaN，之后切回有限帧率也永远节流不了）。
            this.lastFrameTime = now;
            this._renderFrame(now);
            return;
        }
        const elapsed = now - this.lastFrameTime;
        if (elapsed < this.frameTime) return;
        this.lastFrameTime = now - (elapsed % this.frameTime);

        this._renderFrame(now);
    }

    _renderFrame(_now) {
        const delta = this.manager.clock ? this.manager.clock.getDelta() : 0;
        // 限制 delta 以防止长时间切页后的突变
        const clampedDelta = Math.min(delta, 0.1);

        // ── MMD 标准更新顺序：动画 → IK/Grant → 鼠标跟踪 → 物理 ──

        // 1. 更新动画（骨骼关键帧）
        if (this.manager.animationModule && this.manager.animationModule.isPlaying) {
            this.manager.animationModule.update(clampedDelta);
        }

        // 2. IK/Grant 求解（仅在无动画静止状态执行，暂停时跳过）
        //    Grant（付与変換）常用负比率实现骨骼联动（如裙摆反向补偿），
        //    必须在静止状态每帧执行，否则 kinematic 骨骼位置错误导致物理镜像分离。
        //    但在暂停时不应运行——暂停保持的是动画帧的快照，IK 会破坏该快照。
        if (this.manager.animationModule && !this.manager.animationModule.isPlaying && !this.manager.animationModule.isPaused) {
            const anim = this.manager.animationModule;
            const mmd = this.manager.currentModel;
            if (mmd && mmd.mesh) {
                mmd.mesh.updateMatrixWorld(true);
                if (anim.ikSolver) anim.ikSolver.update();
                if (anim.grantSolver) anim.grantSolver.update();
            }
        }

        // 3. 更新鼠标跟踪（在物理之前，让 kinematic 骨骼到位）
        if (this.manager.cursorFollow) {
            this.manager.cursorFollow.update(clampedDelta);
        }

        // 4. 更新物理（库内部有 substep）
        //    暂停时冻结物理，防止头发/裙摆持续摆动
        const animPaused = this.manager.animationModule && this.manager.animationModule.isPaused;
        if (this.manager.enablePhysics && this.manager.currentModel && this.manager.currentModel.physics && !animPaused) {
            this.manager.currentModel.update(clampedDelta);
        }

        // 5. 更新表情模块（眨眼、口型同步等）
        //    暂停时冻结表情，防止自动眨眼导致的循环感
        if (this.manager.expression && !animPaused) {
            this.manager.expression.update(clampedDelta);
        }

        // 更新 OrbitControls
        if (this.manager.controls) {
            this.manager.controls.update();
        }

        // 更新屏幕空间包围盒缓存（用于悬停检测和鼠标穿透判断）
        if (this.manager.interaction) {
            this.manager.interaction.updateScreenBounds();
        }

        // 渲染
        if (this.manager.effect && this.manager.useOutlineEffect) {
            this.manager.effect.render(this.manager.scene, this.manager.camera);
        } else if (this.manager.renderer) {
            this.manager.renderer.render(this.manager.scene, this.manager.camera);
        }

        this._flushRenderWaiters();
    }

    // ═══════════════════ Resize ═══════════════════

    onWindowResize(reason = 'resize') {
        if (!this.manager.renderer || !this.manager.camera) return;
        this.syncRendererPixelRatio(reason);

        const container = this.manager.container;
        let width = container ? (container.clientWidth || container.offsetWidth) : window.innerWidth;
        let height = container ? (container.clientHeight || container.offsetHeight) : window.innerHeight;

        if (width === 0 || height === 0) {
            width = window.innerWidth;
            height = window.innerHeight;
        }

        this.manager.camera.aspect = width / height;
        this.manager.camera.updateProjectionMatrix();
        this.manager.renderer.setSize(width, height);

        if (this.manager.effect) {
            this.manager.effect.setSize(width, height);
        }
    }

    // ═══════════════════ 模型变换 ═══════════════════

    setModelScale(scale) {
        if (!this.manager.currentModel || !this.manager.currentModel.mesh) return;
        this.manager.currentModel.mesh.scale.setScalar(scale);
    }

    resetModelPose() {
        const mmd = this.manager.currentModel;
        if (!mmd || !mmd.mesh) return;

        const hadPhysics = this.manager.enablePhysics;
        this.manager.enablePhysics = false;

        const mesh = mmd.mesh;
        mesh.skeleton.bones.forEach(bone => {
            bone.position.copy(bone.userData?.restPosition || bone.position);
            bone.quaternion.copy(bone.userData?.restQuaternion || bone.quaternion);
        });

        if (mmd.physics && typeof mmd.physics.reset === 'function') {
            mesh.updateMatrixWorld(true);
            mmd.physics.reset();
        }

        this.manager.enablePhysics = hadPhysics;

        // 标记 T-Pose 状态，让鼠标跟踪模块跳过眼骨旋转
        this.manager._isTPose = true;
    }

    resetModelPosition() {
        const mmd = this.manager.currentModel;
        if (!mmd || !mmd.mesh) return;

        const THREE = window.THREE;

        // 禁用物理，防止位置变更期间拉丝
        const hadPhysics = this.manager.enablePhysics;
        this.manager.enablePhysics = false;

        const mesh = mmd.mesh;
        mesh.position.set(0, 0, 0);
        mesh.quaternion.identity();
        mesh.scale.set(1, 1, 1);

        // 复位相机到初始机位，使模型稳定出现在屏幕中央
        // （MMD 的 orbit 仅旋转 mesh 不动相机，但用户可能通过其他入口改动过相机；
        //  同时也为将来相机交互做前向兼容）
        if (THREE && this.manager.camera) {
            try {
                mesh.updateMatrixWorld(true);
                const box = new THREE.Box3().setFromObject(mesh);
                const center = box.getCenter(new THREE.Vector3());
                const size = box.getSize(new THREE.Vector3());

                const modelHeight = size.y > 0 ? size.y : 20;
                const screenHeight = window.innerHeight;
                const targetScreenHeight = screenHeight * 0.45;
                const fov = this.manager.camera.fov * (Math.PI / 180);
                // 让模型在屏幕上占约 45% 高度
                const distance = (modelHeight / 2) / Math.tan(fov / 2) / targetScreenHeight * screenHeight;

                // 把相机锚定到 bounding box 中心，否则模型几何中心偏离世界原点时，
                // 实际相机到目标的距离与 `distance` 不一致，复位构图会漂
                this.manager.camera.position.set(center.x, center.y, center.z + Math.abs(distance));
                this.manager.camera.lookAt(center.x, center.y, center.z);
                this.manager.camera.updateProjectionMatrix();

                if (this.manager.controls) {
                    this.manager.controls.target.set(center.x, center.y, center.z);
                    this.manager.controls.update();
                }
            } catch (err) {
                console.warn('[MMD Core] 重置相机失败，回退到初始机位:', err);
                this.manager.camera.position.set(0, 10, 70);
                this.manager.camera.lookAt(0, 10, 0);
            }
        }

        if (mmd.physics && typeof mmd.physics.reset === 'function') {
            mesh.updateMatrixWorld(true);
            mmd.physics.reset();
        }

        // 恢复物理
        this.manager.enablePhysics = hadPhysics;

        const modelPath = mmd.url;
        if (modelPath) {
            // 构造重置后的相机位置，覆盖后端保存的旧值
            let resetCameraPosition = null;
            if (this.manager.camera) {
                const cam = this.manager.camera;
                const ctrl = this.manager.controls;
                resetCameraPosition = {
                    x: cam.position.x, y: cam.position.y, z: cam.position.z,
                    targetX: ctrl ? ctrl.target.x : 0,
                    targetY: ctrl ? ctrl.target.y : 0,
                    targetZ: ctrl ? ctrl.target.z : 0
                };
            }
            this.saveUserPreferences(
                modelPath,
                { x: 0, y: 0, z: 0 },
                { x: 1, y: 1, z: 1 },
                { x: 0, y: 0, z: 0 },
                null,  // display
                null,  // viewport
                resetCameraPosition
            );
        }

        console.log('[MMD Core] 模型位置已重置，相机已复位');
    }

    // ═══════════════════ 锁定 ═══════════════════

    setLocked(locked) {
        this.manager.isLocked = locked;

        const lockIcon = document.getElementById('mmd-lock-icon');
        if (lockIcon) {
            lockIcon.style.backgroundImage = locked
                ? 'url(/static/icons/locked_icon.png)'
                : 'url(/static/icons/unlocked_icon.png)';
        }

        if (this.manager.interaction && typeof this.manager.interaction.setLocked === 'function') {
            this.manager.interaction.setLocked(locked);
        }

        if (!locked) {
            // 解锁时恢复容器透明度（同时重置淡化状态）
            if (typeof this.manager._setMmdLockedHoverFade === 'function') {
                this.manager._setMmdLockedHoverFade(false);
            } else {
                const container = document.getElementById('mmd-container');
                if (container) {
                    container.style.opacity = '1';
                }
            }
        }

        if (this.manager.controls) {
            this.manager.controls.enablePan = !locked;
        }

        // 同步 Live2D 锁定状态
        if (window.live2dManager) {
            window.live2dManager.isLocked = locked;
        }

        const buttonsContainer = document.getElementById('mmd-floating-buttons');
        if (buttonsContainer) {
            if (this.manager._isInReturnState) {
                buttonsContainer.style.display = 'none';
            } else {
                buttonsContainer.style.display = locked ? 'none' : 'flex';
            }
        }
    }

    // ═══════════════════ 用户偏好持久化 ═══════════════════

    async saveUserPreferences(modelPath, position, scale, rotation, display, viewport, cameraPosition) {
        try {
            // 观看模式只读：viewer 不应把本地拖动覆盖到全局模型布局（也避免向 monitor 的只读端点 POST 触发 405）
            if (window.isViewerMode) {
                return false;
            }
            if (!position || typeof position !== 'object' ||
                !Number.isFinite(position.x) || !Number.isFinite(position.y) || !Number.isFinite(position.z)) {
                console.error('[MMD] 位置值无效:', position);
                return false;
            }
            if (!scale || typeof scale !== 'object' ||
                !Number.isFinite(scale.x) || !Number.isFinite(scale.y) || !Number.isFinite(scale.z) ||
                scale.x <= 0 || scale.y <= 0 || scale.z <= 0) {
                console.error('[MMD] 缩放值无效:', scale);
                return false;
            }

            const preferences = { model_path: modelPath, position, scale };

            if (rotation && typeof rotation === 'object' &&
                Number.isFinite(rotation.x) && Number.isFinite(rotation.y) && Number.isFinite(rotation.z)) {
                preferences.rotation = rotation;
            }
            if (display && typeof display === 'object' &&
                Number.isFinite(display.screenX) && Number.isFinite(display.screenY)) {
                preferences.display = { screenX: display.screenX, screenY: display.screenY };
            }
            if (viewport && typeof viewport === 'object' &&
                Number.isFinite(viewport.width) && Number.isFinite(viewport.height) &&
                viewport.width > 0 && viewport.height > 0) {
                preferences.viewport = { width: viewport.width, height: viewport.height };
            }
            if (cameraPosition && typeof cameraPosition === 'object' &&
                Number.isFinite(cameraPosition.x) && Number.isFinite(cameraPosition.y) && Number.isFinite(cameraPosition.z)) {
                preferences.camera_position = {
                    x: cameraPosition.x, y: cameraPosition.y, z: cameraPosition.z
                };
                if (Number.isFinite(cameraPosition.targetX) && Number.isFinite(cameraPosition.targetY) && Number.isFinite(cameraPosition.targetZ)) {
                    preferences.camera_position.targetX = cameraPosition.targetX;
                    preferences.camera_position.targetY = cameraPosition.targetY;
                    preferences.camera_position.targetZ = cameraPosition.targetZ;
                }
            }

            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 5000);

            let response;
            try {
                response = await fetch('/api/config/preferences', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(preferences),
                    signal: controller.signal
                });
                clearTimeout(timeoutId);
            } catch (error) {
                clearTimeout(timeoutId);
                if (error.name === 'AbortError') {
                    console.warn('[MMD Core] 保存偏好设置请求超时（5秒）');
                    return false;
                }
                throw error;
            }

            if (!response.ok) {
                console.error('[MMD Core] 保存偏好设置失败:', response.status);
                return false;
            }

            const result = await response.json();
            return result.success || false;
        } catch (error) {
            console.error('[MMD Core] 保存偏好设置时出错:', error);
            return false;
        }
    }

    async _restoreUserPreferences(mmd, modelUrl) {
        const THREE = window.THREE;
        if (!THREE || !mmd?.mesh) return;

        try {
            const preferencesResponse = await fetch('/api/config/preferences');
            if (!preferencesResponse.ok) {
                console.warn('[MMD Core] 获取偏好设置失败:', preferencesResponse.status);
                return;
            }

            const allPreferences = await preferencesResponse.json();
            let modelsArray = Array.isArray(allPreferences)
                ? allPreferences
                : (allPreferences?.models && Array.isArray(allPreferences.models) ? allPreferences.models : null);

            if (!modelsArray || modelsArray.length === 0) return;

            // 路径匹配（支持精确匹配、归一化路径、文件名匹配）
            const normalizePath = (path) => {
                if (!path || typeof path !== 'string') return '';
                return path.replace(/^https?:\/\/[^\/]+/, '').replace(/\\/g, '/').toLowerCase();
            };
            const getFilename = (path) => {
                if (!path || typeof path !== 'string') return '';
                const parts = path.split('/').filter(Boolean);
                return parts.length > 0 ? parts[parts.length - 1].toLowerCase() : '';
            };

            const normalizedModelUrl = normalizePath(modelUrl);
            const modelFilename = getFilename(modelUrl);

            const preferences = modelsArray.find(pref => {
                if (!pref || !pref.model_path) return false;
                const prefPath = pref.model_path;
                if (prefPath === modelUrl) return true;
                if (normalizePath(prefPath) === normalizedModelUrl && normalizedModelUrl) return true;
                const prefFilename = getFilename(prefPath);
                if (modelFilename && prefFilename && prefFilename === modelFilename) return true;
                return false;
            });

            if (!preferences) return;

            const mesh = mmd.mesh;

            // 禁用物理：防止位置变更期间渲染循环跑物理导致拉丝
            const hadPhysics = this.manager.enablePhysics;
            this.manager.enablePhysics = false;
            try {

            // 恢复位置
            if (preferences.position) {
                const pos = preferences.position;
                if (Number.isFinite(pos.x) && Number.isFinite(pos.y) && Number.isFinite(pos.z)) {
                    mesh.position.set(pos.x, pos.y, pos.z);
                }
            }

            // 恢复缩放（含跨分辨率归一化）
            // MMD 模型 scale 必须均匀（x=y=z），否则模型变形。
            // 历史偏好或 bug 可能保存了非均匀值，加载时强制均匀化。
            if (preferences.scale) {
                const scl = preferences.scale;
                if (Number.isFinite(scl.x) && Number.isFinite(scl.y) && Number.isFinite(scl.z) &&
                    scl.x > 0 && scl.y > 0 && scl.z > 0) {
                    let uniformScale = (scl.x + scl.y + scl.z) / 3;
                    if (Math.abs(scl.x - scl.y) > 0.001 || Math.abs(scl.y - scl.z) > 0.001) {
                        console.warn(`[MMD Core] 非均匀缩放已修正: (${scl.x.toFixed(3)}, ${scl.y.toFixed(3)}, ${scl.z.toFixed(3)}) → ${uniformScale.toFixed(3)}`);
                    }
                    const savedViewport = preferences.viewport;
                    const currentScreenH = window.screen.height;
                    const hRatio = (savedViewport &&
                        Number.isFinite(savedViewport.height) && savedViewport.height > 0)
                        ? currentScreenH / savedViewport.height : 1;
                    const isExtremeChange = hRatio > 1.8 || hRatio < 0.56;
                    if (isExtremeChange) {
                        uniformScale *= hRatio;
                        console.log('[MMD Core] 屏幕分辨率大幅变化，缩放已归一化');
                    }
                    mesh.scale.setScalar(uniformScale);
                }
            }

            // 恢复旋转
            if (preferences.rotation) {
                const rot = preferences.rotation;
                if (Number.isFinite(rot.x) && Number.isFinite(rot.y) && Number.isFinite(rot.z)) {
                    mesh.rotation.order = 'YXZ';
                    mesh.rotation.set(rot.x, rot.y, rot.z);
                }
            }

            // 物理重置：更新世界矩阵后重置所有刚体到新骨骼位置
            if (mmd.physics && typeof mmd.physics.reset === 'function') {
                mesh.updateMatrixWorld(true);
                mmd.physics.reset();
            }

            } finally {
                this.manager.enablePhysics = hadPhysics;
            }

            console.log('[MMD Core] 偏好设置已恢复:', {
                position: preferences.position,
                scale: preferences.scale,
                rotation: preferences.rotation
            });
        } catch (error) {
            console.error('[MMD Core] 恢复偏好设置失败:', error);
        }
    }

    // ═══════════════════ 资源清理 ═══════════════════

    dispose() {
        // 先停 governor（内部一并清衰减计时器并退出空闲低频模式，不复跑）
        this._stopIdleGovernor();
        this.manager._shouldRender = false;

        if (this.manager._animationFrameId) {
            cancelAnimationFrame(this.manager._animationFrameId);
            this.manager._animationFrameId = null;
        }

        this._flushRenderWaiters();

        this._clearModel();

        // 显式清空最后一帧，避免下次重新显示 canvas 时透出旧模型残留。
        if (this.manager.renderer) {
            try {
                this.manager.renderer.setRenderTarget(null);
                this.manager.renderer.clear(true, true, true);
            } catch (_) { /* ignore clear failures during teardown */ }
        }

        // 清理光照
        [this.manager.ambientLight, this.manager.directionalLight].forEach(light => {
            if (light && light.parent) {
                light.parent.remove(light);
            }
        });

        // 清理 effect
        if (this.manager.effect) {
            this.manager.effect = null;
        }

        // 清理 renderer
        if (this.manager.renderer) {
            this.manager.renderer.dispose();
            this.manager.renderer = null;
        }

        // 清理窗口事件
        if (this.manager._coreWindowHandlers) {
            this.manager._coreWindowHandlers.forEach(({ event, handler }) => {
                window.removeEventListener(event, handler);
            });
            this.manager._coreWindowHandlers = [];
        }

        this.manager._isDisposed = true;
        console.log('[MMD Core] 资源已完全清理');
    }
}

window.MMDCore = MMDCore;

// 监听帧率变更事件（与 live2d-core.js 同名事件对齐）
window.addEventListener('neko-frame-rate-changed', () => {
    const core = window.mmdManager && window.mmdManager.core;
    if (core && typeof core.applyTargetFrameRate === 'function') {
        try { core.applyTargetFrameRate(); } catch (_) {}
    }
});
