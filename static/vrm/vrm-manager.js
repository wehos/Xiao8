/**
 * VRM Manager - 物理控制版 (修复更新顺序)
 */

// 空闲低频 tick 模式常量（对齐 live2d-core.js / mmd-core.js 的同名机制）：
// 无活动时把 rAF 驱动的渲染链切到 setInterval，避免 120Hz 屏上 rAF 请求
// 让 Blink 以显示器刷新率空跑主帧生命周期。弹簧骨物理用真实 elapsed dt，
// 33ms 步长在 0.05s 防爆 clamp 之内，稳定性与 rAF 路径一致。
const VRM_IDLE_FPS = 30;
const VRM_INTERACTIVE_FPS_HOLD_MS = 900;
const VRM_IDLE_FPS_GOVERNOR_INTERVAL_MS = 300;
// medium 画质隔帧物理允许的最低 tick 频率：2×(1000/40) = 50ms，正好是物理 delta 的防爆 clamp
const VRM_PHYSICS_FRAME_SKIP_MIN_FPS = 40;
// 隔帧物理允许的最大步长（秒）：与 renderFrame 里 delta 的 50ms 防爆 clamp 一致
const VRM_PHYSICS_MAX_STEP_S = 0.05;

const COMPRESSED_BUNDLED_VRMA_NAMES = new Set([
    'liked',
    'wait01',
    'wait02',
    'wait03',
    'wait04',
    'wait05',
    '全身展示',
    '射击姿态',
    '屈伸运动',
    '旋转',
    '模特姿势',
    '比 V 手势',
    '致意问候'
]);

function normalizeBundledVrmAnimationPath(animationPath) {
    const value = String(animationPath || '');
    const match = value.match(/^\/static\/vrm\/animation\/([^/?#]+)\.vrma(?:[?#]|$)/i);
    if (!match) {
        return animationPath;
    }
    let assetName = match[1];
    try {
        assetName = decodeURIComponent(assetName);
    } catch (_) {
        // Keep the original path segment when it is not valid URI encoding.
    }
    if (!COMPRESSED_BUNDLED_VRMA_NAMES.has(assetName)) return animationPath;
    return value.replace(/\.vrma(?=[?#]|$)/i, '.vrma.gz');
}

class VRMManager {
    constructor() {
        this.scene = null;
        this.camera = null;
        this.renderer = null;
        this.currentModel = null;
        this.animationMixer = null;

        this.clock = (typeof window.THREE !== 'undefined') ? new window.THREE.Clock() : null;
        this.container = null;
        this._animationFrameId = null;
        this._uiUpdateLoopId = null;
        this.enablePhysics = true;
        this._lookAtTarget = null;
        this._mouseMoveHandler = null;
        this._mouseRaycaster = null;
        this._mouseNDC = null;
        this._lookAtHeadWorldPos = null;
        this._headScreenAnchorProjection = null;
        this._lookAtRayClosestPoint = null;
        this._lookAtDirection = null;
        this._lookAtBaseForward = null;
        this._lookAtBaseRight = null;
        this._lookAtBaseUp = null;
        this._lookAtWorldUp = null;
        this._lookAtDesiredPoint = null;
        // 跟随时间常数（秒）：值越大，跟随越慢、越平滑
        this._lookAtSmoothTime = 0.5;
        // 头部到视线目标的固定距离（球面半径，单位：米）
        this._lookAtDistance = 2.4;
        // 视线角度限制（度）：避免眼睛/头部打到极限位
        this._lookAtMaxYawDeg = 60;
        this._lookAtMaxPitchUpDeg = 35;
        this._lookAtMaxPitchDownDeg = 28;

        // 阴影资源引用（用于清理）
        this._shadowTexture = null;
        this._shadowMaterial = null;
        this._shadowGeometry = null;
        this._shadowMesh = null;

        // 模块作用域的 window 事件处理器数组（避免模块间冲突）：_coreWindowHandlers（VRMCore，如 resize）、_uiWindowHandlers（VRMUIButtons，如 resize, live2d-goodbye-click）、_initWindowHandlers（VRMInit，如 visibilitychange）
        this._coreWindowHandlers = [];
        this._uiWindowHandlers = [];
        this._initWindowHandlers = [];

        // CursorFollow 控制器（眼睛注视 + 头/脖子跟随）
        this._cursorFollow = null;
        this._mouseTrackingEnabled = window.mouseTrackingEnabled !== false; // 鼠标跟踪启用状态
        this._initThreePromise = null;
        this._isDisposed = false;
        this._activeLoadToken = 0;
        this._pendingModelLoadCount = 0;
        this._isLoadingModel = false;
        this._loadState = 'idle';
        this._isModelReadyForInteraction = false;

        // 向后兼容：保留 _windowEventHandlers 作为 core 的别名（避免破坏现有代码）；建议新代码使用 _coreWindowHandlers
        Object.defineProperty(this, '_windowEventHandlers', {
            get: () => this._coreWindowHandlers,
            set: (value) => { this._coreWindowHandlers = value; },
            enumerable: true,
            configurable: true
        });

        this._initModules();
    }

    _isLoadTokenActive(loadToken) {
        return this._activeLoadToken === loadToken;
    }

    _waitForSceneStability(scene, loadToken, options = {}) {
        const requiredStableFrames = options.requiredStableFrames || 2;
        const maxFrames = options.maxFrames || 24;
        const deltaThreshold = options.deltaThreshold || 0.02;
        const THREE = window.THREE;

        if (!scene || !THREE) {
            return Promise.resolve(false);
        }

        return new Promise((resolve) => {
            let stableFrames = 0;
            let frameCount = 0;
            let prevSize = null;

            const tick = () => {
                if (!this._isLoadTokenActive(loadToken) || !scene || !scene.parent) {
                    resolve(false);
                    return;
                }

                frameCount += 1;
                let size = null;
                try {
                    scene.updateMatrixWorld(true);
                    const box = new THREE.Box3().setFromObject(scene);
                    size = box.getSize(new THREE.Vector3());
                } catch (_) {
                    size = null;
                }

                const hasValidSize = size &&
                    Number.isFinite(size.x) &&
                    Number.isFinite(size.y) &&
                    Number.isFinite(size.z) &&
                    size.x > 0.001 &&
                    size.y > 0.001;
                const isStable = hasValidSize &&
                    prevSize &&
                    Math.abs(size.x - prevSize.x) <= deltaThreshold &&
                    Math.abs(size.y - prevSize.y) <= deltaThreshold &&
                    Math.abs(size.z - prevSize.z) <= deltaThreshold;

                if (isStable) {
                    stableFrames += 1;
                } else {
                    stableFrames = 0;
                }

                if (hasValidSize) {
                    prevSize = size.clone();
                }

                if ((hasValidSize && stableFrames >= requiredStableFrames) || frameCount >= maxFrames) {
                    resolve(!!hasValidSize);
                    return;
                }

                requestAnimationFrame(tick);
            };

            requestAnimationFrame(tick);
        });
    }

    _ensureMouseLookAtResources() {
        if (!window.THREE) return false;
        if (!this._mouseRaycaster) this._mouseRaycaster = new window.THREE.Raycaster();
        if (!this._mouseNDC) this._mouseNDC = new window.THREE.Vector2();
        if (!this._lookAtHeadWorldPos) this._lookAtHeadWorldPos = new window.THREE.Vector3();
        if (!this._lookAtRayClosestPoint) this._lookAtRayClosestPoint = new window.THREE.Vector3();
        if (!this._lookAtDirection) this._lookAtDirection = new window.THREE.Vector3();
        if (!this._lookAtBaseForward) this._lookAtBaseForward = new window.THREE.Vector3();
        if (!this._lookAtBaseRight) this._lookAtBaseRight = new window.THREE.Vector3();
        if (!this._lookAtBaseUp) this._lookAtBaseUp = new window.THREE.Vector3();
        if (!this._lookAtWorldUp) this._lookAtWorldUp = new window.THREE.Vector3(0, 1, 0);
        if (!this._lookAtDesiredPoint) this._lookAtDesiredPoint = new window.THREE.Vector3();
        return true;
    }

    _getLookAtHeadWorldPosition() {
        const humanoid = this.currentModel?.vrm?.humanoid;
        let headBone = null;
        if (humanoid) {
            if (typeof humanoid.getRawBoneNode === 'function') {
                headBone = humanoid.getRawBoneNode('head');
            }
            if (!headBone && typeof humanoid.getNormalizedBoneNode === 'function') {
                headBone = humanoid.getNormalizedBoneNode('head');
            }
        }
        if (headBone) {
            headBone.updateMatrixWorld(true);
            headBone.getWorldPosition(this._lookAtHeadWorldPos);
            return this._lookAtHeadWorldPos;
        }
        if (this.currentModel?.vrm?.scene) {
            this.currentModel.vrm.scene.updateMatrixWorld(true);
            this.currentModel.vrm.scene.getWorldPosition(this._lookAtHeadWorldPos);
            this._lookAtHeadWorldPos.y += 1.4;
            return this._lookAtHeadWorldPos;
        }
        this._lookAtHeadWorldPos.set(0, 1.4, 0);
        return this._lookAtHeadWorldPos;
    }

    _clampLookAtDirectionByAngle(direction, headWorldPos) {
        if (!this.camera || !window.THREE) return;

        this._lookAtBaseForward.subVectors(this.camera.position, headWorldPos);
        if (this._lookAtBaseForward.lengthSq() < 1e-8) {
            this._lookAtBaseForward.set(0, 0, 1);
        } else {
            this._lookAtBaseForward.normalize();
        }

        this._lookAtBaseRight.crossVectors(this._lookAtWorldUp, this._lookAtBaseForward);
        if (this._lookAtBaseRight.lengthSq() < 1e-8) {
            this._lookAtBaseRight.set(1, 0, 0);
        } else {
            this._lookAtBaseRight.normalize();
        }
        this._lookAtBaseUp.crossVectors(this._lookAtBaseForward, this._lookAtBaseRight).normalize();

        const x = direction.dot(this._lookAtBaseRight);
        const y = direction.dot(this._lookAtBaseUp);
        const z = direction.dot(this._lookAtBaseForward);

        const yaw = Math.atan2(x, z);
        const horizontalLen = Math.sqrt(x * x + z * z);
        const pitch = Math.atan2(y, Math.max(1e-8, horizontalLen));

        const maxYaw = window.THREE.MathUtils.degToRad(this._lookAtMaxYawDeg);
        const maxPitchUp = window.THREE.MathUtils.degToRad(this._lookAtMaxPitchUpDeg);
        const maxPitchDown = window.THREE.MathUtils.degToRad(this._lookAtMaxPitchDownDeg);

        const clampedYaw = window.THREE.MathUtils.clamp(yaw, -maxYaw, maxYaw);
        const clampedPitch = window.THREE.MathUtils.clamp(pitch, -maxPitchDown, maxPitchUp);
        const cosPitch = Math.cos(clampedPitch);

        direction.copy(this._lookAtBaseRight).multiplyScalar(Math.sin(clampedYaw) * cosPitch)
            .addScaledVector(this._lookAtBaseUp, Math.sin(clampedPitch))
            .addScaledVector(this._lookAtBaseForward, Math.cos(clampedYaw) * cosPitch)
            .normalize();
    }

    _setLookAtTargetByMouse(clientX, clientY) {
        if (!this.camera || !this.renderer || !this._lookAtTarget) return;
        if (!this._ensureMouseLookAtResources()) return;

        const canvas = this.renderer.domElement;
        if (!canvas) return;

        const rect = canvas.getBoundingClientRect();
        if (!rect.width || !rect.height) return;

        this._mouseNDC.x = ((clientX - rect.left) / rect.width) * 2 - 1;
        this._mouseNDC.y = -((clientY - rect.top) / rect.height) * 2 + 1;

        this._mouseRaycaster.setFromCamera(this._mouseNDC, this.camera);
        const headWorldPos = this._getLookAtHeadWorldPosition();
        this._mouseRaycaster.ray.closestPointToPoint(headWorldPos, this._lookAtRayClosestPoint);
        this._lookAtDirection.subVectors(this._lookAtRayClosestPoint, headWorldPos);

        // 当鼠标刚好在头部投影附近时，回退到头部朝向相机方向，避免方向向量为零
        if (this._lookAtDirection.lengthSq() < 1e-8) {
            this._lookAtDirection.subVectors(this.camera.position, headWorldPos);
        }
        if (this._lookAtDirection.lengthSq() < 1e-8) return;

        this._lookAtDirection.normalize();
        this._clampLookAtDirectionByAngle(this._lookAtDirection, headWorldPos);
        this._lookAtDesiredPoint.copy(headWorldPos).addScaledVector(this._lookAtDirection, this._lookAtDistance);
    }

    _initMouseLookAtTracking() {
        if (!this.scene || !this.camera || !window.THREE) return;

        // ── 初始化 CursorFollowController（替代旧的 mousemove 跟踪） ──
        if (typeof window.CursorFollowController !== 'undefined') {
            if (!this._cursorFollow) {
                this._cursorFollow = new window.CursorFollowController();
            }
            if (!this._cursorFollow._initialized) {
                this._cursorFollow.init(this);
            }
            // 同步鼠标跟踪启用状态
            const storedMouseTrackingEnabled = window.mouseTrackingEnabled !== false;
            const isEnabled = window.nekoYuiGuideFaceForwardLock === true
                ? false
                : storedMouseTrackingEnabled;
            console.log(`[VRM] 鼠标跟踪检查: window.mouseTrackingEnabled=${window.mouseTrackingEnabled}, isEnabled=${isEnabled}`);
            if (this._cursorFollow.isEnabled() !== isEnabled) {
                this._cursorFollow.setEnabled(isEnabled);
            }
            // 同步内部状态
            this._mouseTrackingEnabled = storedMouseTrackingEnabled;
            // CursorFollow 拥有自己的 eyesTarget，旧 _lookAtTarget 不再需要
            return;
        }

        // ── 回退：旧的 lookAt 跟踪（CursorFollowController 未加载时） ──
        if (!this.isMouseTrackingEnabled()) return;
        if (!this._ensureMouseLookAtResources()) return;

        if (!this._lookAtTarget) {
            this._lookAtTarget = new window.THREE.Object3D();
            this._lookAtTarget.name = 'VRMLookAtMouseTarget';
        }
        if (this._lookAtTarget.parent !== this.scene) {
            this.scene.add(this._lookAtTarget);
        }

        // 初始位置放在“头部->相机”方向的固定距离上，保持球面轨迹起点稳定
        const headWorldPos = this._getLookAtHeadWorldPosition();
        const initialDir = new window.THREE.Vector3().subVectors(this.camera.position, headWorldPos);
        if (initialDir.lengthSq() < 1e-8) {
            initialDir.set(0, 0, 1);
        } else {
            initialDir.normalize();
        }
        this._lookAtTarget.position.copy(headWorldPos).addScaledVector(initialDir, this._lookAtDistance);
        this._lookAtDesiredPoint.copy(this._lookAtTarget.position);

        if (!this._mouseMoveHandler) {
            this._mouseMoveHandler = (event) => {
                if (!this.isMouseTrackingEnabled()) return;
                // 供空闲低频 governor 判定"光标最近在动"（legacy 视线跟随路径）。
                // 坐标没变的重复事件（Electron Pet 的 preload 轮询在光标静止时也会转发）不算，
                // 否则 VRM 永远降不到空闲帧率。
                const moved = event.clientX !== this._lastLookAtMouseX || event.clientY !== this._lastLookAtMouseY;
                this._lastLookAtMouseX = event.clientX;
                this._lastLookAtMouseY = event.clientY;
                if (moved) this._lastLookAtPointerMoveAt = performance.now();
                this._setLookAtTargetByMouse(event.clientX, event.clientY);
            };
            document.addEventListener('mousemove', this._mouseMoveHandler, { passive: true });
        }
    }

    _initModules() {
        if (!this.core && typeof window.VRMCore !== 'undefined') this.core = new window.VRMCore(this);
        if (!this.expression && typeof window.VRMExpression !== 'undefined') this.expression = new window.VRMExpression(this);
        if (!this.animation && typeof window.VRMAnimation !== 'undefined') {
            this.animation = new window.VRMAnimation(this);
        }
        if (!this.interaction && typeof window.VRMInteraction !== 'undefined') this.interaction = new window.VRMInteraction(this);
    }
    _createBlobShadowTexture() {
        // 在高 DPI 设备上使用更高分辨率，提升阴影质量
        const dpr = window.devicePixelRatio || 1;
        const baseSize = 64;
        const size = Math.max(baseSize, Math.round(baseSize * Math.min(dpr, 2)));

        const canvas = document.createElement('canvas');
        canvas.width = size;
        canvas.height = size;
        const ctx = canvas.getContext('2d');

        if (!ctx) {
            console.warn('[VRM Manager] 无法获取 2d context，返回透明纹理');
            return new window.THREE.CanvasTexture(canvas);
        }

        const center = size / 2;
        const radius = center;
        const gradient = ctx.createRadialGradient(center, center, 0, center, center, radius);
        gradient.addColorStop(0, 'rgba(0, 0, 0, 0.6)');
        gradient.addColorStop(0.5, 'rgba(0, 0, 0, 0.3)');
        gradient.addColorStop(1, 'rgba(0, 0, 0, 0)');

        ctx.fillStyle = gradient;
        ctx.fillRect(0, 0, size, size);

        const texture = new window.THREE.CanvasTexture(canvas);
        return texture;
    }

    _calculateAndAddShadow(result) {
        const SHADOW_SCALE_MULT = 0.5;
        const SHADOW_Y_OFFSET = 0.001;
        const FIX_CENTER_XZ = true;

        result.vrm.scene.updateMatrixWorld(true);

        const bodyBox = new window.THREE.Box3();
        let hasBodyMesh = false;

        result.vrm.scene.traverse((object) => {
            if (object.isSkinnedMesh) {
                object.updateMatrixWorld(true);
                const meshBox = new window.THREE.Box3();
                meshBox.setFromObject(object);
                bodyBox.union(meshBox);
                hasBodyMesh = true;
            }
        });

        if (!hasBodyMesh) {
            bodyBox.setFromObject(result.vrm.scene);
        }

        result.vrm.scene.updateMatrixWorld(true);
        const sceneInverseMatrix = result.vrm.scene.matrixWorld.clone().invert();
        const worldCorners = [
            new window.THREE.Vector3(bodyBox.min.x, bodyBox.min.y, bodyBox.min.z),
            new window.THREE.Vector3(bodyBox.max.x, bodyBox.min.y, bodyBox.min.z),
            new window.THREE.Vector3(bodyBox.min.x, bodyBox.max.y, bodyBox.min.z),
            new window.THREE.Vector3(bodyBox.max.x, bodyBox.max.y, bodyBox.min.z),
            new window.THREE.Vector3(bodyBox.min.x, bodyBox.min.y, bodyBox.max.z),
            new window.THREE.Vector3(bodyBox.max.x, bodyBox.min.y, bodyBox.max.z),
            new window.THREE.Vector3(bodyBox.min.x, bodyBox.max.y, bodyBox.max.z),
            new window.THREE.Vector3(bodyBox.max.x, bodyBox.max.y, bodyBox.max.z),
        ];
        const localBodyBox = new window.THREE.Box3();
        worldCorners.forEach(corner => {
            const localCorner = corner.clone().applyMatrix4(sceneInverseMatrix);
            localBodyBox.expandByPoint(localCorner);
        });

        // 获取包围盒尺寸（用于计算阴影大小，使用世界空间的尺寸）
        const bodySize = new window.THREE.Vector3();
        bodyBox.getSize(bodySize);

        // 计算阴影大小（使用身体宽度和深度的较大值作为基准）
        const shadowDiameter = Math.max(
            Math.max(bodySize.x, bodySize.z) * SHADOW_SCALE_MULT,
            0.3  // 最小尺寸保底
        );

        // 5. 清理之前的阴影资源（如果存在）
        this._disposeShadowResources();

        // 6. 创建阴影纹理和材质
        this._shadowTexture = this._createBlobShadowTexture();
        this._shadowMaterial = new window.THREE.MeshBasicMaterial({
            map: this._shadowTexture,
            transparent: true,
            opacity: 1.0,
            depthWrite: false,  // 不写入深度缓冲，避免遮挡模型
            side: window.THREE.DoubleSide
        });

        // 7. 创建阴影网格
        this._shadowGeometry = new window.THREE.PlaneGeometry(1, 1);
        this._shadowMesh = new window.THREE.Mesh(this._shadowGeometry, this._shadowMaterial);
        this._shadowMesh.rotation.x = -Math.PI / 2;  // 旋转到水平面
        this._shadowMesh.scale.set(shadowDiameter, shadowDiameter, 1);

        // 8. 计算阴影位置（使用多种回退策略）
        let shadowX = 0;
        let shadowY = 0;
        let shadowZ = 0;

        // 优先使用 humanoid 骨骼来精确定位（使用 getNormalizedBoneNode() API 保证 VRM0/VRM1 兼容性）
        if (result.vrm.humanoid) {
            try {
                // 优先使用脚趾骨骼（leftToes/rightToes），因为脚部骨骼在脚踝位置
                const leftToes = result.vrm.humanoid.getNormalizedBoneNode('leftToes');
                const rightToes = result.vrm.humanoid.getNormalizedBoneNode('rightToes');
                const leftFoot = result.vrm.humanoid.getNormalizedBoneNode('leftFoot');
                const rightFoot = result.vrm.humanoid.getNormalizedBoneNode('rightFoot');

                // 优先使用脚趾骨骼，如果不存在则使用脚部骨骼
                const leftTargetBone = leftToes || leftFoot;
                const rightTargetBone = rightToes || rightFoot;

                if (leftTargetBone && rightTargetBone) {
                    // 更新骨骼矩阵
                    leftTargetBone.updateMatrixWorld(true);
                    rightTargetBone.updateMatrixWorld(true);

                    // 获取两脚的世界位置
                    const leftFootPos = new window.THREE.Vector3();
                    const rightFootPos = new window.THREE.Vector3();
                    leftTargetBone.getWorldPosition(leftFootPos);
                    rightTargetBone.getWorldPosition(rightFootPos);

                    // 如果使用的是脚部骨骼（不是脚趾），需要向下偏移到脚底（脚部骨骼在脚踝，脚趾骨骼在脚底）
                    let leftBottomY = leftFootPos.y;
                    let rightBottomY = rightFootPos.y;

                    // 查找最低Y坐标的辅助函数
                    const findLowestY = (bone, currentY) => {
                        let lowest = currentY;
                        if (bone) {
                            bone.updateMatrixWorld(true);
                            const pos = new window.THREE.Vector3();
                            bone.getWorldPosition(pos);
                            if (pos.y < lowest) {
                                lowest = pos.y;
                            }
                            // 递归检查所有子骨骼
                            bone.children.forEach(child => {
                                lowest = findLowestY(child, lowest);
                            });
                        }
                        return lowest;
                    };

                    // 如果使用的是脚部骨骼，需要向下偏移（估算脚的长度）
                    if (!leftToes && leftFoot) {
                        leftBottomY = findLowestY(leftFoot, leftFootPos.y);
                    }

                    if (!rightToes && rightFoot) {
                        rightBottomY = findLowestY(rightFoot, rightFootPos.y);
                    }

                    // 转换为相对于 vrm.scene 的局部坐标
                    result.vrm.scene.updateMatrixWorld(true);
                    const currentSceneInverseMatrix = result.vrm.scene.matrixWorld.clone().invert();

                    // 将最低点转换为局部坐标
                    const leftBottomPos = new window.THREE.Vector3(leftFootPos.x, leftBottomY, leftFootPos.z);
                    const rightBottomPos = new window.THREE.Vector3(rightFootPos.x, rightBottomY, rightFootPos.z);
                    leftBottomPos.applyMatrix4(currentSceneInverseMatrix);
                    rightBottomPos.applyMatrix4(currentSceneInverseMatrix);

                    // Y轴：使用两脚中较低的 Y 值，确保阴影在脚底
                    shadowY = Math.min(leftBottomPos.y, rightBottomPos.y) + SHADOW_Y_OFFSET;

                    // X/Z轴：使用两脚的中点（如果 FIX_CENTER_XZ 为 false，当前固定为 true，此分支不会执行）
                    if (!FIX_CENTER_XZ) {
                        leftFootPos.applyMatrix4(currentSceneInverseMatrix);
                        rightFootPos.applyMatrix4(currentSceneInverseMatrix);
                        shadowX = (leftFootPos.x + rightFootPos.x) / 2;
                        shadowZ = (leftFootPos.z + rightFootPos.z) / 2;
                    }
                } else {
                    // 如果没有脚部骨骼，尝试使用 hips 骨骼
                    const hipsBone = result.vrm.humanoid.getNormalizedBoneNode('hips');
                    if (hipsBone) {
                        hipsBone.updateMatrixWorld(true);

                        const hipsPos = new window.THREE.Vector3();
                        hipsBone.getWorldPosition(hipsPos);

                        // 转换为局部坐标
                        result.vrm.scene.updateMatrixWorld(true);
                        const currentSceneInverseMatrix = result.vrm.scene.matrixWorld.clone().invert();
                        hipsPos.applyMatrix4(currentSceneInverseMatrix);

                        // 使用 hips 的 X/Z 位置（如果 FIX_CENTER_XZ 为 false，当前固定为 true，此分支不会执行）
                        if (!FIX_CENTER_XZ) {
                            shadowX = hipsPos.x;
                            shadowZ = hipsPos.z;
                        }

                        // Y轴：使用本地空间包围盒的最低点（因为 hips 在腰部，不是脚底）
                        shadowY = localBodyBox.min.y + SHADOW_Y_OFFSET;
                    } else {
                        // 如果连 hips 都没有，使用本地空间包围盒的最低点
                        shadowY = localBodyBox.min.y + SHADOW_Y_OFFSET;
                    }
                }
            } catch (e) {
                // 回退到使用本地空间包围盒
                shadowY = localBodyBox.min.y + SHADOW_Y_OFFSET;
            }
        } else {
            // 如果没有 humanoid，使用本地空间包围盒的最低点
            shadowY = localBodyBox.min.y + SHADOW_Y_OFFSET;
        }

        // 如果 FIX_CENTER_XZ 为 true，强制使用 (0, 0) 作为 X/Z
        if (FIX_CENTER_XZ) {
            shadowX = 0;
            shadowZ = 0;
        }

        // 9. 缓存脚骨引用，供 animate loop 每帧更新阴影 Y
        this._shadowFootBones = [];
        if (result.vrm.humanoid) {
            for (const name of ['leftToes', 'rightToes', 'leftFoot', 'rightFoot']) {
                const bone = result.vrm.humanoid.getNormalizedBoneNode(name);
                if (bone) this._shadowFootBones.push(bone);
            }
        }

        // 10. 设置阴影位置
        this._shadowMesh.position.set(shadowX, shadowY, shadowZ);

        // 11. 添加到模型场景中
        result.vrm.scene.add(this._shadowMesh);
    }

    /**
     * 清理阴影资源（纹理、材质、几何体、网格）
     */
    _disposeShadowResources() {
        // 从场景中移除阴影网格
        if (this._shadowMesh) {
            if (this._shadowMesh.parent) {
                this._shadowMesh.parent.remove(this._shadowMesh);
            }
            this._shadowMesh = null;
        }

        // 清理几何体
        if (this._shadowGeometry) {
            this._shadowGeometry.dispose();
            this._shadowGeometry = null;
        }

        // 清理材质
        if (this._shadowMaterial) {
            if (this._shadowMaterial.map) {
                // 检查材质使用的纹理是否就是 _shadowTexture，避免双重释放
                if (this._shadowMaterial.map === this._shadowTexture) {
                    this._shadowMaterial.map.dispose();
                    this._shadowTexture = null;
                } else {
                    this._shadowMaterial.map.dispose();
                }
            }
            this._shadowMaterial.dispose();
            this._shadowMaterial = null;
        }

        // 清理纹理（如果材质没有清理它）
        if (this._shadowTexture) {
            this._shadowTexture.dispose();
            this._shadowTexture = null;
        }
    }

    async initThreeJS(canvasId, containerId, lightingConfig = null) {
        if (this._initThreePromise) {
            return await this._initThreePromise;
        }
        this._isDisposed = false;

        // 恢复容器可见性：在 Live2D/VRM 之间反复切换时，cleanup 会把容器隐藏
        // （display:none + 'hidden' class），若 _isInitialized 为 true，app-character.js
        // 会跳过整个初始化块，导致下次切回 VRM 时容器仍不可见，新模型"加载不出来"。
        // 【修复】仅在非 MMD 子类型时恢复容器可见性，防止 MMD 模式下 VRM 容器
        // 被意外显示（initThreeJS 可能被 loadModel 等间接调用，此时 MMD 正在前台）。
        const isMmdMode = (window.lanlan_config?.live3d_sub_type || '').toLowerCase() === 'mmd';
        const container = containerId ? document.getElementById(containerId) : null;
        if (container && !isMmdMode) {
            container.style.display = 'block';
            container.style.visibility = 'visible';
            container.style.opacity = '1';
            container.classList.remove('hidden');
        }

        // 检查是否已完全初始化（不仅检查 scene，还要检查 camera 和 renderer）
        if (this.scene && this.camera && this.renderer) {
            // 恢复 renderer canvas 可见性（切换清理时会把 domElement 设为 display:none）
            // 【修复】同样受 MMD 守卫保护
            if (this.renderer.domElement && !isMmdMode) {
                this.renderer.domElement.style.display = 'block';
            }
            this._initMouseLookAtTracking();
            this._isInitialized = true;
            return true;
        }

        this._initThreePromise = (async () => {
            if (!this.clock && window.THREE) this.clock = new window.THREE.Clock();
            this._initModules();
            if (!this.core) {
                const errorMsg = window.t ? window.t('vrm.error.coreNotLoaded') : 'VRMCore 尚未加载';
                throw new Error(errorMsg);
            }
            await this.core.init(canvasId, containerId, lightingConfig);
            if (this._isDisposed) return false;
            if (this.interaction) this.interaction.initDragAndZoom();
            this._initMouseLookAtTracking();
            this.startAnimateLoop();
            // 设置初始化标志
            this._isInitialized = true;
            return true;
        })();

        try {
            return await this._initThreePromise;
        } finally {
            this._initThreePromise = null;
        }
    }

    async ensureThreeReady(canvasId, containerId, lightingConfig = null) {
        if (this.scene && this.camera && this.renderer && this._isInitialized) {
            return true;
        }
        return await this.initThreeJS(canvasId, containerId, lightingConfig);
    }

    startAnimateLoop() {
        // 先退出空闲低频 tick 模式（不复跑 rAF，下面自建新链），避免双驱动
        this._exitIdleTickMode(false);
        if (this._animationFrameId) cancelAnimationFrame(this._animationFrameId);
        this._lastRenderTime = 0;
        this._nextRenderTime = 0;

        // 帧体：rAF 驱动与空闲低频 interval 共用（interval 直接调用本函数，
        // 定时器周期即节流器，不经过 rAF 驱动里的 targetFrameRate 跳帧）
        const renderFrame = (frameNow) => {
            // 获取时间增量并限制最大值，防止切屏或卡顿导致物理"爆炸"
            let delta = this.clock ? this.clock.getDelta() : 0.016;
            delta = Math.min(delta, 0.05);

            if (!this.animation && typeof window.VRMAnimation !== 'undefined') this._initModules();

            if (this.currentModel && this.currentModel.vrm) {
                // 0. 主灯跟随相机
                if (this.mainLight && this.camera) {
                    // 复用 scratch 向量：本分支每渲染帧都会执行，避免逐帧 new Vector3
                    if (!this._mainLightOffsetScratch) this._mainLightOffsetScratch = new window.THREE.Vector3();
                    const lightOffset = this._mainLightOffsetScratch.set(0.2, 0.5, 1.5);
                    lightOffset.applyQuaternion(this.camera.quaternion);
                    this.mainLight.position.copy(this.camera.position).add(lightOffset);

                    if (this.currentModel.vrm.scene) {
                        this.mainLight.target = this.currentModel.vrm.scene;
                    }
                }

                // 1. 表情更新
                if (this.expression) {
                    this.expression.update(delta);
                }

                // 2. CursorFollow：更新眼睛目标位置
                if (this._cursorFollow) {
                    this._cursorFollow.updateTarget(delta);
                }

                // 3. 设置 lookAt 目标
                if (this.currentModel.vrm.lookAt) {
                    if (this._cursorFollow && this._cursorFollow.eyesTarget && this._cursorFollow.isEnabled()) {
                        // CursorFollow 已加载且启用 → 使用 eyesTarget
                        if (this.currentModel.vrm.lookAt.target !== this._cursorFollow.eyesTarget) {
                            this.currentModel.vrm.lookAt.target = this._cursorFollow.eyesTarget;
                        }
                    } else if (this._cursorFollow && !this._cursorFollow.isEnabled()) {
                        // CursorFollow 已加载但禁用 → 设为 null，SDK 内部自动跳过 lookAt 求解
                        if (this.currentModel.vrm.lookAt.target !== null) {
                            this.currentModel.vrm.lookAt.target = null;
                        }
                    } else if (!this.isMouseTrackingEnabled()) {
                        // 旧 fallback 也必须遵守禁用状态（例如轻量卡片嵌入未加载 CursorFollow）
                        if (this.currentModel.vrm.lookAt.target !== null) {
                            this.currentModel.vrm.lookAt.target = null;
                        }
                    } else {
                        // CursorFollow 未加载时的旧 fallback 逻辑（保持兼容）
                        if (this._lookAtTarget && this._lookAtDesiredPoint) {
                            const smoothTime = Math.max(0.01, this._lookAtSmoothTime);
                            const alpha = Math.min(1, 1 - Math.exp(-delta / smoothTime));
                            this._lookAtTarget.position.lerp(this._lookAtDesiredPoint, alpha);
                        }
                        const fallbackLookAtTarget = this._lookAtTarget || this.camera;
                        if (this.currentModel.vrm.lookAt.target !== fallbackLookAtTarget) {
                            this.currentModel.vrm.lookAt.target = fallbackLookAtTarget;
                        }
                    }
                }

                // 4. 动画更新（mixer.update → VRMA 动作）
                if (this.animation) {
                    this.animation.update(delta);
                }

                // 5. VRM 核心更新（LookAt / SpringBone 物理）— 受画质设置影响
                // low: 仅 lookAt + expressions；medium: 隔帧物理；high: 每帧物理
                const quality = window.renderQuality || 'medium';
                if (this.enablePhysics && quality !== 'low') {
                    // 30fps 地板上绕过 medium 的隔帧物理：再隔帧会让弹簧骨物理退化到
                    // 15Hz/66.7ms 步长，超出 50ms 防爆 clamp 的设计意图且发丝/裙摆可见抖动；
                    // 30Hz 全帧物理成本 ≈ medium@60 不变。判据是 tick 频率而不是「是否定时器
                    // 驱动」：活动态定时器驱动（45/60fps）仍按 medium 隔帧，物理成本不翻倍。
                    // 再按实际 delta 兜底：rAF 路径被配置限到 30~39fps、或定时器抖动时，
                    // 隔帧后的步长 2×delta 超过 50ms clamp 也走全量物理
                    // 被跳过那一帧的时间累计在 _physicsPendingDelta 里，下一次物理更新把它一起
                    // 补上；隔帧/全量两种模式之间切换时不丢时间也不重复计时（全量分支同样
                    // 冲掉累计值并重置奇偶计数，重新进入隔帧模式从"跳过"一帧开始）。
                    // 换了模型：上一模型残留的累计时间/奇偶计数不能带到新模型
                    if (this._physicsStateModel !== this.currentModel.vrm) {
                        this._physicsStateModel = this.currentModel.vrm;
                        this._physicsFrameSkip = 0;
                        this._physicsPendingDelta = 0;
                    }
                    const pendingDelta = this._physicsPendingDelta || 0;
                    // 补上累计时间后的步长仍受 50ms 防爆 clamp
                    const physicsStep = Math.min(delta + pendingDelta, VRM_PHYSICS_MAX_STEP_S);
                    // 隔帧只在能把弹簧骨物理单独更新时启用；旧版 three-vrm 只有整体 vrm.update(t)，
                    // 累计步长会一并喂给 lookAt/材质，所以旧版一律每帧 update(delta)
                    const canSplitPhysics = this._canSplitVrmPhysicsUpdate(this.currentModel.vrm);
                    if (quality === 'medium' && canSplitPhysics && !this._isLowTickRate() && delta * 2 <= VRM_PHYSICS_MAX_STEP_S) {
                        this._physicsFrameSkip = (this._physicsFrameSkip || 0) + 1;
                        if (this._physicsFrameSkip % 2 === 0) {
                            this._physicsPendingDelta = 0;
                            this._updateVrmWithPhysicsStep(this.currentModel.vrm, delta, physicsStep);
                        } else {
                            this._physicsPendingDelta = pendingDelta + delta;
                            // 跳过的只是弹簧骨物理：其余分量（humanoid / lookAt / 表情 / 约束 / 材质）
                            // 仍按库顺序每帧推进，受约束的骨骼不会显示上一帧姿态
                            this._updateVrmWithoutPhysics(this.currentModel.vrm, delta);
                        }
                    } else {
                        this._physicsFrameSkip = 0;
                        this._physicsPendingDelta = 0;
                        this._updateVrmWithPhysicsStep(this.currentModel.vrm, delta, physicsStep);
                    }
                } else {
                    // 物理关闭 / low 画质：清掉隔帧状态，重新开启时从干净状态起步，
                    // 不把旧会话的累计时间套到当前（甚至新）模型上
                    this._physicsFrameSkip = 0;
                    this._physicsPendingDelta = 0;
                    if (this.currentModel.vrm.lookAt) this.currentModel.vrm.lookAt.update(delta);
                    if (this.currentModel.vrm.expressionManager) this.currentModel.vrm.expressionManager.update(delta);
                }

                // 6. CursorFollow：头/颈加成旋转（在 vrm.update 之后，确保不被覆盖）
                // VRMA 动画播放中（包括 idle）跳过 applyHead：动画自身的 lookAtProxy track
                // 已包含视线方向，由 vrm.lookAt.update() 处理。applyHead 额外叠加头颈旋转
                // 会和动画打架导致脖子抽动（特别是跨 clip crossfade 期间）。
                if (this._cursorFollow && !(this.animation && this.animation.vrmaIsPlaying)) {
                    this._cursorFollow.applyHead(delta);
                }
            }

            // 6.5 阴影 Y 跟随脚骨（每帧更新，修复跳舞时阴影不动 + 初始高度不对）
            if (this._shadowMesh && this._shadowFootBones && this._shadowFootBones.length > 0
                && this.currentModel && this.currentModel.vrm && this.currentModel.vrm.scene) {
                if (!this._shadowTmpMat4) this._shadowTmpMat4 = new window.THREE.Matrix4();
                if (!this._shadowTmpVec3) this._shadowTmpVec3 = new window.THREE.Vector3();
                this._shadowTmpMat4.copy(this.currentModel.vrm.scene.matrixWorld).invert();
                let minY = Infinity;
                for (const bone of this._shadowFootBones) {
                    bone.getWorldPosition(this._shadowTmpVec3);
                    this._shadowTmpVec3.applyMatrix4(this._shadowTmpMat4);
                    if (this._shadowTmpVec3.y < minY) minY = this._shadowTmpVec3.y;
                }
                if (minY !== Infinity) {
                    this._shadowMesh.position.y = minY + 0.001;
                }
            }

            // 7. 交互系统更新（浮动按钮跟随等）
            if (this.interaction) {
                this.interaction.update(delta);
            }

            // 8. 更新控制器
            if (this.controls) {
                this.controls.update();
            }

            // 9. 渲染场景
            if (this.renderer && this.scene && this.camera) {
                this.renderer.render(this.scene, this.camera);
            }

            // 10. VMC Protocol 采样钩子。全局 active 标志由按需加载的
            // sender 维护；未显式启用时不调用任何 VMC 逐帧逻辑。
            if (
                window.__NEKO_VMC_ACTIVE__ === true
                && window.vrmVmcSender
                && this.currentModel
                && this.currentModel.vrm
            ) {
                try {
                    window.vrmVmcSender.sample(this.currentModel.vrm);
                } catch (e) {
                    // VMC 采样异常绝不能影响渲染循环。挂起采样避免每帧重试
                    // 抛异常污染控制台；vrm-vmc-sender.js 的状态轮询会在后端
                    // 仍启用 VMC 时自动恢复采样（最多延迟一个轮询周期）。
                    console.warn('[VRM Manager] VMC sample failed, suspending hook:', e);
                    if (typeof window.vrmVmcSender.suspendSampling === 'function') {
                        window.vrmVmcSender.suspendSampling();
                    } else {
                        // Mixed-cache/third-party fallback: preserve the old
                        // fail-closed behavior when suspension is unavailable.
                        window.vrmVmcSender = null;
                    }
                }
            }
        };
        this._renderFrame = renderFrame;

        const animateLoop = () => {
            // 检查渲染器、场景和相机是否都存在，如果任何一个被 dispose 了则取消动画循环
            if (!this.renderer || !this.scene || !this.camera) {
                if (this._animationFrameId) {
                    cancelAnimationFrame(this._animationFrameId);
                    this._animationFrameId = null;
                }
                return;
            }

            this._animationFrameId = requestAnimationFrame(animateLoop);

            // VMC 启用时使用累计目标时间，避免 144Hz 等非整数倍刷新率
            // 下发送速率退化；关闭时保留原渲染节流行为，隔离功能影响。
            const now = performance.now();
            // 与定时器路径同一套解析：负数/NaN/Infinity 一律按 60，0 = 不限帧
            const targetFps = this._resolveConfiguredTargetFps();
            if (targetFps > 0) {
                const frameInterval = 1000 / targetFps;
                if (window.__NEKO_VMC_ACTIVE__ === true) {
                    if (this._nextRenderTime <= 0 || now - this._nextRenderTime > frameInterval * 4) {
                        this._nextRenderTime = now;
                    }
                    if (now < this._nextRenderTime) return;
                    this._nextRenderTime += frameInterval;
                    this._lastRenderTime = now;
                } else {
                    this._nextRenderTime = 0;
                    if (now - this._lastRenderTime < frameInterval * 0.9) return;
                    this._lastRenderTime = now;
                }
            } else {
                this._nextRenderTime = 0;
                this._lastRenderTime = now;
            }
            renderFrame(now);
        };
        this._rafDriver = animateLoop;

        this._animationFrameId = requestAnimationFrame(animateLoop);
        this._startIdleFpsGovernor();
    }

    // ═══════ 空闲低频 tick 模式（对齐 live2d-core.js / mmd-core.js 的同名机制）═══════

    // 用户配置的目标帧率；负数/非法值一律按 60，0 = 不限帧
    _resolveConfiguredTargetFps() {
        const raw = typeof window.targetFrameRate === 'number' ? Number(window.targetFrameRate) : 60;
        return Number.isFinite(raw) && raw >= 0 ? raw : 60;
    }

    _resolveIdleFps() {
        const configured = this._resolveConfiguredTargetFps();
        return configured === 0 ? VRM_IDLE_FPS : Math.min(VRM_IDLE_FPS, configured);
    }

    // 活动态应改用定时器驱动的帧率（Pet 窗口且配置帧率明显低于显示器刷新率时由
    // frame-pacing.js 给出）；null = 留在 rAF 驱动。非 Pet 页面没有 nekoFramePacing，恒为 null。
    _resolveActiveTimerTickFps() {
        const pacing = window.nekoFramePacing;
        if (!pacing || typeof pacing.activeTimerTickFps !== 'function') return null;
        const fps = Number(pacing.activeTimerTickFps(this._resolveConfiguredTargetFps()));
        return Number.isFinite(fps) && fps > 0 ? fps : null;
    }

    // 分量更新：只有弹簧骨物理吃「当前帧 + 上一跳过帧」的累计步长，LookAt / 表情 / 材质
    // 仍按当前帧 delta 推进——three-vrm 的 VRM.update(t) 会把 t 一并喂给 lookAt 和 MToon
    // 材质，直接传累计步长会让视线/材质动画在一帧里多走一倍。顺序与 three-vrm 的
    // VRM.update 完全一致（VRMCore.update：humanoid → lookAt → 表情；再 约束 → 弹簧骨 → 材质），
    // 约束/弹簧骨必须在 LookAt 驱动的姿态之后跑，否则拿到的是上一帧的骨骼变换。
    // 没有分量 API（旧版 three-vrm）时退回整体 update。
    _canSplitVrmPhysicsUpdate(vrm) {
        return !!(vrm && vrm.springBoneManager && typeof vrm.springBoneManager.update === 'function');
    }

    _updateVrmWithPhysicsStep(vrm, delta, physicsStep) {
        if (this._canSplitVrmPhysicsUpdate(vrm)) {
            this._updateVrmPoseComponents(vrm, delta);
            vrm.springBoneManager.update(physicsStep);
            this._updateVrmMaterials(vrm, delta);
            return;
        }
        vrm.update(physicsStep);
    }

    // 跳过弹簧骨物理的帧：除 springBoneManager 外的分量全部按当前 delta 推进（库顺序不变）
    _updateVrmWithoutPhysics(vrm, delta) {
        this._updateVrmPoseComponents(vrm, delta);
        this._updateVrmMaterials(vrm, delta);
    }

    // VRM.update 里弹簧骨之前的部分：humanoid → lookAt → 表情 → 约束
    _updateVrmPoseComponents(vrm, delta) {
        if (!vrm) return;
        if (vrm.humanoid && typeof vrm.humanoid.update === 'function') vrm.humanoid.update();
        if (vrm.lookAt) vrm.lookAt.update(delta);
        if (vrm.expressionManager) vrm.expressionManager.update(delta);
        if (vrm.nodeConstraintManager && typeof vrm.nodeConstraintManager.update === 'function') vrm.nodeConstraintManager.update();
    }

    // MToon 等材质的逐帧推进（three-vrm VRM.update 的最后一步）；跳过物理的帧也要调
    _updateVrmMaterials(vrm, delta) {
        if (!vrm || !Array.isArray(vrm.materials)) return;
        vrm.materials.forEach((material) => {
            if (material && typeof material.update === 'function') material.update(delta);
        });
    }

    // 当前定时器 tick 频率是否低到不能再隔帧做物理：隔帧后的步长 2×(1000/fps) 必须
    // 落在 50ms 防爆 clamp 之内，即 fps ≥ 40；再低（含 30 地板、以及 31~39 的非
    // 标准配置）就每 tick 全量物理。与 _idleTickMode 区分：活动态定时器驱动也置
    // 该标志，但频率是配置帧率，45/60 仍按 medium 隔帧。
    _isLowTickRate() {
        if (!this._idleTickMode) return false;
        const fps = Number(this._idleTickFps);
        return !(fps >= VRM_PHYSICS_FRAME_SKIP_MIN_FPS);
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
        if (!this.renderer || !this.scene || !this.camera) return;
        // 外部已暂停（pauseRendering 后 rAF 链不存在）：不接管
        if (!this._animationFrameId) return;
        this._idleTickMode = true;
        cancelAnimationFrame(this._animationFrameId);
        this._animationFrameId = null;
        this._idleTickTimer = this._startTimerTick(tickFps);
    }

    // 定时器驱动的 tick 循环：空闲地板（30）和活动态定时器驱动（配置帧率）共用，只有周期不同。
    _startTimerTick(fps) {
        this._idleTickFps = fps;
        const intervalMs = Math.max(4, Math.round(1000 / Math.max(1, fps)));
        return setInterval(() => {
            if (!this._idleTickMode) return;
            if (!this.renderer || !this.scene || !this.camera) { this._exitIdleTickMode(false); return; }
            // 外部代码绕过 startAnimateLoop 拉起了 rAF 链：让位
            if (this._animationFrameId) { this._exitIdleTickMode(false); return; }
            // 纯浏览器隐藏标签页对齐 rAF 暂停语义（Electron 宠物窗关了节流，不受影响）
            if (typeof document !== 'undefined' && document.hidden) return;
            try {
                if (this._renderFrame) this._renderFrame(performance.now());
            } catch (e) {
                if (!this._idleTickErrorLogged) {
                    this._idleTickErrorLogged = true;
                    console.warn('[VRM Manager] 空闲低频 tick 渲染异常（同类后续不再打印）:', e);
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
        if (restartRaf && this.renderer && this.scene && this.camera &&
            !this._animationFrameId && this._rafDriver) {
            this._lastRenderTime = 0;
            this._nextRenderTime = 0;
            this._animationFrameId = requestAnimationFrame(this._rafDriver);
        }
    }

    // 帧率设置变更：有活动按新配置重新升帧；空闲定时器模式按新地板换周期。
    // rAF 驱动路径每帧自己读 window.targetFrameRate，不需要在这里处理。
    applyTargetFrameRate() {
        if (!this.renderer || !this.scene || !this.camera) return;
        if (this._hasRenderActivity()) {
            this._boostInteractiveFPS();
        } else if (this._idleTickMode) {
            this._enterIdleTickMode(this._resolveIdleFps());
        }
    }

    // 有交互/活动时升回 rAF 满帧，并安排在 durationMs 后无活动则回落空闲低频
    _boostInteractiveFPS(durationMs = VRM_INTERACTIVE_FPS_HOLD_MS) {
        const timerFps = this._resolveActiveTimerTickFps();
        if (timerFps != null) {
            // Pet 窗口且配置帧率低于刷新率：活动态也留在定时器驱动，只把周期换成配置帧率。
            // 回到 rAF 只会让 Blink 按刷新率跑主帧再跳掉一半，省不出 CPU/GPU。
            this._enterIdleTickMode(timerFps);
        } else {
            this._exitIdleTickMode();
        }
        if (this._idleFpsRestoreTimer) clearTimeout(this._idleFpsRestoreTimer);
        // 豁免页：只升频、不安排衰减——衰减会在 governor 缺席时把渲染拖回空闲模式
        if (window.__NEKO_DISABLE_AVATAR_IDLE_THROTTLE__ === true) {
            this._idleFpsRestoreTimer = null;
            return;
        }
        this._idleFpsRestoreTimer = setTimeout(() => {
            this._idleFpsRestoreTimer = null;
            if (this.renderer && this.scene && this.camera && !this._hasRenderActivity()) {
                this._enterIdleTickMode();
            }
        }, Math.max(100, Number(durationMs) || VRM_INTERACTIVE_FPS_HOLD_MS));
    }

    // 需要满帧的活动：口型同步、非 idle VRMA 播放、拖拽、光标跟踪余温、
    // 相机变化余温（OrbitControls 阻尼收敛）、模型加载/入场阶段
    _hasRenderActivity() {
        try { if (this.animation && this.animation.lipSyncActive) return true; } catch (_) {}
        try { if (window.appState && window.appState.lipSyncActive) return true; } catch (_) {}
        try {
            if (this.animation && this.animation.vrmaIsPlaying && !this.animation.isIdleAnimation) return true;
        } catch (_) {}
        try { if (this.interaction && this.interaction.isDragging) return true; } catch (_) {}
        try {
            const cf = this._cursorFollow;
            // 时间戳 0 是合法值（performance.now() 起点），不能用真值判断；但 CursorFollow
            // 构造/重置时把 _lastPointerMoveAt 置 0 当「尚无指针输入」，要靠 _hasPointerInput 区分
            if (cf && typeof cf.isEnabled === 'function' && cf.isEnabled() && cf._hasPointerInput === true &&
                Number.isFinite(cf._lastPointerMoveAt) && (performance.now() - cf._lastPointerMoveAt) < VRM_INTERACTIVE_FPS_HOLD_MS) return true;
        } catch (_) {}
        try {
            if (Number.isFinite(this._lastLookAtPointerMoveAt) &&
                (performance.now() - this._lastLookAtPointerMoveAt) < VRM_INTERACTIVE_FPS_HOLD_MS) return true;
        } catch (_) {}
        try {
            if (Number.isFinite(this._lastCameraChangeAt) &&
                (performance.now() - this._lastCameraChangeAt) < VRM_INTERACTIVE_FPS_HOLD_MS) return true;
        } catch (_) {}
        try {
            if (Number.isFinite(this._lastInteractionBoostTs) &&
                (performance.now() - this._lastInteractionBoostTs) < VRM_INTERACTIVE_FPS_HOLD_MS) return true;
        } catch (_) {}
        try {
            if (this._loadState === 'preparing' || this._loadState === 'settling') return true;
        } catch (_) {}
        return false;
    }

    _startIdleFpsGovernor() {
        this._stopIdleFpsGovernor();
        // 页面级豁免：小游戏 demo 等用自有 rAF 循环驱动骨骼姿态的页面，
        // 活动探测看不见它们的动画，节流会造成可见卡顿——由页面声明退出
        if (window.__NEKO_DISABLE_AVATAR_IDLE_THROTTLE__ === true) return;
        // 启动/加载视为活动：先满帧，随后自动衰减
        this._boostInteractiveFPS();
        this._idleFpsGovernorTimer = setInterval(() => {
            // 自终止：dispose / 场景销毁后自动停摆并释放闭包引用
            if (!this.renderer || !this.scene || !this.camera) { this._stopIdleFpsGovernor(); return; }
            // 外部暂停（rAF 链与空闲模式都不存在）：不接管，resume 会重启 governor
            if (!this._idleTickMode && !this._animationFrameId) return;
            if (this._hasRenderActivity()) {
                this._boostInteractiveFPS();
            } else if (!this._idleTickMode && !this._idleFpsRestoreTimer) {
                // 自愈：外部直接重启 rAF 链后无人再带我们回空闲模式
                this._enterIdleTickMode();
            }
        }, VRM_IDLE_FPS_GOVERNOR_INTERVAL_MS);
    }

    _stopIdleFpsGovernor() {
        if (this._idleFpsGovernorTimer) {
            clearInterval(this._idleFpsGovernorTimer);
            this._idleFpsGovernorTimer = null;
        }
        if (this._idleFpsRestoreTimer) {
            clearTimeout(this._idleFpsRestoreTimer);
            this._idleFpsRestoreTimer = null;
        }
        this._exitIdleTickMode(false);
    }

    toggleSpringBone(enable) {
        this.enablePhysics = enable;
    }

    /**
     * 设置鼠标追踪性能档位
     * @param {'none'|'low'|'medium'|'high'} level
     */
    setCursorFollowPerformance(level = 'high') {
        const normalized = typeof level === 'string' ? level.toLowerCase() : 'high';
        const finalLevel = (normalized === 'none' || normalized === 'low' || normalized === 'medium' || normalized === 'high')
            ? normalized
            : 'high';

        if (this._cursorFollow && typeof this._cursorFollow.setPerformanceLevel === 'function') {
            this._cursorFollow.setPerformanceLevel(finalLevel);
        }
        // 保留全局状态，便于初始化前设置
        window.cursorFollowPerformanceLevel = finalLevel;
        return finalLevel;
    }

    /**
     * 获取当前鼠标追踪性能档位
     * @returns {'none'|'low'|'medium'|'high'}
     */
    getCursorFollowPerformance() {
        if (this._cursorFollow && typeof this._cursorFollow.getPerformanceLevel === 'function') {
            return this._cursorFollow.getPerformanceLevel();
        }
        return window.cursorFollowPerformanceLevel || 'high';
    }

    /**
     * 重置模型位置/旋转/缩放到默认值，并把相机拉回屏幕中心（供外部调用，如 N.E.K.O.-PC）
     *
     * 问题背景：之前只重置 scene.position/rotation/scale，但用户如果右键拖拽过（orbit），
     * 相机已不再看向世界原点，此时仅复位模型位置会使模型继续处于屏幕外。
     * 因此这里同时复位相机到依据模型包围盒计算的默认机位，使模型稳定回到屏幕中央。
     */
    resetModelPosition() {
        const model = this.currentModel;
        const scene = model?.vrm?.scene ?? model?.scene;
        if (!scene) return;

        const THREE = window.THREE;
        const vrm = model.vrm || model;

        // 1) 先复位模型的位置与旋转
        scene.position.set(0, 0, 0);
        scene.rotation.set(0, 0, 0);

        // 2) 应用朝向检测（保持和初次加载一致）
        if (window.VRMOrientationDetector && vrm) {
            const versionDefaultRotation = this.core?.vrmVersion === '0.0'
                ? { x: 0, y: Math.PI, z: 0 }
                : null;
            const detectedRotation = window.VRMOrientationDetector.detectAndFixOrientation(
                vrm,
                null,
                { defaultRotation: versionDefaultRotation }
            );
            window.VRMOrientationDetector.applyRotation(vrm, detectedRotation);
        }

        // 3) 根据模型 bounding box 与屏幕大小计算目标缩放，避免硬编码 1 造成视觉过大
        let targetScale = 1;
        if (THREE) {
            try {
                scene.updateMatrixWorld(true);
                const preBox = new THREE.Box3().setFromObject(scene);
                const preSize = preBox.getSize(new THREE.Vector3());
                const unscaledHeight = preSize.y / (scene.scale.y || 1);

                const screenHeight = window.innerHeight;
                const screenWidth = window.innerWidth;
                const isMobile = typeof window.isMobileWidth === 'function' ? window.isMobileWidth() : (screenWidth <= 768);

                if (isMobile) {
                    targetScale = Math.max(0.4, Math.min(0.8, screenHeight / 1800));
                } else if (unscaledHeight > 0 && Number.isFinite(unscaledHeight) && this.camera && this.camera.fov) {
                    // 使用固定参考距离（而非 camera.position.z，因相机可能已被 orbit 偏移）
                    const targetScreenHeight = screenHeight * 0.40;
                    const fov = this.camera.fov * (Math.PI / 180);
                    // 5 = vrm-core.js 首次加载计算缩放时所用的默认相机距离
                    //（见 vrm-core.js 里 `camera.position?.z || 5` 的 fallback）
                    // 这里沿用同一参考值，保证复位前后缩放口径一致、避免视觉跳变
                    const referenceDistance = 5;
                    const worldHeightAtDistance = 2 * Math.tan(fov / 2) * referenceDistance;
                    const scaleRatio = (targetScreenHeight / screenHeight) * (worldHeightAtDistance / unscaledHeight);
                    targetScale = Math.max(0.5, Math.min(1.2, scaleRatio));
                } else {
                    targetScale = Math.max(0.5, Math.min(1.0, screenHeight / 1200));
                }
            } catch (err) {
                console.warn('[VRM Manager] 重置缩放计算失败，使用 1:', err);
                targetScale = 1;
            }
        }
        this.setModelScaleScalar(targetScale);

        // 4) 根据缩放后的 bounding box 重新放置相机，使模型居中显示
        if (THREE && this.camera) {
            try {
                scene.updateMatrixWorld(true);
                const box = new THREE.Box3().setFromObject(scene);
                const center = box.getCenter(new THREE.Vector3());
                const size = box.getSize(new THREE.Vector3());

                const screenHeight = window.innerHeight;
                const screenWidth = window.innerWidth;
                const isMobileDevice = typeof window.isMobileWidth === 'function' ? window.isMobileWidth() : (screenWidth <= 768);

                const scaledModelHeight = size.y > 0 ? size.y : 1.5;
                const targetScreenHeight = screenHeight * 0.40;
                const fov = this.camera.fov * (Math.PI / 180);
                const distance = (scaledModelHeight / 2) / Math.tan(fov / 2) / targetScreenHeight * screenHeight;

                const cameraY = center.y + (isMobileDevice ? scaledModelHeight * 0.2 : scaledModelHeight * 0.1);
                const cameraZ = Math.abs(distance);
                this.camera.position.set(0, cameraY, cameraZ);

                this._cameraTarget = new THREE.Vector3(0, center.y, 0);
                this.camera.lookAt(this._cameraTarget);
                this.camera.updateProjectionMatrix();

                // 同步 OrbitControls 的 target，否则下一帧 controls.update() 会用旧 target 覆盖 lookAt
                if (this.controls) {
                    this.controls.target.copy(this._cameraTarget);
                    this.controls.update();
                }
            } catch (err) {
                console.warn('[VRM Manager] 重置相机失败，回退到初始机位:', err);
                this.camera.position.set(0, 1.1, 1.5);
                this._cameraTarget = new THREE.Vector3(0, 0.9, 0);
                this.camera.lookAt(this._cameraTarget);
                if (this.controls) {
                    this.controls.target.copy(this._cameraTarget);
                    this.controls.update();
                }
            }
        }

        const modelUrl = model.url || '';
        if (modelUrl && this.core && typeof this.core.saveUserPreferences === 'function') {
            // 构造重置后的相机位置，覆盖后端保存的旧值，避免下次加载时恢复到 orbit 后的坏相机
            let resetCameraPosition = null;
            if (this.camera) {
                const target = this._cameraTarget || new THREE.Vector3(0, 0, 0);
                resetCameraPosition = {
                    x: this.camera.position.x,
                    y: this.camera.position.y,
                    z: this.camera.position.z,
                    qx: this.camera.quaternion.x,
                    qy: this.camera.quaternion.y,
                    qz: this.camera.quaternion.z,
                    qw: this.camera.quaternion.w,
                    targetX: target.x,
                    targetY: target.y,
                    targetZ: target.z
                };
            }
            this.core.saveUserPreferences(
                modelUrl,
                { x: 0, y: 0, z: 0 },
                { x: targetScale, y: targetScale, z: targetScale },
                { x: scene.rotation.x, y: scene.rotation.y, z: scene.rotation.z },
                null,  // display
                null,  // viewport
                resetCameraPosition
            ).catch(err => console.warn('[VRM Manager] 保存重置偏好失败:', err));
        }

        console.log('[VRM Manager] 模型位置已重置，相机已复位，目标缩放:', targetScale.toFixed(3));
    }

    /**
     * 暂停渲染循环（用于节省资源，例如进入模型管理界面时）
     */
    pauseRendering() {
        // 空闲低频模式下 _animationFrameId 为 null（interval 驱动），必须显式
        // 退出该模式（不复跑），否则暂停对 interval 无效、渲染继续
        const wasIdleTicking = this._idleTickMode === true;
        this._exitIdleTickMode(false);
        if (this._idleFpsRestoreTimer) {
            clearTimeout(this._idleFpsRestoreTimer);
            this._idleFpsRestoreTimer = null;
        }
        if (this._animationFrameId) {
            cancelAnimationFrame(this._animationFrameId);
            this._animationFrameId = null;
            console.log('[VRM Manager] 渲染循环已暂停');
        } else if (wasIdleTicking) {
            console.log('[VRM Manager] 渲染循环已暂停（自空闲低频模式）');
        }
    }

    /**
     * 恢复渲染循环（从暂停状态恢复）
     */
    resumeRendering() {
        if (!this._animationFrameId) {
            this.startAnimateLoop();
            console.log('[VRM Manager] 渲染循环已恢复');
        }
    }

    async loadModel(modelUrl, options = {}) {
        this._pendingModelLoadCount += 1;
        this._isLoadingModel = true;
        try {
            return await this._loadModelImplementation(modelUrl, options);
        } finally {
            this._pendingModelLoadCount = Math.max(0, this._pendingModelLoadCount - 1);
            this._isLoadingModel = this._pendingModelLoadCount > 0;
        }
    }

    async _loadModelImplementation(modelUrl, options = {}) {
        // 并发正确性由 vrm-core.loadModel 的 token 守卫兜底，实现「后到者胜」：每次 loadModel
        // 在入口同步 bump _activeLoadToken 取代前一轮；被取代的旧加载在 core 内触碰共享
        // scene/currentModel 之前的守卫处 bail，不会交错改写共享状态或留下幽灵模型。
        //
        // 因此不再排串行队列：队列里每个 predecessor 在其 successor 入队时其实已被 token
        // 取代，让新加载 await 它只是「空等一个已被自己取代的旧加载」——而 GLTFLoader.load
        // 无超时/中止，慢或挂死的旧加载会无限期阻塞正常的 VRM→VRM 切换（Codex P2）。
        // token-only 方案与 mmd-manager.loadModel 对偶；代价是快速连切时被取代的加载会各自
        // 完成一次下载后才 bail（带宽浪费），换取活性，符合 MMD 既有取舍。
        const loadToken = ++this._activeLoadToken;
        this._loadState = 'preparing';
        this._isModelReadyForInteraction = false;
        // .catch 而非 await：本函数按竞态契约不得 await（见 static contract 测试），
        // 失败路径必须复位加载态——_hasRenderActivity 把 preparing/settling 视为
        // 活动，卡在 preparing 会让空闲 governor 永久 boost、本会话节流失效，
        // 且加载前画布已置 opacity 0，旧场景会在不可见画布上满帧空转。
        return this._loadModelInternal(modelUrl, options, loadToken).catch((e) => {
            if (this._isLoadTokenActive(loadToken)) this._loadState = 'idle';
            throw e;
        });
    }

    /**
     * loadModel 的实际加载体。loadToken 由 loadModel 在入口分配后传入；同一时刻可能有
     * 多个实例并发在跑（快速连切），但只有持 current token 的那个能穿过 core 的守卫改写
     * 共享状态，被取代的实例在 core 返回 null 后一路空转到早退，不 clobber 胜出者的状态。
     */
    async _loadModelInternal(modelUrl, options, loadToken) {
        this._loadState = 'preparing';
        this._isModelReadyForInteraction = false;
        // 新一轮加载：取消上一轮可能还在跑的 T-pose 回退重试，避免旧重试打断新模型动画
        if (this._idleRecoveryTimerId) {
            clearTimeout(this._idleRecoveryTimerId);
            this._idleRecoveryTimerId = null;
        }
        this._initModules();
        if (!this.core) this.core = new window.VRMCore(this);

        // 清理之前的阴影资源（如果存在旧模型）
        this._disposeShadowResources();

        // 确保场景已初始化
        if (!this.scene || !this.camera || !this.renderer) {
            const canvasId = options.canvasId || 'vrm-canvas';
            const containerId = options.containerId || 'vrm-container';

            const canvas = document.getElementById(canvasId);
            const container = document.getElementById(containerId);

            if (canvas && container) {
                const threeReady = await this.ensureThreeReady(canvasId, containerId);
                if (!threeReady) {
                    // 仅当自己仍是当前加载时才复位状态，避免被取代的实例 clobber 胜出者
                    if (this._isLoadTokenActive(loadToken)) this._loadState = 'idle';
                    return null;
                }
            } else {
                const errorMsg = window.t
                    ? window.t('vrm.error.sceneNotInitialized')
                    : `无法加载模型：场景未初始化。找不到 canvas(#${canvasId}) 或 container(#${containerId})。`;
                throw new Error(errorMsg);
            }
        }


        // 先无过渡地立即隐藏画布，避免旧过渡导致加载期闪帧
        if (this.renderer && this.renderer.domElement) {
            this.renderer.domElement.style.transition = 'none';
            this.renderer.domElement.style.opacity = '0';
        }

        // 加载模型
        const result = await this.core.loadModel(modelUrl, options, loadToken);
        if (!this._isLoadTokenActive(loadToken)) {
            // 已被更新的加载取代：core 已在守卫处 bail 并释放本次资源，这里直接返回，
            // 不写 _loadState——否则会 clobber 正在并发跑的胜出者的状态（无队列后新增约束）。
            return result;
        }

        // 模型切换后重置头部跟踪状态（滤波器/权重/累计角度）
        if (this._cursorFollow) {
            this._cursorFollow.reset();
        }

        // 动态计算阴影位置和大小
        if (options.addShadow !== false && result && result.vrm && result.vrm.scene) {
            this._calculateAndAddShadow(result);
            result.vrm.scene.visible = false;
        }

        // 加载完保持 3D 对象不可见（防 T-Pose）
        if (result && result.vrm && result.vrm.scene) {
            result.vrm.scene.visible = false;
        }

        if (!this._animationFrameId) this.startAnimateLoop();

        // 获取默认循环动画路径：优先从 options 传入，其次从配置读取，最后使用默认值
        const DEFAULT_LOOP_ANIMATION = normalizeBundledVrmAnimationPath(
            options.idleAnimation ||
                window.lanlan_config?.vrmIdleAnimation ||
                '/static/vrm/animation/wait03.vrma.gz'
        );

        // 确保 animation 模块已初始化
        if (!this.animation) {
            this._initModules();
        }

        // 辅助函数：显示模型并淡入画布
        const showAndFadeIn = () => {
            if (!this._isLoadTokenActive(loadToken)) return;
            if (this._loadState !== 'ready') return;
            if (this.currentModel?.vrm?.scene) {
                // 启用物理
                this.enablePhysics = true;

                // 更新世界矩阵，确保骨骼位置正确
                this.currentModel.vrm.scene.updateMatrixWorld(true);

                const springBoneManager = this.currentModel.vrm.springBoneManager;
                if (springBoneManager) {
                    // 记录信息（调试用）
                    const sceneRotY = this.currentModel.vrm.scene.rotation.y;
                    console.log('[VRM] Scene rotation Y:', sceneRotY);

                    // 减小碰撞体半径
                    // 原因：UniVRM 导出 bug (#673) 导致碰撞体普遍过大
                    // 50% 是经验值，修复所有测试模型
                    // 参考：https://github.com/vrm-c/UniVRM/issues/673
                    const COLLIDER_REDUCTION = 0.5;  // 可调整，经验默认值
                    const collidersSet = springBoneManager.colliders;
                    const colliders = collidersSet ? Array.from(collidersSet) : [];
                    colliders.forEach(collider => {
                        if (collider.shape?.radius !== undefined) {
                            // 保存真正的原始半径（只在第一次）
                            if (collider._rawOriginalRadius === undefined) {
                                collider._rawOriginalRadius = collider.shape.radius;
                            }
                            // 应用基础缩减
                            const reducedRadius = collider._rawOriginalRadius * COLLIDER_REDUCTION;
                            collider.shape.radius = reducedRadius;
                            // 保存缩减后的半径作为"工作半径"供场景缩放使用
                            collider._originalRadius = reducedRadius;
                        }
                    });
                    console.log(`[VRM] Reduced ${colliders.length} colliders by ${(1 - COLLIDER_REDUCTION) * 100}%`);
                }

                this.currentModel.vrm.scene.visible = true;
                requestAnimationFrame(() => {
                    if (!this._isLoadTokenActive(loadToken)) return;
                    if (this.renderer && this.renderer.domElement) {
                        this.renderer.domElement.style.transition = 'opacity 1.0s ease-in-out';
                        this.renderer.domElement.style.opacity = '1';
                    }
                });
            }
        };

        // 加载待机动画（作为 Promise，与场景稳定性并行等待）
        // 返回 true 表示动画成功播放；false 表示模块未就绪或播放失败（需要后续回退重试）。
        let animationReady = Promise.resolve(true);
        if (options.autoPlay !== false) {
            animationReady = (async () => {
                if (!this.currentModel || !this.currentModel.vrm) return false;

                // 放宽模块加载等待窗口（原 10×100ms = 1s 在慢速网络下会静默跳过，
                // 导致模型以 T-pose 亮相），改为 30×100ms = 3s
                let retries = 30;
                while (retries > 0) {
                    if (!this._isLoadTokenActive(loadToken)) return false;
                    if (!this.currentModel || !this.currentModel.vrm) return false;

                    if (!this.animation) this._initModules();
                    if (this.animation) break;

                    if (typeof window.VRMAnimation !== 'undefined') {
                        this._initModules();
                        break;
                    }

                    await new Promise(resolve => {
                        this._retryTimerId = setTimeout(() => {
                            this._retryTimerId = null;
                            resolve();
                        }, 100);
                    });
                    retries--;
                }

                if (this._retryTimerId) {
                    clearTimeout(this._retryTimerId);
                    this._retryTimerId = null;
                }

                if (!this.animation) {
                    console.warn('[VRM Manager] VRMAnimation 模块未加载，稍后将后台重试以避免卡 T-pose');
                    return false;
                }
                const currentLoadToken = this._activeLoadToken;
                if (loadToken !== currentLoadToken) return false;

                try {
                    if (!this._isLoadTokenActive(loadToken)) return false;
                    await this.playVRMAAnimation(DEFAULT_LOOP_ANIMATION, {
                        loop: true,
                        immediate: true,
                        isIdle: true
                    });
                    return true;
                } catch (err) {
                    console.warn('[VRM Manager] 自动播放失败，稍后将后台重试以避免卡 T-pose:', err);
                    return false;
                }
            })();
        }

        if (this.expression) {
            this.expression.setMood('neutral');
        }
        if (this.setupFloatingButtons && !window._cardExportPage) {
            this.setupFloatingButtons();
        }

        // 应用保存的局部跟踪设置
        if (this._cursorFollow) {
            this._cursorFollow.setLocalTrackingEnabled(window.humanoidLocalTrackingEnabled === true);
        }

        // 同时等待场景稳定和待机动画加载完成，确保模型不以 T-pose 显示
        this._loadState = 'settling';
        const stabilityPromise = (result && result.vrm && result.vrm.scene && this._isLoadTokenActive(loadToken))
            ? this._waitForSceneStability(result.vrm.scene, loadToken)
            : Promise.resolve(false);
        const [stabilityResult, animationSucceeded] = await Promise.all([stabilityPromise, animationReady]);

        if (this._isLoadTokenActive(loadToken) && stabilityResult === true) {
            this._loadState = 'ready';
            this._isModelReadyForInteraction = true;

            // 首次加载围栏：与拖拽结束共用默认可见像素阈值，避免重启时把允许的半出屏位置拉回屏内。
            if (this.interaction && this.currentModel?.vrm?.scene) {
                try {
                    const currentPos = this.currentModel.vrm.scene.position.clone();
                    const correctedPos = this.interaction.clampModelPosition(currentPos);
                    if (!currentPos.equals(correctedPos)) {
                        this.currentModel.vrm.scene.position.copy(correctedPos);
                        console.log('[VRM Manager] 首次加载围栏已校正模型位置');
                    }
                } catch (e) {
                    console.warn('[VRM Manager] 首次加载围栏检查失败:', e);
                }
            }

            window.dispatchEvent(new CustomEvent('vrm-model-loaded', {
                detail: {
                    modelUrl,
                    model: this.currentModel
                }
            }));

            showAndFadeIn();

            // T-pose 防卡死回退：autoPlay 未关闭、但首轮动画没播起来时（模块加载超时 / 播放抛错），
            // 在后台周期性重试，直到成功播放或 loadToken 失效。这样即使 VRMAnimation 模块延迟加载
            // 或网络抖动，模型最终也会脱离 T-pose 进入待机动画。
            if (options.autoPlay !== false && !animationSucceeded) {
                this._scheduleIdleAnimationRetry(loadToken, DEFAULT_LOOP_ANIMATION);
            }
        } else if (this._isLoadTokenActive(loadToken)) {
            this._loadState = 'idle';
            this._isModelReadyForInteraction = false;
        }
        return result;
    }

    /**
     * 在后台周期性重试待机动画，避免 VRMAnimation 模块加载慢/播放失败时模型卡在 T-pose。
     * - 只要 loadToken 仍然有效（没有被新的 loadModel 替换），就会一直尝试。
     * - 一旦 animation 模块就绪并成功触发 playVRMAAnimation，立即停止。
     * - 用户手动播放了非 idle 动画时（isIdleAnimation=false 且有 currentAction）也会停止，不打扰手动操作。
     */
    _scheduleIdleAnimationRetry(loadToken, idleAnimationUrl) {
        if (this._idleRecoveryTimerId) {
            clearTimeout(this._idleRecoveryTimerId);
            this._idleRecoveryTimerId = null;
        }

        const MAX_ATTEMPTS = 20;      // 最多尝试 20 次
        const INTERVAL_MS = 500;      // 每次间隔 500ms -> 总计最长约 10s
        let attempts = 0;

        const attempt = async () => {
            this._idleRecoveryTimerId = null;
            // token 失效或已被 dispose：停止
            if (this._isDisposed || !this._isLoadTokenActive(loadToken)) return;
            if (!this.currentModel || !this.currentModel.vrm) return;

            // 仅当动画"真正在运行"时才放弃重试：
            // 单纯 currentAction 非 null 不能等同于正在播放——stopVRMAAnimation 用 500ms
            // fadeOut 后才清空 currentAction；单次性 clip 播完后 currentAction 也会悬挂
            // 指向已停止的 action。此时若提前 return，模型仍会卡在 T-pose。
            const anim = this.animation;
            const actionRunning = !!(
                anim
                && anim.vrmaIsPlaying
                && anim.currentAction
                && typeof anim.currentAction.isRunning === 'function'
                && anim.currentAction.isRunning()
            );
            if (actionRunning) {
                // 无论是用户手动动画还是已在播的 idle，模型都已脱离 T-pose
                return;
            }

            if (!this.animation) this._initModules();

            if (this.animation) {
                try {
                    await this.playVRMAAnimation(idleAnimationUrl, {
                        loop: true,
                        immediate: true,
                        isIdle: true
                    });
                    console.log('[VRM Manager] T-pose 回退重试成功，已切入待机动画');
                    return; // 成功，结束
                } catch (err) {
                    console.warn('[VRM Manager] T-pose 回退重试播放失败:', err);
                }
            }

            attempts += 1;
            if (attempts >= MAX_ATTEMPTS) {
                console.warn('[VRM Manager] T-pose 回退重试已达上限，放弃');
                return;
            }
            this._idleRecoveryTimerId = setTimeout(attempt, INTERVAL_MS);
        };

        this._idleRecoveryTimerId = setTimeout(attempt, INTERVAL_MS);
    }

    async playVRMAAnimation(url, opts) {
        if (!this.animation) this._initModules();
        if (this.animation) {
            return this.animation.playVRMAAnimation(normalizeBundledVrmAnimationPath(url), opts);
        }
        return false;
    }

    seekVRMAAnimation(timeSeconds, options) {
        if (this.animation && typeof this.animation.seekTo === 'function') {
            return this.animation.seekTo(timeSeconds, options);
        }
        return false;
    }

    stopVRMAAnimation() {
        if (this.animation) this.animation.stopVRMAAnimation();
    }
    onWindowResize() {
        if (!this.camera || !this.renderer) return;

        let width, height;

        // 多窗口模式下（Pet 窗口可能被缩小），渲染缓冲区使用实际可见尺寸，
        // 配合 setPixelRatio（vrm-core.js）保证 Retina 清晰度，
        // 避免分配全屏分辨率的 GPU buffer 浪费显存。
        if (window.__NEKO_MULTI_WINDOW__) {
            const screenWidth = window.screen.width || 1920;
            const screenHeight = window.screen.height || 1080;
            const visibleWidth = (this.container && this.container.clientWidth > 0)
                ? this.container.clientWidth : (window.innerWidth || screenWidth);
            const visibleHeight = (this.container && this.container.clientHeight > 0)
                ? this.container.clientHeight : (window.innerHeight || screenHeight);

            this.camera.aspect = visibleWidth / visibleHeight;
            this.camera.updateProjectionMatrix();
            this.renderer.setSize(visibleWidth, visibleHeight, false);
            this.renderer.domElement.style.width = visibleWidth + 'px';
            this.renderer.domElement.style.height = visibleHeight + 'px';
            return;
        }

        if (this.container && this.container.clientWidth > 0 && this.container.clientHeight > 0) {
            width = this.container.clientWidth;
            height = this.container.clientHeight;
        } else {
            width = window.innerWidth;
            height = window.innerHeight;
        }

        this.camera.aspect = width / height;
        this.camera.updateProjectionMatrix();
        this.renderer.setSize(width, height);
    }
    getCurrentModel() {
        return this.currentModel;
    }
    setModelPosition(x, y, z) {
        if (this.currentModel?.vrm?.scene) this.currentModel.vrm.scene.position.set(x, y, z);
    }

    /**
     * 设置模型缩放，同时同步缩放 SpringBone 碰撞体半径
     * @param {number} x - X 轴缩放
     * @param {number} y - Y 轴缩放  
     * @param {number} z - Z 轴缩放
     */
    setModelScale(x, y, z) {
        if (!this.currentModel?.vrm?.scene) return;

        // 使用统一缩放值（取 x 作为参考）
        const scaleFactor = x;

        // 1. 缩放场景
        this.currentModel.vrm.scene.scale.set(x, y, z);

        // 2. 同步缩放碰撞体半径
        const springBoneManager = this.currentModel.vrm.springBoneManager;
        if (springBoneManager) {
            const colliders = springBoneManager.colliders
                ? Array.from(springBoneManager.colliders)
                : [];

            colliders.forEach(collider => {
                if (collider.shape?.radius !== undefined) {
                    // 保存原始半径（只在第一次缩放时保存）
                    if (collider._originalRadius === undefined) {
                        collider._originalRadius = collider.shape.radius;
                    }
                    // 根据缩放因子调整半径
                    collider.shape.radius = collider._originalRadius * scaleFactor;
                }
            });

            console.log(`[VRM] Scaled ${colliders.length} colliders with factor ${scaleFactor.toFixed(3)}`);
        }
    }

    /**
     * 设置模型统一缩放（便捷方法）
     * @param {number} scale - 统一缩放值
     */
    setModelScaleScalar(scale) {
        this.setModelScale(scale, scale, scale);
    }

    /**
     * 完整清理 VRM 资源（用于模型切换）
     * 包括：取消动画循环、清理模型资源、清理场景/渲染器、重置初始化状态
     */
    async dispose() {
        console.log('[VRM Manager] 开始完整清理 VRM 资源...');
        // Release the process-wide VMC publisher lease before tearing down
        // the model. This also removes vrm-vmc-sender.js' strong reference to
        // the VRM so switching to Live2D/MMD cannot retain a disposed model.
        const vmcVrm = this.currentModel?.vrm;
        if (
            vmcVrm
            && window.__NEKO_VMC_ACTIVE__ === true
            && window.vrmVmcSender
            && typeof window.vrmVmcSender.releaseVrm === 'function'
        ) {
            try {
                window.vrmVmcSender.releaseVrm(vmcVrm);
            } catch (error) {
                console.warn('[VRM Manager] VMC source release failed:', error);
            }
        }
        this._isDisposed = true;

        // 使在途的 loadModel 失效：bump token 后，被取代的旧 load 的 core.loadModel
        // 恢复时会在守卫处 bail、不覆写新会话；无串行队列，无需清理队列指针。
        ++this._activeLoadToken;
        this._loadState = 'idle';
        this._isModelReadyForInteraction = false;

        // 1. 取消动画循环（最关键）；先停 governor（一并清衰减计时器、
        //    退出空闲低频模式且不复跑）
        this._stopIdleFpsGovernor();
        if (this._animationFrameId) {
            cancelAnimationFrame(this._animationFrameId);
            this._animationFrameId = null;
        }

        // 2. 清理 UI 更新循环（!= null：兼容 _updateFloatingButtonsPositionNow
        //    留下的 0 哨兵；一并清空闲低频模式的 pending 再入定时器）
        if (this._uiUpdateLoopId != null) {
            cancelAnimationFrame(this._uiUpdateLoopId);
            this._uiUpdateLoopId = null;
        }
        if (this._uiLoopIdleTimeout) {
            clearTimeout(this._uiLoopIdleTimeout);
            this._uiLoopIdleTimeout = null;
        }

        // 3. 清理重试定时器（loadModel 中的 tryPlayAnimation 重试）
        if (this._retryTimerId) {
            clearTimeout(this._retryTimerId);
            this._retryTimerId = null;
        }
        // 3b. 清理 T-pose 回退重试定时器
        if (this._idleRecoveryTimerId) {
            clearTimeout(this._idleRecoveryTimerId);
            this._idleRecoveryTimerId = null;
        }

        // 4. 清理阴影资源
        this._disposeShadowResources();

        // 5. 清理模型资源（调用 core.disposeVRM）
        if (this.core && typeof this.core.disposeVRM === 'function') {
            await this.core.disposeVRM();
            // 若 dispose 期间发生了重新初始化，则终止本次清理，避免清掉新实例
            if (!this._isDisposed) return;
        }

        // 6. 清理动画模块（先停止动画，再清理资源）
        if (this.animation) {
            if (typeof this.animation.stopVRMAAnimation === 'function') {
                this.animation.stopVRMAAnimation();
            }
            if (typeof this.animation.dispose === 'function') {
                this.animation.dispose();
            }
        }

        // 7. 清理交互模块的定时器
        if (this.interaction) {
            if (this.interaction._hideButtonsTimer) {
                clearTimeout(this.interaction._hideButtonsTimer);
                this.interaction._hideButtonsTimer = null;
            }
            if (this.interaction._savePositionDebounceTimer) {
                clearTimeout(this.interaction._savePositionDebounceTimer);
                this.interaction._savePositionDebounceTimer = null;
            }
            // 清理交互模块的初始化定时器
            if (this.interaction._initTimerId) {
                clearTimeout(this.interaction._initTimerId);
                this.interaction._initTimerId = null;
            }
            // 清理交互模块的拖拽和缩放事件监听器
            if (typeof this.interaction.cleanupDragAndZoom === 'function') {
                this.interaction.cleanupDragAndZoom();
            }
        }

        // 7.5 清理 CursorFollow 控制器
        if (this._cursorFollow) {
            this._cursorFollow.destroy();
            this._cursorFollow = null;
        }

        // 8. 清理场景中的所有对象（包括灯光）
        if (this._mouseMoveHandler) {
            document.removeEventListener('mousemove', this._mouseMoveHandler);
            this._mouseMoveHandler = null;
        }
        if (this._lookAtTarget && this._lookAtTarget.parent) {
            this._lookAtTarget.parent.remove(this._lookAtTarget);
        }
        this._lookAtTarget = null;
        this._mouseRaycaster = null;
        this._mouseNDC = null;
        this._lookAtHeadWorldPos = null;
        this._headScreenAnchorProjection = null;
        this._lookAtRayClosestPoint = null;
        this._lookAtDirection = null;
        this._lookAtBaseForward = null;
        this._lookAtBaseRight = null;
        this._lookAtBaseUp = null;
        this._lookAtWorldUp = null;
        this._lookAtDesiredPoint = null;

        if (this.scene) {
            // 遍历并清理所有子对象
            while (this.scene.children.length > 0) {
                const child = this.scene.children[0];
                this.scene.remove(child);

                // 如果是可清理的对象，调用 dispose
                if (child.geometry) child.geometry.dispose();
                if (child.material) {
                    // 辅助函数：清理单个材质的纹理资源
                    const disposeMaterialTextures = (material) => {
                        const textureKeys = ['map', 'normalMap', 'aoMap', 'emissiveMap', 'metalnessMap', 'roughnessMap'];
                        textureKeys.forEach(key => {
                            if (material[key] && typeof material[key].dispose === 'function') {
                                material[key].dispose();
                            }
                        });
                    };

                    if (Array.isArray(child.material)) {
                        child.material.forEach(m => {
                            disposeMaterialTextures(m);
                            m.dispose();
                        });
                    } else {
                        disposeMaterialTextures(child.material);
                        child.material.dispose();
                    }
                }
            }
        }

        // 9. 清理渲染器（但不销毁 canvas，因为后续可能还要用）
        if (this.renderer) {
            // 清理所有纹理
            this.renderer.dispose();
            // 重置 canvas 样式
            if (this.renderer.domElement) {
                this.renderer.domElement.style.display = 'none';
                this.renderer.domElement.style.opacity = '0';
            }
        }

        // 10. 清理轨道控制器
        if (this.controls) {
            if (typeof this.controls.dispose === 'function') {
                this.controls.dispose();
            }
            this.controls = null;
        }

        // 11. 清理 UI 元素（浮动按钮、锁图标等）
        if (typeof this.cleanupUI === 'function') {
            this.cleanupUI();
        }

        // 11.5. 关闭所有设置窗口并清理定时器（防止定时器泄漏）
        if (typeof this.closeAllSettingsWindows === 'function') {
            this.closeAllSettingsWindows();
        }

        // 清理 vrm-init.js 中的 visibilitychange 监听器
        if (typeof window.cleanupVRMInit === 'function') {
            window.cleanupVRMInit();
        }

        // 清理 window 事件监听器（按模块分别清理，避免模块间冲突）
        // 清理 Core 模块的 handlers（VRMCore.init() 中注册的 resize 监听器等）
        if (this._coreWindowHandlers && this._coreWindowHandlers.length > 0) {
            this._coreWindowHandlers.forEach(({ event, handler }) => {
                window.removeEventListener(event, handler);
            });
            this._coreWindowHandlers = [];
        }

        // 清理 UI 模块的 handlers（VRMUIButtons 中注册的 resize, live2d-goodbye-click 等）
        // 注意：UI 模块有自己的 cleanupUI() 方法，但这里也清理以确保完整性
        if (this._uiWindowHandlers && this._uiWindowHandlers.length > 0) {
            this._uiWindowHandlers.forEach(({ event, handler }) => {
                window.removeEventListener(event, handler);
            });
            this._uiWindowHandlers = [];
        }

        // 清理 Init 模块的 handlers（vrm-init.js 中的 visibilitychange 等）
        // 注意：Init 模块有自己的 cleanupVRMInit() 方法，但这里也清理以确保完整性
        if (this._initWindowHandlers && this._initWindowHandlers.length > 0) {
            this._initWindowHandlers.forEach(({ event, handler }) => {
                window.removeEventListener(event, handler);
            });
            this._initWindowHandlers = [];
        }

        // 12. 重置引用和状态
        this.currentModel = null;
        this.animationMixer = null;
        this.scene = null;
        this.camera = null;
        this.renderer = null;
        this.ambientLight = null;
        this.directionalLight = null;
        this.spotLight = null;
        this.canvas = null;
        this.container = null;

        // 13. 重置初始化标志（确保下次切回 VRM 时会重新初始化）
        this._isInitialized = false;

        console.log('[VRM Manager] VRM 资源清理完成');
    }

    /**
     * 设置鼠标跟踪是否启用
     * @param {boolean} enabled - 是否启用鼠标跟踪
     */
    setMouseTrackingEnabled(enabled) {
        this._mouseTrackingEnabled = enabled;
        window.mouseTrackingEnabled = enabled;
        const effectiveEnabled = enabled && window.nekoYuiGuideFaceForwardLock !== true;

        if (this._cursorFollow) {
            this._cursorFollow.setEnabled(effectiveEnabled);
            return;
        }

        if (!effectiveEnabled) {
            if (this._mouseMoveHandler) {
                document.removeEventListener('mousemove', this._mouseMoveHandler);
                this._mouseMoveHandler = null;
            }
            if (this.currentModel?.vrm?.lookAt) {
                this.currentModel.vrm.lookAt.target = null;
            }
            return;
        }

        this._initMouseLookAtTracking();
    }

    /**
     * 获取鼠标跟踪是否启用
     * @returns {boolean}
     */
    isMouseTrackingEnabled() {
        if (window.nekoYuiGuideFaceForwardLock === true) {
            return false;
        }
        return this._mouseTrackingEnabled !== false;
    }

    _projectWorldPositionToScreen(worldPosition) {
        if (!worldPosition || !this.camera || !this.renderer || typeof window.THREE === 'undefined') {
            return null;
        }

        const canvas = this.renderer.domElement;
        if (!canvas) return null;

        const canvasRect = canvas.getBoundingClientRect();
        if (!canvasRect.width || !canvasRect.height) return null;

        if (!this._headScreenAnchorProjection) {
            this._headScreenAnchorProjection = new window.THREE.Vector3();
        }

        this.camera.updateMatrixWorld(true);
        this._headScreenAnchorProjection.copy(worldPosition).project(this.camera);

        if (!Number.isFinite(this._headScreenAnchorProjection.x) ||
            !Number.isFinite(this._headScreenAnchorProjection.y)) {
            return null;
        }

        return {
            x: canvasRect.left + (this._headScreenAnchorProjection.x * 0.5 + 0.5) * canvasRect.width,
            y: canvasRect.top + (-this._headScreenAnchorProjection.y * 0.5 + 0.5) * canvasRect.height
        };
    }

    getHeadScreenAnchor() {
        if (!this.currentModel || !this.camera || !this.renderer || typeof window.THREE === 'undefined') {
            return null;
        }
        if (!this._ensureMouseLookAtResources()) {
            return null;
        }

        return this._projectWorldPositionToScreen(this._getLookAtHeadWorldPosition());
    }

    getHeadDetectionGeometryInfo() {
        const bounds = this.getModelScreenBounds();
        if (!bounds) {
            return null;
        }

        const headAnchor = this.getHeadScreenAnchor();
        return {
            type: 'vrm',
            bounds,
            rawHeadAnchor: headAnchor || null,
            headAnchor: headAnchor || null,
            headRect: null,
            headMode: 'head',
            headSource: 'bone',
            bodyRect: null,
            bodySource: null,
            reliableHeadRect: false,
            preciseDisplayInfoRect: false,
            coarseHitAreaHeadRect: false
        };
    }

    /**
     * 获取 VRM 模型在屏幕上的边界（用于局部跟踪）
     * @returns {Object|null} 边界对象 { left, right, top, bottom, width, height, centerX, centerY } 或 null
     */
    getModelScreenBounds() {
        if (!this.currentModel || !this.camera || !this.renderer) {
            return null;
        }

        const canvasRect = this.renderer.domElement.getBoundingClientRect();
        const canvasWidth = canvasRect.width;
        const canvasHeight = canvasRect.height;
        if (!(canvasWidth > 0) || !(canvasHeight > 0)) {
            return null;
        }

        const scene = this.currentModel.vrm?.scene ?? this.currentModel.scene;
        if (!scene) return null;

        const box = new window.THREE.Box3().setFromObject(scene);
        const corners = [
            new window.THREE.Vector3(box.min.x, box.min.y, box.min.z),
            new window.THREE.Vector3(box.min.x, box.min.y, box.max.z),
            new window.THREE.Vector3(box.min.x, box.max.y, box.min.z),
            new window.THREE.Vector3(box.min.x, box.max.y, box.max.z),
            new window.THREE.Vector3(box.max.x, box.min.y, box.min.z),
            new window.THREE.Vector3(box.max.x, box.min.y, box.max.z),
            new window.THREE.Vector3(box.max.x, box.max.y, box.min.z),
            new window.THREE.Vector3(box.max.x, box.max.y, box.max.z)
        ];

        let screenLeft = Infinity, screenRight = -Infinity;
        let screenTop = Infinity, screenBottom = -Infinity;

        for (const corner of corners) {
            corner.project(this.camera);
            const sx = canvasRect.left + (corner.x * 0.5 + 0.5) * canvasWidth;
            const sy = canvasRect.top + (-corner.y * 0.5 + 0.5) * canvasHeight;
            screenLeft = Math.min(screenLeft, sx);
            screenRight = Math.max(screenRight, sx);
            screenTop = Math.min(screenTop, sy);
            screenBottom = Math.max(screenBottom, sy);
        }

        if (!Number.isFinite(screenLeft) || !Number.isFinite(screenRight) ||
            !Number.isFinite(screenTop) || !Number.isFinite(screenBottom)) {
            return null;
        }

        const width = screenRight - screenLeft;
        const height = screenBottom - screenTop;
        if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 2 || height <= 2) {
            return null;
        }

        return {
            left: screenLeft,
            right: screenRight,
            top: screenTop,
            bottom: screenBottom,
            width: width,
            height: height,
            centerX: (screenLeft + screenRight) / 2,
            centerY: (screenTop + screenBottom) / 2
        };
    }
}

window.VRMManager = VRMManager;

// 监听帧率变更事件（与 live2d-core.js 同名事件对齐）
window.addEventListener('neko-frame-rate-changed', () => {
    const manager = window.vrmManager;
    if (manager && typeof manager.applyTargetFrameRate === 'function') {
        try { manager.applyTargetFrameRate(); } catch (_) {}
    }
});
