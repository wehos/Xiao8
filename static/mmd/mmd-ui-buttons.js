/**
 * MMD UI Buttons - 浮动按钮系统（精简版）
 * 使用 AvatarButtonMixin 的 MMD 特定实现
 */

// 应用 mixin 到 MMD Manager
AvatarButtonMixin.apply(MMDManager.prototype, 'mmd', {
    containerElementId: 'mmd-floating-buttons',
    returnContainerId: 'mmd-return-button-container',
    returnBtnId: 'mmd-btn-return',
    lockIconId: 'mmd-lock-icon',
    popupPrefix: 'mmd',
    buttonClassPrefix: 'mmd-floating-btn',
    triggerBtnClass: 'mmd-trigger-btn',
    triggerIconClass: 'mmd-trigger-icon',
    returnBtnClass: 'mmd-return-btn',
    returnBreathingStyleId: 'mmd-return-button-breathing-styles'
});

function isYuiGuideFloatingToolbarSuppressed() {
    return !!(
        window.isNekoYuiGuideFloatingToolbarSuppressed
        && window.isNekoYuiGuideFloatingToolbarSuppressed()
    );
}

/**
 * 设置浮动按钮系统（MMD 特定）
 */
MMDManager.prototype.setupFloatingButtons = function() {
    if (window.location.pathname.includes('model_manager')) return;

    // 防御性检查：当前模型类型不是 MMD 时不创建按钮（防止过时的异步回调）
    // 守卫形式与 vrm-ui-buttons.js 对齐：cfgType 为空时放行（启动早期 lanlan_config
    // 尚未注入的窗口期），仅当明确切到非 MMD 类型才退出。
    var cfgType = (window.lanlan_config && window.lanlan_config.model_type || '').toLowerCase();
    var cfgSub = (window.lanlan_config && window.lanlan_config.live3d_sub_type || '').toLowerCase();
    var isMmd = cfgType === 'mmd' || (cfgType === 'live3d' && cfgSub === 'mmd');
    if (cfgType && !isMmd) return;

    // 基础框架初始化
    const buttonsContainer = this.setupFloatingButtonsBase();

    const opts = this._avatarButtonOptions;

    buttonsContainer.addEventListener('mouseenter', () => { this._mmdButtonsHovered = true; });
    buttonsContainer.addEventListener('mouseleave', () => { this._mmdButtonsHovered = false; });

    // MMD 特定的响应式布局处理
    const applyResponsiveFloatingLayout = () => {
        this.syncResponsiveButtonVisibility(buttonsContainer);
        if (isYuiGuideFloatingToolbarSuppressed()) {
            buttonsContainer.style.display = 'none';
            buttonsContainer.style.visibility = 'hidden';
            buttonsContainer.style.opacity = '0';
            return;
        }
        buttonsContainer.style.removeProperty('visibility');
        buttonsContainer.style.removeProperty('opacity');
        if (this._isInReturnState) { buttonsContainer.style.display = 'none'; return; }
        const isLocked = this.isLocked;
        if (isLocked) { buttonsContainer.style.display = 'none'; return; }
        if (window.isMobileWidth && window.isMobileWidth()) {
            buttonsContainer.style.flexDirection = 'column';
            buttonsContainer.style.bottom = '116px';
            buttonsContainer.style.right = '16px';
            buttonsContainer.style.left = '';
            buttonsContainer.style.top = '';
            buttonsContainer.style.display = 'flex';
        } else {
            buttonsContainer.style.flexDirection = 'column';
            buttonsContainer.style.bottom = '';
            buttonsContainer.style.right = '';
            buttonsContainer.style.left = '';
            buttonsContainer.style.top = '';
        }
    };
    applyResponsiveFloatingLayout();

    // 锁图标显示逻辑
    const shouldShowLockIcon = () => {
        if (isYuiGuideFloatingToolbarSuppressed()) return false;
        // 教程期间始终显示锁图标，防止高亮框位置异常
        if (window.isInTutorial) return true;
        const isLocked = this.isLocked;
        if (this._isInReturnState) return false;
        if (isLocked) return true;
        const mouse = this._mmdMousePos;
        if (!mouse) return false;
        if (!this._mmdMousePosTs || (Date.now() - this._mmdMousePosTs > 1500)) return false;
        if (this._mmdLockIcon) {
            const rect = this._mmdLockIcon.getBoundingClientRect();
            const expandPx = 8;
            if (mouse.x >= rect.left - expandPx && mouse.x <= rect.right + expandPx &&
                mouse.y >= rect.top - expandPx && mouse.y <= rect.bottom + expandPx) return true;
        }
        const centerX = this._mmdModelCenterX;
        const centerY = this._mmdModelCenterY;
        if (typeof centerX !== 'number' || typeof centerY !== 'number') return false;
        if (this._mmdMouseInModelRegion) return true;
        const dx = mouse.x - centerX;
        const dy = mouse.y - centerY;
        const dist = Math.hypot(dx, dy);
        const modelHeight = Math.max(0, Number(this._mmdModelScreenHeight) || 0);
        const threshold = Math.max(90, Math.min(260, modelHeight * 0.55));
        return dist <= threshold;
    };
    this._shouldShowMmdLockIcon = shouldShowLockIcon;

    // 鼠标位置跟踪
    const updateMousePosition = (e) => {
        this._mmdMousePos = { x: typeof e.clientX === 'number' ? e.clientX : 0, y: typeof e.clientY === 'number' ? e.clientY : 0 };
        this._mmdMousePosTs = Date.now();
    };
    const mouseListenerOptions = { passive: true, capture: true };
    window.addEventListener('mousemove', updateMousePosition, mouseListenerOptions);
    this._uiWindowHandlers.push({ event: 'mousemove', handler: updateMousePosition, target: window, options: mouseListenerOptions });
    window.addEventListener('pointermove', updateMousePosition, mouseListenerOptions);
    this._uiWindowHandlers.push({ event: 'pointermove', handler: updateMousePosition, target: window, options: mouseListenerOptions });
    window.addEventListener('resize', applyResponsiveFloatingLayout);
    this._uiWindowHandlers.push({ event: 'resize', handler: applyResponsiveFloatingLayout, target: window });

    // 获取按钮配置
    const buttonConfigs = this.getDefaultButtonConfigs();
    this._buttonConfigs = buttonConfigs;
    this._floatingButtons = this._floatingButtons || {};

    // 创建按钮
    buttonConfigs.forEach(config => {
        if (window.isMobileWidth && window.isMobileWidth() && (config.id === 'agent' || config.id === 'goodbye')) return;

        const { btnWrapper, btn, imgOff, imgOn } = this.createButtonElement(config, buttonsContainer);

        // 点击事件处理
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            e.preventDefault();

            if (config.id === 'mic') {
                const isMicStarting = window.isMicStarting || false;
                if (isMicStarting) {
                    if (btn.dataset.active !== 'true') this.setButtonActive(config.id, true);
                    return;
                }
            }
            if (config.id === 'screen') {
                const isRecording = window.isRecording || false;
                const wantToActivate = btn.dataset.active !== 'true';
                if (wantToActivate && !isRecording) {
                    if (typeof window.showStatusToast === 'function') {
                        window.showStatusToast(window.t ? window.t('app.screenShareRequiresVoice') : '屏幕分享仅用于音视频通话', 3000);
                    }
                    return;
                }
            }
            if (config.popupToggle) return;

            const currentActive = btn.dataset.active === 'true';
            let targetActive = !currentActive;

            if (config.id === 'mic' || config.id === 'screen') {
                window.dispatchEvent(new CustomEvent(`live2d-${config.id}-toggle`, { detail: { active: targetActive } }));
                this.setButtonActive(config.id, targetActive);
            } else if (config.id === 'goodbye') {
                window.dispatchEvent(new CustomEvent('live2d-goodbye-click'));
                return;
            } else if (config.id === 'social') {
                // 与 Live2D 共用 opener（app-ui.js 监听 live2d-social-click）
                window.dispatchEvent(new CustomEvent('live2d-social-click'));
                return;
            }

            btn.style.background = targetActive ? 'var(--neko-btn-bg-active, rgba(255,255,255,0.75))' : 'var(--neko-btn-bg-hover, rgba(255,255,255,0.8))';
        });

        btnWrapper.appendChild(btn);

        // 语音会话快捷控制（仅非手机模式下的麦克风按钮）
        if (config.id === 'mic' && config.hasPopup && config.separatePopupTrigger && !(window.isMobileWidth && window.isMobileWidth())) {
            this.createVoiceSessionQuickControls(btnWrapper);
        }

        // 处理弹窗
        let triggerBtn = null;
        let triggerImg = null;
        if (config.hasPopup && config.separatePopupTrigger) {
            if (window.isMobileWidth && window.isMobileWidth() && config.id === 'mic') {
                buttonsContainer.appendChild(btnWrapper);
                this._floatingButtons[config.id] = { button: btn, imgOff, imgOn, triggerButton: null, triggerImg: null };
                return;
            }

            const popup = this.createPopup(config.id);
            triggerBtn = document.createElement('button');
            triggerBtn.type = 'button';
            triggerBtn.className = 'mmd-trigger-btn';
            triggerBtn.setAttribute('aria-label', 'Open popup');

            const iconVersion = window.APP_VERSION ? `?v=${window.APP_VERSION}` : '?v=1.0.0';
            triggerImg = document.createElement('img');
            triggerImg.src = '/static/icons/play_trigger_icon.png' + iconVersion;
            triggerImg.alt = '';
            triggerImg.className = `mmd-trigger-icon-${config.id}`;
            Object.assign(triggerImg.style, {
                width: '22px', height: '22px', objectFit: 'contain',
                pointerEvents: 'none', imageRendering: 'crisp-edges',
                transition: 'transform 0.3s cubic-bezier(0.1, 0.9, 0.2, 1)'
            });
            Object.assign(triggerBtn.style, {
                width: '24px', height: '24px', borderRadius: '50%',
                background: 'var(--neko-btn-bg, rgba(255,255,255,0.65))', backdropFilter: 'saturate(180%) blur(20px)',
                border: 'var(--neko-btn-border, 1px solid rgba(255,255,255,0.18))',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                cursor: 'pointer', userSelect: 'none',
                boxShadow: 'var(--neko-btn-shadow, 0 2px 4px rgba(0,0,0,0.04), 0 4px 8px rgba(0,0,0,0.08))',
                transition: 'all 0.1s ease', pointerEvents: 'auto', marginLeft: '-10px'
            });
            triggerBtn.appendChild(triggerImg);

            const stopTriggerEvent = (e) => { e.stopPropagation(); };
            ['pointerdown', 'mousedown', 'touchstart'].forEach(evt => triggerBtn.addEventListener(evt, stopTriggerEvent));

            const isPopupVisible = () => popup.style.display === 'flex' && popup.style.opacity === '1';
            const repositionPopup = () => {
                if (!isPopupVisible()) return;
                const popupUi = window.AvatarPopupUI || null;
                if (!popupUi || typeof popupUi.positionPopup !== 'function') return;
                void popup.offsetHeight;
                const pos = popupUi.positionPopup(popup, {
                    buttonId: config.id,
                    buttonPrefix: 'mmd-btn-',
                    triggerPrefix: 'mmd-trigger-icon-',
                    rightMargin: 20,
                    bottomMargin: 60,
                    topMargin: 8,
                    gap: 8,
                    sidePanelWidth: (config.id === 'settings' || config.id === 'agent') ? 320 : 0
                });
                popup.dataset.opensLeft = String(!!(pos && pos.opensLeft));
            };

            triggerBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                if (isPopupVisible()) {
                    this.showPopup(config.id, popup);
                    return;
                }

                this.showPopup(config.id, popup);
                await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                if (!isPopupVisible()) return;

                if (config.id === 'mic') {
                    if (typeof window.renderFloatingMicList === 'function') {
                        await window.renderFloatingMicList(popup);
                        repositionPopup();
                    }
                }
                if (config.id === 'screen') {
                    await this.renderScreenSourceList(popup);
                    repositionPopup();
                }
            });

            const triggerWrapper = document.createElement('div');
            triggerWrapper.style.position = 'relative';
            ['pointerdown', 'mousedown', 'touchstart'].forEach(evt => triggerWrapper.addEventListener(evt, stopTriggerEvent));

            triggerWrapper.appendChild(triggerBtn);
            triggerWrapper.appendChild(popup);
            btnWrapper.appendChild(triggerWrapper);
        } else if (config.popupToggle) {
            const popup = this.createPopup(config.id);
            btnWrapper.appendChild(popup);

            let isToggling = false;
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (isToggling) return;
                const isPopupVisible = popup.style.display === 'flex' && popup.style.opacity !== '0' && popup.style.opacity !== '';
                if (!isPopupVisible && config.exclusive) {
                    this.closePopupById(config.exclusive);
                    const exclusiveData = this._floatingButtons[config.exclusive];
                    if (exclusiveData && exclusiveData.button) {
                        exclusiveData.button.style.background = 'var(--neko-btn-bg, rgba(255,255,255,0.65))';
                    }
                    if (exclusiveData && exclusiveData.imgOff && exclusiveData.imgOn) {
                        exclusiveData.imgOff.style.opacity = '1';
                        exclusiveData.imgOn.style.opacity = '0';
                    }
                }
                isToggling = true;
                this.showPopup(config.id, popup);
                setTimeout(() => {
                    const newPopupVisible = popup.style.display === 'flex' && popup.style.opacity !== '0' && popup.style.opacity !== '';
                    if (newPopupVisible) {
                        btn.style.background = 'var(--neko-btn-bg-active, rgba(255,255,255,0.75))';
                        if (imgOff && imgOn) { imgOff.style.opacity = '0'; imgOn.style.opacity = '1'; }
                    } else {
                        btn.style.background = 'var(--neko-btn-bg, rgba(255,255,255,0.65))';
                        if (imgOff && imgOn) { imgOff.style.opacity = '1'; imgOn.style.opacity = '0'; }
                    }
                    isToggling = false;
                }, 200);
            });
        }

        buttonsContainer.appendChild(btnWrapper);
        this._floatingButtons[config.id] = {
            button: btn,
            imgOff,
            imgOn,
            triggerButton: (config.hasPopup && config.separatePopupTrigger && !(window.isMobileWidth && window.isMobileWidth())) ? triggerBtn : null,
            triggerImg: (config.hasPopup && config.separatePopupTrigger && !(window.isMobileWidth && window.isMobileWidth())) ? triggerImg : null
        };
    });
    applyResponsiveFloatingLayout();

    // 处理"请她离开"事件
    // 注意：返回按钮的位置、显示、以及浮动按钮的隐藏均由 app-ui 统一处理，
    // 此处仅更新内部状态标志。不能在此隐藏按钮容器，否则 app-ui 无法读取按钮位置。
    const goodbyeHandler = () => {
        this._isInReturnState = true;
        if (this._physicsRestoreTimer) {
            clearTimeout(this._physicsRestoreTimer);
            this._physicsRestoreTimer = null;
        }
    };
    this._uiWindowHandlers.push({ event: 'live2d-goodbye-click', handler: goodbyeHandler });
    window.addEventListener('live2d-goodbye-click', goodbyeHandler);

    // 处理"请她回来"事件
    const returnHandler = () => {
        this._isInReturnState = false;
        this._snapUIPosition = true;
        if (this._returnButtonContainer) this._returnButtonContainer.style.display = 'none';

        // 回来时先禁用物理、重置姿态，等渐入动画结束再恢复
        const hadPhysics = this.enablePhysics;
        this.enablePhysics = false;
        if (this.currentModel && this.currentModel.physics && typeof this.currentModel.physics.reset === 'function') {
            this.currentModel.physics.reset();
        }

        const bc = document.getElementById('mmd-floating-buttons');
        if (!bc) { this.setupFloatingButtons(); return; }
        const isMobile = window.isMobileWidth && window.isMobileWidth();
        if (isMobile) {
            bc.style.removeProperty('display');
            bc.style.removeProperty('visibility');
            bc.style.removeProperty('opacity');
        } else {
            bc.style.display = 'none';
            bc.style.visibility = 'hidden';
            bc.style.opacity = '0';
        }

        if (this.core && typeof this.core.setLocked === 'function') {
            this.core.setLocked(false);
        }

        if (isMobile) {
            applyResponsiveFloatingLayout();
        }

        if (this._mmdLockIcon) {
            this._mmdLockIcon.style.backgroundImage = 'url(/static/icons/unlocked_icon.png)';
            if (isMobile) {
                this._mmdLockIcon.style.removeProperty('display');
                this._mmdLockIcon.style.removeProperty('visibility');
                this._mmdLockIcon.style.removeProperty('opacity');
                this._mmdLockIcon.style.display = shouldShowLockIcon() ? 'block' : 'none';
            } else {
                this._mmdLockIcon.style.display = 'none';
                this._mmdLockIcon.style.visibility = 'hidden';
                this._mmdLockIcon.style.opacity = '0';
            }
        }

        if (hadPhysics) {
            if (this._physicsRestoreTimer) clearTimeout(this._physicsRestoreTimer);
            this._physicsRestoreTimer = setTimeout(() => {
                this._physicsRestoreTimer = null;
                if (this._isInReturnState) return;
                if (this.currentModel && this.currentModel.physics && typeof this.currentModel.physics.reset === 'function') {
                    this.currentModel.physics.reset();
                }
                this.enablePhysics = true;
            }, 800);
        }
    };
    this._uiWindowHandlers.push({ event: 'mmd-return-click', handler: returnHandler });
    this._uiWindowHandlers.push({ event: 'live2d-return-click', handler: returnHandler });
    window.addEventListener('mmd-return-click', returnHandler);
    window.addEventListener('live2d-return-click', returnHandler);

    // 创建"请她回来"按钮
    const returnButtonContainer = this.createReturnButton();
    this._addReturnButtonBreathingAnimation();

    // 创建锁图标
    document.querySelectorAll('#mmd-lock-icon').forEach(el => el.remove());
    const lockIcon = document.createElement('div');
    lockIcon.id = 'mmd-lock-icon';
    lockIcon.dataset.mmdLock = 'true';
    document.body.appendChild(lockIcon);
    this._mmdLockIcon = lockIcon;

    Object.assign(lockIcon.style, {
        position: 'fixed', zIndex: '99999', width: '32px', height: '32px',
        cursor: 'pointer', display: 'none',
        backgroundImage: 'url(/static/icons/unlocked_icon.png)',
        backgroundSize: 'contain', backgroundRepeat: 'no-repeat', backgroundPosition: 'center',
        pointerEvents: 'auto', transition: 'transform 0.1s'
    });

    const toggleLock = (e) => {
        if (e) { e.preventDefault(); e.stopPropagation(); }
        const currentLocked = this.isLocked;
        const newLocked = !currentLocked;
        if (this.core && typeof this.core.setLocked === 'function') {
            this.core.setLocked(newLocked);
        }
        const isLocked = this.isLocked;
        lockIcon.style.backgroundImage = isLocked ? 'url(/static/icons/locked_icon.png)' : 'url(/static/icons/unlocked_icon.png)';

        const currentTransform = lockIcon.style.transform || '';
        const baseScaleMatch = currentTransform.match(/scale\(([\d.]+)\)/);
        const baseScale = baseScaleMatch ? parseFloat(baseScaleMatch[1]) : 1.0;
        lockIcon.style.transform = `scale(${baseScale * 0.9})`;
        setTimeout(() => { lockIcon.style.transform = `scale(${baseScale})`; }, 100);

        lockIcon.style.display = shouldShowLockIcon() ? 'block' : 'none';
        applyResponsiveFloatingLayout();
    };
    lockIcon.addEventListener('mousedown', toggleLock);
    lockIcon.addEventListener('touchstart', toggleLock, { passive: false });

    const suppressionChangeHandler = () => {
        requestAnimationFrame(() => {
            applyResponsiveFloatingLayout();
            const currentLockIcon = this._mmdLockIcon;
            if (currentLockIcon && !this._isInReturnState) {
                const shouldShowLock = !!(this._shouldShowMmdLockIcon && this._shouldShowMmdLockIcon());
                currentLockIcon.style.display = shouldShowLock ? 'block' : 'none';
                currentLockIcon.style.visibility = shouldShowLock ? 'visible' : 'hidden';
                currentLockIcon.style.opacity = shouldShowLock ? '' : '0';
            }
        });
    };
    this._uiWindowHandlers.push({
        event: 'neko:yui-guide-floating-toolbar-suppression-change',
        handler: suppressionChangeHandler,
        target: window
    });
    window.addEventListener('neko:yui-guide-floating-toolbar-suppression-change', suppressionChangeHandler);

    // 启动 UI 更新循环
    this._startUIUpdateLoop();

    // 初始化后显示按钮
    setTimeout(() => {
        applyResponsiveFloatingLayout();
        if (this._mmdLockIcon) this._mmdLockIcon.style.display = shouldShowLockIcon() ? 'block' : 'none';
    }, 100);

    this._syncButtonStatesWithGlobalState();

    // 点击按钮栏/弹窗/侧面板之外的区域时，自动关闭所有弹窗
    if (this._outsideClickHandler) {
        document.removeEventListener('click', this._outsideClickHandler);
    }
    this._outsideClickHandler = (e) => {
        const path = e.composedPath ? e.composedPath() : (e.path || []);
        if (path.includes(buttonsContainer)) return;
        if (path.some(n => n && n.id && n.id.startsWith('mmd-popup-'))) return;
        if (path.some(n => n && typeof n.hasAttribute === 'function' && n.hasAttribute('data-neko-sidepanel'))) return;
        const openPopup = Array.from(document.querySelectorAll('[id^="mmd-popup-"]')).find(el =>
            getComputedStyle(el).display === 'flex');
        if (!openPopup) return;
        this.closeAllPopups();
    };
    document.addEventListener('click', this._outsideClickHandler);
    this._uiWindowHandlers = this._uiWindowHandlers || [];
    this._uiWindowHandlers.push({ event: 'click', handler: this._outsideClickHandler, target: document });

    // 通知外部浮动按钮已就绪
    window.dispatchEvent(new CustomEvent('live2d-floating-buttons-ready'));
};

/**
 * MMD UI 更新循环
 */
MMDManager.prototype._startUIUpdateLoop = function() {
    if (this._uiUpdateLoopId !== null && this._uiUpdateLoopId !== undefined) return;

    const getVisibleButtonCount = () => {
        const mobile = window.isMobileWidth && window.isMobileWidth();
        return [{ id: 'mic' }, { id: 'screen', mobileOnly: true }, { id: 'agent' }, { id: 'social' }, { id: 'settings' }, { id: 'goodbye' }]
            .filter(c => !(c.mobileOnly && !mobile))
            .filter(c => !(mobile && (c.id === 'agent' || c.id === 'goodbye'))).length;
    };
    const baseButtonSize = 48;
    const baseGap = 12;
    let lastMobileUpdate = 0;
    const MOBILE_UPDATE_INTERVAL = 100;

    // ── 锁定后淡化机制（与 VRM 侧对齐） ──
    const mmdContainer = document.getElementById('mmd-container');
    const hoverFadeThreshold = 60;
    const STATIONARY_FADE_DELAY = 1000;
    let ctrlFadeActive = false;
    let stationaryFadeActive = false;
    let isCtrlPressed = false;
    let hasEnteredHoverRange = false;
    let stationaryFadeTimer = null;

    const clearStationaryFadeTimer = () => {
        if (stationaryFadeTimer !== null) {
            clearTimeout(stationaryFadeTimer);
            stationaryFadeTimer = null;
        }
    };

    const applyFade = (forceFade) => {
        if (!mmdContainer) return;
        // 确保过渡动画已设置（仅操作 opacity，避免干扰其他属性）
        if (!mmdContainer.style.transition || mmdContainer.style.transition.indexOf('opacity') === -1) {
            mmdContainer.style.transition = 'opacity 0.3s ease';
        }
        let shouldFade = forceFade !== undefined ? forceFade : (ctrlFadeActive || stationaryFadeActive);
        if (window.lockedHoverFadeEnabled === false) shouldFade = false;
        mmdContainer.style.opacity = shouldFade ? '0.12' : '1';
    };
    this._setMmdLockedHoverFade = applyFade;

    // 监听锁定悬停淡化设置变更
    const onLockedHoverFadeChanged = () => {
        if (window.lockedHoverFadeEnabled === false) {
            ctrlFadeActive = false;
            stationaryFadeActive = false;
            applyFade();
        }
    };
    if (this._mmdLockedHoverFadeChangedListener) {
        window.removeEventListener('neko-locked-hover-fade-changed', this._mmdLockedHoverFadeChangedListener);
    }
    this._mmdLockedHoverFadeChangedListener = onLockedHoverFadeChanged;
    window.addEventListener('neko-locked-hover-fade-changed', onLockedHoverFadeChanged);

    // Ctrl 键跟踪
    const onKeyDown = (event) => {
        if (event.ctrlKey || event.metaKey) isCtrlPressed = true;
    };
    const onKeyUp = (event) => {
        if (!event.ctrlKey && !event.metaKey) {
            isCtrlPressed = false;
            ctrlFadeActive = false;
            applyFade();
        }
    };
    const onBlur = () => {
        // blur 时 Ctrl 键事件无法到达，必须主动清除 Ctrl 状态避免卡死
        isCtrlPressed = false;
        ctrlFadeActive = false;
        // 锁定状态下 blur 通常由鼠标穿透点击引起，保留静止淡化状态避免闪烁
        if (this.isLocked) {
            applyFade();
            return;
        }
        clearStationaryFadeTimer();
        if (stationaryFadeActive) {
            stationaryFadeActive = false;
        }
        applyFade();
        hasEnteredHoverRange = false;
    };

    // 清理旧的键盘 / blur 监听器
    if (this._mmdCtrlKeyDownListener) window.removeEventListener('keydown', this._mmdCtrlKeyDownListener);
    if (this._mmdCtrlKeyUpListener) window.removeEventListener('keyup', this._mmdCtrlKeyUpListener);
    if (this._mmdWindowBlurListener) window.removeEventListener('blur', this._mmdWindowBlurListener);

    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    window.addEventListener('blur', onBlur);
    this._mmdCtrlKeyDownListener = onKeyDown;
    this._mmdCtrlKeyUpListener = onKeyUp;
    this._mmdWindowBlurListener = onBlur;

    // 空闲低频模式下改用 ~33ms 定时器转一次性 rAF：一次性 rAF 不形成连续
    // vsync 链，Blink 主帧调度随渲染循环一起降到地板频率。否则本循环单独
    // 就能把 BeginMainFrame 顶回显示器刷新率，渲染侧的空闲化收益归零。
    const scheduleNext = () => {
        if (this._uiUpdateLoopId === null || this._uiUpdateLoopId === undefined) return;
        if (this.core && this.core._idleTickMode) {
            if (this._uiLoopIdleTimeout) clearTimeout(this._uiLoopIdleTimeout);
            // 周期跟随渲染 tick 频率：活动态定时器驱动（配置帧率）下按钮跟手，空闲地板下 ~33ms。
            // 定时器到点直接调 update，不再转一次性 rAF——那会再等一个 vsync，按钮比模型慢半拍
            const tickFps = Number(this.core._idleTickFps) > 0 ? Number(this.core._idleTickFps) : 30;
            this._uiLoopIdleTimeout = setTimeout(() => {
                this._uiLoopIdleTimeout = null;
                if (this._uiUpdateLoopId === null || this._uiUpdateLoopId === undefined) return;
                update();
            }, Math.max(4, Math.round(1000 / tickFps)));
            return;
        }
        this._uiUpdateLoopId = requestAnimationFrame(update);
    };

    const update = () => {
        if (this._uiUpdateLoopId === null || this._uiUpdateLoopId === undefined) return;

        if (!this.currentModel || !this.currentModel.mesh) {
            scheduleNext();
            return;
        }

        if (this._isInReturnState) {
            scheduleNext();
            return;
        }

        if (window.isMobileWidth && window.isMobileWidth()) {
            const now = performance.now();
            if (now - lastMobileUpdate < MOBILE_UPDATE_INTERVAL) {
                scheduleNext();
                return;
            }
            lastMobileUpdate = now;
        }

        const buttonsContainer = document.getElementById('mmd-floating-buttons');
        const lockIcon = this._mmdLockIcon;

        if (!this.camera || !this.renderer) {
            scheduleNext();
            return;
        }

        try {
            const renderer = this.renderer;
            const canvasRect = renderer.domElement.getBoundingClientRect();
            const canvasWidth = canvasRect.width;
            const canvasHeight = canvasRect.height;
            const isMobile = window.isMobileWidth && window.isMobileWidth();
            const modelBounds = typeof this.getModelScreenBounds === 'function'
                ? this.getModelScreenBounds()
                : null;
            if (!modelBounds) {
                if (!isMobile) {
                    if (buttonsContainer) {
                        buttonsContainer.style.display = 'none';
                        buttonsContainer.style.visibility = 'hidden';
                        buttonsContainer.style.opacity = '0';
                    }
                    if (lockIcon && !this._isInReturnState) {
                        lockIcon.style.display = 'none';
                        lockIcon.style.visibility = 'hidden';
                        lockIcon.style.opacity = '0';
                    }
                }
                scheduleNext();
                return;
            }

            const visibleLeft = Math.max(0, Math.min(canvasWidth, modelBounds.left - canvasRect.left));
            const visibleRight = Math.max(0, Math.min(canvasWidth, modelBounds.right - canvasRect.left));
            const visibleTop = Math.max(0, Math.min(canvasHeight, modelBounds.top - canvasRect.top));
            const visibleBottom = Math.max(0, Math.min(canvasHeight, modelBounds.bottom - canvasRect.top));
            const visibleHeight = Math.max(1, visibleBottom - visibleTop);

            const modelScreenHeight = visibleHeight;
            const modelCenterY = canvasRect.top + (visibleTop + visibleBottom) / 2;
            const modelCenterX = canvasRect.left + (visibleLeft + visibleRight) / 2;
            this._mmdModelCenterX = modelCenterX;
            this._mmdModelCenterY = modelCenterY;
            this._mmdModelScreenHeight = modelScreenHeight;

            const mouse = this._mmdMousePos;
            const mouseStale = !this._mmdMousePosTs || (Date.now() - this._mmdMousePosTs > 1500);
            const mouseDist = (mouse && !mouseStale) ? Math.hypot(mouse.x - modelCenterX, mouse.y - modelCenterY) : Infinity;
            const baseThreshold = Math.max(90, Math.min(260, modelScreenHeight * 0.55));

            const padX = Math.max(60, (visibleRight - visibleLeft) * 0.3);
            const padY = Math.max(40, (visibleBottom - visibleTop) * 0.2);
            const mouseInModelRegion = mouse && !mouseStale &&
                mouse.x >= canvasRect.left + visibleLeft - padX &&
                mouse.x <= canvasRect.left + visibleRight + padX &&
                mouse.y >= canvasRect.top + visibleTop - padY &&
                mouse.y <= canvasRect.top + visibleBottom + padY;

            this._mmdMouseInModelRegion = !!mouseInModelRegion;

            // ── 锁定后淡化逻辑（与 VRM 侧对齐） ──
            const isMobileDevice = (window.appUtils && typeof window.appUtils.isMobile === 'function' && window.appUtils.isMobile()) || /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
            if (!isMobileDevice && mouse && !mouseStale) {
                // 鼠标在 UI 元素（锁图标 / 浮动按钮）上时，重置淡化状态
                let isOverUi = false;
                if (lockIcon && lockIcon.style.display !== 'none') {
                    const lr = lockIcon.getBoundingClientRect();
                    if (mouse.x >= lr.left && mouse.x <= lr.right && mouse.y >= lr.top && mouse.y <= lr.bottom) isOverUi = true;
                }
                if (!isOverUi && buttonsContainer && buttonsContainer.style.display !== 'none') {
                    const br = buttonsContainer.getBoundingClientRect();
                    if (mouse.x >= br.left && mouse.x <= br.right && mouse.y >= br.top && mouse.y <= br.bottom) isOverUi = true;
                }
                if (isOverUi) {
                    clearStationaryFadeTimer();
                    ctrlFadeActive = false;
                    stationaryFadeActive = false;
                    hasEnteredHoverRange = false;
                    applyFade();
                }

                // 计算鼠标到模型屏幕包围盒的距离
                const sMinX = canvasRect.left + visibleLeft;
                const sMaxX = canvasRect.left + visibleRight;
                const sMinY = canvasRect.top + visibleTop;
                const sMaxY = canvasRect.top + visibleBottom;
                const dx = Math.max(sMinX - mouse.x, 0, mouse.x - sMaxX);
                const dy = Math.max(sMinY - mouse.y, 0, mouse.y - sMaxY);
                const distToModel = Math.sqrt(dx * dx + dy * dy);
                const isNearModel = distToModel < hoverFadeThreshold;

                // 静止自动淡化：锁定 + 鼠标在模型范围内静止 1 秒 → 变淡
                if (this.isLocked && isNearModel && !isOverUi) {
                    if (!hasEnteredHoverRange) {
                        hasEnteredHoverRange = true;
                        if (stationaryFadeTimer === null && !stationaryFadeActive) {
                            stationaryFadeTimer = setTimeout(() => {
                                stationaryFadeTimer = null;
                                stationaryFadeActive = true;
                                applyFade();
                            }, STATIONARY_FADE_DELAY);
                        }
                    }
                } else {
                    if (stationaryFadeTimer !== null || stationaryFadeActive) {
                        clearStationaryFadeTimer();
                        stationaryFadeActive = false;
                        applyFade();
                    }
                    hasEnteredHoverRange = false;
                }

                // Ctrl 淡化：锁定 + Ctrl + 在模型范围内（UI 上时跳过）
                ctrlFadeActive = this.isLocked && isCtrlPressed && isNearModel && !isOverUi;
                applyFade();
            }

            const showThreshold = baseThreshold;
            const hideThreshold = baseThreshold * 1.2;
            if (this._mmdUiNearModel !== true && (mouseDist <= showThreshold || mouseInModelRegion)) {
                this._mmdUiNearModel = true;
            } else if (this._mmdUiNearModel !== false && mouseDist >= hideThreshold && !mouseInModelRegion) {
                this._mmdUiNearModel = false;
            } else if (typeof this._mmdUiNearModel !== 'boolean') {
                this._mmdUiNearModel = false;
            }

            const visibleCount = getVisibleButtonCount();
            const baseToolbarHeight = baseButtonSize * visibleCount + baseGap * (visibleCount - 1);
            const targetToolbarHeight = modelScreenHeight / 2;
            const scale = Math.max(0.5, Math.min(1.0, targetToolbarHeight / baseToolbarHeight));

            if (buttonsContainer) {
                if (isMobile) {
                    buttonsContainer.style.transformOrigin = 'right bottom';
                    const suppressed = isYuiGuideFloatingToolbarSuppressed();
                    buttonsContainer.style.visibility = suppressed ? 'hidden' : 'visible';
                    buttonsContainer.style.opacity = suppressed ? '0' : '1';
                    buttonsContainer.style.display = suppressed || this.isLocked ? 'none' : 'flex';
                    if (lockIcon && !this._isInReturnState) {
                        const shouldShowLock = !!(this._shouldShowMmdLockIcon && this._shouldShowMmdLockIcon());
                        lockIcon.style.display = shouldShowLock ? 'block' : 'none';
                        lockIcon.style.visibility = shouldShowLock ? 'visible' : 'hidden';
                        lockIcon.style.opacity = shouldShowLock ? '' : '0';
                    }
                } else {
                    buttonsContainer.style.transformOrigin = 'left top';
                    const screenWidth = window.innerWidth;
                    const screenHeight = window.innerHeight;
                    const targetX = canvasRect.left + visibleRight * 0.8 + visibleLeft * 0.2;
                    const actualToolbarHeight = baseToolbarHeight * scale;
                    const actualToolbarWidth = 80 * scale;
                    const offsetY = Math.min(modelScreenHeight * 0.1, screenHeight * 0.08);
                    const targetY = modelCenterY - actualToolbarHeight / 2 - offsetY;
                    const boundedY = Math.max(20, Math.min(targetY, screenHeight - actualToolbarHeight - 20));
                    const boundedX = Math.max(0, Math.min(targetX, screenWidth - actualToolbarWidth));

                    const rawLeft = parseFloat(buttonsContainer.style.left);
                    if (this._snapUIPosition || Number.isNaN(rawLeft)) {
                        buttonsContainer.style.left = `${boundedX}px`;
                        buttonsContainer.style.top = `${boundedY}px`;
                        this._snapUIPosition = false;
                    } else {
                        const currentTop = parseFloat(buttonsContainer.style.top) || boundedY;
                        const dist = Math.sqrt(Math.pow(boundedX - rawLeft, 2) + Math.pow(boundedY - currentTop, 2));
                        if (dist > 0.5) {
                            const lerpFactor = 0.15;
                            buttonsContainer.style.left = `${rawLeft + (boundedX - rawLeft) * lerpFactor}px`;
                            buttonsContainer.style.top = `${currentTop + (boundedY - currentTop) * lerpFactor}px`;
                        }
                    }

                    const isLocked = this.isLocked;
                    const hoveringButtons = this._mmdButtonsHovered === true;
                    const popupUi = window.AvatarPopupUI || null;
                    const isFallbackOverlayVisible = (element) => {
                        if (!element) return false;
                        const style = window.getComputedStyle(element);
                        const opacity = Number.parseFloat(style.opacity || '1');
                        if (style.display === 'none' || style.visibility === 'hidden' || opacity <= 0) return false;

                        const rect = element.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) return false;

                        return rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth;
                    };
                    const hasOpenOverlay = popupUi && typeof popupUi.hasVisibleOverlay === 'function'
                        ? popupUi.hasVisibleOverlay('mmd')
                        : Array.from(document.querySelectorAll('[id^="mmd-popup-"], [data-neko-sidepanel-owner^="mmd-popup-"]'))
                            .some(isFallbackOverlayVisible);
                    const inTutorial = buttonsContainer.dataset.inTutorial === 'true' || window.isInTutorial === true;
                    const isUiPositionReady =
                        Number.isFinite(parseFloat(buttonsContainer.style.left)) &&
                        Number.isFinite(parseFloat(buttonsContainer.style.top)) &&
                        !this._snapUIPosition;
                    const shouldShowButtons = !isYuiGuideFloatingToolbarSuppressed() && isUiPositionReady &&
                        (inTutorial || (!isLocked && (this._mmdUiNearModel || hoveringButtons || hasOpenOverlay)));
                    buttonsContainer.style.display = shouldShowButtons ? 'flex' : 'none';
                    buttonsContainer.style.visibility = shouldShowButtons ? 'visible' : 'hidden';
                    buttonsContainer.style.opacity = shouldShowButtons ? '1' : '0';

                    if (lockIcon && !this._isInReturnState) {
                        const lockTargetX = canvasRect.left + visibleRight * 0.7 + visibleLeft * 0.3;
                        const lockTargetY = canvasRect.top + visibleTop * 0.3 + visibleBottom * 0.7;

                        lockIcon.style.transformOrigin = 'center center';
                        lockIcon.style.transform = `scale(${scale})`;

                        const baseLockIconSize = 32;
                        const actualLockIconSize = baseLockIconSize * scale;
                        const maxLockX = screenWidth - actualLockIconSize;
                        const defaultMaxLockY = screenHeight - actualLockIconSize - 20;
                        const maxLockY = typeof window.getNekoYuiGuideLockIconMaxTop === 'function'
                            ? window.getNekoYuiGuideLockIconMaxTop(defaultMaxLockY, actualLockIconSize)
                            : defaultMaxLockY;
                        const boundedLockX = Math.max(0, Math.min(lockTargetX, maxLockX));
                        const minLockY = Math.min(20, maxLockY);
                        const boundedLockY = Math.max(minLockY, Math.min(lockTargetY, maxLockY));

                        const rawLockLeft = parseFloat(lockIcon.style.left);
                        if (Number.isNaN(rawLockLeft)) {
                            lockIcon.style.left = `${boundedLockX}px`;
                            lockIcon.style.top = `${boundedLockY}px`;
                        } else {
                            const currentLockTop = parseFloat(lockIcon.style.top) || boundedLockY;
                            const lockDist = Math.sqrt(Math.pow(boundedLockX - rawLockLeft, 2) + Math.pow(boundedLockY - currentLockTop, 2));
                            if (lockDist > 0.5) {
                                const lerpFactor = 0.15;
                                lockIcon.style.left = `${rawLockLeft + (boundedLockX - rawLockLeft) * lerpFactor}px`;
                                lockIcon.style.top = `${currentLockTop + (boundedLockY - currentLockTop) * lerpFactor}px`;
                            }
                        }
                        const shouldShowLock = (this._shouldShowMmdLockIcon && this._shouldShowMmdLockIcon()) &&
                            Number.isFinite(parseFloat(lockIcon.style.left)) &&
                            Number.isFinite(parseFloat(lockIcon.style.top));
                        lockIcon.style.display = shouldShowLock ? 'block' : 'none';
                        lockIcon.style.visibility = shouldShowLock ? 'visible' : 'hidden';
                        lockIcon.style.opacity = shouldShowLock ? '' : '0';

                    }
                }
                if (lockIcon) {
                    const isLockVisible = !this._isInReturnState &&
                        lockIcon.style.display !== 'none' && lockIcon.style.visibility !== 'hidden';
                    if (isLockVisible) {
                        const lockRect = lockIcon.getBoundingClientRect();
                        const popupUi = window.AvatarPopupUI || null;
                        const isLockOverlapped = popupUi && typeof popupUi.isRectOverlappedByVisibleOverlay === 'function'
                            ? popupUi.isRectOverlappedByVisibleOverlay(lockRect, 'mmd')
                            : Array.from(document.querySelectorAll('[id^="mmd-popup-"], [data-neko-sidepanel-owner^="mmd-popup-"]')).some(element => {
                                const style = window.getComputedStyle(element);
                                const computedOpacity = Number.parseFloat(style.opacity || '1');
                                const targetOpacity = Number.parseFloat(element.style.opacity || style.opacity || '1');
                                if (style.display === 'none' || style.visibility === 'hidden' ||
                                    (computedOpacity <= 0 && targetOpacity <= 0)) return false;
                                const overlayRect = element.getBoundingClientRect();
                                return lockRect.right > overlayRect.left && lockRect.left < overlayRect.right &&
                                    lockRect.bottom > overlayRect.top && lockRect.top < overlayRect.bottom;
                            });
                        // 与角色形象半透明状态完全同步：容器淡化(opacity<1)时锁图标镜像同一透明度
                        const mmdFadeContainer = document.getElementById('mmd-container');
                        const mmdFadeOpacity = mmdFadeContainer ? parseFloat(mmdFadeContainer.style.opacity) : NaN;
                        lockIcon.style.opacity = (Number.isFinite(mmdFadeOpacity) && mmdFadeOpacity < 1)
                            ? String(mmdFadeOpacity)
                            : (isLockOverlapped ? '0.3' : '');
                        lockIcon.style.pointerEvents = isLockOverlapped ? 'none' : 'auto';
                    } else {
                        lockIcon.style.pointerEvents = 'none';
                    }
                }
                buttonsContainer.style.transform = `scale(${scale})`;
            }
        } catch (error) {
            if (window.DEBUG_MODE) console.debug('[MMD UI] 更新循环单帧异常:', error);
        }

        scheduleNext();
    };

    this._updateFloatingButtonsPositionNow = () => {
        if (this._uiUpdateLoopId === null || this._uiUpdateLoopId === undefined) return;
        cancelAnimationFrame(this._uiUpdateLoopId);
        // 同步 flush 前清掉空闲模式的 pending 再入定时器，防止 update() 内的
        // scheduleNext 与旧定时器分叉出两条更新链
        if (this._uiLoopIdleTimeout) {
            clearTimeout(this._uiLoopIdleTimeout);
            this._uiLoopIdleTimeout = null;
        }
        this._uiUpdateLoopId = 0;
        update();
    };
    this._uiUpdateLoopId = requestAnimationFrame(update);
};

/**
 * 将屏幕像素偏移量应用到 MMD 模型的世界坐标
 * 用于"请她回来"按钮被拖拽后，模型跟随出现在新位置
 */
MMDManager.prototype.applyScreenDelta = function(screenDx, screenDy, options = {}) {
    const mesh = this.currentModel && this.currentModel.mesh;
    if (!mesh || !this.camera || !this.renderer) return;

    const camera = this.camera;

    // canvas 在 goodbye 状态下被 display:none 隐藏，getBoundingClientRect 全为 0
    const canvasRect = this.renderer.domElement.getBoundingClientRect();
    const viewWidth = canvasRect.width > 0 ? canvasRect.width : window.innerWidth;
    const viewHeight = canvasRect.height > 0 ? canvasRect.height : window.innerHeight;
    if (viewWidth <= 0 || viewHeight <= 0) return;

    const cameraDistance = camera.position.distanceTo(mesh.position);
    if (cameraDistance < 0.001) return;

    const fov = camera.fov * (Math.PI / 180);
    const worldHeight = 2 * Math.tan(fov / 2) * cameraDistance;
    const worldWidth = worldHeight * camera.aspect;

    const pixelToWorldX = worldWidth / viewWidth;
    const pixelToWorldY = worldHeight / viewHeight;

    const right = new window.THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion);
    const up = new window.THREE.Vector3(0, 1, 0).applyQuaternion(camera.quaternion);

    mesh.position.add(right.clone().multiplyScalar(screenDx * pixelToWorldX));
    mesh.position.add(up.clone().multiplyScalar(-screenDy * pixelToWorldY));

    if (options.clamp !== false && this.interaction && typeof this.interaction.clampModelPosition === 'function') {
        const clamped = this.interaction.clampModelPosition(mesh.position.clone());
        if (clamped && clamped.isVector3) mesh.position.copy(clamped);
    }
};
