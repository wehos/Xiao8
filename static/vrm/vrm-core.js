/**
 * VRM 核心模块 - 负责场景初始化、模型加载、性能管理等核心功能
 */

/**
 * VRM 光照默认值 —— 防御性赋值（仅当 HTML 模板未注入时生效）
 *
 * 正常流程下，此常量已由 HTML <head> 中的内联 <script> 从后端注入
 * （config/__init__.py → Jinja2 → window.VRM_DEFAULT_LIGHTING）。
 * 此处仅作为兜底，确保非模板页面或单元测试也能正常工作。
 */
if (!window.VRM_DEFAULT_LIGHTING) {
    window.VRM_DEFAULT_LIGHTING = Object.freeze({
        ambient: 0.83,
        main: 1.91,
        fill: 0.0,
        rim: 0.0,
        top: 0.0,
        bottom: 0.0,
        exposure: 1.1,
        toneMapping: 7,          // 7 = THREE.NeutralToneMapping
        outlineWidthScale: 1.0,
    });
}

class VRMCore {
    constructor(manager) {
        this.manager = manager;
        this.vrmVersion = null;
        this.performanceMode = this.detectPerformanceMode();
        this.targetFPS = this.performanceMode === 'low' ? 30 : (this.performanceMode === 'medium' ? 45 : 60);
        this.frameTime = 1000 / this.targetFPS;
        this.lastFrameTime = 0;
    }

    static _vrmUtilsCache = null;
    /**
     * 获取 VRMUtils（带缓存）
     * 使用 importmap 中的映射，确保与 @pixiv/three-vrm-animation 使用相同的 three-vrm-core 版本
     * @returns {Promise<VRMUtils|null>} VRMUtils 对象，如果导入失败则返回 null
     */
    static async _getVRMUtils() {
        if (VRMCore._vrmUtilsCache) {
            return VRMCore._vrmUtilsCache;
        }
        try {
            // 使用 importmap 中的映射，确保与 @pixiv/three-vrm-animation 使用相同的 three-vrm-core 版本（推荐 v3.x+）
            const { VRMUtils } = await import('@pixiv/three-vrm');
            VRMCore._vrmUtilsCache = VRMUtils;
            return VRMUtils;
        } catch (error) {
            console.warn('[VRM Core] 无法导入 VRMUtils，请检查 importmap 配置:', error);
            return null;
        }
    }

    static _getSceneRotation(vrm) {
        if (!vrm || !vrm.scene || !vrm.scene.rotation) {
            return null;
        }

        const { x, y, z } = vrm.scene.rotation;
        if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) {
            return null;
        }

        return { x, y, z };
    }

    static _isFiniteRotation(rotation) {
        return !!rotation &&
            Number.isFinite(rotation.x) &&
            Number.isFinite(rotation.y) &&
            Number.isFinite(rotation.z);
    }

    static _isIdentityRotation(rotation) {
        return VRMCore._isFiniteRotation(rotation) &&
            Math.abs(rotation.x) < 0.000001 &&
            Math.abs(rotation.y) < 0.000001 &&
            Math.abs(rotation.z) < 0.000001;
    }

    getEffectiveSavedRotation(savedRotation, versionDefaultRotation) {
        if (!VRMCore._isFiniteRotation(savedRotation)) {
            return null;
        }

        // 旧逻辑可能把漏判的 VRM0.x 朝向自动保存为 identity。
        // 对有明确 VRM0.x 版本默认旋转的模型，将该缓存视为可迁移旧值。
        // 这也会迁移用户有意保存的 VRM0 identity；特殊背对模型需保存非 identity 旋转或按个案处理。
        if (this.vrmVersion === '0.0' &&
            VRMCore._isIdentityRotation(savedRotation) &&
            VRMCore._isFiniteRotation(versionDefaultRotation) &&
            !VRMCore._isIdentityRotation(versionDefaultRotation)) {
            console.log('[VRM Core] 忽略旧版 VRM0.x identity rotation 缓存，改用版本默认旋转');
            return null;
        }

        return savedRotation;
    }

    /**
     * 对 VRM0.x 应用 three-vrm 官方朝向兼容，并返回统一旋转管线应使用的默认旋转。
     * VRM1.0 不需要该兼容处理；已有用户保存 rotation 时，后续统一旋转管线仍会优先使用用户值。
     */
    async applyVRM0CompatibilityRotation(vrm) {
        if (this.vrmVersion !== '0.0' || !vrm || !vrm.scene) {
            return null;
        }

        try {
            const VRMUtils = await VRMCore._getVRMUtils();
            if (VRMUtils && typeof VRMUtils.rotateVRM0 === 'function') {
                VRMUtils.rotateVRM0(vrm);
                if (VRMCore._isIdentityRotation(VRMCore._getSceneRotation(vrm))) {
                    console.warn('[VRM Core] VRMUtils.rotateVRM0 未产生旋转，回退到 Y 轴 180 度旋转');
                    vrm.scene.rotation.y = Math.PI;
                }
            } else {
                vrm.scene.rotation.y = Math.PI;
            }
        } catch (error) {
            console.warn('[VRM Core] VRM0.x 官方朝向兼容失败，回退到 Y 轴 180 度旋转:', error);
            vrm.scene.rotation.y = Math.PI;
        }

        vrm.scene.updateMatrixWorld(true);

        const rotation = VRMCore._getSceneRotation(vrm);
        if (rotation) {
            console.log('[VRM Core] 已应用 VRM0.x 朝向兼容:', rotation);
        }
        return rotation;
    }

    /**
     * 检测设备性能模式
     */
    detectPerformanceMode() {
        let savedMode = null;
        try {
            savedMode = localStorage.getItem('vrm_performance_mode');
            if (savedMode && ['low', 'medium', 'high'].includes(savedMode)) {
                return savedMode;
            }
        } catch (e) {
            console.debug('[VRM Core] localStorage 访问失败:', e);
        }
        
        try {
            const canvas = document.createElement('canvas');
            const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
            
            if (!gl) {
                return 'low';
            }
            
            const debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
            if (debugInfo) {
                const renderer = gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL);
                const isLowEndGPU = 
                    renderer.includes('Intel') && 
                    (renderer.includes('HD Graphics') || renderer.includes('Iris') || renderer.includes('UHD'));
                const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
                const isLowEndMobile = isMobile && navigator.hardwareConcurrency <= 4;
                
                if (isLowEndGPU || isLowEndMobile) {
                    return 'low';
                }
            }
            
            const cores = navigator.hardwareConcurrency || 4;
            if (cores <= 2) {
                return 'low';
            } else if (cores <= 4) {
                return 'medium';
            }
            
            return 'high';
        } catch (e) {
            return 'medium';
        }
    }

    /**
     * 检测 VRM 模型版本
     * @param {Object} vrm - three-vrm 解析后的 VRM 对象
     * @param {Object} [gltf] - 原始 GLTF 加载结果，用于读取 extensionsUsed
     */
    detectVRMVersion(vrm, gltf) {
        try {
            // 1️⃣ 最可靠：直接检查 GLTF JSON 中的 extensionsUsed
            //    VRM 1.0 使用 VRMC_vrm 扩展，VRM 0.x 使用 VRM 扩展
            if (gltf?.parser?.json?.extensionsUsed) {
                const exts = gltf.parser.json.extensionsUsed;
                if (exts.includes('VRMC_vrm')) {
                    return '1.0';
                }
                if (exts.includes('VRM')) {
                    return '0.0';
                }
            }

            // 2️⃣ 回退：检查 vrm.meta 属性
            if (vrm.meta) {
                // VRM 0.x: meta 中有 vrmVersion / metaVersion / exporterVersion
                if (vrm.meta.vrmVersion || vrm.meta.metaVersion) {
                    const version = vrm.meta.vrmVersion || vrm.meta.metaVersion;
                    if (version && (version.startsWith('1') || version.includes('1.0'))) {
                        return '1.0';
                    }
                    return '0.0';
                }

                // VRM 1.0: meta 中有 authors (数组)，VRM 0.x 是 author (字符串)
                if (Array.isArray(vrm.meta.authors)) {
                    return '1.0';
                }
                if (typeof vrm.meta.author === 'string') {
                    return '0.0';
                }

                // VRM 0.x: meta 中有 exporterVersion
                if (vrm.meta.exporterVersion) {
                    return '0.0';
                }
            }

            // 3️⃣ 最后回退：启发式检查
            if (vrm.humanoid && vrm.humanoid.humanBones) {
                const boneNames = Object.keys(vrm.humanoid.humanBones);
                if (boneNames.length > 50) {
                    return '1.0';
                }
            }

            if (vrm.expressionManager && vrm.expressionManager.expressions) {
                let exprCount;
                if (vrm.expressionManager.expressions instanceof Map) {
                    exprCount = vrm.expressionManager.expressions.size;
                } else {
                    exprCount = Object.keys(vrm.expressionManager.expressions).length;
                }
                if (exprCount > 10) {
                    return '1.0';
                }
            }

            return '0.0';
        } catch (error) {
            console.warn('[VRM] 版本检测异常，默认为 0.0:', error);
            return '0.0';
        }
    }

    /**
     * 设置锁定状态并同步更新 UI
     * @param {boolean} locked - 是否锁定
     */
    setLocked(locked) {
        this.manager.isLocked = locked;

        if (this._lockIconImages) {
            const { locked: imgLocked, unlocked: imgUnlocked } = this._lockIconImages;
            if (imgLocked) imgLocked.style.opacity = locked ? '1' : '0';
            if (imgUnlocked) imgUnlocked.style.opacity = locked ? '0' : '1';
        } else {
            const lockIcon = document.getElementById('vrm-lock-icon');
            if (lockIcon) {
                lockIcon.style.backgroundImage = locked ? 'url(/static/icons/locked_icon.png)' : 'url(/static/icons/unlocked_icon.png)';
            }
        }

        if (this.manager.interaction && typeof this.manager.interaction.setLocked === 'function') {
            this.manager.interaction.setLocked(locked);
        }

        if (!locked) {
            const vrmContainer = document.getElementById('vrm-container');
            if (vrmContainer) {
                vrmContainer.style.opacity = '1';
            }
        }

        if (this.manager.controls) {
            this.manager.controls.enablePan = !locked;
        }

        if (window.live2dManager) {
            window.live2dManager.isLocked = locked;
        }

        const buttonsContainer = document.getElementById('vrm-floating-buttons');
        if (buttonsContainer) {
            // 如果处于返回状态，保持按钮隐藏，不要因为解锁而显示按钮
            if (this.manager._isInReturnState) {
                buttonsContainer.style.display = 'none';
            } else {
                buttonsContainer.style.display = locked ? 'none' : 'flex';
            }
        }
    }

    /**
     * 应用性能设置
     */
    _getPerformancePixelRatio() {
        const devicePixelRatio = window.devicePixelRatio || 1;
        let pixelRatio;

        if (this.performanceMode === 'low') {
            // 低性能模式：限制最大为 1.0
            pixelRatio = Math.min(1.0, devicePixelRatio);
        } else if (this.performanceMode === 'medium') {
            // 中性能模式：限制最大为 1.5
            pixelRatio = Math.min(1.5, devicePixelRatio);
        } else {
            // 高性能模式：使用完整设备像素比，确保清晰度
            pixelRatio = devicePixelRatio;
        }
        
        // 确保 pixelRatio 至少为 1.0（避免模糊）
        return Math.max(1.0, pixelRatio);
    }

    _getQualityPixelRatio(quality) {
        if (quality === 'low') return 0.8;
        if (quality === 'medium') return 1.0;
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
            console.log('[VRM Core] renderer pixelRatio 已刷新:', {
                reason,
                prevPixelRatio,
                nextPixelRatio,
                devicePixelRatio: window.devicePixelRatio || 1
            });
        }
    }

    /**
     * 优化材质设置 - VRoid Hub 风格
     * 核心理念：柔和的阴影过渡、明亮的阴影颜色、减少对比度
     */
    optimizeMaterials() {
        if (!this.manager.currentModel || !this.manager.currentModel.vrm || !this.manager.currentModel.vrm.scene) return;
        
        this.manager.currentModel.vrm.scene.traverse((object) => {
            if (object.isMesh || object.isSkinnedMesh) {
                // VRoid Hub 风格：禁用阴影投射和接收，使用纯粹的 MToon 着色
                object.castShadow = false;
                object.receiveShadow = false;

                // 优化 MToon 材质以获得 VRoid Hub 风格的柔和二次元效果
                if (object.material) {
                    const materials = Array.isArray(object.material) ? object.material : [object.material];
                    materials.forEach(mat => {
                        // 检查是否为 MToon 材质
                        if (mat.version === 'mtoon' || mat.isMToonMaterial || (mat.userData && mat.userData.vrmMaterialProperties)) {
                            // === VRoid Hub 风格核心参数 ===
                            
                            // 1. 阴影过渡柔和度（关键参数）
                            // VRoid Hub 使用非常柔和的过渡，几乎看不到明显的阴影边界
                            // 0.0 = 完全平滑渐变，1.0 = 硬边二值化
                            if (mat.shadingToonyFactor !== undefined) {
                                mat.shadingToonyFactor = 0.3; // VRoid Hub 风格：柔和过渡
                            }
                            
                            // 2. 阴影偏移（控制受光面积）
                            // 正值 = 更多区域被照亮，负值 = 更多阴影
                            // VRoid Hub 倾向于让更多区域受光，减少暗部
                            if (mat.shadingShiftFactor !== undefined) {
                                mat.shadingShiftFactor = 0.15; // 增加受光面积，脸部更明亮
                            }
                            
                            // 3. 自动提亮阴影颜色（shadeColor）
                            // VRoid Hub 的阴影不是黑色/灰色，而是主色调的较淡版本
                            if (mat.shadeColorFactor !== undefined && mat.uniforms?.litFactor?.value) {
                                const litColor = mat.uniforms.litFactor.value;
                                const shadeColor = mat.shadeColorFactor;
                                
                                // 计算当前阴影颜色与主色调的关系
                                // 如果阴影颜色太暗（与主色差距过大），自动提亮
                                const brightness = (c) => (c.r + c.g + c.b) / 3;
                                const litBrightness = brightness(litColor);
                                const shadeBrightness = brightness(shadeColor);
                                
                                // 如果阴影亮度低于主色的 60%，进行提亮处理
                                if (litBrightness > 0 && shadeBrightness < litBrightness * 0.6) {
                                    // 目标：让阴影亮度达到主色的 70-80%
                                    const targetBrightness = litBrightness * 0.75;
                                    const factor = targetBrightness / Math.max(shadeBrightness, 0.01);
                                    
                                    // 混合提亮：保持色相，提升亮度
                                    shadeColor.r = Math.min(1, shadeColor.r * factor * 0.8 + litColor.r * 0.2);
                                    shadeColor.g = Math.min(1, shadeColor.g * factor * 0.8 + litColor.g * 0.2);
                                    shadeColor.b = Math.min(1, shadeColor.b * factor * 0.8 + litColor.b * 0.2);
                                }
                            }
                            
                            // 4. 边缘光（Rim Light）- 轻微增强轮廓
                            if (mat.rimLightingMixFactor !== undefined) {
                                mat.rimLightingMixFactor = 0.3; // 轻微边缘光，不要太强
                            }
                            
                            // 5. 参数化边缘光设置（如果材质支持）
                            if (mat.parametricRimFresnelPowerFactor !== undefined) {
                                mat.parametricRimFresnelPowerFactor = 3.0; // 控制边缘光范围
                            }
                            if (mat.parametricRimLiftFactor !== undefined) {
                                mat.parametricRimLiftFactor = 0.1; // 边缘光基础强度
                            }
                            
                            mat.needsUpdate = true;
                        }
                    });
                }
            }
        });
    }

    /**
     * 检查 Three.js 依赖是否完整
     */
    _ensureThreeReady() {
        const THREE = window.THREE;
        if (!THREE) {
            const errorMsg = window.t ? window.t('vrm.error.threeNotLoaded') : 'Three.js库未加载，请确保已引入three.js';
            throw new Error(errorMsg);
        }
        
        const required = [
            { name: 'WebGLRenderer', obj: THREE.WebGLRenderer },
            { name: 'Clock', obj: THREE.Clock },
            { name: 'Scene', obj: THREE.Scene },
            { name: 'PerspectiveCamera', obj: THREE.PerspectiveCamera },
            { name: 'PCFSoftShadowMap', obj: THREE.PCFSoftShadowMap }
        ];
        
        const missing = required.filter(item => !item.obj);
        if (missing.length > 0) {
            const missingNames = missing.map(item => item.name).join(', ');
            const errorMsg = window.t 
                ? window.t('vrm.error.threeIncomplete', { missing: missingNames, defaultValue: `Three.js 依赖不完整，缺少: ${missingNames}` })
                : `Three.js 依赖不完整，缺少: ${missingNames}`;
            throw new Error(errorMsg);
        }
        
        // 检查编码相关字段（兼容新旧版本）
        if (THREE.SRGBColorSpace === undefined && THREE.sRGBEncoding === undefined) {
            console.warn('[VRM Core] Three.js 版本可能过旧，缺少颜色空间定义');
        }
    }

    async init(canvasId, containerId, lightingConfig = null, options = {}) {
        this._ensureThreeReady();
        const THREE = window.THREE;
        const embed = options && options.embed === true;
        const resizeMode = String(options?.resizeMode || 'host-window');
        if (!['fixed', 'host-window'].includes(resizeMode)) {
            throw new Error(`不支持的 VRM resizeMode: ${resizeMode}`);
        }

        this.manager.container = document.getElementById(containerId);
        this.manager.canvas = document.getElementById(canvasId);

        if (this.manager.canvas && !this.manager.canvas.id) {
            this.manager.canvas.id = canvasId;
        }

        if (!this.manager.container) {
            const errorMsg = window.t ? window.t('vrm.error.containerNotFound', { id: containerId }) : `找不到容器元素: ${containerId}`;
            throw new Error(errorMsg);
        }

        if (!this.manager.canvas) {
            const errorMsg = window.t ? window.t('vrm.error.canvasNotFound', { id: canvasId }) : `找不到canvas元素: ${canvasId}`;
            throw new Error(errorMsg);
        }

        if (!embed) {
            this.manager.container.style.display = 'block';
            this.manager.container.style.visibility = 'visible';
            this.manager.container.style.opacity = '1';
            this.manager.container.style.width = '100%';
            this.manager.container.style.height = '100%';
            this.manager.container.style.position = 'fixed';
            this.manager.container.style.top = '0';
            this.manager.container.style.left = '0';
            this.manager.container.style.setProperty('pointer-events', 'auto', 'important');
        }

        this.manager.clock = new THREE.Clock();
        this.manager.scene = new THREE.Scene();
        this.manager.scene.background = null;

        let width = this.manager.container.clientWidth || this.manager.container.offsetWidth;
        let height = this.manager.container.clientHeight || this.manager.container.offsetHeight;
        if (width === 0 || height === 0) {
            width = window.innerWidth;
            height = window.innerHeight;
        }
        // A 30° lens is unnecessarily tight for full-body companion motion:
        // raised hands, ears and wide gestures can leave the viewport even when
        // the torso is still visible. Start wider and let the interaction module
        // expand further only when the animated bounds actually need it.
        this.manager.camera = new THREE.PerspectiveCamera(34, width / height, 0.05, 2000);
        this.manager.camera.position.set(0, 1.1, 1.5);
        this.manager.camera.lookAt(0, 0.9, 0);

        const antialias = true;
        const precision = 'highp';
        
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
            console.error('[VRMCore] WebGL is not available in this browser');
            this.manager.renderer = null;
            return;
        }
        
        try {
            this.manager.renderer = new THREE.WebGLRenderer({ 
                canvas: this.manager.canvas,
                alpha: true, 
                antialias: antialias,
                powerPreference: 'high-performance',
                precision: precision,
                preserveDrawingBuffer: false,
                stencil: false,
                depth: true
            });
        } catch (e) {
            console.error('[VRMCore] Failed to create WebGLRenderer:', e);
            this.manager.renderer = null;
            return;
        }
        
        this.manager.renderer.setSize(width, height);
        this.applyPerformanceSettings();
        this.manager.renderer.shadowMap.enabled = true;
        this.manager.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
        if (THREE.SRGBColorSpace !== undefined) {
            // 新 API (r152+)
            this.manager.renderer.outputColorSpace = THREE.SRGBColorSpace;
        } else {
            this.manager.renderer.outputEncoding = THREE.sRGBEncoding;
        }
        
        // 使用光照配置中的色调映射和曝光，若无则使用全局默认值
        const _defaults = window.VRM_DEFAULT_LIGHTING;
        const _initToneMapping = lightingConfig?.toneMapping ?? _defaults.toneMapping;
        const _initExposure = lightingConfig?.exposure ?? _defaults.exposure;
        this.manager.renderer.toneMapping = _initToneMapping;
        this.manager.renderer.toneMappingExposure = _initExposure;

        const canvas = this.manager.renderer.domElement;
        canvas.style.setProperty('pointer-events', 'auto', 'important');
        canvas.style.setProperty('touch-action', 'none', 'important');
        canvas.style.setProperty('user-select', 'none', 'important');
        canvas.style.cursor = 'default';
        canvas.style.width = '100%';
        canvas.style.height = '100%';
        canvas.style.display = 'block';

        if (typeof window.OrbitControls !== 'undefined') {
            this.manager.controls = new window.OrbitControls(this.manager.camera, this.manager.renderer.domElement);
            this.manager.controls.enableRotate = false;
            this.manager.controls.enablePan = true;
            this.manager.controls.enableZoom = false;
            this.manager.controls.minDistance = 0.5;
            this.manager.controls.maxDistance = 10;
            this.manager.controls.target.set(0, 1, 0);
            this.manager.controls.enableDamping = true;
            this.manager.controls.dampingFactor = 0.1;
            // OrbitControls 只在 update() 真正移动了相机时才发 change 事件：
            // 打时间戳供空闲低频 governor 判定（覆盖拖拽平移 + 阻尼收敛期）
            this.manager.controls.addEventListener('change', () => {
                this.manager._lastCameraChangeAt = performance.now();
            });
            this.manager.controls.update();
        }

        this.manager.scene.add(this.manager.camera);

        // 使用光照配置（如果提供），否则使用全局默认值
        const defaultLighting = window.VRM_DEFAULT_LIGHTING;
        const lighting = lightingConfig || defaultLighting;

        // 环境光：使用 HemisphereLight，天空色和地面色都接近白色
        // VRoid Hub 风格：极高强度，几乎消除所有暗部，让模型看起来柔和明亮
        const hemisphereLight = new THREE.HemisphereLight(
            0xffffff,   // 天空色：纯白
            0xf0f0f0,   // 地面色：接近白色，减少底部阴影
            lighting.ambient ?? defaultLighting.ambient
        );
        this.manager.scene.add(hemisphereLight);
        this.manager.ambientLight = hemisphereLight;

        // 主光：会在渲染循环中跟随相机，这里只设置初始位置
        // VRoid Hub 风格：正面柔和照明，位置会动态更新
        const mainLight = new THREE.DirectionalLight(0xffffff, lighting.main ?? defaultLighting.main);
        mainLight.position.set(0, 1.0, 3.0);
        mainLight.castShadow = false;  // VRoid Hub 不使用实时阴影
        this.manager.scene.add(mainLight);
        this.manager.mainLight = mainLight;

        // 补光：保留但默认关闭
        const fillLight = new THREE.DirectionalLight(0xffffff, lighting.fill ?? defaultLighting.fill);
        fillLight.position.set(-2, 1, 2);
        fillLight.castShadow = false;
        this.manager.scene.add(fillLight);
        this.manager.fillLight = fillLight;

        // 轮廓光：保留但默认关闭（MToon 材质有内建边缘光）
        const rimLight = new THREE.DirectionalLight(0xffffff, lighting.rim ?? defaultLighting.rim);
        rimLight.position.set(0, 1, -2);
        rimLight.castShadow = false;
        this.manager.scene.add(rimLight);
        this.manager.rimLight = rimLight;

        // 顶光：保留但默认关闭
        const topLight = new THREE.DirectionalLight(0xffffff, lighting.top ?? defaultLighting.top);
        topLight.position.set(0, 3, 0);
        topLight.castShadow = false;
        this.manager.scene.add(topLight);
        this.manager.topLight = topLight;

        // 底部补光：保留但默认关闭
        const bottomLight = new THREE.DirectionalLight(0xffffff, lighting.bottom ?? defaultLighting.bottom);
        bottomLight.position.set(0, -1, 1);
        bottomLight.castShadow = false;
        this.manager.scene.add(bottomLight);
        this.manager.bottomLight = bottomLight;

        // Embedded mini-game renderers are resized by the official Avatar host.
        // Keeping the engine listener-free prevents a second window policy from
        // racing container/fixed sizing. The main application keeps the existing
        // host-window behavior because it remains the default.
        if (resizeMode === 'fixed') return;

        // 使用 Core 模块专用的 handlers 数组
        if (!this.manager._coreWindowHandlers) {
            this.manager._coreWindowHandlers = [];
        }
        
        // 创建命名函数并存储在 manager 上，以便 dispose() 可以移除它；如果已存在则复用，避免重复注册
        if (!this.manager._resizeHandler) {
            this.manager._resizeHandler = () => {
                if (this.manager && this.manager.core && typeof this.manager.core.syncRendererPixelRatio === 'function') {
                    this.manager.core.syncRendererPixelRatio('resize');
                }
                if (this.manager && typeof this.manager.onWindowResize === 'function') {
                    this.manager.onWindowResize();
                }
            };
        }
        
        // 检查是否已经注册过，避免重复注册
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
                    if (this.manager && this.manager.core && typeof this.manager.core.syncRendererPixelRatio === 'function') {
                        this.manager.core.syncRendererPixelRatio('electron-display-changed');
                    }
                    if (this.manager && typeof this.manager.onWindowResize === 'function') {
                        this.manager.onWindowResize();
                    }
                    requestAnimationFrame(() => {
                        if (this.manager && this.manager.core && typeof this.manager.core.syncRendererPixelRatio === 'function') {
                            this.manager.core.syncRendererPixelRatio('electron-display-changed:settled');
                        }
                        if (this.manager && typeof this.manager.onWindowResize === 'function') {
                            this.manager.onWindowResize();
                        }
                    });
                    setTimeout(() => {
                        if (this.manager && this.manager.core && typeof this.manager.core.syncRendererPixelRatio === 'function') {
                            this.manager.core.syncRendererPixelRatio('electron-display-changed:delayed');
                        }
                        if (this.manager && typeof this.manager.onWindowResize === 'function') {
                            this.manager.onWindowResize();
                        }
                    }, 120);
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
    }

    async loadModel(modelUrl, options = {}, managerLoadToken = null) {
        const THREE = window.THREE;
        const embed = options.embed === true;
        if (!THREE) {
            const errorMsg = window.t ? window.t('vrm.error.threeNotLoadedForModel') : 'Three.js库未加载，无法加载VRM模型';
            throw new Error(errorMsg);
        }

        // 加载令牌：由 vrm-manager._loadModelInternal 在入口分配后传入；直接调用时
        // 回退读当前值（等价旧行为）。core.loadModel 在多个 await（GLTF 网络加载、偏好
        // fetch、3 帧等待）之间会 remove/dispose/add 共享 scene 并覆写 currentModel，
        // manager 的串行队列在 dispose 后会被清空以保证活性，因此这些跨越 await 的共享
        // 副作用必须自带 token 守卫，否则被取代的迟到加载会毁掉新会话已装好的模型。
        // 与 mmd-core.loadModel 的 token 守卫对偶。
        const loadToken = managerLoadToken !== null ? managerLoadToken : this.manager._activeLoadToken;

        if (!modelUrl || 
            modelUrl === 'undefined' || 
            modelUrl === 'null' || 
            (typeof modelUrl === 'string' && (modelUrl.trim() === '' || modelUrl.includes('undefined')))) {
            console.error('[VRM Core] 模型路径无效:', modelUrl, '类型:', typeof modelUrl);
            const errorMsg = window.t ? window.t('vrm.error.invalidModelPath', { path: modelUrl, defaultValue: `VRM 模型路径无效: ${modelUrl}` }) : `VRM 模型路径无效: ${modelUrl}`;
            throw new Error(errorMsg);
        }

        try {
            let GLTFLoader, VRMLoaderPlugin;
            try {
                const gltfModule = await import('three/addons/loaders/GLTFLoader.js');
                GLTFLoader = gltfModule.GLTFLoader;
            } catch (importError) {
                const errorMsg = window.t 
                    ? window.t('vrm.error.gltfLoaderImportFailed', { error: importError.message, defaultValue: `无法导入 GLTFLoader: ${importError.message}` })
                    : `无法导入 GLTFLoader: ${importError.message}`;
                throw new Error(errorMsg);
            }
            
            try {
                const vrmModule = await import('@pixiv/three-vrm');
                VRMLoaderPlugin = vrmModule.VRMLoaderPlugin;
            } catch (importError) {
                const errorMsg = window.t 
                    ? window.t('vrm.error.vrmLoaderImportFailed', { error: importError.message, defaultValue: `无法导入 VRMLoaderPlugin: ${importError.message}` })
                    : `无法导入 VRMLoaderPlugin: ${importError.message}`;
                throw new Error(errorMsg);
            }

            const loader = new GLTFLoader();
            loader.register((parser) => new VRMLoaderPlugin(parser));

            const loadGLTF = (url) => {
                return new Promise((resolve, reject) => {
                    loader.load(
                        url,
                        (gltf) => resolve(gltf),
                        (progress) => {
                            if (progress.total > 0) {
                                const percent = (progress.loaded / progress.total) * 100;
                                if (options.onProgress) {
                                    options.onProgress(progress);
                                }
                            }
                        },
                        (error) => reject(error)
                    );
                });
            };

            let gltf = null;
            
            try {
                gltf = await loadGLTF(modelUrl);
            } catch (error) {
                let fallbackUrl = null;
                // 使用 window.VRM_PATHS 动态获取路径前缀，而不是硬编码
                const userPrefix = (window.VRM_PATHS?.user_vrm || '/user_vrm').replace(/\/+$/, '');
                const staticPrefix = (window.VRM_PATHS?.static_vrm || '/static/vrm').replace(/\/+$/, '');
                
                if (modelUrl.startsWith(staticPrefix + '/')) {
                    const filename = modelUrl.replace(staticPrefix + '/', '');
                    fallbackUrl = `${userPrefix}/${filename}`;
                } else if (modelUrl.startsWith(userPrefix + '/')) {
                    const filename = modelUrl.replace(userPrefix + '/', '');
                    fallbackUrl = `${staticPrefix}/${filename}`;
                }
                
                if (fallbackUrl) {
                    console.warn(`[VRM Core] 从 ${modelUrl} 加载失败，尝试备用路径: ${fallbackUrl}`);
                    try {
                        gltf = await loadGLTF(fallbackUrl);
                        modelUrl = fallbackUrl;
                        console.log(`[VRM Core] 使用备用路径成功加载: ${modelUrl}`);
                    } catch (fallbackError) {
                        console.error(`[VRM Core] 从备用路径 ${fallbackUrl} 也加载失败:`, fallbackError);
                        const errorMsg = window.t ? window.t('vrm.error.modelLoadFailed', { url: modelUrl, fallback: fallbackUrl }) : `无法加载 VRM 模型: ${modelUrl} 和 ${fallbackUrl} 都失败`;
                        throw new Error(errorMsg);
                    }
                } else {
                    throw error;
                }
            }

            // 加载竞态守卫（一道）：GLTF 网络加载 + 依赖导入是最长的 await；若在此期间被
            // dispose 或新一轮 loadModel 取代（loadToken 失效），必须在触碰共享 scene/
            // currentModel 之前退出——否则本次迟到的加载会把新会话已装好的模型 remove/
            // disposeVRM 掉（幽灵模型损坏）。释放本次已加载但不再采用的 VRM 资源避免显存泄漏。
            if (this.manager._activeLoadToken !== loadToken) {
                console.log('[VRM Core] 模型加载已被取代（GLTF 加载后），跳过并释放资源');
                await this._disposeAbandonedVRM(gltf.userData?.vrm);
                return null;
            }

            if (this.manager.currentModel && this.manager.currentModel.vrm) {
                // 清理交互模块的事件监听器
                if (this.manager.interaction && typeof this.manager.interaction.cleanupDragAndZoom === 'function') {
                    this.manager.interaction.cleanupDragAndZoom();
                }
                if (this.manager.interaction && typeof this.manager.interaction.cleanupFloatingButtonsMouseTracking === 'function') {
                    this.manager.interaction.cleanupFloatingButtonsMouseTracking();
                }
                
                // disposeVRM 内部也会尝试从 parent/scene 移除，这里保留 remove 也行，但必须 await
                // 确保旧模型完全清理后再继续，避免竞态条件（旧 VRM 还在 deepDispose、新 VRM 已加入 scene）
                this.manager.scene.remove(this.manager.currentModel.vrm.scene);
                await this.disposeVRM();
            }

            // 获取 VRM 实例
            const vrm = gltf.userData.vrm;
            if (!vrm) {
                console.error('[VRM] 加载失败: gltf.userData:', gltf.userData);
                console.error('[VRM] 加载失败: gltf.scene:', gltf.scene);
                const errorMsg = window.t ? window.t('vrm.error.invalidVRMFormat', { file: modelUrl }) : `加载的模型不是有效的 VRM 格式。文件: ${modelUrl}`;
                throw new Error(errorMsg);
            }

            // 检测 VRM 模型版本（0.0 或 1.0）
            this.vrmVersion = this.detectVRMVersion(vrm, gltf);
            const versionDefaultRotation = await this.applyVRM0CompatibilityRotation(vrm);

            // 计算模型的边界框，用于确定合适的初始大小
            const box = new THREE.Box3().setFromObject(vrm.scene);
            const size = box.getSize(new THREE.Vector3());
            const center = box.getCenter(new THREE.Vector3());

            // 获取保存的用户偏好设置
            let preferences = null;
            if (!embed) {
                try {
                // 添加超时保护（5秒超时）
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 5000);
                
                let preferencesResponse;
                try {
                    preferencesResponse = await fetch('/api/config/preferences', {
                        signal: controller.signal
                    });
                    clearTimeout(timeoutId);
                } catch (error) {
                    clearTimeout(timeoutId);
                    if (error.name === 'AbortError') {
                        console.warn('[VRM Core] 获取偏好设置请求超时（5秒）');
                        throw new Error('请求超时');
                    }
                    throw error;
                }
                
                if (!preferencesResponse.ok) {
                    let errorText = '';
                    try {
                        const contentType = preferencesResponse.headers.get('content-type');
                        if (contentType && contentType.includes('application/json')) {
                            const errorData = await preferencesResponse.json();
                            errorText = JSON.stringify(errorData);
                        } else {
                            errorText = await preferencesResponse.text();
                        }
                    } catch (e) {
                        errorText = preferencesResponse.statusText || '未知错误';
                    }
                    throw new Error(`获取偏好设置失败: ${preferencesResponse.status} ${preferencesResponse.statusText}${errorText ? ` - ${errorText}` : ''}`);
                }
                
                const allPreferences = await preferencesResponse.json();

                let modelsArray = null;
                if (Array.isArray(allPreferences)) {
                    modelsArray = allPreferences;
                } else if (allPreferences && allPreferences.models && Array.isArray(allPreferences.models)) {
                    modelsArray = allPreferences.models;
                }

                if (modelsArray && modelsArray.length > 0) {
                    // 使用共享的路径处理工具函数（避免与 vrm-init.js 重复）
                    const normalizePath = window._vrmPathUtils?.normalizePath || ((path) => {
                        if (!path || typeof path !== 'string') return '';
                        let normalized = path.replace(/^https?:\/\/[^\/]+/, '');
                        // 使用 window.VRM_PATHS 动态获取路径前缀，而不是硬编码
                        const userPrefix = (window.VRM_PATHS?.user_vrm || '/user_vrm').replace(/\/+$/, '');
                        const staticPrefix = (window.VRM_PATHS?.static_vrm || '/static/vrm').replace(/\/+$/, '');
                        // 转义正则表达式特殊字符并构建匹配模式
                        const escapeRegex = (str) => str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                        normalized = normalized
                            .replace(new RegExp(`^${escapeRegex(userPrefix)}/`), '/')
                            .replace(new RegExp(`^${escapeRegex(staticPrefix)}/`), '/');
                        return normalized.toLowerCase();
                    });
                    
                    const getFilename = window._vrmPathUtils?.getFilename || ((path) => {
                        if (!path || typeof path !== 'string') return '';
                        const parts = path.split('/').filter(Boolean);
                        return parts.length > 0 ? parts[parts.length - 1].toLowerCase() : '';
                    });
                    
                    const normalizedModelUrl = normalizePath(modelUrl);
                    const modelFilename = getFilename(modelUrl);
                    
                    preferences = modelsArray.find(pref => {
                        if (!pref || !pref.model_path) return false;
                        const prefPath = pref.model_path;
                        
                        if (prefPath === modelUrl) return true;
                        
                        const normalizedPrefPath = normalizePath(prefPath);
                        if (normalizedPrefPath && normalizedPrefPath === normalizedModelUrl) return true;
                        
                        const prefFilename = getFilename(prefPath);
                        if (modelFilename && prefFilename && prefFilename === modelFilename) return true;
                        
                        return false;
                    });
                }
                } catch (error) {
                    console.error('[VRM Core] 获取用户偏好设置失败:', error);
                }
            }

            if (preferences) {
                if (preferences.position) {
                    const pos = preferences.position;
                    if (Number.isFinite(pos.x) && Number.isFinite(pos.y) && Number.isFinite(pos.z)) {
                        vrm.scene.position.set(pos.x, pos.y, pos.z);
                    } else {
                        vrm.scene.position.set(-center.x, -center.y, -center.z);
                    }
                } else {
                    vrm.scene.position.set(0, 0, 0);
                }

                if (preferences.scale) {
                    const scl = preferences.scale;
                    if (Number.isFinite(scl.x) && Number.isFinite(scl.y) && Number.isFinite(scl.z) &&
                        scl.x > 0 && scl.y > 0 && scl.z > 0) {
                        // 检查是否需要跨分辨率缩放归一化
                        const savedViewport = preferences.viewport;
                        const currentScreenH = window.screen.height;
                        // 仅在屏幕分辨率发生"跨代"级别变化时（如 1080p→4K）才归一化缩放
                        const hRatio = (savedViewport &&
                            Number.isFinite(savedViewport.height) && savedViewport.height > 0)
                            ? currentScreenH / savedViewport.height : 1;
                        const isExtremeChange = hRatio > 1.8 || hRatio < 0.56;
                        if (isExtremeChange) {
                            vrm.scene.scale.set(scl.x * hRatio, scl.y * hRatio, scl.z * hRatio);
                            console.log('[VRM Core] 屏幕分辨率大幅变化，缩放已归一化:', { savedHeight: savedViewport?.height, currentScreenH, hRatio });
                        } else {
                            vrm.scene.scale.set(scl.x, scl.y, scl.z);
                        }
                    }
                }

                // 注意：不在这里直接设置 rotation，避免双重旋转
                // rotation 将在检测器阶段之后统一设置（见下方代码）
            } else {
                vrm.scene.position.set(0, 0, 0);
            }
            
            // 等待 3 帧确保 DOM 布局和 Three.js 场景完全稳定后再处理旋转
            await new Promise(resolve => {
                let frameCount = 0;
                const waitFrames = () => {
                    frameCount++;
                    if (frameCount >= 3) {
                        resolve();
                    } else {
                        requestAnimationFrame(waitFrames);
                    }
                };
                requestAnimationFrame(waitFrames);
            });

            // 加载竞态守卫（二道）：一道守卫之后经过了 applyVRM0 旋转 / 偏好 fetch / 3 帧等待
            // 等 await，期间可能被更新的加载取代。这里是「最后一个 await 之后、任何共享 manager
            // 写入（相机位姿 / enableFaceCamera / scene.add / currentModel）之前」的屏障——若不
            // 在此拦截，被取代的旧加载会用它自己模型的存档相机覆写共享相机，把已经可见的新模型
            // 顶到屏外（Codex P2）。此处到 scene.add / currentModel 全程再无 await，一道即可覆盖。
            if (this.manager._activeLoadToken !== loadToken) {
                console.log('[VRM Core] 模型加载已被取代（共享相机/入场前），跳过并释放资源');
                await this._disposeAbandonedVRM(vrm);
                return null;
            }

            // 旋转设置统一在这里处理，确保只应用一次
            // 先通过检测器检测并修复方向，然后应用最终的旋转值
            const savedRotation = this.getEffectiveSavedRotation(preferences?.rotation, versionDefaultRotation);
            
            let detectedRotation = window.VRMOrientationDetector
                ? window.VRMOrientationDetector.detectAndFixOrientation(vrm, savedRotation, {
                    defaultRotation: versionDefaultRotation
                })
                : (versionDefaultRotation || { x: 0, y: 0, z: 0 });
            
            if (window.VRMOrientationDetector) {
                window.VRMOrientationDetector.applyRotation(vrm, detectedRotation);
            } else {
                // 如果没有检测器，回退到直接使用保存的旋转值或版本默认旋转
                if (savedRotation && 
                    Number.isFinite(savedRotation.x) && 
                    Number.isFinite(savedRotation.y) && 
                    Number.isFinite(savedRotation.z)) {
                    vrm.scene.rotation.set(savedRotation.x, savedRotation.y, savedRotation.z);
                    vrm.scene.updateMatrixWorld(true);
                    detectedRotation = VRMCore._getSceneRotation(vrm) || detectedRotation;
                } else if (versionDefaultRotation &&
                    Number.isFinite(versionDefaultRotation.x) &&
                    Number.isFinite(versionDefaultRotation.y) &&
                    Number.isFinite(versionDefaultRotation.z)) {
                    vrm.scene.rotation.set(versionDefaultRotation.x, versionDefaultRotation.y, versionDefaultRotation.z);
                    vrm.scene.updateMatrixWorld(true);
                    detectedRotation = VRMCore._getSceneRotation(vrm) || detectedRotation;
                }
            }
            
            const hasSavedRotation = VRMCore._isFiniteRotation(savedRotation);
            
            if (!embed && !hasSavedRotation && typeof this.saveUserPreferences === 'function') {
                // 标准化位置为普通对象 {x, y, z}
                // 始终从 vrm.scene 获取当前位置，确保 z 值有效
                // （旧版偏好设置可能只有 x 和 y，没有 z 值）
                const currentPosition = {
                    x: vrm.scene.position.x,
                    y: vrm.scene.position.y,
                    z: vrm.scene.position.z
                };
                
                // 标准化缩放为普通对象 {x, y, z}
                const currentScale = {
                    x: vrm.scene.scale.x,
                    y: vrm.scene.scale.y,
                    z: vrm.scene.scale.z
                };
                
                this.saveUserPreferences(
                    modelUrl,
                    currentPosition,
                    currentScale,
                    detectedRotation,
                    null // display
                ).catch(err => {
                    console.error(`[VRM Core] 自动保存rotation时出错:`, err);
                });
            }
            
            if (this.manager.interaction) {
                this.manager.interaction.enableFaceCamera = false;
            }

            if (!preferences || !preferences.scale) {
                if (options.scale) {
                    vrm.scene.scale.set(options.scale.x || 1, options.scale.y || 1, options.scale.z || 1);
                } else {
                    // 根据模型大小和屏幕大小计算合适的默认缩放
                    const modelHeight = size.y;
                    const screenHeight = window.innerHeight;
                    const screenWidth = window.innerWidth;

                    // 计算合适的初始缩放（参考Live2D的默认大小计算，参考 vrm.js）
                    // Electron Pet 窗口永不进入手机模式，统一走 canonical 谓词。
                    const isMobile = typeof window.isMobileWidth === 'function' ? window.isMobileWidth() : (window.innerWidth <= 768);
                    let targetScale;

                    if (isMobile) {
                        // 移动端：使用固定缩放值，确保模型可见但不会太大
                        targetScale = Math.max(0.4, Math.min(0.8, screenHeight / 1800));
                    } else {
                        // 桌面端：使用更平衡的计算方式
                        if (modelHeight > 0 && Number.isFinite(modelHeight)) {
                            // 目标：让模型在屏幕上的高度约为屏幕高度的0.4-0.5倍
                            const targetScreenHeight = screenHeight * 0.40;
                            
                            // 检查相机是否存在
                            if (this.manager.camera && this.manager.camera.fov) {
                                const fov = this.manager.camera.fov * (Math.PI / 180);
                                const cameraDistance = this.manager.camera.position?.z || 5; // 默认距离 5
                                const worldHeightAtDistance = 2 * Math.tan(fov / 2) * cameraDistance;
                                const scaleRatio = (targetScreenHeight / screenHeight) * (worldHeightAtDistance / modelHeight);
                                
                                // 限制在合理范围内：最小 0.5，最大 1.2
                                targetScale = Math.max(0.5, Math.min(1.2, scaleRatio));
                            } else {
                                // 如果相机不存在，使用简单的比例计算
                                // 假设标准 VRM 模型高度约为 1.5 单位，目标屏幕高度为屏幕的 0.45 倍
                                const standardModelHeight = 1.5;
                                const scaleRatio = (targetScreenHeight / screenHeight) * (standardModelHeight / modelHeight);
                                targetScale = Math.max(0.5, Math.min(1.2, scaleRatio));
                            }
                        } else {
                            // 如果模型高度无效，使用屏幕尺寸计算
                            targetScale = Math.max(0.5, Math.min(1.0, screenHeight / 1200));
                        }
                    }
                    
                    // 确保缩放值在合理范围内（最小 0.5，最大 1.2）
                    targetScale = Math.max(0.5, Math.min(1.2, targetScale));
                    
                    // 应用计算出的 targetScale
                    vrm.scene.scale.setScalar(targetScale);
                }
            }

            // 恢复相机位置，并记录 _cameraTarget（统一的观察目标）
            if (this.manager.camera && this.manager.camera.fov) {
                const savedCameraPos = preferences?.camera_position;

                // 验证相机位置有效性：检查坐标有限且位置非零（orbit 后 z 可能为负）
                const camPosValid = savedCameraPos &&
                    Number.isFinite(savedCameraPos.x) && Number.isFinite(savedCameraPos.y) && Number.isFinite(savedCameraPos.z) &&
                    (savedCameraPos.x * savedCameraPos.x + savedCameraPos.y * savedCameraPos.y + savedCameraPos.z * savedCameraPos.z) > 0.01;
                if (camPosValid) {
                    // 恢复相机位置
                    this.manager.camera.position.set(savedCameraPos.x, savedCameraPos.y, savedCameraPos.z);

                    // 优先用四元数精确恢复朝向（避免 lookAt 转换误差）
                    if (Number.isFinite(savedCameraPos.qx) && Number.isFinite(savedCameraPos.qy) &&
                        Number.isFinite(savedCameraPos.qz) && Number.isFinite(savedCameraPos.qw)) {
                        this.manager.camera.quaternion.set(
                            savedCameraPos.qx, savedCameraPos.qy,
                            savedCameraPos.qz, savedCameraPos.qw
                        );
                    } else if (Number.isFinite(savedCameraPos.targetX) && Number.isFinite(savedCameraPos.targetY) && Number.isFinite(savedCameraPos.targetZ)) {
                        // 兼容旧数据：用 lookAt
                        this.manager.camera.lookAt(savedCameraPos.targetX, savedCameraPos.targetY, savedCameraPos.targetZ);
                    } else {
                        this.manager.camera.lookAt(center.x, center.y, center.z);
                    }

                    // 恢复 _cameraTarget（zoom/orbit 的中心点）
                    if (Number.isFinite(savedCameraPos.targetX) && Number.isFinite(savedCameraPos.targetY) && Number.isFinite(savedCameraPos.targetZ)) {
                        this.manager._cameraTarget = new THREE.Vector3(savedCameraPos.targetX, savedCameraPos.targetY, savedCameraPos.targetZ);
                    } else {
                        // 从相机前方方向重建 _cameraTarget
                        const forward = new THREE.Vector3(0, 0, -1).applyQuaternion(this.manager.camera.quaternion);
                        const box2 = new THREE.Box3().setFromObject(vrm.scene);
                        const mc = box2.getCenter(new THREE.Vector3());
                        const dist = this.manager.camera.position.distanceTo(mc);
                        this.manager._cameraTarget = this.manager.camera.position.clone().add(forward.multiplyScalar(dist));
                    }
                    console.log('[VRM Core] 已恢复保存的相机位置:', savedCameraPos);
                } else {
                    // 没有保存的相机位置时，根据模型大小和屏幕大小计算默认值
                    const scaledModelHeight = size.y * vrm.scene.scale.y;
                    const screenHeight = window.innerHeight;
                    const screenWidth = window.innerWidth;

                    const targetScreenHeight = screenHeight * 0.40;
                    const fov = this.manager.camera.fov * (Math.PI / 180);
                    const distance = (scaledModelHeight / 2) / Math.tan(fov / 2) / targetScreenHeight * screenHeight;

                    const isMobileDevice = typeof window.isMobileWidth === 'function' ? window.isMobileWidth() : (screenWidth <= 768);
                    const cameraY = center.y + (isMobileDevice ? scaledModelHeight * 0.2 : scaledModelHeight * 0.1);
                    const cameraZ = Math.abs(distance);
                    this.manager.camera.position.set(0, cameraY, cameraZ);

                    // 默认观察目标：包围盒中心
                    this.manager._cameraTarget = new THREE.Vector3(0, center.y, 0);
                    this.manager.camera.lookAt(this.manager._cameraTarget);
                }
            } else {
                console.warn('[VRM Core] 相机未初始化，跳过相机位置调整');
            }
            
            // 添加到场景前先隐藏，避免首帧/T-pose 闪现
            vrm.scene.visible = false;

            // 添加到场景 - 确保场景已初始化
            if (!this.manager.scene) {
                const errorMsg = window.t ? window.t('vrm.error.sceneNotInitializedForAdd') : '场景未初始化。请先调用 initThreeJS() 初始化场景。';
                throw new Error(errorMsg);
            }

            // 此处到 scene.add 之间无 await，二道守卫（3 帧等待后）已覆盖，无需重复校验。
            this.manager.scene.add(vrm.scene);

            // 优化材质设置（根据性能模式）
            this.optimizeMaterials();
            // 根据当前画质设置应用阴影/描边/像素比
            this.applyQualitySettings(window.renderQuality || 'medium');

            if (this.manager.controls) {
                this.manager.controls.target.set(0, center.y, 0);
                this.manager.controls.update();
            }

            if (this.manager.renderer && this.manager.scene && this.manager.camera) {
                this.manager.renderer.render(this.manager.scene, this.manager.camera);
            }

            if (vrm.humanoid) {
                if (vrm.humanoid.autoUpdateHumanBones !== undefined && !vrm.humanoid.autoUpdateHumanBones) {
                    vrm.humanoid.autoUpdateHumanBones = true;
                }
                vrm.humanoid.update();
            }

            this.manager.animationMixer = new THREE.AnimationMixer(vrm.scene);

            if (gltf.animations && gltf.animations.length > 0) {
                const action = this.manager.animationMixer.clipAction(gltf.animations[0]);
                action.play();
            }

            this.manager.currentModel = {
                vrm: vrm,
                gltf: gltf,
                scene: vrm.scene,
                url: modelUrl
            };

            if (this.manager.animation && typeof this.manager.animation.updateMouthExpressionMapping === 'function') {
                this.manager.animation.updateMouthExpressionMapping();
            }

            if (this.manager.interaction && typeof this.manager.interaction.enableMouseTracking === 'function') {
                this.manager.interaction.enableMouseTracking(true);
            }

            return this.manager.currentModel;
        } catch (error) {
            console.error('加载 VRM 模型失败:', error);
            throw error;
        }
    }

    /**
     * 释放一个「已加载完成但因 loadToken 失效不会被采用」的 VRM 资源。
     * 用于 loadModel 的竞态守卫早退路径：此时 vrm 尚未写入 manager.currentModel，
     * 不能走 disposeVRM（那依赖 currentModel），需就地 deepDispose 避免 GPU 显存泄漏。
     */
    async _disposeAbandonedVRM(vrm) {
        if (!vrm || !vrm.scene) return;
        try {
            if (vrm.scene.parent) vrm.scene.parent.remove(vrm.scene);
            const VRMUtils = await VRMCore._getVRMUtils();
            if (VRMUtils && typeof VRMUtils.deepDispose === 'function') {
                VRMUtils.deepDispose(vrm.scene);
            }
        } catch (e) {
            console.warn('[VRM Core] 释放被取代的 VRM 资源失败:', e);
        }
    }

    async disposeVRM() {
        if (!this.manager.currentModel || !this.manager.currentModel.vrm) return;
        
        const vrm = this.manager.currentModel.vrm;
        
        if (vrm.scene && vrm.scene.parent) {
            vrm.scene.parent.remove(vrm.scene);
        } else if (this.manager.scene && vrm.scene && this.manager.scene.children.includes(vrm.scene)) {
            this.manager.scene.remove(vrm.scene);
        }
        
        if (this.manager.expression) {
            if (this.manager.expression.neutralReturnTimer) {
                clearTimeout(this.manager.expression.neutralReturnTimer);
                this.manager.expression.neutralReturnTimer = null;
            }
            this.manager.expression.currentWeights = {};
            this.manager.expression.manualBlinkInProgress = null;
            this.manager.expression.manualExpressionInProgress = null;
            this.manager.expression.currentMood = 'neutral';
        }
        
        if (this.manager.animation) {
            if (this.manager.animation._springBoneTimer) {
                clearTimeout(this.manager.animation._springBoneTimer);
                this.manager.animation._springBoneTimer = null;
            }
            if (typeof this.manager.animation.stopVRMAAnimation === 'function') {
                this.manager.animation.stopVRMAAnimation();
            }
        }
        
        // 清理交互模块的定时器
        if (this.manager.interaction) {
            if (this.manager.interaction._hideButtonsTimer) {
                clearTimeout(this.manager.interaction._hideButtonsTimer);
                this.manager.interaction._hideButtonsTimer = null;
            }
            if (this.manager.interaction._savePositionDebounceTimer) {
                clearTimeout(this.manager.interaction._savePositionDebounceTimer);
                this.manager.interaction._savePositionDebounceTimer = null;
            }
        }
        
        // 清理自动播放动画的重试 timer
        if (this.manager._retryTimerId) {
            clearTimeout(this.manager._retryTimerId);
            this.manager._retryTimerId = null;
        }
        
        if (this.manager.animationMixer) {
            if (vrm.scene) {
                this.manager.animationMixer.uncacheRoot(vrm.scene);
            }
            this.manager.animationMixer.stopAllAction();
            this.manager.animationMixer = null;
        }

        if (vrm.scene) {
            // 使用 VRMUtils.deepDispose 清理场景（推荐方式，更安全且符合库的设计）
            // 这会自动清理所有几何体、材质、贴图等资源
            try {
                const VRMUtils = await VRMCore._getVRMUtils();
                if (VRMUtils && typeof VRMUtils.deepDispose === 'function') {
                    VRMUtils.deepDispose(vrm.scene);
                } else {
                    // 如果 VRMUtils 不可用，回退到手动清理
                    throw new Error('VRMUtils.deepDispose 不可用');
                }
            } catch (error) {
                // 如果导入失败，回退到手动清理（兼容性处理）
                console.warn('[VRM Core] 无法使用 VRMUtils.deepDispose，回退到手动清理:', error);
                // 清理 VRMLookAtQuaternionProxy（如果存在）
                const lookAtProxy = vrm.scene.getObjectByName('lookAtQuaternionProxy');
                if (lookAtProxy) {
                    vrm.scene.remove(lookAtProxy);
                }
                
                vrm.scene.traverse((object) => {
                    if (object.geometry) object.geometry.dispose();
                    if (object.material) {
                        if (Array.isArray(object.material)) {
                            object.material.forEach(m => {
                                if (m.map) m.map.dispose();
                                if (m.normalMap) m.normalMap.dispose();
                                if (m.roughnessMap) m.roughnessMap.dispose();
                                if (m.metalnessMap) m.metalnessMap.dispose();
                                if (m.emissiveMap) m.emissiveMap.dispose();
                                if (m.aoMap) m.aoMap.dispose();
                                m.dispose();
                            });
                        } else {
                            if (object.material.map) object.material.map.dispose();
                            if (object.material.normalMap) object.material.normalMap.dispose();
                            if (object.material.roughnessMap) object.material.roughnessMap.dispose();
                            if (object.material.metalnessMap) object.material.metalnessMap.dispose();
                            if (object.material.emissiveMap) object.material.emissiveMap.dispose();
                            if (object.material.aoMap) object.material.aoMap.dispose();
                            object.material.dispose();
                        }
                    }
                });
            }
        }
        
        // 清理 currentModel 引用（在清理完成后才设置为 null）
        this.manager.currentModel = null;
    }

    /**
     * 保存用户偏好设置（位置、缩放等）
     * @param {string} modelPath - 模型路径
     * @param {object} position - 位置 {x, y, z}
     * @param {object} scale - 缩放 {x, y, z}
     * @param {object} rotation - 旋转 {x, y, z}（可选）
     * @param {object} display - 显示器信息（可选）
     * @param {object} viewport - 视口尺寸 {width, height}（可选，用于跨分辨率归一化）
     * @returns {Promise<boolean>} 是否保存成功
     */
    async saveUserPreferences(modelPath, position, scale, rotation, display, viewport, cameraPosition) {
        try {
            // 观看模式只读：viewer 不应把本地拖动覆盖到全局模型布局（也避免向 monitor 的只读端点 POST 触发 405）
            if (window.isViewerMode) {
                return false;
            }
            // 验证位置值
            if (!position || typeof position !== 'object' ||
                !Number.isFinite(position.x) || !Number.isFinite(position.y) || !Number.isFinite(position.z)) {
                console.error('[VRM] 位置值无效:', position);
                return false;
            }

            // 验证缩放值（VRM使用统一缩放，但保存为对象格式以兼容Live2D的数据结构）
            if (!scale || typeof scale !== 'object' ||
                !Number.isFinite(scale.x) || !Number.isFinite(scale.y) || !Number.isFinite(scale.z)) {
                console.error('[VRM] 缩放值无效:', scale);
                return false;
            }

            // 验证缩放值必须为正数
            if (scale.x <= 0 || scale.y <= 0 || scale.z <= 0) {
                console.error('[VRM] 缩放值必须为正数:', scale);
                return false;
            }

            const preferences = {
                model_path: modelPath,
                position: position,
                scale: scale
            };

            // 如果有旋转信息，添加到偏好中
            if (rotation && typeof rotation === 'object' &&
                Number.isFinite(rotation.x) && Number.isFinite(rotation.y) && Number.isFinite(rotation.z)) {
                preferences.rotation = rotation;
            }

            // 如果有显示器信息，添加到偏好中（用于多屏幕位置恢复）
            if (display && typeof display === 'object' &&
                Number.isFinite(display.screenX) && Number.isFinite(display.screenY)) {
                preferences.display = {
                    screenX: display.screenX,
                    screenY: display.screenY
                };
            }

            // 如果有视口信息，添加到偏好中（用于跨分辨率缩放归一化）
            if (viewport && typeof viewport === 'object' &&
                Number.isFinite(viewport.width) && Number.isFinite(viewport.height) &&
                viewport.width > 0 && viewport.height > 0) {
                preferences.viewport = {
                    width: viewport.width,
                    height: viewport.height
                };
            }

            // 如果有相机位置信息，添加到偏好中（用于恢复滚轮缩放状态）
            if (cameraPosition && typeof cameraPosition === 'object' &&
                Number.isFinite(cameraPosition.x) && Number.isFinite(cameraPosition.y) && Number.isFinite(cameraPosition.z)) {
                preferences.camera_position = {
                    x: cameraPosition.x,
                    y: cameraPosition.y,
                    z: cameraPosition.z
                };
            }
            
            // 添加超时保护（5秒超时）
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 5000);
            
            let response;
            try {
                response = await fetch('/api/config/preferences', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(preferences),
                    signal: controller.signal
                });
                
                clearTimeout(timeoutId);
            } catch (error) {
                clearTimeout(timeoutId);
                if (error.name === 'AbortError') {
                    console.warn('[VRM Core] 保存偏好设置请求超时（5秒）');
                    throw new Error('请求超时');
                }
                throw error;
            }

            if (!response.ok) {
                let errorText = '';
                try {
                    const contentType = response.headers.get('content-type');
                    if (contentType && contentType.includes('application/json')) {
                        const errorData = await response.json();
                        errorText = JSON.stringify(errorData);
                    } else {
                        errorText = await response.text();
                    }
                } catch (e) {
                    errorText = response.statusText || '未知错误';
                }
                throw new Error(`保存偏好设置失败: ${response.status} ${response.statusText}${errorText ? ` - ${errorText}` : ''}`);
            }

            // 安全解析 JSON，避免空响应体或非 JSON 响应导致异常被吞掉
            const result = await response.json().catch((parseError) => {
                const statusText = response.statusText || '';
                const truncatedStatusText = statusText.length > 50 ? statusText.substring(0, 50) + '...' : statusText;
                console.warn(`[VRM Core] 保存偏好设置响应解析失败: ${response.status} ${truncatedStatusText}`, parseError);
                return {};
            });
            
            return result.success || false;
        } catch (error) {
            console.error('[VRM] 保存用户偏好失败:', error);
            return false;
        }
    }

    /**
     * 根据画质设置应用渲染优化（纯性能分级，不改变视觉风格）
     * Low:    pixelRatio 0.8、无物理（render loop 控制）、禁描边
     * Medium: pixelRatio 1.0、有物理（跳帧）、禁描边
     * High:   pixelRatio = devicePixelRatio、有物理、有描边
     */
    applyQualitySettings(quality) {
        if (!this.manager.renderer) return;

        if (quality === 'low') {
            this.manager.renderer.setPixelRatio(0.8);
        } else if (quality === 'medium') {
            this.manager.renderer.setPixelRatio(1.0);
        } else {
            this.applyPerformanceSettings();
        }

        if (!this.manager.currentModel?.vrm?.scene) return;
        const disableOutline = (quality === 'low' || quality === 'medium');
        this.manager.currentModel.vrm.scene.traverse((object) => {
            if (!object.isMesh && !object.isSkinnedMesh) return;
            const mats = Array.isArray(object.material) ? object.material : [object.material];
            mats.forEach(mat => {
                if (!mat) return;

                if (mat._isOutline || mat.isOutline) {
                    if (disableOutline) {
                        if (mat._savedVisible === undefined) {
                            mat._savedVisible = mat.visible;
                        }
                        mat.visible = false;
                    } else {
                        if (mat._savedVisible !== undefined) {
                            mat.visible = mat._savedVisible;
                            delete mat._savedVisible;
                        }
                    }
                }
            });
        });
    }
}

// 导出到全局
window.VRMCore = VRMCore;

// 监听画质变更事件，实时应用
window.addEventListener('neko-render-quality-changed', (e) => {
    const quality = e.detail?.quality;
    if (quality && window.vrmManager?.core) {
        window.vrmManager.core.applyQualitySettings(quality);
        if (typeof window.vrmManager.onWindowResize === 'function') {
            window.vrmManager.onWindowResize();
        }
    }
});
