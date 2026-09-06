/**
 * VRM 交互模块
 * 负责拖拽、缩放、鼠标跟踪等交互功能
 */

// 确保 THREE 可用（只从全局对象读取，避免 TDZ ReferenceError）
// 使用 var 避免重复声明错误，或检查是否已存在
var THREE = (typeof window !== 'undefined' && window.THREE) || (typeof globalThis !== 'undefined' && globalThis.THREE) || null;
if (!THREE) {
    console.error('[VRM Interaction] THREE.js 未加载，交互功能将不可用');
}

const VRM_MAX_SAFE_FOV = 44;

function calculateFramingRatio(bounds, width, height, padding) {
    const viewWidth = Number(width);
    const viewHeight = Number(height);
    const safePadding = Math.max(0, Number(padding) || 0);
    if (!bounds || !Number.isFinite(viewWidth) || !Number.isFinite(viewHeight)
        || viewWidth <= 0 || viewHeight <= 0) return Infinity;
    const values = [bounds.minX, bounds.maxX, bounds.minY, bounds.maxY].map(Number);
    if (!values.every(Number.isFinite)) return Infinity;
    const centerX = viewWidth / 2;
    const centerY = viewHeight / 2;
    const usableHalfWidth = Math.max(1, centerX - safePadding);
    const usableHalfHeight = Math.max(1, centerY - safePadding);
    const ratioX = Math.max(Math.abs(values[0] - centerX), Math.abs(values[1] - centerX)) / usableHalfWidth;
    const ratioY = Math.max(Math.abs(values[2] - centerY), Math.abs(values[3] - centerY)) / usableHalfHeight;
    return Math.max(ratioX, ratioY);
}

function calculateExpandedFov(currentFov, bounds, width, height, padding, maximumFov) {
    const fov = Number(currentFov);
    const viewWidth = Number(width);
    const viewHeight = Number(height);
    const safePadding = Math.max(0, Number(padding) || 0);
    const maxFov = Math.max(fov, Number(maximumFov) || VRM_MAX_SAFE_FOV);
    if (!Number.isFinite(fov) || !Number.isFinite(viewWidth) || !Number.isFinite(viewHeight)
        || viewWidth <= 0 || viewHeight <= 0 || !bounds) return fov;
    const values = [bounds.minX, bounds.maxX, bounds.minY, bounds.maxY].map(Number);
    if (!values.every(Number.isFinite)) return fov;

    const requiredRatio = calculateFramingRatio(bounds, viewWidth, viewHeight, safePadding);
    if (!Number.isFinite(requiredRatio) || requiredRatio <= 1.01) return fov;

    const radians = fov * Math.PI / 180;
    const expanded = 2 * Math.atan(Math.tan(radians / 2) * requiredRatio) * 180 / Math.PI;
    return Math.min(maxFov, Math.max(fov, expanded));
}

if (typeof window !== 'undefined') {
    window.NekoVRMSafeFraming = Object.freeze({ calculateExpandedFov, calculateFramingRatio });
}

class VRMInteraction {
    constructor(manager) {
        this.manager = manager;

        // 拖拽和缩放相关
        this.isDragging = false;
        this.dragMode = null;
        this.previousMousePosition = { x: 0, y: 0 };
        this.isLocked = false;
        this._isInitializingDragAndZoom = false;
        this._initTimerId = null;
        this._initRetryCount = 0;
        this._maxInitRetries = 50; // 最多重试50次（约5秒）

        // 拖拽相关事件处理器引用（用于清理）
        this.mouseDownHandler = null;
        this.mouseUpHandler = null;
        this.mouseLeaveHandler = null;
        this.auxClickHandler = null;
        this.mouseEnterHandler = null;
        this.dragHandler = null;
        this.wheelHandler = null;
        this.mouseHoverHandler = null;  // 鼠标悬停时动态更新光标

        // 射线检测（用于判断鼠标是否在模型上）
        this._raycaster = THREE ? new THREE.Raycaster() : null;
        this._mouseNDC = THREE ? new THREE.Vector2() : null;

        // 鼠标跟踪相关
        this.mouseTrackingEnabled = false;
        this.mouseMoveHandler = null;

        // 开启"始终面朝相机" 
        this.enableFaceCamera = true;

        // 浮动按钮鼠标跟踪缓存（用于性能优化）
        this._cachedBox = null;
        this._cachedCorners = null;
        this._cachedScreenBounds = null; // { minX, maxX, minY, maxY }
        this._floatingButtonsPendingFrame = null; // RAF ID，用于取消
        this._lastModelUpdateTime = 0;
        this._safeFramingBaselineFov = null;
        this._safeFramingLastExpandedAt = 0;

        // 出界回弹配置（与聊天框风格一致）
        this._snapConfig = {
            duration: 260,
            easingType: 'easeOutBack'
        };
        this._snapCancelFrame = null;
        this._isSnappingModel = false;
        this._snapResolve = null;
        this._lastPanDragPointerScreen = null;
        this._panDragModelCenterOffset = null;
        this._dragHintPanStartPointer = null;
        this._dragHintPanLastPointer = null;
        this._dragHintApproachShown = false;
    }


    /**
     * 使用 avatar-ui-drag.js 中的共享工具函数（按钮 pointer-events 管理）
     */
    _disableButtonPointerEvents() {
        if (window.DragHelpers) {
            window.DragHelpers.disableButtonPointerEvents();
        }
    }

    _restoreButtonPointerEvents() {
        if (window.DragHelpers) {
            window.DragHelpers.restoreButtonPointerEvents();
        }
    }

    _getProjectedModelCenterInWindow() {
        const scene = this.manager.currentModel?.vrm?.scene;
        const camera = this.manager.camera;
        const renderer = this.manager.renderer;
        if (!scene || !camera || !renderer || !THREE) return null;

        try {
            scene.updateMatrixWorld(true);
            const box = new THREE.Box3().setFromObject(scene);
            if (!box || !Number.isFinite(box.min.x) || !Number.isFinite(box.max.x)) return null;

            const canvasRect = renderer.domElement.getBoundingClientRect();
            const screenWidth = canvasRect.width;
            const screenHeight = canvasRect.height;
            if (!(screenWidth > 0) || !(screenHeight > 0)) return null;

            const corners = [
                new THREE.Vector3(box.min.x, box.min.y, box.min.z),
                new THREE.Vector3(box.max.x, box.min.y, box.min.z),
                new THREE.Vector3(box.min.x, box.max.y, box.min.z),
                new THREE.Vector3(box.max.x, box.max.y, box.min.z),
                new THREE.Vector3(box.min.x, box.min.y, box.max.z),
                new THREE.Vector3(box.max.x, box.min.y, box.max.z),
                new THREE.Vector3(box.min.x, box.max.y, box.max.z),
                new THREE.Vector3(box.max.x, box.max.y, box.max.z),
            ];
            let minX = Infinity, maxX = -Infinity;
            let minY = Infinity, maxY = -Infinity;
            corners.forEach(corner => {
                const projected = corner.clone().project(camera);
                const x = (projected.x * 0.5 + 0.5) * screenWidth + canvasRect.left;
                const y = (-projected.y * 0.5 + 0.5) * screenHeight + canvasRect.top;
                minX = Math.min(minX, x);
                maxX = Math.max(maxX, x);
                minY = Math.min(minY, y);
                maxY = Math.max(maxY, y);
            });
            const x = (minX + maxX) / 2;
            const y = (minY + maxY) / 2;
            if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
            return { x, y };
        } catch (_) {
            return null;
        }
    }

    _rememberPanDragPointer(event, { captureOffset = false } = {}) {
        const screenX = Number(event?.screenX);
        const screenY = Number(event?.screenY);
        if (Number.isFinite(screenX) && Number.isFinite(screenY)) {
            this._lastPanDragPointerScreen = { x: screenX, y: screenY };
        }

        if (!captureOffset) return;
        const clientX = Number(event?.clientX);
        const clientY = Number(event?.clientY);
        const center = this._getProjectedModelCenterInWindow();
        if (center && Number.isFinite(clientX) && Number.isFinite(clientY)) {
            this._panDragModelCenterOffset = {
                x: center.x - clientX,
                y: center.y - clientY
            };
        } else {
            this._panDragModelCenterOffset = { x: 0, y: 0 };
        }
    }

    _captureDragHintPointer(event) {
        const screenX = Number(event?.screenX);
        const screenY = Number(event?.screenY);
        if (!Number.isFinite(screenX) || !Number.isFinite(screenY)) return null;
        return { screenX, screenY };
    }

    _rememberDragHintPanPointer(event, { start = false } = {}) {
        const pointer = this._captureDragHintPointer(event);
        if (!pointer) return;
        if (start) {
            pointer.startedAt = Date.now();
            this._dragHintPanStartPointer = pointer;
        }
        this._dragHintPanLastPointer = pointer;
    }

    async _recordDragHintPointerEdgeRelease(source) {
        const helper = window.NekoAvatarMultiScreenDragHint;
        if (!helper || typeof helper.recordPointerEdgeRelease !== 'function') return false;
        const start = this._dragHintPanStartPointer;
        const release = this._dragHintPanLastPointer;
        if (!start || !release) return false;
        return await helper.recordPointerEdgeRelease(source, {
            startedAt: start.startedAt,
            startScreenX: start.screenX,
            startScreenY: start.screenY,
            screenX: release.screenX,
            screenY: release.screenY
        });
    }

    async _recordDragHintPointerEdgeApproach(source) {
        const helper = window.NekoAvatarMultiScreenDragHint;
        if (!helper || typeof helper.recordPointerEdgeApproach !== 'function') return false;
        if (this._dragHintApproachShown) return false;
        const start = this._dragHintPanStartPointer;
        const pointer = this._dragHintPanLastPointer;
        if (!start || !pointer) return false;
        const shown = await helper.recordPointerEdgeApproach(source, {
            startedAt: start.startedAt,
            startScreenX: start.screenX,
            startScreenY: start.screenY,
            screenX: pointer.screenX,
            screenY: pointer.screenY
        });
        if (shown) this._dragHintApproachShown = true;
        return shown;
    }

    _moveModelCenterToWindowPoint(targetX, targetY) {
        const scene = this.manager.currentModel?.scene;
        const camera = this.manager.camera;
        const renderer = this.manager.renderer;
        if (!scene || !camera || !renderer || !THREE) return false;
        if (!Number.isFinite(targetX) || !Number.isFinite(targetY)) return false;

        const center = this._getProjectedModelCenterInWindow();
        if (!center) return false;

        const canvasRect = renderer.domElement.getBoundingClientRect();
        const screenWidth = canvasRect.width;
        const screenHeight = canvasRect.height;
        if (!(screenWidth > 0) || !(screenHeight > 0)) return false;

        const deltaPxX = targetX - center.x;
        const deltaPxY = targetY - center.y;
        if (deltaPxX === 0 && deltaPxY === 0) return true;

        const modelCenterWorld = scene.position.clone();
        const cameraDistance = camera.position.distanceTo(modelCenterWorld);
        const fov = camera.fov * (Math.PI / 180);
        const worldHeight = 2 * Math.tan(fov / 2) * cameraDistance;
        const worldWidth = worldHeight * (screenWidth / screenHeight);
        const pixelToWorldX = worldWidth / screenWidth;
        const pixelToWorldY = worldHeight / screenHeight;

        const right = new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion);
        const up = new THREE.Vector3(0, 1, 0).applyQuaternion(camera.quaternion);

        scene.position.add(right.clone().multiplyScalar(deltaPxX * pixelToWorldX));
        scene.position.add(up.clone().multiplyScalar(-deltaPxY * pixelToWorldY));
        return true;
    }

    /**
     * 射线检测：判断屏幕坐标 (clientX, clientY) 是否命中 VRM 模型
     * @returns {boolean} 是否命中
     */
    _hitTestModel(clientX, clientY) {
        if (!this._raycaster || !this.manager.camera || !this.manager.currentModel?.scene) {
            return false;
        }
        const canvas = this.manager.renderer?.domElement;
        if (!canvas) return false;
        const rect = canvas.getBoundingClientRect();
        // 转换为 NDC 坐标 (-1 ~ 1)
        this._mouseNDC.x = ((clientX - rect.left) / rect.width) * 2 - 1;
        this._mouseNDC.y = -((clientY - rect.top) / rect.height) * 2 + 1;
        this._raycaster.setFromCamera(this._mouseNDC, this.manager.camera);
        const intersects = this._raycaster.intersectObject(this.manager.currentModel.scene, true);
        return intersects.length > 0;
    }

    /**
     * 【修改】初始化拖拽和缩放功能
     * 已移除所有导致报错的 LookAt/mouseNDC 代码
     */
    initDragAndZoom() {
        if (!this.manager.renderer) return;

        // 如果已经在等待初始化，直接返回（防止重复定时器）
        if (this._isInitializingDragAndZoom) {
            return;
        }

        // 确保 camera 已初始化
        if (!this.manager.camera) {
            // 设置标记位，防止重复触发
            this._isInitializingDragAndZoom = true;
            // 清除之前的定时器（如果存在）
            if (this._initTimerId !== null) {
                clearTimeout(this._initTimerId);
            }
            // 设置新的定时器
            this._initTimerId = setTimeout(() => {
                this._isInitializingDragAndZoom = false;
                this._initTimerId = null;
                this._initRetryCount++;
                if (this._initRetryCount >= this._maxInitRetries) {
                    console.warn('[VRM Interaction] 相机初始化超时，放弃拖拽和缩放功能');
                    return;
                }
                if (this.manager.camera) {
                    this.initDragAndZoom();
                }
            }, 100);
            return;
        }

        // camera 已就绪，清除标记位和定时器
        this._isInitializingDragAndZoom = false;
        if (this._initTimerId !== null) {
            clearTimeout(this._initTimerId);
            this._initTimerId = null;
        }

        const canvas = this.manager.renderer.domElement;
        if (!THREE) {
            console.error('[VRM Interaction] THREE.js 未加载，无法初始化拖拽和缩放');
            return;
        }

        const isYuiGuideDragLocked = () => {
            const body = document.body;
            return !!(body && (
                body.classList.contains('yui-guide-home-ui-suppressed')
                || body.classList.contains('yui-taking-over')
            ));
        };

        // 先清理旧的事件监听器
        this.cleanupDragAndZoom();

        // 1. 鼠标按下
        this.mouseDownHandler = (e) => {
            if (!this.manager._isModelReadyForInteraction) return;
            if (this.checkLocked()) return;
            if (isYuiGuideDragLocked()) return;

            // 如果正在回弹动画，优先取消，避免拖拽冲突
            if (this._snapCancelFrame) {
                this._snapCancelFrame();
                this._snapCancelFrame = null;
                if (this._snapResolve) {
                    this._snapResolve(false);
                    this._snapResolve = null;
                }
                this._isSnappingModel = false;
            }

            if (e.button === 0 || e.button === 1) { // 左键或中键
                // 只有点击到模型才开始拖拽（射线检测）
                if (!this._hitTestModel(e.clientX, e.clientY)) {
                    return; // 未命中模型，不拦截事件
                }
                this.isDragging = true;
                this.dragMode = 'pan';
                // 同步升频：不等 300ms governor 轮询，消除拖拽起步的 30fps 顿挫
                if (typeof this.manager._boostInteractiveFPS === 'function') this.manager._boostInteractiveFPS();
                this.previousMousePosition = { x: e.clientX, y: e.clientY };
                this._rememberPanDragPointer(e, { captureOffset: true });
                this._rememberDragHintPanPointer(e, { start: true });
                this._dragHintApproachShown = false;
                canvas.style.cursor = 'move';
                e.preventDefault();
                e.stopPropagation();

                // 开始拖动时，临时禁用按钮的 pointer-events
                this._disableButtonPointerEvents();
            } else if (e.button === 2) { // 右键 - 模型旋转
                this.isDragging = true;
                this.dragMode = 'orbit';
                if (typeof this.manager._boostInteractiveFPS === 'function') this.manager._boostInteractiveFPS();
                this.previousMousePosition = { x: e.clientX, y: e.clientY };
                canvas.style.cursor = 'crosshair';
                e.preventDefault();
                e.stopPropagation();

                // 缓存拖拽起始状态，用于幂等计算总旋转量（对齐 MMD 行为）
                // 之前实现是 "相机绕模型公转 + NDC lookAt 补偿"：
                //   1) 逐帧累加 phi/theta，phi 一旦被 clamp 到极区就不可逆；
                //   2) NDC 补偿是一阶近似，拖多了会微漂；
                //   3) 滚轮改走 scene.scale 后（见 commit e234db8），camera.y 不再
                //      随缩放联动，Box3 中心的 y 却会跟着 scale 漂移，导致 offset.y/radius
                //      偏离 π/2，上下转瞬间"不对称"。
                // 改为直接绕包围盒中心旋转 scene 本身、相机完全不动，彻底摆脱上述耦合。
                const scene = this.manager.currentModel?.scene;
                if (scene) {
                    const box = new THREE.Box3().setFromObject(scene);
                    this._orbitPivot = box.getCenter(new THREE.Vector3());
                    this._orbitStartQuat = scene.quaternion.clone();
                    this._orbitStartPos = scene.position.clone();
                    this._orbitStartMouse = { x: e.clientX, y: e.clientY };
                }

                this._disableButtonPointerEvents();
            }
        };

        // 2. 鼠标移动 (核心拖拽逻辑)
        this.dragHandler = (e) => {
            if (!this.manager._isModelReadyForInteraction) return;
            if (isYuiGuideDragLocked()) {
                if (this.isDragging) {
                    e.preventDefault();
                    e.stopPropagation();
                    this.isDragging = false;
                    this.dragMode = null;
                    canvas.style.cursor = 'default';
                    this._restoreButtonPointerEvents();
                }
                return;
            }
            if (this.checkLocked()) {
                if (this.isDragging) {
                    e.preventDefault();
                    e.stopPropagation();
                    this.isDragging = false;
                    this.dragMode = null;
                    canvas.style.cursor = 'default';
                    // 恢复按钮的 pointer-events
                    this._restoreButtonPointerEvents();
                }
                return;
            }

            if (!this.isDragging || !this.manager.currentModel) return;

            const deltaX = e.clientX - this.previousMousePosition.x;
            const deltaY = e.clientY - this.previousMousePosition.y;
            if (this.dragMode === 'pan') {
                this._rememberPanDragPointer(e);
                this._rememberDragHintPanPointer(e);
                void this._recordDragHintPointerEdgeApproach('vrm');
            }

            if (this.dragMode === 'pan' && this.manager.currentModel && this.manager.currentModel.scene) {
                // 动态计算平移速度：根据相机距离和FOV，使鼠标移动距离与屏幕上模型移动距离同步
                // 这样无论缩放级别如何，鼠标移动100像素，模型在屏幕上也移动100像素
                const camera = this.manager.camera;
                const renderer = this.manager.renderer;

                // 计算相机到模型中心的距离
                const modelCenter = this.manager.currentModel.scene.position.clone();
                const cameraDistance = camera.position.distanceTo(modelCenter);

                // 计算在当前距离下，屏幕视口对应的世界空间尺寸
                const fov = camera.fov * (Math.PI / 180); // 转换为弧度
                const screenHeight = renderer.domElement.clientHeight;
                const screenWidth = renderer.domElement.clientWidth;

                // 在相机距离处，视口的世界空间高度
                const worldHeight = 2 * Math.tan(fov / 2) * cameraDistance;
                // 根据宽高比计算世界空间宽度
                const worldWidth = worldHeight * (screenWidth / screenHeight);

                // 计算每像素对应的世界空间距离
                const pixelToWorldX = worldWidth / screenWidth;
                const pixelToWorldY = worldHeight / screenHeight;

                const right = new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion);
                const up = new THREE.Vector3(0, 1, 0).applyQuaternion(camera.quaternion);

                // 计算新位置：鼠标移动的像素 × 每像素对应的世界空间距离
                const newPosition = this.manager.currentModel.scene.position.clone();
                newPosition.add(right.multiplyScalar(deltaX * pixelToWorldX));
                newPosition.add(up.multiplyScalar(-deltaY * pixelToWorldY));

                // 使用边界限制
                const finalPosition = this.clampModelPosition(newPosition);

                // 应用位置（按钮和锁图标位置由 _startUIUpdateLoop 自动更新）
                this.manager.currentModel.scene.position.copy(finalPosition);
            } else if (this.dragMode === 'orbit' && this._orbitStartQuat && this._orbitPivot) {
                // 右键拖拽：模型绕包围盒中心旋转（Y轴左右 + X轴上下），相机保持不动
                // 对齐 MMD 的 orbit 实现（见 mmd-interaction.js:256-279）。
                const scene = this.manager.currentModel?.scene;
                if (!scene) return;

                const rotateSpeed = 0.005;
                const totalDx = e.clientX - this._orbitStartMouse.x;
                const totalDy = e.clientY - this._orbitStartMouse.y;

                // 世界轴 Y（左右）+ 世界轴 X（上下）
                const yQuat = new THREE.Quaternion().setFromAxisAngle(
                    new THREE.Vector3(0, 1, 0), totalDx * rotateSpeed);
                const xQuat = new THREE.Quaternion().setFromAxisAngle(
                    new THREE.Vector3(1, 0, 0), totalDy * rotateSpeed);
                const totalQuat = new THREE.Quaternion().multiplyQuaternions(yQuat, xQuat);

                // 绕 _orbitPivot 旋转：先把 scene.position 相对 pivot 的偏移旋转后放回，
                // 使包围盒中心保持不动，模型只在屏幕上"原地转身"。
                const offset = new THREE.Vector3().subVectors(this._orbitStartPos, this._orbitPivot);
                const rotatedOffset = offset.clone().applyQuaternion(totalQuat);
                scene.position.copy(this._orbitPivot).add(rotatedOffset);

                // 每帧从起始姿态重新乘出（幂等），避免增量累加的漂移
                scene.quaternion.copy(this._orbitStartQuat).premultiply(totalQuat);
            }

            this.previousMousePosition = { x: e.clientX, y: e.clientY };
        };

        // 3. 鼠标释放
        this.mouseUpHandler = async (e) => {
            if (!this.manager._isModelReadyForInteraction) return;
            if (this.isDragging) {
                e.preventDefault();
                e.stopPropagation();
                if (this.dragMode === 'pan') {
                    this._rememberPanDragPointer(e);
                    this._rememberDragHintPanPointer(e);
                }
                // 保留本次拖拽类型再清状态，跨屏切换只对 pan 生效
                // （orbit 绕包围盒中心原地转身，屏幕投影不位移，无需多屏切换）
                const wasPanDrag = this.dragMode === 'pan';
                this.isDragging = false;
                this.dragMode = null;
                canvas.style.cursor = 'default';

                // 拖拽结束后恢复按钮的 pointer-events
                this._restoreButtonPointerEvents();

                // 多屏幕支持：仅对平移拖拽检测是否移出当前屏幕并切换到新屏幕
                // 与 Live2D 行为对齐：若发生切屏，_checkAndSwitchDisplay 内部负责回弹和保存
                const displaySwitched = wasPanDrag
                    ? await this._checkAndSwitchDisplay()
                    : false;

                if (!displaySwitched) {
                    if (wasPanDrag) {
                        await this._recordDragHintPointerEdgeRelease('vrm');
                    }
                    // 拖拽结束后：若超出屏幕范围，执行回弹
                    await this._snapModelIntoScreen({ animate: true });

                    // 拖动结束后保存位置（包含回弹后的位置）
                    await this._savePositionAfterInteraction();
                }
            }
        };

        // 5. 鼠标进入
        this.mouseEnterHandler = () => {
            if (!this.isDragging) {
                canvas.style.cursor = 'default';
            }
        };

        // 5.5 鼠标悬停时动态更新光标（不拖拽时检测是否在模型附近）
        // 仅使用屏幕包围盒判断，避免高频射线检测导致掉帧
        let _lastHoverHitTestAt = 0;
        this.mouseHoverHandler = (e) => {
            if (this.isDragging || this.checkLocked()) return;
            const now = performance.now();
            if ((now - _lastHoverHitTestAt) < 80) return;
            _lastHoverHitTestAt = now;
            if (this.isDragging) return;
            const bounds = this._cachedScreenBounds;
            if (!bounds) {
                canvas.style.cursor = 'default';
                return;
            }
            // 椭圆近似（内切于包围盒），不外扩
            const cx = (bounds.minX + bounds.maxX) / 2;
            const cy = (bounds.minY + bounds.maxY) / 2;
            const rx = (bounds.maxX - bounds.minX) / 2 * 0.6;
            const ry = (bounds.maxY - bounds.minY) / 2 * 0.95;
            const nx = rx > 0 ? (e.clientX - cx) / rx : 0;
            const ny = ry > 0 ? (e.clientY - cy) / ry : 0;
            const isNearModel = (nx * nx + ny * ny) <= 1;
            canvas.style.cursor = isNearModel ? 'grab' : 'default';
        };

        // 6. 滚轮缩放
        // 对齐 MMD 行为：直接缩放 scene.scale，围绕模型自身原点进行，
        // 避免因 "相机靠近固定 _cameraTarget" 在角色被拖到边缘时把角色甩出屏幕。
        this.wheelHandler = (e) => {
            if (this.checkLocked() || !this.manager.currentModel) return;

            // 检查事件目标是否是 canvas 或其子元素，如果不是则不拦截事件（允许聊天区域正常滚动）
            const canvasEl = this.manager.renderer?.domElement;
            if (!canvasEl) return;

            const target = e.target;
            // 检查目标是否是 canvas 本身或其子元素
            const isCanvasOrDescendant = target === canvasEl || canvasEl.contains(target);

            // 只有当事件发生在 canvas 或其子元素上时，才拦截事件
            if (!isCanvasOrDescendant) {
                return; // 不拦截，允许事件继续传播到聊天区域
            }

            if (!this._hitTestModel(e.clientX, e.clientY)) {
                return;
            }

            // 真正命中模型的缩放才升频：同步 boost（不等 300ms governor 轮询）
            // 消除从空闲地板起步的首段 30fps 顿挫；时间戳供 governor 续期
            this.manager._lastInteractionBoostTs = performance.now();
            if (typeof this.manager._boostInteractiveFPS === 'function') {
                this.manager._boostInteractiveFPS();
            }

            e.preventDefault();
            e.stopPropagation();

            const scene = this.manager.currentModel.scene;
            if (!scene) return;

            const scaleFactor = e.deltaY > 0 ? 0.95 : 1.05;
            // 夹到一个合理范围，避免反复滚轮把模型缩到 0 或爆炸大
            const minScale = 0.1;
            const maxScale = 50.0;
            const currentScale = scene.scale.x || 1;
            const newScale = Math.max(minScale, Math.min(maxScale, currentScale * scaleFactor));

            // 通过 manager.setModelScaleScalar 统一缩放，同时同步 SpringBone 碰撞体半径。
            // 这里不走 scene.scale.setScalar 的静默回退：那样只会缩视觉 mesh、不同步 collider，
            // 反而把状态失配藏起来。API 不可用时显式告警并中止，以便尽早暴露问题。
            if (typeof this.manager.setModelScaleScalar !== 'function') {
                console.warn('[VRM Interaction] manager.setModelScaleScalar 不可用，跳过缩放以避免 SpringBone 状态失配');
                return;
            }
            this.manager.setModelScaleScalar(newScale);

            // 缩放结束后防抖保存位置
            this._debouncedSavePosition();
        };

        this.auxClickHandler = (e) => {
            if (e.button === 1) { e.preventDefault(); e.stopPropagation(); }
        };

        // 7. 禁止右键菜单（canvas 上）
        this.contextMenuHandler = (e) => {
            e.preventDefault();
        };

        // 绑定事件
        canvas.addEventListener('mousedown', this.mouseDownHandler);
        document.addEventListener('mousemove', this.dragHandler); // 绑定到 document 以支持拖出画布
        document.addEventListener('mouseup', this.mouseUpHandler);
        canvas.addEventListener('mouseenter', this.mouseEnterHandler);
        canvas.addEventListener('mousemove', this.mouseHoverHandler); // 动态光标（悬停检测）
        // 保存 wheel 监听器选项，确保添加和移除时使用相同的选项
        this._wheelListenerOptions = { passive: false, capture: true };
        canvas.addEventListener('wheel', this.wheelHandler, this._wheelListenerOptions);
        canvas.addEventListener('auxclick', this.auxClickHandler);
        canvas.addEventListener('contextmenu', this.contextMenuHandler);


    }
    /**
     * 【新增】让模型身体始终朝向相机
     * 消除透视带来的“侧身”感，让平移看起来像 2D 移动
     */
    _updateModelFacing(delta) {
        if (!this.enableFaceCamera) return;
        // 手动 orbit 期间不自动朝向相机，避免和用户拖拽对抗
        // （当前 vrm-core.js:887 加载完会把 enableFaceCamera=false，这里是防御性守卫）
        if (this.dragMode === 'orbit') return;
        if (!this.manager.currentModel || !this.manager.currentModel.scene || !this.manager.camera) return;

        const model = this.manager.currentModel.scene;
        const camera = this.manager.camera;

        // 1. 计算向量 (忽略 Y 轴)
        const dx = camera.position.x - model.position.x;
        const dz = camera.position.z - model.position.z;

        // 2. 计算目标角度
        // VRM 默认朝向 +Z，atan2(x, z) 对应 Y 轴旋转
        let targetAngle = Math.atan2(dx, dz);
        if (this.manager.__soccerFixedCameraNormalizeYaw) {
            targetAngle += Math.PI;
        }

        // 3. 平滑插值处理角度突变
        const currentAngle = model.rotation.y;
        let diff = targetAngle - currentAngle;

        while (diff > Math.PI) diff -= Math.PI * 2;
        while (diff < -Math.PI) diff += Math.PI * 2;

        // 4. 应用旋转 (速度可调)
        const rotateSpeed = 10.0;
        if (Math.abs(diff) > 0.001) {
            model.rotation.y += diff * rotateSpeed * delta;
        }
    }
    /**
     * 检查锁定状态（使用VRM管理器自己的锁定状态）
     * @returns {boolean} 是否锁定
     */
    checkLocked() {
        // 使用 VRM 管理器自己的锁定状态
        if (this.manager && typeof this.manager.isLocked !== 'undefined') {
            this.isLocked = this.manager.isLocked;
        }
        return this.isLocked;
    }

    /**
     * 每帧更新（由 VRMManager 驱动）
     */
    update(delta) {
        // 更新身体朝向（按钮位置由 _startUIUpdateLoop 处理）
        this._updateModelFacing(delta);
    }

    /**
     * 设置锁定状态
     */
    setLocked(locked) {
        this.isLocked = locked;
        if (this.manager) {
            this.manager.isLocked = locked;
        }

        if (!locked && typeof this._setLockedHoverFade === 'function') {
            this._setLockedHoverFade(false);
        }

        // 不再修改 pointerEvents，改用逻辑拦截
        // 这样锁定时虽然不能移动/缩放，但依然可以点中模型弹出菜单

        if (locked && this.isDragging) {
            this.isDragging = false;
            this.dragMode = null;
            if (this.manager.renderer) {
                this.manager.renderer.domElement.style.cursor = 'default';
            }
            // 恢复按钮的 pointer-events
            this._restoreButtonPointerEvents();
        }
    }

    /**
     * 确保模型不会完全消失 - 只在极端情况下重置位置
     * @param {THREE.Vector3} position - 目标位置
     * @returns {THREE.Vector3} - 调整后的位置
     */
    ensureModelVisibility(position) {
        if (!THREE) {
            console.error('[VRM Interaction] THREE.js 未加载，无法确保模型可见性');
            return position;
        }

        // 如果模型移动得太远（超出20个单位），重置到原点
        const maxAllowedDistance = 20;
        const distanceFromOrigin = position.length();

        if (distanceFromOrigin > maxAllowedDistance) {
            return new THREE.Vector3(0, 0, 0);
        }

        return position;
    }

    /**
     * 清理拖拽和缩放相关事件监听器
     * 注意：如果事件监听器在添加时使用了选项（如 { capture: true, passive: false }），
     * 移除时必须使用相同的选项，否则 removeEventListener 不会生效
     */
    cleanupDragAndZoom() {
        if (!this.manager.renderer) return;

        // 清理初始化定时器（如果存在）
        if (this._initTimerId !== null) {
            clearTimeout(this._initTimerId);
            this._initTimerId = null;
        }
        this._isInitializingDragAndZoom = false;

        const canvas = this.manager.renderer.domElement;

        // 移除所有事件监听器
        // 注意：这些事件在添加时没有使用选项，所以移除时也不需要选项
        if (this.mouseDownHandler) {
            canvas.removeEventListener('mousedown', this.mouseDownHandler);
            this.mouseDownHandler = null;
        }
        if (this.dragHandler) {
            document.removeEventListener('mousemove', this.dragHandler);
            this.dragHandler = null;
        }
        if (this.mouseUpHandler) {
            document.removeEventListener('mouseup', this.mouseUpHandler);
            this.mouseUpHandler = null;
        }

        if (this.auxClickHandler) {
            canvas.removeEventListener('auxclick', this.auxClickHandler);
            this.auxClickHandler = null;
        }
        if (this.mouseEnterHandler) {
            canvas.removeEventListener('mouseenter', this.mouseEnterHandler);
            this.mouseEnterHandler = null;
        }
        if (this.mouseHoverHandler) {
            canvas.removeEventListener('mousemove', this.mouseHoverHandler);
            this.mouseHoverHandler = null;
        }
        if (this.wheelHandler) {
            // 移除时必须使用与添加时相同的选项，否则 removeEventListener 不会生效
            canvas.removeEventListener('wheel', this.wheelHandler, this._wheelListenerOptions || { capture: true });
            this.wheelHandler = null;
            this._wheelListenerOptions = null;
        }
        if (this.contextMenuHandler) {
            canvas.removeEventListener('contextmenu', this.contextMenuHandler);
            this.contextMenuHandler = null;
        }

        // 【维护注意】cleanup 可能在拖动进行中被调用（如切换模型、dispose），
        //  必须检查并恢复按钮事件，否则 body 上的 neko-model-dragging class 残留。
        if (this.isDragging) {
            this.isDragging = false;
            this.dragMode = null;
            if (canvas) canvas.style.cursor = 'default';
            this._restoreButtonPointerEvents();
        }
    }

    /**
     * 【基于可见像素的边界限制】
     * 
     * 计算模型包围盒在屏幕上的可见区域，只在可见像素小于阈值时才进行校正。
     * 这样无论模型放多大，只要屏幕上还能看到足够的部分，就不会强制限制位置。
     **/
    clampModelPosition(position, { minVisiblePixels = 200 } = {}) {
        if (!this.manager.camera || !this.manager.renderer || !this.manager.currentModel?.vrm) {
            return position;
        }

        if (!THREE) {
            console.error('[VRM Interaction] THREE.js 未加载，无法限制模型位置');
            return position;
        }

        const camera = this.manager.camera;
        const renderer = this.manager.renderer;
        const vrm = this.manager.currentModel.vrm;

        const MIN_VISIBLE_PIXELS = minVisiblePixels;

        try {
            // 1. 临时将模型移动到目标位置，计算包围盒
            const originalPosition = vrm.scene.position.clone();
            vrm.scene.position.copy(position);
            vrm.scene.updateMatrixWorld(true);

            // 2. 计算模型在目标位置的包围盒
            const box = new THREE.Box3().setFromObject(vrm.scene);

            // 恢复原始位置
            vrm.scene.position.copy(originalPosition);
            vrm.scene.updateMatrixWorld(true);

            // 3. 获取包围盒的 8 个顶点并投影到屏幕空间
            const corners = [
                new THREE.Vector3(box.min.x, box.min.y, box.min.z),
                new THREE.Vector3(box.max.x, box.min.y, box.min.z),
                new THREE.Vector3(box.min.x, box.max.y, box.min.z),
                new THREE.Vector3(box.max.x, box.max.y, box.min.z),
                new THREE.Vector3(box.min.x, box.min.y, box.max.z),
                new THREE.Vector3(box.max.x, box.min.y, box.max.z),
                new THREE.Vector3(box.min.x, box.max.y, box.max.z),
                new THREE.Vector3(box.max.x, box.max.y, box.max.z),
            ];

            const canvasRect = renderer.domElement.getBoundingClientRect();
            const screenWidth = canvasRect.width;
            const screenHeight = canvasRect.height;

            // Never demand more visible pixels than the viewport can supply
            const effectiveMinX = Math.min(MIN_VISIBLE_PIXELS, screenWidth);
            const effectiveMinY = Math.min(MIN_VISIBLE_PIXELS, screenHeight);

            // 计算模型在屏幕上的边界框
            let modelMinX = Infinity, modelMaxX = -Infinity;
            let modelMinY = Infinity, modelMaxY = -Infinity;

            corners.forEach(corner => {
                const projected = corner.clone().project(camera);
                const screenX = (projected.x * 0.5 + 0.5) * screenWidth;
                const screenY = (-projected.y * 0.5 + 0.5) * screenHeight;
                modelMinX = Math.min(modelMinX, screenX);
                modelMaxX = Math.max(modelMaxX, screenX);
                modelMinY = Math.min(modelMinY, screenY);
                modelMaxY = Math.max(modelMaxY, screenY);
            });

            // 4. 计算模型在屏幕内的可见区域
            const visibleMinX = Math.max(0, modelMinX);
            const visibleMaxX = Math.min(screenWidth, modelMaxX);
            const visibleMinY = Math.max(0, modelMinY);
            const visibleMaxY = Math.min(screenHeight, modelMaxY);

            const visibleWidth = Math.max(0, visibleMaxX - visibleMinX);
            const visibleHeight = Math.max(0, visibleMaxY - visibleMinY);

            // 5. 按线性维度判定：水平和垂直方向各自需要至少 effective minimum 可见
            const modelOverflowsH = modelMinX < 0 || modelMaxX > screenWidth;
            const modelOverflowsV = modelMinY < 0 || modelMaxY > screenHeight;
            const needsClampH = modelOverflowsH && visibleWidth < effectiveMinX;
            const needsClampV = modelOverflowsV && visibleHeight < effectiveMinY;

            if (!needsClampH && !needsClampV) {
                return position;
            }

            // 6. 可见区域太小，需要将模型拉回
            const modelCenterX = (modelMinX + modelMaxX) / 2;
            const modelCenterY = (modelMinY + modelMaxY) / 2;
            const screenCenterX = screenWidth / 2;
            const screenCenterY = screenHeight / 2;

            // 仅校正需要拉回的维度
            let moveX = 0, moveY = 0;

            if (needsClampH) {
                if (modelMaxX < effectiveMinX) {
                    moveX = effectiveMinX - modelMaxX;
                } else if (modelMinX > screenWidth - effectiveMinX) {
                    moveX = (screenWidth - effectiveMinX) - modelMinX;
                }
            }

            if (needsClampV) {
                if (modelMaxY < effectiveMinY) {
                    moveY = effectiveMinY - modelMaxY;
                } else if (modelMinY > screenHeight - effectiveMinY) {
                    moveY = (screenHeight - effectiveMinY) - modelMinY;
                }
            }

            // 7. 将屏幕像素移动距离转换为世界空间距离
            const modelCenter = position.clone();
            const cameraDistance = camera.position.distanceTo(modelCenter);
            const fov = camera.fov * (Math.PI / 180);
            const worldHeight = 2 * Math.tan(fov / 2) * cameraDistance;
            const worldWidth = worldHeight * (screenWidth / screenHeight);

            const pixelToWorldX = worldWidth / screenWidth;
            const pixelToWorldY = worldHeight / screenHeight;

            const right = new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion);
            const up = new THREE.Vector3(0, 1, 0).applyQuaternion(camera.quaternion);

            const correctedPos = position.clone();
            correctedPos.add(right.multiplyScalar(moveX * pixelToWorldX));
            correctedPos.add(up.multiplyScalar(-moveY * pixelToWorldY)); // Y 轴反向

            return correctedPos;

        } catch (error) {
            console.warn('[VRM Interaction] 边界检测失败，跳过限制:', error);
            return position;
        }
    }

    /**
     * 获取回弹缓动函数
     */
    _getSnapEasingFunction() {
        const easingType = this._snapConfig?.easingType || 'easeOutBack';

        const easingMap = {
            easeOutBack: (t) => {
                const c1 = 1.70158;
                const c3 = c1 + 1;
                return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
            },
            easeOutCubic: (t) => (--t) * t * t + 1
        };

        return easingMap[easingType] || easingMap.easeOutCubic;
    }

    /**
     * 执行模型回弹动画
     */
    _animateModelToPosition(startPosition, targetPosition) {
        if (!this.manager.currentModel?.scene) {
            return Promise.resolve(false);
        }

        if (!Number.isFinite(targetPosition?.x) || !Number.isFinite(targetPosition?.y) || !Number.isFinite(targetPosition?.z)) {
            return Promise.resolve(false);
        }

        if (this._snapCancelFrame) {
            this._snapCancelFrame();
            this._snapCancelFrame = null;
            if (this._snapResolve) {
                this._snapResolve(false);
                this._snapResolve = null;
            }
            this._isSnappingModel = false;
        }

        const duration = this._snapConfig?.duration || 260;
        const easingFn = this._getSnapEasingFunction();
        const startTime = performance.now();
        const scene = this.manager.currentModel.scene;

        this._isSnappingModel = true;

        return new Promise((resolve) => {
            this._snapResolve = resolve;
            const animate = (currentTime) => {
                const elapsed = currentTime - startTime;
                const progress = Math.min(elapsed / duration, 1);
                const eased = easingFn(progress);

                const newX = startPosition.x + (targetPosition.x - startPosition.x) * eased;
                const newY = startPosition.y + (targetPosition.y - startPosition.y) * eased;
                const newZ = startPosition.z + (targetPosition.z - startPosition.z) * eased;

                scene.position.set(newX, newY, newZ);

                if (progress < 1) {
                    this._snapCancelFrame = this._scheduleSnapFrame(animate);
                } else {
                    scene.position.copy(targetPosition);
                    this._isSnappingModel = false;
                    this._snapCancelFrame = null;
                    this._snapResolve = null;
                    resolve(true);
                }
            };

            this._snapCancelFrame = this._scheduleSnapFrame(animate);
        });
    }

    /**
     * 出界回弹：保持原有边界检查逻辑不变，仅在需要时执行回弹动画
     */
    async _snapModelIntoScreen({ animate = true } = {}) {
        if (this._isSnappingModel) return false;
        if (!this.manager.currentModel?.scene || !this.manager.camera || !this.manager.renderer) return false;
        if (!THREE) return false;

        const scene = this.manager.currentModel.scene;
        const startPosition = scene.position.clone();

        // 使用原有的边界检查逻辑计算目标位置
        const targetPosition = this.clampModelPosition(startPosition.clone());

        if (!targetPosition || !targetPosition.isVector3) {
            return false;
        }

        const distance = startPosition.distanceTo(targetPosition);
        if (!Number.isFinite(distance) || distance < 0.0001) {
            return false;
        }

        if (!animate) {
            scene.position.copy(targetPosition);
            return true;
        }

        return await this._animateModelToPosition(startPosition, targetPosition);
    }

    /**
     * 多屏幕支持：检测模型是否移出当前屏幕并切换到新屏幕
     * 返回 true 表示发生了切屏（内部已保存位置），返回 false 表示未切屏
     */
    async _checkAndSwitchDisplay() {
        // 仅在 Electron 环境下执行
        if (!window.electronScreen || !window.electronScreen.moveWindowToDisplay) {
            return false;
        }
        if (!THREE) return false;

        const scene = this.manager.currentModel?.scene;
        const vrm = this.manager.currentModel?.vrm;
        const camera = this.manager.camera;
        const renderer = this.manager.renderer;
        if (!scene || !vrm || !camera || !renderer) return false;

        const recordDisplaySwitchMiss = () => {
            if (window.NekoAvatarMultiScreenDragHint &&
                typeof window.NekoAvatarMultiScreenDragHint.recordDisplaySwitchMiss === 'function') {
                window.NekoAvatarMultiScreenDragHint.recordDisplaySwitchMiss('vrm');
            }
        };
        let displaySwitchAttempted = false;

        try {
            // 1. 计算模型在当前窗口中的屏幕空间中心点（像素）
            vrm.scene.updateMatrixWorld(true);
            const box = new THREE.Box3().setFromObject(vrm.scene);
            if (!box || !Number.isFinite(box.min.x) || !Number.isFinite(box.max.x)) {
                return false;
            }

            const canvasRect = renderer.domElement.getBoundingClientRect();
            const screenWidth = canvasRect.width;
            const screenHeight = canvasRect.height;
            if (!(screenWidth > 0) || !(screenHeight > 0)) return false;

            const corners = [
                new THREE.Vector3(box.min.x, box.min.y, box.min.z),
                new THREE.Vector3(box.max.x, box.min.y, box.min.z),
                new THREE.Vector3(box.min.x, box.max.y, box.min.z),
                new THREE.Vector3(box.max.x, box.max.y, box.min.z),
                new THREE.Vector3(box.min.x, box.min.y, box.max.z),
                new THREE.Vector3(box.max.x, box.min.y, box.max.z),
                new THREE.Vector3(box.min.x, box.max.y, box.max.z),
                new THREE.Vector3(box.max.x, box.max.y, box.max.z),
            ];

            let modelMinX = Infinity, modelMaxX = -Infinity;
            let modelMinY = Infinity, modelMaxY = -Infinity;
            corners.forEach(corner => {
                const projected = corner.clone().project(camera);
                const sx = (projected.x * 0.5 + 0.5) * screenWidth;
                const sy = (-projected.y * 0.5 + 0.5) * screenHeight;
                modelMinX = Math.min(modelMinX, sx);
                modelMaxX = Math.max(modelMaxX, sx);
                modelMinY = Math.min(modelMinY, sy);
                modelMaxY = Math.max(modelMaxY, sy);
            });

            // 模型中心（相对于 canvas 左上角的像素），再偏移 canvas 在窗口中的位置
            const modelCenterX = (modelMinX + modelMaxX) / 2 + canvasRect.left;
            const modelCenterY = (modelMinY + modelMaxY) / 2 + canvasRect.top;
            const dragPointer = this._lastPanDragPointerScreen;
            const hasDragPointer = dragPointer
                && Number.isFinite(dragPointer.x)
                && Number.isFinite(dragPointer.y);
            const pointerOffset = this._panDragModelCenterOffset || { x: 0, y: 0 };
            this._lastPanDragPointerScreen = null;
            this._panDragModelCenterOffset = null;

            const windowWidth = window.innerWidth;
            const windowHeight = window.innerHeight;
            const modelCenterInsideWindow = modelCenterX >= 0 && modelCenterX < windowWidth &&
                modelCenterY >= 0 && modelCenterY < windowHeight;
            // 没有拖拽释放点时维持旧逻辑：模型中心仍在当前窗口内就不切屏。
            if (!hasDragPointer && modelCenterInsideWindow) {
                return false;
            }

            // 2. 多屏幕检查。第一版提示机制不以多屏数量为前置条件：
            // 只要用户把模型中心拖出当前窗口但未完成切屏，就记一次 miss。
            displaySwitchAttempted = true;
            const displays = await window.electronScreen.getAllDisplays();
            if (!displays || displays.length <= 1) {
                recordDisplaySwitchMiss();
                return false;
            }

            // 3. 计算模型中心在整个桌面（screen）上的绝对坐标
            const currentDisplay = await window.electronScreen.getCurrentDisplay();
            if (!currentDisplay) {
                console.warn('[VRM] 无法获取当前显示器信息');
                recordDisplaySwitchMiss();
                return false;
            }
            let currentScreenX = currentDisplay.screenX;
            let currentScreenY = currentDisplay.screenY;
            if (!Number.isFinite(currentScreenX) || !Number.isFinite(currentScreenY)) {
                if (currentDisplay.bounds &&
                    Number.isFinite(currentDisplay.bounds.x) &&
                    Number.isFinite(currentDisplay.bounds.y)) {
                    currentScreenX = currentDisplay.bounds.x;
                    currentScreenY = currentDisplay.bounds.y;
                } else {
                    recordDisplaySwitchMiss();
                    return false;
                }
            }

            const modelScreenX = currentScreenX + modelCenterX;
            const modelScreenY = currentScreenY + modelCenterY;
            const pointerWindowX = hasDragPointer ? dragPointer.x - currentScreenX : null;
            const pointerWindowY = hasDragPointer ? dragPointer.y - currentScreenY : null;
            const pointerOutsideCurrentWindow = hasDragPointer && !(
                pointerWindowX >= 0 && pointerWindowX < windowWidth &&
                pointerWindowY >= 0 && pointerWindowY < windowHeight
            );
            if (hasDragPointer && !pointerOutsideCurrentWindow && modelCenterInsideWindow) {
                return false;
            }
            const useDragPointerForSwitch = hasDragPointer && pointerOutsideCurrentWindow;
            const switchScreenX = useDragPointerForSwitch ? dragPointer.x : modelScreenX;
            const switchScreenY = useDragPointerForSwitch ? dragPointer.y : modelScreenY;

            // 4. 优先使用跨出当前窗口的释放点；否则回退到模型中心，保留旧的宽模型边缘拖拽行为。
            let targetDisplay = null;
            for (const display of displays) {
                const dx = Number.isFinite(display.screenX) ? display.screenX
                    : (display.bounds && display.bounds.x);
                const dy = Number.isFinite(display.screenY) ? display.screenY
                    : (display.bounds && display.bounds.y);
                const dw = Number.isFinite(display.width) ? display.width
                    : (display.bounds && display.bounds.width);
                const dh = Number.isFinite(display.height) ? display.height
                    : (display.bounds && display.bounds.height);
                if (!Number.isFinite(dx) || !Number.isFinite(dy) ||
                    !Number.isFinite(dw) || !Number.isFinite(dh)) continue;
                if (switchScreenX >= dx && switchScreenX < dx + dw &&
                    switchScreenY >= dy && switchScreenY < dy + dh) {
                    targetDisplay = { ...display, screenX: dx, screenY: dy, width: dw, height: dh };
                    break;
                }
            }
            if (!targetDisplay) {
                return false;
            }

            console.log('[VRM] 检测到模型移出当前屏幕，准备切换到屏幕:', targetDisplay.id);

            const result = await window.electronScreen.moveWindowToDisplay(switchScreenX, switchScreenY);

            if (!(result && result.success && !result.sameDisplay)) {
                recordDisplaySwitchMiss();
                return false;
            }
            console.log('[VRM] 屏幕切换成功:', result);

            // 5. 将模型在世界坐标中偏移，使拖拽抓取点落到释放鼠标的位置。
            const desiredModelCenterX = useDragPointerForSwitch
                ? switchScreenX - targetDisplay.screenX + (Number(pointerOffset.x) || 0)
                : modelScreenX - targetDisplay.screenX;
            const desiredModelCenterY = useDragPointerForSwitch
                ? switchScreenY - targetDisplay.screenY + (Number(pointerOffset.y) || 0)
                : modelScreenY - targetDisplay.screenY;

            // 6. 等待一帧让新窗口尺寸生效，再执行回弹与保存
            await new Promise(resolve => requestAnimationFrame(resolve));
            this._moveModelCenterToWindowPoint(desiredModelCenterX, desiredModelCenterY);

            if (useDragPointerForSwitch) {
                await this._savePositionAfterInteraction();
            } else {
                await this._snapModelIntoScreen({ animate: true });
                await this._savePositionAfterInteraction();
            }
            if (window.NekoAvatarMultiScreenDragHint &&
                typeof window.NekoAvatarMultiScreenDragHint.markDisplaySwitchSuccess === 'function') {
                window.NekoAvatarMultiScreenDragHint.markDisplaySwitchSuccess('vrm');
            }

            return true;
        } catch (error) {
            console.error('[VRM] 检测/切换屏幕时出错:', error);
            if (displaySwitchAttempted) recordDisplaySwitchMiss();
            return false;
        }
    }


    /**
     * 启用/禁用鼠标跟踪（用于控制浮动按钮显示/隐藏）
     */
    enableMouseTracking(enabled) {
        this.mouseTrackingEnabled = enabled;

        // 确保拖拽和缩放功能已初始化
        if (enabled && (!this.mouseDownHandler || !this.dragHandler || !this.wheelHandler)) {
            this.initDragAndZoom();
        }

        if (enabled) {
            this.setupFloatingButtonsMouseTracking();
        } else {
            this.cleanupFloatingButtonsMouseTracking();
        }
    }

    /**
     * 更新模型包围盒和屏幕边界缓存（在模型或骨骼更新时调用）
     * 这个方法应该被外部调用，例如在模型加载、动画更新或骨骼变化时
     */
    updateModelBoundsCache() {
        if (!this.manager.currentModel?.vrm || !this.manager.camera || !this.manager.renderer || !THREE) {
            this._cachedBox = null;
            this._cachedCorners = null;
            this._cachedScreenBounds = null;
            return;
        }

        try {
            const vrm = this.manager.currentModel.vrm;
            const camera = this.manager.camera;
            const renderer = this.manager.renderer;

            // 计算模型在屏幕上的包围盒
            this._cachedBox = new THREE.Box3().setFromObject(vrm.scene);
            this._cachedCorners = [
                new THREE.Vector3(this._cachedBox.min.x, this._cachedBox.min.y, this._cachedBox.min.z),
                new THREE.Vector3(this._cachedBox.max.x, this._cachedBox.min.y, this._cachedBox.min.z),
                new THREE.Vector3(this._cachedBox.min.x, this._cachedBox.max.y, this._cachedBox.min.z),
                new THREE.Vector3(this._cachedBox.max.x, this._cachedBox.max.y, this._cachedBox.min.z),
                new THREE.Vector3(this._cachedBox.min.x, this._cachedBox.min.y, this._cachedBox.max.z),
                new THREE.Vector3(this._cachedBox.max.x, this._cachedBox.min.y, this._cachedBox.max.z),
                new THREE.Vector3(this._cachedBox.min.x, this._cachedBox.max.y, this._cachedBox.max.z),
                new THREE.Vector3(this._cachedBox.max.x, this._cachedBox.max.y, this._cachedBox.max.z),
            ];

            // 投影到屏幕空间并计算边界
            const canvasRect = renderer.domElement.getBoundingClientRect();
            let minX = Infinity, maxX = -Infinity;
            let minY = Infinity, maxY = -Infinity;

            this._cachedCorners.forEach(corner => {
                const worldPos = corner.clone();
                worldPos.project(camera);
                const screenX = (worldPos.x * 0.5 + 0.5) * canvasRect.width + canvasRect.left;
                const screenY = (-worldPos.y * 0.5 + 0.5) * canvasRect.height + canvasRect.top;
                minX = Math.min(minX, screenX);
                maxX = Math.max(maxX, screenX);
                minY = Math.min(minY, screenY);
                maxY = Math.max(maxY, screenY);
            });

            this._cachedScreenBounds = { minX, maxX, minY, maxY };
            this._lastModelUpdateTime = Date.now();

            // Keep animated extremities inside a modest screen-safe frame by
            // widening the lens, never by changing the model's bones, authored
            // root motion, saved position or user scale. FOV grows in small
            // increments and returns to the user's baseline only after the
            // animated bounds have stayed comfortably inside the safe frame.
            const viewWidth = canvasRect.width;
            const viewHeight = canvasRect.height;
            const padding = Math.max(24, Math.min(64, Math.min(viewWidth, viewHeight) * 0.04));
            const localBounds = {
                minX: minX - canvasRect.left,
                maxX: maxX - canvasRect.left,
                minY: minY - canvasRect.top,
                maxY: maxY - canvasRect.top
            };
            if (!Number.isFinite(this._safeFramingBaselineFov)) {
                this._safeFramingBaselineFov = camera.fov;
            }
            const targetFov = calculateExpandedFov(
                camera.fov,
                localBounds,
                viewWidth,
                viewHeight,
                padding,
                VRM_MAX_SAFE_FOV
            );
            if (Number.isFinite(targetFov) && targetFov > camera.fov + 0.05) {
                camera.fov = Math.min(targetFov, camera.fov + 0.75);
                this._safeFramingLastExpandedAt = Date.now();
                camera.updateProjectionMatrix();
            } else {
                const framingRatio = calculateFramingRatio(localBounds, viewWidth, viewHeight, padding);
                const baselineFov = Number(this._safeFramingBaselineFov);
                const canRelax = !this.isDragging && framingRatio < 0.88
                    && Number.isFinite(baselineFov) && camera.fov > baselineFov + 0.05
                    && Date.now() - this._safeFramingLastExpandedAt >= 1500;
                if (canRelax) {
                    camera.fov = Math.max(baselineFov, camera.fov - 0.35);
                    camera.updateProjectionMatrix();
                }
            }
        } catch (error) {
            console.warn('[VRM Interaction] 更新模型边界缓存失败:', error);
            this._cachedBox = null;
            this._cachedCorners = null;
            this._cachedScreenBounds = null;
        }
    }

    /**
     * 设置浮动按钮的鼠标跟踪
     */
    setupFloatingButtonsMouseTracking() {
        if (!this.manager.renderer || !this.manager.currentModel) return;

        const canvas = this.manager.renderer.domElement;
        const useUiLoopVisibility = () => typeof this.manager._shouldShowVrmLockIcon === 'function';
        const getModelThreshold = () => {
            const modelHeight = Math.max(0, Number(this._vrmModelScreenHeight) || 0);
            return Math.max(120, Math.min(320, modelHeight > 0 ? modelHeight * 0.6 : 180));
        };
        const hoverFadeThreshold = 60;

        // Ctrl+锁定+近距离 → 容器变淡（与 Live2D 侧 setLockedHoverFade 对齐）
        // 注意：vrm-core.js init 时设置了 container.style.opacity='1'（内联样式），
        // CSS class 优先级低于内联样式，因此必须直接操作 style.opacity 才能生效
        const vrmContainer = document.getElementById('vrm-container');
        let ctrlFadeActive = false;      // Ctrl 按住淡化
        let stationaryFadeActive = false; // 静止1秒淡化
        let isCtrlPressed = false;

        // 静止自动淡化：鼠标在模型范围内静止1秒后自动淡化
        this._vrmStationaryFadeTimer = null;
        this._vrmHasEnteredHoverRange = false; // 是否已进入过模型范围
        const STATIONARY_FADE_DELAY = 1000;

        const clearStationaryFadeTimer = () => {
            if (this._vrmStationaryFadeTimer !== null) {
                clearTimeout(this._vrmStationaryFadeTimer);
                this._vrmStationaryFadeTimer = null;
            }
        };

        const applyFade = (forceFade) => {
            if (!vrmContainer) return;
            let shouldFade = forceFade !== undefined ? forceFade : (ctrlFadeActive || stationaryFadeActive);
            if (window.lockedHoverFadeEnabled === false) shouldFade = false;
            vrmContainer.style.opacity = shouldFade ? '0.12' : '1';
        };
        this._setLockedHoverFade = applyFade;

        // 监听锁定悬停淡化设置变更
        const onLockedHoverFadeChanged = () => {
            if (window.lockedHoverFadeEnabled === false) {
                ctrlFadeActive = false;
                stationaryFadeActive = false;
                applyFade();
            }
        };
        if (this._lockedHoverFadeChangedListener) {
            window.removeEventListener('neko-locked-hover-fade-changed', this._lockedHoverFadeChangedListener);
        }
        this._lockedHoverFadeChangedListener = onLockedHoverFadeChanged;
        window.addEventListener('neko-locked-hover-fade-changed', onLockedHoverFadeChanged);

        // 初始化缓存
        this.updateModelBoundsCache();

        // 清除之前的定时器和 RAF
        if (this._hideButtonsTimer) {
            clearTimeout(this._hideButtonsTimer);
            this._hideButtonsTimer = null;
        }
        if (this._floatingButtonsPendingFrame !== null) {
            cancelAnimationFrame(this._floatingButtonsPendingFrame);
            this._floatingButtonsPendingFrame = null;
        }

        // 辅助函数：显示按钮并更新位置
        const showButtons = () => {
            if (this.checkLocked()) return;

            // 重新获取按钮容器（防止引用失效）
            const currentButtonsContainer = document.getElementById('vrm-floating-buttons');
            if (!currentButtonsContainer) return;

            if (window.live2dManager) {
                window.live2dManager.isFocusing = true;
            }

            // 新版显隐逻辑由 vrm-ui-buttons 的更新循环统一接管
            if (!useUiLoopVisibility()) {
                // 显示浮动按钮（位置由 _startUIUpdateLoop 自动更新）
                currentButtonsContainer.style.display = 'flex';

                // 鼠标靠近时显示锁图标
                const lockIcon = document.getElementById('vrm-lock-icon');
                if (lockIcon) {
                    lockIcon.style.display = 'block';
                }
            }

            // 清除隐藏定时器（按钮显示时不需要隐藏）
            if (this._hideButtonsTimer) {
                clearTimeout(this._hideButtonsTimer);
                this._hideButtonsTimer = null;
            }
        };

        // 辅助函数：使用缓存计算鼠标到模型的距离
        const calculateDistanceToModel = (mouseX, mouseY) => {
            if (!this._cachedScreenBounds) {
                // 缓存未就绪，返回一个很大的距离
                return Infinity;
            }

            const { minX, maxX, minY, maxY } = this._cachedScreenBounds;
            // 计算鼠标到模型包围盒的距离
            const dx = Math.max(minX - mouseX, 0, mouseX - maxX);
            const dy = Math.max(minY - mouseY, 0, mouseY - maxY);
            return Math.sqrt(dx * dx + dy * dy);
        };

        const collectUiRects = () => {
            const rects = [];
            const pushRect = (el) => {
                if (!el) return;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return;
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return;
                rects.push(rect);
            };

            pushRect(document.getElementById('vrm-floating-buttons'));
            pushRect(document.getElementById('vrm-lock-icon'));

            return rects;
        };

        const isPointNearRect = (x, y, rect, padding = 16) => {
            return x >= rect.left - padding &&
                x <= rect.right + padding &&
                y >= rect.top - padding &&
                y <= rect.bottom + padding;
        };

        const isPointerNearUi = (mouseX, mouseY) => {
            const uiRects = collectUiRects();
            if (!uiRects.length) return false;
            return uiRects.some((rect) => isPointNearRect(mouseX, mouseY, rect, 16));
        };

        const isPointerInTransitCorridor = (mouseX, mouseY) => {
            const currentButtonsContainer = document.getElementById('vrm-floating-buttons');
            if (!currentButtonsContainer) return false;
            if (currentButtonsContainer.style.display !== 'flex') return false;
            if (!this._cachedScreenBounds) return false;

            const { minX, maxX, minY, maxY } = this._cachedScreenBounds;
            const modelCenterX = (minX + maxX) / 2;
            const modelCenterY = (minY + maxY) / 2;
            const btnRect = currentButtonsContainer.getBoundingClientRect();
            const uiCenterX = (btnRect.left + btnRect.right) / 2;
            const uiCenterY = (btnRect.top + btnRect.bottom) / 2;

            const vx = uiCenterX - modelCenterX;
            const vy = uiCenterY - modelCenterY;
            const vLenSq = vx * vx + vy * vy;
            if (vLenSq < 1) return false;

            // Point-to-segment distance with a slightly expanded corridor.
            const wx = mouseX - modelCenterX;
            const wy = mouseY - modelCenterY;
            let t = (wx * vx + wy * vy) / vLenSq;
            t = Math.max(-0.08, Math.min(1.08, t));
            const projX = modelCenterX + t * vx;
            const projY = modelCenterY + t * vy;
            const dx = mouseX - projX;
            const dy = mouseY - projY;

            const corridorWidth = Math.max(26, Math.min(64, Math.hypot(btnRect.width, btnRect.height) * 0.18));
            return (dx * dx + dy * dy) <= corridorWidth * corridorWidth;
        };

        const shouldKeepUiVisible = (mouseX, mouseY, distanceToModel) => {
            const popupUi = window.AvatarPopupUI || null;
            if (popupUi && typeof popupUi.hasVisibleOverlay === 'function' && popupUi.hasVisibleOverlay('vrm')) {
                return true;
            }
            const threshold = getModelThreshold();
            if (distanceToModel < threshold) return true;
            if (isPointerNearUi(mouseX, mouseY)) return true;
            if (isPointerInTransitCorridor(mouseX, mouseY)) return true;
            return false;
        };

        // 辅助函数：启动隐藏定时器（简化版本，使用缓存）
        const startHideTimer = (delay = 1000) => {
            if (this.checkLocked()) return;

            if (this._hideButtonsTimer) {
                clearTimeout(this._hideButtonsTimer);
                this._hideButtonsTimer = null;
            }

            this._hideButtonsTimer = setTimeout(() => {
                // 检查鼠标是否在锁图标或按钮上
                const lockIcon = document.getElementById('vrm-lock-icon');
                let isMouseOverLock = false;
                if (lockIcon && lockIcon.style.display === 'block') {
                    const lockRect = lockIcon.getBoundingClientRect();
                    const mouseX = this._lastMouseX || 0;
                    const mouseY = this._lastMouseY || 0;
                    isMouseOverLock = mouseX >= lockRect.left && mouseX <= lockRect.right &&
                        mouseY >= lockRect.top && mouseY <= lockRect.bottom;
                }

                const popupUi = window.AvatarPopupUI || null;
                const hasOpenOverlay = !!(popupUi && typeof popupUi.hasVisibleOverlay === 'function' && popupUi.hasVisibleOverlay('vrm'));
                if (this._isMouseOverButtons || isMouseOverLock || hasOpenOverlay) {
                    this._hideButtonsTimer = null;
                    startHideTimer(delay);
                    return;
                }

                // 使用缓存计算距离（避免重复的 Box3 计算）
                const mouseX = this._lastMouseX || 0;
                const mouseY = this._lastMouseY || 0;
                const distance = calculateDistanceToModel(mouseX, mouseY);

                if (shouldKeepUiVisible(mouseX, mouseY, distance)) {
                    // 鼠标仍在模型附近，重新启动定时器
                    this._hideButtonsTimer = null;
                    startHideTimer(delay);
                    return;
                }

                // 鼠标不在模型附近，隐藏按钮
                if (window.live2dManager) {
                    window.live2dManager.isFocusing = false;
                }

                if (!useUiLoopVisibility()) {
                    const currentButtonsContainer = document.getElementById('vrm-floating-buttons');
                    if (currentButtonsContainer) {
                        currentButtonsContainer.style.display = 'none';
                    }

                    if (lockIcon && !lockIcon.dataset.clickProtection) {
                        lockIcon.style.display = 'none';
                    }
                }

                this._hideButtonsTimer = null;
            }, delay);
        };

        const onMouseEnter = () => showButtons();


        // RAF 回调：执行昂贵的 Box3 和投影计算
        const performExpensiveCalculation = () => {
            this._floatingButtonsPendingFrame = null;

            if (!this.manager.currentModel || !this.manager.currentModel.vrm) return;
            if (!this.manager.renderer || !this.manager.camera) return;

            // 更新缓存（如果模型已更新）
            const now = Date.now();
            if (!this._cachedScreenBounds || (now - this._lastModelUpdateTime) > 250) {
                this.updateModelBoundsCache();
            }

            const mouseX = this._lastMouseX || 0;
            const mouseY = this._lastMouseY || 0;

            // 检查鼠标是否在按钮或锁图标上
            const currentButtonsContainer = document.getElementById('vrm-floating-buttons');
            let isOverButtons = false;
            if (currentButtonsContainer && currentButtonsContainer.style.display === 'flex') {
                const buttonsRect = currentButtonsContainer.getBoundingClientRect();
                isOverButtons = mouseX >= buttonsRect.left && mouseX <= buttonsRect.right &&
                    mouseY >= buttonsRect.top && mouseY <= buttonsRect.bottom;
            }

            let isOverLock = false;
            const lockIcon = document.getElementById('vrm-lock-icon');
            if (lockIcon && lockIcon.style.display === 'block') {
                const lockRect = lockIcon.getBoundingClientRect();
                isOverLock = mouseX >= lockRect.left && mouseX <= lockRect.right &&
                    mouseY >= lockRect.top && mouseY <= lockRect.bottom;
            }

            this._isMouseOverButtons = isOverButtons || isOverLock;

            // 如果鼠标在按钮或锁图标上，不变淡，直接显示
            // 同时重置所有淡化状态，防止离开 UI 后残留状态导致立即重新淡化
            if (isOverButtons || isOverLock) {
                clearStationaryFadeTimer();
                ctrlFadeActive = false;
                stationaryFadeActive = false;
                this._vrmHasEnteredHoverRange = false;
                applyFade(false);
                showButtons();
                return;
            }

            // 使用缓存计算距离（避免重复的 Box3 计算）
            const distance = calculateDistanceToModel(mouseX, mouseY);

            // 静止自动淡化：锁定 + 鼠标在模型附近 + 静止超过1秒 → 变淡
            // Ctrl 按住淡化：锁定 + Ctrl + 鼠标在模型附近 → 变淡
            const ctrlKeyPressed = isCtrlPressed;
            const isNearModel = distance < hoverFadeThreshold;

            // 静止时启动定时器，移出范围时清除（移动端无鼠标悬停，跳过）
            const isMobileDevice = (window.appUtils && typeof window.appUtils.isMobile === 'function' && window.appUtils.isMobile()) || /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
            if (!isMobileDevice && this.checkLocked() && isNearModel) {
                // 首次进入范围：设置标志并启动定时器
                if (!this._vrmHasEnteredHoverRange) {
                    this._vrmHasEnteredHoverRange = true;
                    if (this._vrmStationaryFadeTimer === null && !stationaryFadeActive) {
                        this._vrmStationaryFadeTimer = setTimeout(() => {
                            stationaryFadeActive = true;
                            applyFade();
                        }, STATIONARY_FADE_DELAY);
                    }
                }
                // 已在范围内：移动时不重启定时器
            } else {
                // 移出范围：清除定时器并重置标志
                if (this._vrmStationaryFadeTimer !== null || stationaryFadeActive) {
                    clearStationaryFadeTimer();
                    stationaryFadeActive = false;
                    applyFade();
                }
                this._vrmHasEnteredHoverRange = false;
            }

            // Ctrl 淡化：锁定 + Ctrl + 在模型范围内（独立于静止淡化，移动端跳过）
            ctrlFadeActive = !isMobileDevice && this.checkLocked() && ctrlKeyPressed && isNearModel;
            applyFade();

            // 锁定状态下不处理按钮显示/隐藏
            if (this.checkLocked()) return;

            if (shouldKeepUiVisible(mouseX, mouseY, distance)) {
                showButtons();
            } else {
                startHideTimer();
            }
        };

        const onPointerMove = (event) => {
            if (!this.manager._isModelReadyForInteraction) return;
            if (!this.manager.currentModel || !this.manager.currentModel.vrm) return;
            if (!this.manager.renderer || !this.manager.camera) return;

            // 从事件更新 Ctrl 键状态（与 Live2D 侧一致）
            if (event.isTrusted) {
                isCtrlPressed = event.ctrlKey || event.metaKey;
            } else if (event.ctrlKey || event.metaKey) {
                isCtrlPressed = true;
            }

            // 更新鼠标位置（轻量级操作）
            this._lastMouseX = event.clientX;
            this._lastMouseY = event.clientY;

            // 使用 RAF 节流昂贵的计算（避免每帧都计算 Box3 和投影）
            if (this._floatingButtonsPendingFrame === null) {
                this._floatingButtonsPendingFrame = requestAnimationFrame(performExpensiveCalculation);
            }
        };

        // Ctrl 键跟踪（与 Live2D 侧 _ctrlKeyDownListener / _ctrlKeyUpListener 对齐）
        const onKeyDown = (event) => {
            if (event.ctrlKey || event.metaKey) {
                isCtrlPressed = true;
            }
        };
        const onKeyUp = (event) => {
            if (!event.ctrlKey && !event.metaKey) {
                isCtrlPressed = false;
                // Ctrl 释放时重新计算淡化状态，让 stationaryFadeActive 有机会生效
                ctrlFadeActive = false;
                applyFade();
            }
        };
        const onBlur = () => {
            // blur 时 Ctrl 键事件无法到达，必须主动清除 Ctrl 状态避免卡死
            isCtrlPressed = false;
            ctrlFadeActive = false;
            // 锁定状态下 blur 通常由鼠标穿透点击引起，保留静止淡化状态避免闪烁
            if (this.checkLocked()) {
                applyFade();
                return;
            }
            clearStationaryFadeTimer();
            // blur 时清除定时器和淡化状态，焦点恢复后需重新触发
            if (stationaryFadeActive) {
                stationaryFadeActive = false;
            }
            applyFade();
            this._vrmHasEnteredHoverRange = false;
        };
        this._vrmClearStationaryFadeTimer = clearStationaryFadeTimer;

        // 清理旧的键盘 / blur 监听器
        if (this._vrmCtrlKeyDownListener) {
            window.removeEventListener('keydown', this._vrmCtrlKeyDownListener);
        }
        if (this._vrmCtrlKeyUpListener) {
            window.removeEventListener('keyup', this._vrmCtrlKeyUpListener);
        }
        if (this._vrmWindowBlurListener) {
            window.removeEventListener('blur', this._vrmWindowBlurListener);
        }

        window.addEventListener('keydown', onKeyDown);
        window.addEventListener('keyup', onKeyUp);
        window.addEventListener('blur', onBlur);

        canvas.addEventListener('mouseenter', onMouseEnter);
        // 监听 window 而非 canvas，使得鼠标穿透模式下 preload 轮询派发的
        // 合成 pointermove 事件也能到达此处，保持淡化机制正常工作
        window.addEventListener('pointermove', onPointerMove);
        window.addEventListener('mousemove', onPointerMove);

        this._vrmCtrlKeyDownListener = onKeyDown;
        this._vrmCtrlKeyUpListener = onKeyUp;
        this._vrmWindowBlurListener = onBlur;
        this._floatingButtonsMouseEnter = onMouseEnter;
        this._floatingButtonsPointerMove = onPointerMove;

        if (this.manager.currentModel && !this.checkLocked()) {
            setTimeout(() => {
                showButtons();
                if (!useUiLoopVisibility()) {
                    startHideTimer();
                }
            }, 100);
        }
    }

    /**
     * 清理浮动按钮的鼠标跟踪
     */
    cleanupFloatingButtonsMouseTracking() {
        if (!this.manager.renderer) return;

        const canvas = this.manager.renderer.domElement;

        if (this._floatingButtonsMouseEnter) {
            canvas.removeEventListener('mouseenter', this._floatingButtonsMouseEnter);
            this._floatingButtonsMouseEnter = null;
        }
        if (this._floatingButtonsMouseLeave) {
            canvas.removeEventListener('mouseleave', this._floatingButtonsMouseLeave);
            this._floatingButtonsMouseLeave = null;
        }
        if (this._floatingButtonsPointerMove) {
            window.removeEventListener('pointermove', this._floatingButtonsPointerMove);
            window.removeEventListener('mousemove', this._floatingButtonsPointerMove);
            this._floatingButtonsPointerMove = null;
        }
        // 清理 Ctrl 键 / blur 监听器
        if (this._vrmCtrlKeyDownListener) {
            window.removeEventListener('keydown', this._vrmCtrlKeyDownListener);
            this._vrmCtrlKeyDownListener = null;
        }
        if (this._vrmCtrlKeyUpListener) {
            window.removeEventListener('keyup', this._vrmCtrlKeyUpListener);
            this._vrmCtrlKeyUpListener = null;
        }
        if (this._vrmWindowBlurListener) {
            window.removeEventListener('blur', this._vrmWindowBlurListener);
            this._vrmWindowBlurListener = null;
        }
        // 清理锁定悬停淡化监听器
        if (this._lockedHoverFadeChangedListener) {
            window.removeEventListener('neko-locked-hover-fade-changed', this._lockedHoverFadeChangedListener);
            this._lockedHoverFadeChangedListener = null;
        }
        // 清除变淡状态
        if (typeof this._setLockedHoverFade === 'function') {
            this._setLockedHoverFade(false);
            this._setLockedHoverFade = null;
        }
        if (this._vrmClearStationaryFadeTimer) {
            this._vrmClearStationaryFadeTimer();
            this._vrmClearStationaryFadeTimer = null;
        }
        if (this._hideButtonsTimer) {
            clearTimeout(this._hideButtonsTimer);
            this._hideButtonsTimer = null;
        }
        // 清理 RAF 标志
        if (this._floatingButtonsPendingFrame !== null) {
            cancelAnimationFrame(this._floatingButtonsPendingFrame);
            this._floatingButtonsPendingFrame = null;
        }
    }

    /**
     * 保存模型位置和状态到后端（交互结束后调用）
     */
    async _savePositionAfterInteraction() {
        if (!this.manager.currentModel || !this.manager.currentModel.url) {
            return;
        }

        const scene = this.manager.currentModel.scene;
        if (!scene) {
            return;
        }

        const position = {
            x: scene.position.x,
            y: scene.position.y,
            z: scene.position.z
        };

        const scale = {
            x: scene.scale.x,
            y: scene.scale.y,
            z: scene.scale.z
        };

        const rotation = {
            x: scene.rotation.x,
            y: scene.rotation.y,
            z: scene.rotation.z
        };

        // 验证数据有效性
        if (!Number.isFinite(position.x) || !Number.isFinite(position.y) || !Number.isFinite(position.z) ||
            !Number.isFinite(scale.x) || !Number.isFinite(scale.y) || !Number.isFinite(scale.z)) {
            console.warn('[VRM] 位置或缩放数据无效，跳过保存');
            return;
        }

        // 获取当前窗口所在显示器的信息（用于多屏幕位置恢复）
        let displayInfo = null;
        if (window.electronScreen && window.electronScreen.getCurrentDisplay) {
            try {
                const currentDisplay = await window.electronScreen.getCurrentDisplay();
                if (currentDisplay) {
                    let screenX = currentDisplay.screenX;
                    let screenY = currentDisplay.screenY;

                    // 如果 screenX/screenY 不存在，尝试从 bounds 获取
                    if (!Number.isFinite(screenX) || !Number.isFinite(screenY)) {
                        if (currentDisplay.bounds &&
                            Number.isFinite(currentDisplay.bounds.x) &&
                            Number.isFinite(currentDisplay.bounds.y)) {
                            screenX = currentDisplay.bounds.x;
                            screenY = currentDisplay.bounds.y;
                        }
                    }

                    if (Number.isFinite(screenX) && Number.isFinite(screenY)) {
                        displayInfo = {
                            screenX: screenX,
                            screenY: screenY
                        };
                    }
                }
            } catch (error) {
                console.warn('[VRM] 获取显示器信息失败:', error);
            }
        }

        // 获取当前屏幕尺寸（用于跨分辨率缩放归一化）
        // 使用 screen.width/height 而非 renderer/窗口尺寸，避免临时视口变化（F12、输入法等）污染保存数据
        let viewportInfo = null;
        const screenW = window.screen.width;
        const screenH = window.screen.height;
        if (Number.isFinite(screenW) && Number.isFinite(screenH) && screenW > 0 && screenH > 0) {
            viewportInfo = { width: screenW, height: screenH };
        }

        // 获取当前相机位置、朝向和观察目标
        let cameraPosition = null;
        if (this.manager.camera) {
            const target = this.manager._cameraTarget || new THREE.Vector3(0, 0, 0);
            cameraPosition = {
                x: this.manager.camera.position.x,
                y: this.manager.camera.position.y,
                z: this.manager.camera.position.z,
                // 保存四元数（精确的相机朝向，避免 lookAt 转换误差）
                qx: this.manager.camera.quaternion.x,
                qy: this.manager.camera.quaternion.y,
                qz: this.manager.camera.quaternion.z,
                qw: this.manager.camera.quaternion.w,
                // 保存观察目标（用于 zoom/orbit 的中心点）
                targetX: target.x,
                targetY: target.y,
                targetZ: target.z
            };
        }

        // 异步保存，不阻塞交互
        if (this.manager.core && typeof this.manager.core.saveUserPreferences === 'function') {
            this.manager.core.saveUserPreferences(
                this.manager.currentModel.url,
                position,
                scale,
                rotation,
                displayInfo,
                viewportInfo,
                cameraPosition
            ).then(success => {
                if (!success) {
                    console.warn('[VRM] 自动保存位置失败');
                }
            }).catch(error => {
                console.error('[VRM] 自动保存位置时出错:', error);
            });
        }
    }

    /**
     * 防抖动保存位置的辅助函数（用于滚轮缩放等连续操作）
     */
    _debouncedSavePosition() {
        // 清除之前的定时器
        if (this._savePositionDebounceTimer) {
            clearTimeout(this._savePositionDebounceTimer);
        }

        // 设置新的定时器，500ms后保存
        this._savePositionDebounceTimer = setTimeout(() => {
            this._savePositionAfterInteraction().catch(error => {
                console.error('[VRM] 防抖动保存位置时出错:', error);
            });
        }, 500);
    }

    /**
     * 清理交互资源
     */
    // 排一帧回弹动画：Electron Pet 里渲染后端切到定时器驱动时走同周期定时器
    // （frame-pacing.requestPacedFrame），否则 rAF。返回取消函数（存到 _snapCancelFrame）。
    _scheduleSnapFrame(callback) {
        const pacing = window.nekoFramePacing;
        if (pacing && typeof pacing.requestPacedFrame === 'function') {
            return pacing.requestPacedFrame(callback);
        }
        const id = requestAnimationFrame(callback);
        return () => cancelAnimationFrame(id);
    }

    dispose() {
        this.enableMouseTracking(false);
        this.cleanupDragAndZoom();
        // 确保拖拽相关的 pointer-events 被恢复
        this._restoreButtonPointerEvents();
        // 确保初始化定时器被清理（即使 renderer 不存在）
        if (this._initTimerId !== null) {
            clearTimeout(this._initTimerId);
            this._initTimerId = null;
        }
        // 清理所有可能的定时器
        if (this._hideButtonsTimer) {
            clearTimeout(this._hideButtonsTimer);
            this._hideButtonsTimer = null;
        }

        // 清理位置保存防抖定时器
        if (this._savePositionDebounceTimer) {
            clearTimeout(this._savePositionDebounceTimer);
            this._savePositionDebounceTimer = null;
        }

        // 清理回弹动画
        if (this._snapCancelFrame) {
            this._snapCancelFrame();
            this._snapCancelFrame = null;
        }
        if (this._snapResolve) {
            this._snapResolve(false);
            this._snapResolve = null;
        }
        this._isSnappingModel = false;

        // 重置状态
        this.isDragging = false;
        this.dragMode = null;
        this.isLocked = false;
    }
}

// 导出到全局
window.VRMInteraction = VRMInteraction;
