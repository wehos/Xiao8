/**
 * app-interpage/bootstrap-resources-and-model-reload.js
 * Inter-page / cross-tab communication.
 *
 * Handles BroadcastChannel dispatch, postMessage listeners, model hot-reload, UI commands, and overlay cleanup.
 * Dependencies loaded before these parts:
 * - app-state.js -> window.appState, window.appConst
 * Runtime dependencies available by the time handlers fire:
 * - window.showStatusToast
 * - window.stopMicCapture / window.clearAudioQueue
 * - window.live2dManager / window.vrmManager
 * - initLive2DModel / initVRMModel globals
 * Load all parts in filename order; this is a classic global script (no import/export).
 */
(function () {
    'use strict';

    window.appInterpage = window.appInterpage || {};
    const I = window.__appInterpageParts || (window.__appInterpageParts = {});
I.mod = window.appInterpage;
    I.S = window.appState;
    // const C = window.appConst;  // not used in this module currently
    const MAIN_UI_HIDDEN_BY_MODEL_MANAGER_KEY = '__NEKO_MAIN_UI_HIDDEN_BY_MODEL_MANAGER';
    I.ICEBREAKER_BRIDGE_STORAGE_KEY = 'neko_new_user_icebreaker_bridge_event';
    I.YUI_GUIDE_CHAT_BRIDGE_QUEUE_KEY = 'neko_yui_guide_chat_bridge_queue_v1';

    // =====================================================================
    // Message deduplication (BC + postMessage deliver the same message twice)
    // =====================================================================
    var _processedMsgKeys = {};
    I.CROSS_WINDOW_IDLE_ACTIVITY_MIN_INTERVAL_MS = 250;
    I._lastCrossWindowIdleActivityAt = 0;
    I.yuiGuideTargetGeometryRegistry = null;
    I.yuiGuideBridgeCommandBus = null;

    I.createAppInterpageScopedResources = function createAppInterpageScopedResources() {
        if (
            window.YuiGuideCommon
            && typeof window.YuiGuideCommon.createScopedTutorialResources === 'function'
        ) {
            try {
                return window.YuiGuideCommon.createScopedTutorialResources({ window: window });
            } catch (error) {
                console.warn('[YuiGuide] scoped resource helper unavailable; using local fallback:', error);
            }
        }

        var destroyed = false;
        var timers = [];
        var intervals = [];
        var listeners = [];

        function removeFrom(list, value) {
            var index = list.indexOf(value);
            if (index !== -1) {
                list.splice(index, 1);
            }
        }

        function addEventListener(target, type, handler, options) {
            if (destroyed || !target || typeof target.addEventListener !== 'function') {
                return null;
            }
            target.addEventListener(type, handler, options);
            listeners.push({
                target: target,
                type: type,
                handler: handler,
                options: options
            });
            return handler;
        }

        function setScopedTimeout(callback, delayMs) {
            if (destroyed || typeof window.setTimeout !== 'function') {
                return 0;
            }
            var timerId = window.setTimeout(function scopedTimeoutCallback() {
                removeFrom(timers, timerId);
                if (typeof callback === 'function') {
                    callback();
                }
            }, delayMs);
            timers.push(timerId);
            return timerId;
        }

        function clearScopedTimeout(timerId) {
            if (!timerId || typeof window.clearTimeout !== 'function') {
                return;
            }
            removeFrom(timers, timerId);
            window.clearTimeout(timerId);
        }

        function setScopedInterval(callback, delayMs) {
            if (destroyed || typeof window.setInterval !== 'function') {
                return 0;
            }
            var intervalId = window.setInterval(function scopedIntervalCallback() {
                if (typeof callback === 'function') {
                    callback();
                }
            }, delayMs);
            intervals.push(intervalId);
            return intervalId;
        }

        function clearScopedInterval(intervalId) {
            if (!intervalId || typeof window.clearInterval !== 'function') {
                return;
            }
            removeFrom(intervals, intervalId);
            window.clearInterval(intervalId);
        }

        function destroy() {
            destroyed = true;
            timers.slice().forEach(clearScopedTimeout);
            intervals.slice().forEach(clearScopedInterval);
            listeners.splice(0).forEach(function (entry) {
                try {
                    entry.target.removeEventListener(entry.type, entry.handler, entry.options);
                } catch (_) {}
            });
        }

        return {
            addEventListener: addEventListener,
            setTimeout: setScopedTimeout,
            clearTimeout: clearScopedTimeout,
            setInterval: setScopedInterval,
            clearInterval: clearScopedInterval,
            destroy: destroy,
            isDestroyed: function () { return destroyed; }
        };
    }

    I.yuiGuideInterpageResources = I.createAppInterpageScopedResources();
    I.yuiGuideChatSpotlightResources = I.createAppInterpageScopedResources();

    /**
     * Returns true if this action+timestamp+optional lifecycle sequence was
     * already processed (duplicate).
     * First call for a given key returns false and registers it.
     */
    I.isDuplicateMessage = function isDuplicateMessage(action, timestamp, lifecycleSequence) {
        if (!timestamp) return false;  // no timestamp → cannot deduplicate
        var key = action + '_' + timestamp;
        var sequence = Number(lifecycleSequence);
        if (Number.isSafeInteger(sequence) && sequence > 0) {
            key += '_' + sequence;
        }
        if (_processedMsgKeys[key]) return true;
        _processedMsgKeys[key] = true;
        setTimeout(function () { delete _processedMsgKeys[key]; }, 5000);
        return false;
    }

    I.getYuiGuideBridgeMessageTimestamp = function getYuiGuideBridgeMessageTimestamp(message) {
        var timestamp = Number(message && message.timestamp);
        return Number.isFinite(timestamp) && timestamp > 0 ? timestamp : Date.now();
    }

    I.shouldBypassYuiGuideMessageDedup = function shouldBypassYuiGuideMessageDedup(action, message) {
        return (message && message.bypassDedup === true)
            || action === 'yui_guide_set_chat_spotlight'
            || action === 'yui_guide_set_chat_cursor'
            || action === 'yui_guide_rotate_compact_tool_wheel'
            || action === 'yui_guide_set_chat_buttons_disabled'
            || action === 'yui_guide_set_chat_input_locked'
            || action === 'yui_guide_set_compact_chat_fixed_layout'
            || action === 'yui_guide_set_compact_history_open'
            || action === 'yui_guide_set_avatar_tool_menu_open'
            || action === 'yui_guide_set_compact_tool_fan_open'
            || action === 'yui_guide_set_compact_tool_wheel_index';
    }

    I.isHighVolumeBroadcastChannelAction = function isHighVolumeBroadcastChannelAction(action) {
        return action === 'model_manager_window_state'
            || action === 'idle_return_ball_state'
            || action === 'idle_chat_minimized_state'
            || action === 'idle_chat_compact_surface_state'
            || action === 'idle_cat1_compact_mirror_state'
            || action === 'idle_chat_pair_move_bounds';
    }

    I.isMainUIHiddenByModelManager = function isMainUIHiddenByModelManager() {
        return window[MAIN_UI_HIDDEN_BY_MODEL_MANAGER_KEY] === true;
    }

    function ensureMainUIHiddenStyle() {
        if (document.getElementById('neko-main-ui-hidden-by-model-manager-style')) return;
        var style = document.createElement('style');
        style.id = 'neko-main-ui-hidden-by-model-manager-style';
        style.textContent = [
            'body.neko-main-ui-hidden-by-model-manager #live2d-container,',
            'body.neko-main-ui-hidden-by-model-manager #vrm-container,',
            'body.neko-main-ui-hidden-by-model-manager #mmd-container,',
            'body.neko-main-ui-hidden-by-model-manager #pngtuber-container,',
            'body.neko-main-ui-hidden-by-model-manager #pngtuber-container .pngtuber-image,',
            'body.neko-main-ui-hidden-by-model-manager #live2d-canvas,',
            'body.neko-main-ui-hidden-by-model-manager #vrm-canvas,',
            'body.neko-main-ui-hidden-by-model-manager #mmd-canvas,',
            'body.neko-main-ui-hidden-by-model-manager #react-chat-window-overlay,',
            'body.neko-main-ui-hidden-by-model-manager #live2d-floating-buttons,',
            'body.neko-main-ui-hidden-by-model-manager #vrm-floating-buttons,',
            'body.neko-main-ui-hidden-by-model-manager #mmd-floating-buttons,',
            'body.neko-main-ui-hidden-by-model-manager #pngtuber-floating-buttons,',
            'body.neko-main-ui-hidden-by-model-manager #live2d-lock-icon,',
            'body.neko-main-ui-hidden-by-model-manager #vrm-lock-icon,',
            'body.neko-main-ui-hidden-by-model-manager #mmd-lock-icon,',
            'body.neko-main-ui-hidden-by-model-manager #pngtuber-lock-icon,',
            'body.neko-main-ui-hidden-by-model-manager #live2d-return-button-container,',
            'body.neko-main-ui-hidden-by-model-manager #vrm-return-button-container,',
            'body.neko-main-ui-hidden-by-model-manager #mmd-return-button-container,',
            'body.neko-main-ui-hidden-by-model-manager #pngtuber-return-button-container {',
            '  display: none !important;',
            '  visibility: hidden !important;',
            '  pointer-events: none !important;',
            '}'
        ].join('\n');
        (document.head || document.documentElement).appendChild(style);
    }

    function setMainUIHiddenByModelManager(hidden) {
        window[MAIN_UI_HIDDEN_BY_MODEL_MANAGER_KEY] = !!hidden;
        ensureMainUIHiddenStyle();
        if (document.body) {
            document.body.classList.toggle('neko-main-ui-hidden-by-model-manager', !!hidden);
        }
        try {
            window.dispatchEvent(new CustomEvent('neko:main-ui-hidden-by-model-manager-changed', {
                detail: { hidden: !!hidden }
            }));
        } catch (_) {}
    }

    function ensureModelManagerOverlapHiddenStyle() {
        if (document.getElementById('neko-model-hidden-by-manager-overlap-style')) return;
        var style = document.createElement('style');
        style.id = 'neko-model-hidden-by-manager-overlap-style';
        style.textContent = [
            'body.neko-model-hidden-by-manager-overlap #live2d-container,',
            'body.neko-model-hidden-by-manager-overlap #vrm-container,',
            'body.neko-model-hidden-by-manager-overlap #mmd-container,',
            'body.neko-model-hidden-by-manager-overlap #pngtuber-container,',
            'body.neko-model-hidden-by-manager-overlap #pngtuber-container .pngtuber-image,',
            'body.neko-model-hidden-by-manager-overlap #live2d-canvas,',
            'body.neko-model-hidden-by-manager-overlap #vrm-canvas,',
            'body.neko-model-hidden-by-manager-overlap #mmd-canvas {',
            '  visibility: hidden !important;',
            '  pointer-events: none !important;',
            '}'
        ].join('\n');
        (document.head || document.documentElement).appendChild(style);
    }

    function setModelManagerOverlapModelHidden(hidden) {
        ensureModelManagerOverlapHiddenStyle();
        if (document.body) {
            document.body.classList.toggle('neko-model-hidden-by-manager-overlap', !!hidden);
        }
    }

    function invalidateModelManagerOverlapBounds() {
        modelManagerCachedModelClientBounds = null;
        if (modelManagerOverlapHidden) {
            modelManagerOverlapHidden = false;
            setModelManagerOverlapModelHidden(false);
        }
        scheduleModelManagerWindowOverlapRefresh();
    }

    // Browser/dev fallback for model-manager overlap tracking. Electron uses
    // BrowserWindow bounds in the main process; regular web popups report their
    // outer screen rect through the existing inter-page channel instead.
    var MODEL_MANAGER_WINDOW_STATE_TTL_MS = 1200;
    var modelManagerWindowStates = Object.create(null);
    var modelManagerCachedModelClientBounds = null;
    var modelManagerOverlapHidden = false;
    var modelManagerOverlapRefreshTimer = 0;
    var mainUIHideOwners = Object.create(null);
    var deferredMainUIShowAfterModelReload = null;
    var activeMMDReloadCanvasSessionId = '';

    function getMainUIHideOwner(options) {
        return String((options && (options.owner || options.reason)) || 'legacy-main-ui');
    }

    function hasMainUIHideOwner() {
        return Object.keys(mainUIHideOwners).length > 0;
    }

    function normalizeModelManagerScreenRect(rect) {
        if (!rect || typeof rect !== 'object') return null;
        var x = Number(rect.x);
        var y = Number(rect.y);
        var width = Number(rect.width);
        var height = Number(rect.height);
        if (!Number.isFinite(x) || !Number.isFinite(y) ||
            !Number.isFinite(width) || !Number.isFinite(height) ||
            width <= 0 || height <= 0) {
            return null;
        }
        return { x: x, y: y, width: width, height: height };
    }

    function modelManagerRectFullyCovers(coverRect, targetRect) {
        var cover = normalizeModelManagerScreenRect(coverRect);
        var target = normalizeModelManagerScreenRect(targetRect);
        if (!cover || !target) return false;
        var tolerance = 1;
        return target.x >= cover.x - tolerance &&
            target.y >= cover.y - tolerance &&
            target.x + target.width <= cover.x + cover.width + tolerance &&
            target.y + target.height <= cover.y + cover.height + tolerance;
    }

    function isVisibleModelManagerElement(element) {
        if (!element) return false;
        try {
            var style = window.getComputedStyle(element);
            return style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                parseFloat(style.opacity || '1') !== 0;
        } catch (_) {
            return false;
        }
    }

    function normalizeModelManagerClientRect(rect) {
        if (!rect) return null;
        var left = Number(rect.left ?? rect.x ?? rect.minX);
        var top = Number(rect.top ?? rect.y ?? rect.minY);
        var right = Number(rect.right ?? rect.maxX);
        var bottom = Number(rect.bottom ?? rect.maxY);
        var width = Number(rect.width);
        var height = Number(rect.height);
        if (!Number.isFinite(width) && Number.isFinite(left) && Number.isFinite(right)) width = right - left;
        if (!Number.isFinite(height) && Number.isFinite(top) && Number.isFinite(bottom)) height = bottom - top;
        if (!Number.isFinite(left) || !Number.isFinite(top) ||
            !Number.isFinite(width) || !Number.isFinite(height) ||
            width <= 0 || height <= 0) {
            return null;
        }
        return { x: left, y: top, width: width, height: height };
    }

    function clipModelManagerClientRectToViewport(rect) {
        var normalized = normalizeModelManagerClientRect(rect);
        if (!normalized) return null;
        var viewportWidth = Number(window.innerWidth);
        var viewportHeight = Number(window.innerHeight);
        if (!Number.isFinite(viewportWidth) || !Number.isFinite(viewportHeight) ||
            viewportWidth <= 0 || viewportHeight <= 0) {
            return normalized;
        }
        var left = Math.max(0, normalized.x);
        var top = Math.max(0, normalized.y);
        var right = Math.min(viewportWidth, normalized.x + normalized.width);
        var bottom = Math.min(viewportHeight, normalized.y + normalized.height);
        if (right <= left || bottom <= top) return null;
        return { x: left, y: top, width: right - left, height: bottom - top };
    }

    function getModelManagerPngTuberClientRect(allowHidden) {
        var selectors = [
            '#pngtuber-canvas',
            '#pngtuber-container img.pngtuber-image',
            '#pngtuber-container canvas.pngtuber-image',
            '#pngtuber-container .pngtuber-image',
            '.pngtuber-image',
            '[data-pngtuber-interactive]'
        ];
        var union = null;
        try {
            document.querySelectorAll(selectors.join(', ')).forEach(function (element) {
                if (!allowHidden && !isVisibleModelManagerElement(element)) return;
                var rect = normalizeModelManagerClientRect(element.getBoundingClientRect());
                if (!rect) return;
                if (!union) {
                    union = rect;
                    return;
                }
                var left = Math.min(union.x, rect.x);
                var top = Math.min(union.y, rect.y);
                var right = Math.max(union.x + union.width, rect.x + rect.width);
                var bottom = Math.max(union.y + union.height, rect.y + rect.height);
                union = { x: left, y: top, width: right - left, height: bottom - top };
            });
        } catch (_) {}
        return union;
    }

    function getModelManagerActiveModelClientRect(allowHidden) {
        try {
            var configuredModelType = String(window.lanlan_config?.model_type || 'live2d').toLowerCase();
            var activeModelType = configuredModelType;
            if (configuredModelType === 'live3d') {
                activeModelType = String(window.lanlan_config?.live3d_sub_type || 'vrm').toLowerCase() === 'mmd'
                    ? 'mmd'
                    : 'vrm';
            }

            var live2dContainer = document.getElementById('live2d-container');
            if (activeModelType === 'live2d' &&
                (allowHidden || isVisibleModelManagerElement(live2dContainer)) &&
                window.live2dManager) {
                var live2dModel = window.live2dManager.getCurrentModel && window.live2dManager.getCurrentModel();
                if (live2dModel && typeof live2dModel.getBounds === 'function') {
                    var live2dBounds = normalizeModelManagerClientRect(live2dModel.getBounds());
                    if (live2dBounds) return live2dBounds;
                }
            }

            var vrmContainer = document.getElementById('vrm-container');
            var vrmInteraction = window.vrmManager && window.vrmManager.interaction;
            if (activeModelType === 'vrm' &&
                (allowHidden || isVisibleModelManagerElement(vrmContainer)) &&
                vrmInteraction) {
                if (typeof vrmInteraction.updateModelBoundsCache === 'function') vrmInteraction.updateModelBoundsCache();
                var vrmBounds = normalizeModelManagerClientRect(vrmInteraction._cachedScreenBounds);
                if (vrmBounds) return vrmBounds;
            }

            var mmdContainer = document.getElementById('mmd-container');
            var mmdInteraction = window.mmdManager && window.mmdManager.interaction;
            if (activeModelType === 'mmd' &&
                (allowHidden || isVisibleModelManagerElement(mmdContainer)) &&
                mmdInteraction) {
                if (typeof mmdInteraction.updateModelBoundsCache === 'function') mmdInteraction.updateModelBoundsCache();
                else if (typeof mmdInteraction.updateScreenBounds === 'function') mmdInteraction.updateScreenBounds();
                var mmdBounds = normalizeModelManagerClientRect(mmdInteraction._cachedScreenBounds);
                if (mmdBounds) return mmdBounds;
            }

            var pngtuberContainer = document.getElementById('pngtuber-container');
            if (activeModelType === 'pngtuber' &&
                (allowHidden || isVisibleModelManagerElement(pngtuberContainer))) {
                return getModelManagerPngTuberClientRect(allowHidden);
            }
        } catch (_) {}
        return null;
    }

    function getModelManagerBrowserContentScreenOrigin() {
        var outerX = Number(window.screenX) || 0;
        var outerY = Number(window.screenY) || 0;
        var horizontalFrame = Math.max(0, (Number(window.outerWidth) - Number(window.innerWidth)) / 2) || 0;
        var verticalFrame = Math.max(0, Number(window.outerHeight) - Number(window.innerHeight) - horizontalFrame) || 0;
        return { x: outerX + horizontalFrame, y: outerY + verticalFrame };
    }

    function getModelManagerActiveModelScreenRect() {
        var clientRect = getModelManagerActiveModelClientRect(modelManagerOverlapHidden);
        if (clientRect) modelManagerCachedModelClientBounds = clientRect;
        else clientRect = modelManagerCachedModelClientBounds;
        if (!clientRect) return null;
        clientRect = clipModelManagerClientRectToViewport(clientRect);
        if (!clientRect) return null;
        var origin = getModelManagerBrowserContentScreenOrigin();
        return {
            x: origin.x + clientRect.x,
            y: origin.y + clientRect.y,
            width: clientRect.width,
            height: clientRect.height
        };
    }

    function refreshModelManagerWindowOverlap() {
        if (!_isModelHostPage()) return false;
        var now = Date.now();
        var visibleModelManagerStates = [];
        Object.keys(modelManagerWindowStates).forEach(function (instanceId) {
            var state = modelManagerWindowStates[instanceId];
            if (!state || now - state.updatedAt > MODEL_MANAGER_WINDOW_STATE_TTL_MS) {
                delete modelManagerWindowStates[instanceId];
                return;
            }
            if (state.active && state.visible && state.bounds) {
                visibleModelManagerStates.push(state);
            }
        });
        if (!visibleModelManagerStates.length) {
            modelManagerCachedModelClientBounds = null;
            if (!modelManagerOverlapHidden) return false;
            modelManagerOverlapHidden = false;
            setModelManagerOverlapModelHidden(false);
            return false;
        }
        var modelBounds = getModelManagerActiveModelScreenRect();
        var shouldHide = visibleModelManagerStates.some(function (state) {
            return modelManagerRectFullyCovers(state.bounds, modelBounds);
        });
        if (shouldHide === modelManagerOverlapHidden) return shouldHide;
        modelManagerOverlapHidden = shouldHide;
        setModelManagerOverlapModelHidden(shouldHide);
        if (!shouldHide) modelManagerCachedModelClientBounds = null;
        return shouldHide;
    }

    function scheduleModelManagerWindowOverlapRefresh() {
        if (modelManagerOverlapRefreshTimer) return;
        modelManagerOverlapRefreshTimer = I.yuiGuideInterpageResources.setTimeout(function () {
            modelManagerOverlapRefreshTimer = 0;
            refreshModelManagerWindowOverlap();
        }, 100);
    }

    I.handleModelManagerWindowState = function handleModelManagerWindowState(message) {
        if (!_isModelHostPage() || !message) return;
        var instanceId = String(message.instanceId || 'model-manager-window');
        if (message.active === false) {
            delete modelManagerWindowStates[instanceId];
        } else {
            modelManagerWindowStates[instanceId] = {
                active: true,
                visible: message.visible !== false,
                bounds: normalizeModelManagerScreenRect(message.bounds),
                updatedAt: Date.now()
            };
        }
        scheduleModelManagerWindowOverlapRefresh();
    };

    if (_isModelHostPage()) {
        I.yuiGuideInterpageResources.setInterval(refreshModelManagerWindowOverlap, 500);
    }

    I.applyTutorialChatIdentityOverride = function applyTutorialChatIdentityOverride(payload) {
        var detail = payload || {};
        if (detail.active) {
            window.__NEKO_TUTORIAL_CHAT_IDENTITY_OVERRIDE__ = {
                active: true,
                displayName: detail.displayName || 'YUI',
                avatarDataUrl: detail.avatarDataUrl || '',
                modelType: detail.modelType || ''
            };
            window.__NEKO_TUTORIAL_ASSISTANT_NAME_OVERRIDE__ = detail.displayName || 'YUI';
            if (window.appChatAvatar && typeof window.appChatAvatar.setTutorialAvatarOverride === 'function') {
                window.appChatAvatar.setTutorialAvatarOverride(detail.avatarDataUrl || '', detail.modelType || '');
            } else {
                window.__nekoPendingTutorialChatIdentity = {
                    active: true,
                    avatarDataUrl: detail.avatarDataUrl || '',
                    modelType: detail.modelType || ''
                };
            }
        } else {
            delete window.__NEKO_TUTORIAL_CHAT_IDENTITY_OVERRIDE__;
            delete window.__NEKO_TUTORIAL_ASSISTANT_NAME_OVERRIDE__;
            if (window.appChatAvatar && typeof window.appChatAvatar.clearTutorialAvatarOverride === 'function') {
                window.appChatAvatar.clearTutorialAvatarOverride();
            } else {
                window.__nekoPendingTutorialChatIdentity = { active: false };
            }
        }
        window.dispatchEvent(new CustomEvent('neko:tutorial-chat-identity-changed', {
            detail: {
                active: !!detail.active,
                displayName: detail.displayName || '',
                avatarDataUrl: detail.avatarDataUrl || '',
                modelType: detail.modelType || ''
            }
        }));
    }

    // =====================================================================
    // Overlay cleanup helpers
    // =====================================================================

    /**
     * Remove Live2D overlay UI elements (floating buttons, lock icon, etc.)
     */
    I.cleanupLive2DOverlayUI = function cleanupLive2DOverlayUI() {
        const live2dManager = window.live2dManager;

        if (live2dManager) {
            if (live2dManager._lockIconTicker && live2dManager.pixi_app?.ticker) {
                try {
                    live2dManager.pixi_app.ticker.remove(live2dManager._lockIconTicker);
                } catch (_) {
                    // ignore
                }
                live2dManager._lockIconTicker = null;
            }
            if (live2dManager._floatingButtonsTicker && live2dManager.pixi_app?.ticker) {
                try {
                    live2dManager.pixi_app.ticker.remove(live2dManager._floatingButtonsTicker);
                } catch (_) {
                    // ignore
                }
                live2dManager._floatingButtonsTicker = null;
            }
            if (live2dManager._floatingButtonsResizeHandler) {
                window.removeEventListener('resize', live2dManager._floatingButtonsResizeHandler);
                live2dManager._floatingButtonsResizeHandler = null;
            }
            if (live2dManager.tutorialProtectionTimer) {
                clearInterval(live2dManager.tutorialProtectionTimer);
                live2dManager.tutorialProtectionTimer = null;
            }
            live2dManager._floatingButtonsContainer = null;
            live2dManager._returnButtonContainer = null;
            live2dManager._lockIconElement = null;
            live2dManager._lockIconImages = null;
        }

        document.querySelectorAll('#live2d-floating-buttons, #live2d-lock-icon, #live2d-return-button-container')
            .forEach(function (el) {
                if (window._removeNekoFloatingButtonsElement) {
                    window._removeNekoFloatingButtonsElement(el);
                } else {
                    el.remove();
                }
            });
    }

    /**
     * Remove VRM overlay UI elements.
     */
    I.cleanupVRMOverlayUI = function cleanupVRMOverlayUI() {
        if (window.vrmManager && typeof window.vrmManager.cleanupUI === 'function') {
            window.vrmManager.cleanupUI();
            return;
        }
        document.querySelectorAll('#vrm-floating-buttons, #vrm-lock-icon, #vrm-return-button-container')
            .forEach(function (el) {
                if (window._removeNekoFloatingButtonsElement) {
                    window._removeNekoFloatingButtonsElement(el);
                } else {
                    el.remove();
                }
            });
    }

    /**
     * Remove MMD overlay UI elements.
     */
    I.cleanupMMDOverlayUI = function cleanupMMDOverlayUI() {
        if (window.mmdManager && typeof window.mmdManager.cleanupFloatingButtons === 'function') {
            window.mmdManager.cleanupFloatingButtons();
            return;
        }
        document.querySelectorAll('#mmd-floating-buttons, #mmd-lock-icon, #mmd-return-button-container')
            .forEach(function (el) {
                if (window._removeNekoFloatingButtonsElement) {
                    window._removeNekoFloatingButtonsElement(el);
                } else {
                    el.remove();
                }
            });
    }

    I.cleanupPNGTuberOverlayUI = function cleanupPNGTuberOverlayUI() {
        if (window.pngtuberManager && typeof window.pngtuberManager.cleanupFloatingButtons === 'function') {
            window.pngtuberManager.cleanupFloatingButtons();
            return;
        }
        document.querySelectorAll('#pngtuber-floating-buttons, #pngtuber-lock-icon, #pngtuber-return-button-container')
            .forEach(function (el) {
                if (window._removeNekoFloatingButtonsElement) {
                    window._removeNekoFloatingButtonsElement(el);
                } else {
                    el.remove();
                }
            });
    }

    function markMMDCanvasLoadingSession(canvas, loadingSessionId) {
        if (!canvas) return;
        canvas.dataset.mmdLoadingSessionId = String(loadingSessionId);
        canvas.style.visibility = 'hidden';
        canvas.style.pointerEvents = 'none';
    }

    function restoreMMDCanvasForLoadingSession(canvas, loadingSessionId) {
        if (!canvas) return false;
        if (canvas.dataset.mmdLoadingSessionId !== String(loadingSessionId)) {
            return false;
        }
        delete canvas.dataset.mmdLoadingSessionId;
        canvas.style.visibility = 'visible';
        canvas.style.pointerEvents = 'auto';
        return true;
    }

    function isMMDLoadingSessionActive(canvas, loadingSessionId) {
        return !!canvas && canvas.dataset.mmdLoadingSessionId === String(loadingSessionId);
    }

    function clearMMDCanvasLoadingSession(canvas) {
        if (!canvas) return;
        delete canvas.dataset.mmdLoadingSessionId;
        canvas.style.visibility = 'hidden';
        canvas.style.pointerEvents = 'none';
    }

    // =====================================================================
    // Shared: memory-edited session reset logic
    // =====================================================================

    /**
     * Common handler for memory_edited events (used by both BroadcastChannel
     * and postMessage code paths).
     *
     * @param {string} catgirlName  - name of the character whose memory was edited
     */
    I.handleMemoryEdited = async function handleMemoryEdited(catgirlName) {
        console.log(
            window.t('console.memoryEditedRefreshContext'),
            catgirlName
        );

        // Was the user in voice mode before the edit?
        var wasRecording = I.S.isRecording;

        // Stop current mic capture
        if (I.S.isRecording && typeof window.stopMicCapture === 'function') {
            window.stopMicCapture();
        }

        // Tell backend to drop old context
        if (I.S.socket && I.S.socket.readyState === WebSocket.OPEN) {
            I.S.socket.send(JSON.stringify({ action: 'end_session' }));
            console.log('[Memory] 已向后端发送 end_session');
        }

        // Reset text session so next message reloads context
        if (I.S.isTextSessionActive) {
            I.S.isTextSessionActive = false;
            console.log('[Memory] 文本会话已重置，下次发送将重新加载上下文');
        }

        // Stop any playing AI audio (wait for decoder reset to avoid races)
        if (typeof window.clearAudioQueue === 'function') {
            try {
                await window.clearAudioQueue();
            } catch (e) {
                console.error('[Memory] clearAudioQueue 失败:', e);
            }
        }

        // If was in voice mode, wait for session teardown then re-connect
        if (wasRecording) {
            window.showStatusToast(
                window.t ? window.t('memory.refreshingContext') : '正在刷新上下文...',
                3000
            );
            // Wait for backend session to fully end
            await new Promise(function (resolve) { setTimeout(resolve, 1500); });
            // Trigger full startup flow via micButton click
            try {
                var micButton = document.getElementById('micButton');
                if (micButton) micButton.click();
            } catch (e) {
                console.error('[Memory] 自动重连语音失败:', e);
            }
        } else {
            window.showStatusToast(
                window.t ? window.t('memory.refreshed') : '记忆已更新，下次对话将使用新记忆',
                4000
            );
        }
    }

    // =====================================================================
    // Model hot-reload
    // =====================================================================

    /**
     * Capability check: does the current page host the full model UI?
     * index.html (served at / and /{lanlan_name}) has live2d-container,
     * vrm-container AND mmd-container. Other pages (chat, subtitle, etc.)
     * lack the complete set and must not run model reload / hide / show.
     */
    function _isModelHostPage() {
        return !!(document.getElementById('live2d-container')
              && document.getElementById('vrm-container')
              && document.getElementById('mmd-container'));
    }

    async function _waitForLive2DManagerIdle(timeoutMs) {
        var manager = window.live2dManager;
        if (!manager || !manager._isLoadingModel) {
            return;
        }

        var waitMs = Number.isFinite(timeoutMs) && timeoutMs > 0 ? timeoutMs : 30000;
        var startedAt = Date.now();
        console.log('[Model] Live2D 模型仍在加载，等待空闲后继续热切换');

        while (manager && manager._isLoadingModel) {
            if (Date.now() - startedAt >= waitMs) {
                console.warn('[Model] 等待 Live2D 模型加载空闲超时，继续尝试热切换');
                return;
            }
            await new Promise(function (resolve) {
                setTimeout(resolve, 80);
            });
            manager = window.live2dManager;
        }
    }

    /**
     * Handle model hot-swap triggered from another tab (model_manager).
     *
     * Concurrency-safe: if a reload is already in flight, the new request
     * is queued and executed once the current one finishes.
     *
     * @param {string} [targetLanlanName='']  - optional character name filter
     * @param {object} [reloadOptions]        - runtime-only reload options
     */
    I.handleModelReload = async function handleModelReload(targetLanlanName, reloadOptions) {
        targetLanlanName = targetLanlanName || '';
        reloadOptions = reloadOptions || {};
        var temporaryConfig = reloadOptions.temporaryConfig && typeof reloadOptions.temporaryConfig === 'object'
            ? reloadOptions.temporaryConfig
            : null;
        var suppressToast = !!reloadOptions.suppressToast;
        var skipIdleRestore = !!reloadOptions.skipIdleRestore;
        var skipPersistentExpressions = !!reloadOptions.skipPersistentExpressions;
        var deferRevealPrepared = !!reloadOptions.deferRevealPrepared;
        var throwOnError = !!reloadOptions.throwOnError;
        var reloadKey = JSON.stringify({
            lanlan_name: targetLanlanName,
            temporaryConfig: temporaryConfig || null,
            skipIdleRestore: skipIdleRestore,
            skipPersistentExpressions: skipPersistentExpressions,
            deferRevealPrepared: deferRevealPrepared
        });

        if (window._lastModelReloadKey === reloadKey && Date.now() - (window._lastModelReloadAt || 0) < 1000) {
            console.log('[Model] 忽略短时间内重复的模型重载请求');
            return window._lastModelReloadResult;
        }

        // 只有承载完整模型 UI 的页面才处理重载；Chat 等子窗口缺少渲染容器，
        // 执行会导致异常并弹出误导性的"模型切换失败"toast。
        if (!_isModelHostPage()) {
            console.log('[Model] 当前页面无模型容器，跳过模型重载');
            return;
        }

        // If the message targets a different character, ignore it
        var currentLanlanName = window.lanlan_config?.lanlan_name || '';
        if (targetLanlanName && currentLanlanName && targetLanlanName !== currentLanlanName) {
            console.log('[Model] 忽略来自其它角色的模型重载请求:', { targetLanlanName: targetLanlanName, currentLanlanName: currentLanlanName });
            return;
        }

        // Concurrency: wait if another reload is in-flight
        if (window._modelReloadInFlight) {
            console.log('[Model] 模型重载已在进行中，等待完成后重试');
            if (window._modelReloadKey === reloadKey) {
                console.log('[Model] 模型重载已在进行，复用当前重载请求');
                return window._modelReloadPromise;
            }
            console.log('[Model] 模型重载已在进行，记录最后一次不同的重载请求');
            var pendingResolve;
            var pendingReject;
            var pendingPromise = new Promise(function (resolve, reject) {
                pendingResolve = resolve;
                pendingReject = reject;
            });
            if (window._pendingModelReload && typeof window._pendingModelReload.resolve === 'function') {
                window._pendingModelReload.resolve(false);
            }
            window._pendingModelReload = {
                targetLanlanName: targetLanlanName,
                reloadOptions: reloadOptions,
                resolve: pendingResolve,
                reject: pendingReject
            };
            return pendingPromise;
        }

        // Mark in-flight
        window._modelReloadInFlight = true;
        window._modelReloadKey = reloadKey;
        window._pendingModelReload = null;

        var resolveReload;
        window._modelReloadPromise = new Promise(function (resolve) {
            resolveReload = resolve;
        });

        console.log('[Model] 开始热切换模型');
        let mmdRequestSessionId = '';
        let activeMmdLoadingSessionId = '';
        var reloadSucceeded = false;

        function ensureLive2DRenderActive(reason) {
            try {
                var preserveAvatarCornerPeekOpacity = window.nekoYuiGuideAvatarCornerPeekActive === true;
                var manager = window.live2dManager || null;
                var app = manager && manager.pixi_app;
                var ticker = app && app.ticker;
                var currentModel = manager && typeof manager.getCurrentModel === 'function'
                    ? manager.getCurrentModel()
                    : manager && manager.currentModel;

                if (currentModel) {
                    currentModel.visible = true;
                    if (!preserveAvatarCornerPeekOpacity) {
                        currentModel.alpha = 1;
                    }
                    if (currentModel.renderable !== undefined) {
                        currentModel.renderable = true;
                    }
                }
                if (app && app.stage) {
                    app.stage.visible = true;
                    if (!preserveAvatarCornerPeekOpacity) {
                        app.stage.alpha = 1;
                    }
                    if (app.stage.renderable !== undefined) {
                        app.stage.renderable = true;
                    }
                }
                if (ticker) {
                    if (!ticker.started && typeof ticker.start === 'function') {
                        ticker.start();
                    }
                    if (typeof ticker.update === 'function') {
                        ticker.update();
                    }
                }
                if (app && app.renderer && typeof app.renderer.render === 'function' && app.stage) {
                    app.renderer.render(app.stage);
                }
            } catch (error) {
                console.warn('[Model] Live2D render activation failed:', reason || 'unknown', error);
            }
        }

        function scheduleLive2DRenderActivation(reason) {
            [80, 300].forEach(function (delayMs) {
                window.setTimeout(function () {
                    ensureLive2DRenderActive((reason || 'model-reload-live2d') + ':delay-' + delayMs);
                }, delayMs);
            });
        }

        try {
            // 1. Re-fetch page config, or use a caller-provided temporary runtime config.
            var nameForConfig = targetLanlanName || currentLanlanName;
            var data;
            if (temporaryConfig) {
                data = Object.assign({ success: true }, temporaryConfig);
            } else {
                var pageConfigUrl = nameForConfig
                    ? '/api/config/page_config?lanlan_name=' + encodeURIComponent(nameForConfig)
                    : '/api/config/page_config';
                var response = await fetch(pageConfigUrl);
                data = await response.json();
            }

            if (data.success) {
                var newModelPath = data.model_path || '';
                var newModelType = (data.model_type || 'live2d').toLowerCase();
                var live3dSubType = (data.live3d_sub_type || '').toLowerCase();
                var oldModelType = window.lanlan_config?.model_type || 'live2d';
                var nextLighting = (temporaryConfig && !Object.prototype.hasOwnProperty.call(data, 'lighting'))
                    ? (window.lanlan_config?.lighting || null)
                    : ((data.lighting && typeof data.lighting === 'object')
                        ? Object.assign({}, data.lighting)
                        : null);

                window.lanlan_config = window.lanlan_config || {};
                window.lanlan_config.lighting = nextLighting;

                console.log('[Model] 模型切换:', {
                    oldType: oldModelType,
                    newType: newModelType,
                    newPath: newModelPath
                });

                // Empty model path -> fall back to default for VRM/Live3D-VRM
                if (!newModelPath) {
                    if (newModelType === 'vrm' || (newModelType === 'live3d' && live3dSubType === 'vrm')) {
                        newModelPath = '/static/vrm/sister1.0.vrm';
                        console.info('[Model] VRM模型路径为空，使用默认模型:', newModelPath);
                    } else {
                        console.warn('[Model] 模型路径为空，仍然执行模型类型切换');
                    }
                }

                // Cross-type switch: clean up the old overlay
                var oldLive3dSubType = (window.lanlan_config?.live3d_sub_type || '').toLowerCase();
                var typeChanged = oldModelType !== newModelType ||
                    (newModelType === 'live3d' && oldLive3dSubType !== live3dSubType);

                // 提前更新 config，防止异步间隙中其他代码基于过时类型重建按钮
                if (typeChanged && window.lanlan_config) {
                    window.lanlan_config.model_type = newModelType;
                    window.lanlan_config.live3d_sub_type = live3dSubType;
                }

                if (typeChanged) {
                    if (oldModelType === 'live2d') I.cleanupLive2DOverlayUI();
                    if (oldModelType === 'vrm') I.cleanupVRMOverlayUI();
                    if (oldModelType === 'live3d') {
                        I.cleanupVRMOverlayUI();
                        I.cleanupMMDOverlayUI();
                    }
                    if (oldModelType === 'pngtuber') {
                        if (window.pngtuberManager && typeof window.pngtuberManager.hide === 'function') {
                            window.pngtuberManager.hide();
                        }
                        I.cleanupPNGTuberOverlayUI();
                        var oldPngtuberContainer = document.getElementById('pngtuber-container');
                        if (oldPngtuberContainer) {
                            oldPngtuberContainer.style.display = 'none';
                            oldPngtuberContainer.style.visibility = 'hidden';
                            oldPngtuberContainer.classList.add('hidden');
                            oldPngtuberContainer.style.pointerEvents = 'none';
                        }
                    }
                }

                // 3. Switch based on model type
                if (newModelType === 'vrm' || (newModelType === 'live3d' && live3dSubType === 'vrm')) {
                    window.vrmModel = newModelPath;
                    window.cubism4Model = '';

                    // Hide Live2D
                    console.log('[Model] 隐藏 Live2D 模型');
                    var live2dContainer = document.getElementById('live2d-container');
                    if (live2dContainer) {
                        live2dContainer.style.display = 'none';
                        live2dContainer.classList.add('hidden');
                    }

                    // Hide MMD
                    var mmdContainer = document.getElementById('mmd-container');
                    if (mmdContainer) {
                        mmdContainer.style.display = 'none';
                        mmdContainer.classList.add('hidden');
                    }
                    var mmdCanvas = document.getElementById('mmd-canvas');
                    if (mmdCanvas) {
                        mmdCanvas.style.visibility = 'hidden';
                        mmdCanvas.style.pointerEvents = 'none';
                    }
                    if (window.mmdManager && typeof window.mmdManager.pauseRendering === 'function') {
                        window.mmdManager.pauseRendering();
                    }
                    if (window.live2dManager && typeof window.live2dManager.pauseRendering === 'function') {
                        window.live2dManager.pauseRendering();
                    }
                    // 清空 Live2D 画布残留像素，避免透明窗口穿透
                    if (window.live2dManager && window.live2dManager.pixi_app && window.live2dManager.pixi_app.renderer) {
                        window.live2dManager.pixi_app.renderer.clear();
                    }

                    // Show & reload VRM
                    console.log('[Model] 加载 VRM 模型:', newModelPath);
                    var vrmContainer = document.getElementById('vrm-container');
                    if (vrmContainer) {
                        vrmContainer.classList.remove('hidden');
                        vrmContainer.style.display = 'block';
                        vrmContainer.style.visibility = 'visible';
                        vrmContainer.style.removeProperty('pointer-events');
                    }

                    var vrmCanvas = document.getElementById('vrm-canvas');
                    if (vrmCanvas) {
                        vrmCanvas.style.visibility = 'visible';
                        vrmCanvas.style.pointerEvents = 'auto';
                    }

                    // Ensure VRM manager is initialised
                    if (!window.vrmManager) {
                        console.log('[Model] VRM 管理器未初始化，等待初始化完成');
                        if (typeof initVRMModel === 'function') {
                            await initVRMModel();
                        }
                    }

                    // Load the new model
                    if (window.vrmManager) {
                        // 【关键修复】确保容器和 canvas 存在，并恢复 Three.js 场景可见性。
                        // 角色切换的清理逻辑会将 renderer.domElement 设为 display:none，
                        // 而 loadModel 内部在 scene/camera/renderer 已存在时不会调用
                        // ensureThreeReady（也就不会恢复 canvas 可见性），导致从 Live2D
                        // 切换到 VRM 时模型加载成功但不可见。
                        // initThreeJS 在已初始化时是幂等的，但会无条件恢复容器/canvas 可见性。
                        {
                            var vrmContainerEl = document.getElementById('vrm-container');
                            if (vrmContainerEl && !vrmContainerEl.querySelector('canvas')) {
                                var newCanvas = document.createElement('canvas');
                                newCanvas.id = 'vrm-canvas';
                                vrmContainerEl.appendChild(newCanvas);
                            }
                        }
                        await window.vrmManager.initThreeJS('vrm-canvas', 'vrm-container', nextLighting);

                        // 停止旧的待机轮换
                        if (typeof window._stopVrmIdleRotation === 'function') window._stopVrmIdleRotation();
                        if (typeof window._stopMmdIdleRotation === 'function') window._stopMmdIdleRotation();

                        // 【修复】在 loadModel 之前获取角色待机动作列表，
                        // 更新 lanlan_config 使 loadModel 内部读取到正确的待机动作 URL，
                        // 避免使用初始页面加载时的过时值导致动画加载失败进入 T-pose。
                        // 先清空旧值，确保 fetch 失败时 loadModel 回退到安全的硬编码默认值
                        // 而非残留的上一个角色的待机动作 URL。
                        var vrmIdleList = [];
                        window.lanlan_config.vrmIdleAnimation = '';
                        window.lanlan_config.vrmIdleAnimations = [];
                        if (nameForConfig) {
                            try {
                                var charResVrm = await fetch('/api/characters');
                                if (charResVrm.ok) {
                                    var charDataVrm = await charResVrm.json();
                                    var catDataVrm = charDataVrm?.['猫娘']?.[nameForConfig];
                                    // 【修复】兼容新旧版字段，穿透 _reserved 读取 VRM 待机动作
                                    var rawVrmIdle = catDataVrm?._reserved?.avatar?.vrm?.idle_animation
                                                  || catDataVrm?.idle_animation
                                                  || catDataVrm?.idleAnimations
                                                  || catDataVrm?.idleAnimation;
                                    vrmIdleList = Array.isArray(rawVrmIdle) ? rawVrmIdle : (rawVrmIdle ? [rawVrmIdle] : []);
                                    window.lanlan_config.vrmIdleAnimation = vrmIdleList[0] || '';
                                    window.lanlan_config.vrmIdleAnimations = vrmIdleList;
                                }
                            } catch (e) {
                                console.warn('[Model] 获取VRM待机动作列表失败:', e);
                            }
                        }

                        await window.vrmManager.loadModel(newModelPath);

                        // 启动待机动作轮换（多个动作时自动切换）
                        if (vrmIdleList.length > 0 && typeof window._startVrmIdleRotation === 'function') {
                            window._startVrmIdleRotation(vrmIdleList);
                        }

                        // 重新应用打光/曝光/描边；若角色未保存自定义光照，则回退到默认值，避免沿用上一个角色的灯光状态。
                        var effectiveLighting = window.lanlan_config?.lighting || window.VRM_DEFAULT_LIGHTING || null;
                        if (effectiveLighting && typeof window.applyVRMLighting === 'function') {
                            window.applyVRMLighting(effectiveLighting, window.vrmManager);
                            if (typeof window.applyVRMOutlineWidth === 'function') {
                                var currentModelRef = window.vrmManager?.currentModel;
                                var outlineScale = effectiveLighting.outlineWidthScale;
                                requestAnimationFrame(function () {
                                    if (window.vrmManager?.currentModel !== currentModelRef) {
                                        return;
                                    }
                                    if (outlineScale !== undefined) {
                                        window.applyVRMOutlineWidth(outlineScale, window.vrmManager);
                                    }
                                });
                            }
                        }

                        // 重启 UI 更新循环（被 handleHideMainUI 停止）。
                        // handleShowMainUI 在 _modelReloadInFlight 为 true 时会跳过，
                        // 因此必须在模型加载完成后手动重启，否则悬浮按钮不会重新出现。
                        if (window.vrmManager && window.vrmManager._uiUpdateLoopId == null
                            && typeof window.vrmManager._startUIUpdateLoop === 'function') {
                            window.vrmManager._snapUIPosition = true;
                            window.vrmManager._startUIUpdateLoop();
                        }
                    } else {
                        console.error('[Model] VRM 管理器初始化失败');
                    }
                } else if (newModelType === 'live3d' && live3dSubType === 'mmd') {
                    // MMD mode (Live3D sub-type)
                    window.cubism4Model = '';
                    window.vrmModel = '';

                    // Hide Live2D
                    console.log('[Model] 隐藏 Live2D 模型');
                    var live2dContainerMmd = document.getElementById('live2d-container');
                    if (live2dContainerMmd) {
                        live2dContainerMmd.style.display = 'none';
                        live2dContainerMmd.classList.add('hidden');
                    }

                    // Hide VRM
                    var vrmContainerMmd = document.getElementById('vrm-container');
                    if (vrmContainerMmd) {
                        vrmContainerMmd.style.display = 'none';
                        vrmContainerMmd.classList.add('hidden');
                    }
                    var vrmCanvasMmd = document.getElementById('vrm-canvas');
                    if (vrmCanvasMmd) {
                        vrmCanvasMmd.style.visibility = 'hidden';
                        vrmCanvasMmd.style.pointerEvents = 'none';
                    }
                    if (window.vrmManager && typeof window.vrmManager.pauseRendering === 'function') {
                        window.vrmManager.pauseRendering();
                    }
                    if (window.vrmManager && window.vrmManager.renderer) {
                        window.vrmManager.renderer.clear();
                    }
                    if (window.live2dManager && typeof window.live2dManager.pauseRendering === 'function') {
                        window.live2dManager.pauseRendering();
                    }
                    if (window.live2dManager && window.live2dManager.pixi_app && window.live2dManager.pixi_app.renderer) {
                        window.live2dManager.pixi_app.renderer.clear();
                    }

                    // Show MMD container
                    console.log('[Model] 加载 MMD 模型:', newModelPath);
                    var mmdContainerShow = document.getElementById('mmd-container');
                    if (mmdContainerShow) {
                        mmdContainerShow.classList.remove('hidden');
                        mmdContainerShow.style.display = 'block';
                        mmdContainerShow.style.visibility = 'visible';
                        mmdContainerShow.style.removeProperty('pointer-events');
                    }
                    var mmdCanvasShow = document.getElementById('mmd-canvas');
                    const loadingSessionId = window._createMMDLoadingSessionId
                        ? window._createMMDLoadingSessionId('mmd-interpage')
                        : `mmd-interpage-${Date.now()}`;
                    if (mmdCanvasShow) {
                        // 先隐藏 canvas，避免旧帧或加载中的模型透过半透明 overlay 露出。
                        markMMDCanvasLoadingSession(mmdCanvasShow, loadingSessionId);
                    }
                    activeMMDReloadCanvasSessionId = loadingSessionId;
                    mmdRequestSessionId = loadingSessionId;
                    activeMmdLoadingSessionId = loadingSessionId;
                    window.MMDLoadingOverlay?.begin(loadingSessionId, { stage: 'engine' });

                    // Ensure MMD manager is initialised
                    if (!window.mmdManager) {
                        console.log('[Model] MMD 管理器未初始化，等待初始化完成');
                        if (typeof initMMDModel === 'function') {
                            const initializedManager = await initMMDModel();
                            if (!initializedManager || !window.mmdManager || window.mmdManager._isDisposed) {
                                throw new Error('MMD 管理器初始化失败');
                            }
                        }
                    }

                    // Load MMD model
                    if (window.mmdManager) {
                        // 提前获取设置并预置物理开关
                        let savedSettings = null;
                        try {
                            window.MMDLoadingOverlay?.update(loadingSessionId, { stage: 'settings' });
                            var settingsRes = await fetch('/api/characters/catgirl/' + encodeURIComponent(nameForConfig) + '/mmd_settings');
                            var settingsData = await settingsRes.json();
                            if (settingsData.success && settingsData.settings) {
                                savedSettings = settingsData.settings;
                                if (savedSettings.physics?.enabled != null) {
                                    window.mmdManager.enablePhysics = !!savedSettings.physics.enabled;
                                }
                            }
                        } catch (settingsErr) {
                            console.warn('[Model] 获取MMD设置失败:', settingsErr);
                        }
                        // 停止旧的待机轮换
                        if (typeof window._stopVrmIdleRotation === 'function') window._stopVrmIdleRotation();
                        if (typeof window._stopMmdIdleRotation === 'function') window._stopMmdIdleRotation();

                        window.MMDLoadingOverlay?.update(loadingSessionId, { stage: 'model' });
                        await window.mmdManager.loadModel(newModelPath, { loadingSessionId });

                        // 应用完整设置（光照、渲染、物理、鼠标跟踪）
                        if (savedSettings) {
                            window.mmdManager.applySettings(savedSettings);
                        }

                        // 播放待机动作 & 启动轮换
                        if (nameForConfig) {
                            try {
                                const charRes = await fetch('/api/characters');
                                if (charRes.ok) {
                                    const charData = await charRes.json();
                                    const catData = charData?.['猫娘']?.[nameForConfig];
                                    // 【修复】兼容新旧版字段，穿透 _reserved 读取 MMD 待机动作
                                    let rawMmdIdle = catData?._reserved?.avatar?.mmd?.idle_animation
                                                  || catData?.mmd_idle_animations
                                                  || catData?.mmd_idle_animation;
                                    let idleList = Array.isArray(rawMmdIdle) ? rawMmdIdle : (rawMmdIdle ? [rawMmdIdle] : []);
                                    if (idleList.length > 0) {
                                        try {
                                            window.MMDLoadingOverlay?.update(loadingSessionId, { stage: 'idle' });
                                            await window.mmdManager.loadAnimation(idleList[0]);
                                            window.mmdManager.playAnimation();
                                            console.log('[Model] 已播放待机动作:', idleList[0]);
                                            if (typeof window._startMmdIdleRotation === 'function') {
                                                window._startMmdIdleRotation(idleList);
                                            }
                                        } catch (idleErr) {
                                            console.warn('[Model] 播放待机动作失败:', idleErr);
                                        }
                                    }
                                }
                            } catch (idleErr) {
                                console.warn('[Model] 获取角色待机动作失败:', idleErr);
                            }
                        }
                        window.MMDLoadingOverlay?.update(loadingSessionId, { stage: 'done' });
                        if (window._waitForMMDRenderFrame) {
                            await window._waitForMMDRenderFrame(window.mmdManager);
                        }
                        var mmdCanvasReady = document.getElementById('mmd-canvas');
                        if (mmdRequestSessionId === loadingSessionId && isMMDLoadingSessionActive(mmdCanvasReady, loadingSessionId)) {
                            window.MMDLoadingOverlay?.end(loadingSessionId);
                            restoreMMDCanvasForLoadingSession(mmdCanvasReady, loadingSessionId);
                            activeMMDReloadCanvasSessionId = '';
                            mmdRequestSessionId = '';
                            activeMmdLoadingSessionId = '';
                        }

                        // 重启 UI 更新循环（被 handleHideMainUI 停止）。
                        // handleShowMainUI 在 _modelReloadInFlight 为 true 时会跳过，
                        // 因此必须在模型加载完成后手动重启，否则悬浮按钮不会重新出现。
                        if (window.mmdManager && window.mmdManager._uiUpdateLoopId == null
                            && typeof window.mmdManager._startUIUpdateLoop === 'function') {
                            window.mmdManager._snapUIPosition = true;
                            window.mmdManager._startUIUpdateLoop();
                        }
                    } else {
                        console.error('[Model] MMD 管理器初始化失败');
                        throw new Error('MMD 管理器初始化失败');
                    }
                } else if (newModelType === 'pngtuber') {
                    window.cubism4Model = '';
                    window.vrmModel = '';
                    window.mmdModel = '';

                    var live2dContainerPng = document.getElementById('live2d-container');
                    if (live2dContainerPng) {
                        live2dContainerPng.style.display = 'none';
                        live2dContainerPng.classList.add('hidden');
                    }
                    var vrmContainerPng = document.getElementById('vrm-container');
                    if (vrmContainerPng) {
                        vrmContainerPng.style.display = 'none';
                        vrmContainerPng.classList.add('hidden');
                    }
                    var mmdContainerPng = document.getElementById('mmd-container');
                    if (mmdContainerPng) {
                        mmdContainerPng.style.display = 'none';
                        mmdContainerPng.classList.add('hidden');
                    }

                    if (window.live2dManager && typeof window.live2dManager.pauseRendering === 'function') {
                        window.live2dManager.pauseRendering();
                    }
                    if (window.live2dManager && window.live2dManager.pixi_app && window.live2dManager.pixi_app.renderer) {
                        window.live2dManager.pixi_app.renderer.clear();
                    }
                    if (window.vrmManager && typeof window.vrmManager.pauseRendering === 'function') {
                        window.vrmManager.pauseRendering();
                    }
                    if (window.vrmManager && window.vrmManager.renderer) {
                        window.vrmManager.renderer.clear();
                    }
                    if (window.mmdManager && typeof window.mmdManager.pauseRendering === 'function') {
                        window.mmdManager.pauseRendering();
                    }

                    var pngtuberConfig = (data.pngtuber && typeof data.pngtuber === 'object')
                        ? Object.assign({}, data.pngtuber)
                        : { idle_image: newModelPath };
                    if (window.lanlan_config) {
                        window.lanlan_config.pngtuber = Object.assign({}, pngtuberConfig);
                    }
                    if (typeof window.loadPNGTuberAvatar !== 'function') {
                        throw new Error('PNGTuber runtime not loaded');
                    }
                    await window.loadPNGTuberAvatar(pngtuberConfig);
                } else {
                    // Live2D mode
                    window.cubism4Model = newModelPath;
                    window.vrmModel = '';

                    // Hide VRM
                    console.log('[Model] 隐藏 VRM 模型');
                    var vrmContainer2 = document.getElementById('vrm-container');
                    if (vrmContainer2) {
                        vrmContainer2.style.display = 'none';
                        vrmContainer2.classList.add('hidden');
                    }
                    var vrmCanvas2 = document.getElementById('vrm-canvas');
                    if (vrmCanvas2) {
                        vrmCanvas2.style.visibility = 'hidden';
                        vrmCanvas2.style.pointerEvents = 'none';
                    }

                    // Hide MMD
                    var mmdContainer2 = document.getElementById('mmd-container');
                    if (mmdContainer2) {
                        mmdContainer2.style.display = 'none';
                        mmdContainer2.classList.add('hidden');
                    }
                    var mmdCanvas2 = document.getElementById('mmd-canvas');
                    if (mmdCanvas2) {
                        clearMMDCanvasLoadingSession(mmdCanvas2);
                    }
                    if (window.vrmManager && typeof window.vrmManager.pauseRendering === 'function') {
                        window.vrmManager.pauseRendering();
                    }
                    // 清空VRM画布残留像素，避免透明窗口穿透
                    if (window.vrmManager && window.vrmManager.renderer) {
                        window.vrmManager.renderer.clear();
                    }
                    if (window.mmdManager && typeof window.mmdManager.pauseRendering === 'function') {
                        window.mmdManager.pauseRendering();
                    }

                    // Hide PNGTuber when returning to Live2D.
                    if (window.pngtuberManager && typeof window.pngtuberManager.hide === 'function') {
                        window.pngtuberManager.hide();
                    }
                    I.cleanupPNGTuberOverlayUI();
                    var pngtuberContainer2 = document.getElementById('pngtuber-container');
                    if (pngtuberContainer2) {
                        pngtuberContainer2.style.display = 'none';
                        pngtuberContainer2.classList.add('hidden');
                    }

                    // Show & reload Live2D
                    var live2dContainer2 = document.getElementById('live2d-container');
                    if (live2dContainer2) {
                        live2dContainer2.classList.remove('hidden');
                        live2dContainer2.style.display = 'block';
                        live2dContainer2.style.visibility = 'visible';
                        live2dContainer2.style.setProperty('pointer-events', 'none', 'important');
                    }
                    var live2dCanvas2 = document.getElementById('live2d-canvas');
                    if (live2dCanvas2) {
                        live2dCanvas2.style.visibility = 'visible';
                        live2dCanvas2.style.pointerEvents = 'auto';
                    }

                    if (newModelPath) {
                        console.log('[Model] 加载 Live2D 模型:', newModelPath);

                        // Ensure Live2D manager is initialised
                        if (!window.live2dManager) {
                            console.log('[Model] Live2D 管理器未初始化，等待初始化完成');
                            if (temporaryConfig && typeof window.Live2DManager === 'function') {
                                window.live2dManager = new window.Live2DManager();
                            } else if (typeof initLive2DModel === 'function') {
                                await initLive2DModel();
                            }
                        }

                        // Load the new model
                        if (window.live2dManager) {
                            // Ensure PIXI app is initialised
                            if (!window.live2dManager.pixi_app) {
                                // 安全网：如果 canvas 被 PIXI.destroy(true) 从 DOM 移除，重新创建
                                var live2dCanvasEl = document.getElementById('live2d-canvas');
                                if (!live2dCanvasEl) {
                                    console.log('[Model] live2d-canvas 不存在，重新创建');
                                    live2dCanvasEl = document.createElement('canvas');
                                    live2dCanvasEl.id = 'live2d-canvas';
                                    var live2dContainerEl = document.getElementById('live2d-container');
                                    if (live2dContainerEl) {
                                        live2dContainerEl.appendChild(live2dCanvasEl);
                                    }
                                }
                                console.log('[Model] PIXI 应用未初始化，正在初始化...');
                                await window.live2dManager.initPIXI('live2d-canvas', 'live2d-container');
                            }
                            await _waitForLive2DManagerIdle(30000);

                            // Apply saved user preferences to avoid "reset" on return from model manager
                            var modelPreferences = null;
                            try {
                                var preferences = await window.live2dManager.loadUserPreferences();
                                modelPreferences = preferences ? preferences.find(function (p) { return p && p.model_path === newModelPath; }) : null;
                            } catch (prefError) {
                                console.warn('[Model] 读取 Live2D 用户偏好失败，将继续加载模型:', prefError);
                            }

                            await window.live2dManager.loadModel(newModelPath, {
                                preferences: modelPreferences,
                                isMobile: typeof window.isMobileWidth === 'function' ? window.isMobileWidth() : (window.innerWidth <= 768),
                                suppressInitialIdle: skipIdleRestore,
                                suppressPersistentExpressions: skipPersistentExpressions
                            });
                            if (live2dContainer2) {
                                live2dContainer2.classList.remove('hidden');
                                live2dContainer2.style.display = 'block';
                                live2dContainer2.style.visibility = 'visible';
                                if (!deferRevealPrepared) {
                                    live2dContainer2.style.removeProperty('opacity');
                                }
                                live2dContainer2.style.setProperty('pointer-events', 'none', 'important');
                            }
                            if (live2dCanvas2) {
                                live2dCanvas2.style.display = 'block';
                                live2dCanvas2.style.visibility = 'visible';
                                if (!deferRevealPrepared) {
                                    live2dCanvas2.style.removeProperty('opacity');
                                }
                                live2dCanvas2.style.pointerEvents = 'auto';
                            }
                            if (window.lanlan_config) {
                                window.lanlan_config.model_type = newModelType;
                                window.lanlan_config.live3d_sub_type = live3dSubType;
                            }
                            if (!deferRevealPrepared && typeof window.showLive2d === 'function') {
                                window.showLive2d();
                            }
                            if (window.live2dManager && typeof window.live2dManager.resumeRendering === 'function') {
                                window.live2dManager.resumeRendering();
                            }
                            ensureLive2DRenderActive('model-reload-live2d');
                            scheduleLive2DRenderActivation('model-reload-live2d');

                            // Sync legacy global references
                            if (window.LanLan1) {
                                window.LanLan1.live2dModel = window.live2dManager.getCurrentModel();
                                window.LanLan1.currentModel = window.live2dManager.getCurrentModel();
                            }

                            // 恢复 Live2D 待机动作。教程临时模型不读取用户模型的待机动作，避免把不匹配的动作套到 yui-lolita。
                            if (!skipIdleRestore) {
                                restoreLive2DIdleAnimationOnMainPage();
                            }
                        } else {
                            console.error('[Model] Live2D 管理器初始化失败');
                        }
                    } else {
                        console.warn('[Model] Live2D 模型路径为空，已切换容器但跳过模型加载');
                        window.showStatusToast(
                            window.t ? window.t('app.modelPathEmpty') : '模型路径为空',
                            2000
                        );
                    }
                }

                // 4. Commit config only after successful switch
                if (window.lanlan_config) {
                    window.lanlan_config.model_type = newModelType;
                    window.lanlan_config.live3d_sub_type = live3dSubType;
                }

                // 5. Success toast
                if (!suppressToast) {
                    window.showStatusToast(
                        window.t ? window.t('app.modelSwitched') : '模型已切换',
                        2000
                    );
                }
                reloadSucceeded = true;
            } else {
                console.error('[Model] 获取页面配置失败:', data.error);
                if (!suppressToast) {
                    window.showStatusToast(
                        window.t ? window.t('app.modelSwitchFailed') : '模型切换失败',
                        3000
                    );
                }
                if (throwOnError) {
                    throw new Error(data.error || 'page_config_failed');
                }
            }
        } catch (error) {
            console.error('[Model] 模型热切换失败:', error);
            if (activeMmdLoadingSessionId) {
                window.MMDLoadingOverlay?.fail(activeMmdLoadingSessionId, { detail: error?.message || String(error) });
                var failedMmdCanvas = document.getElementById('mmd-canvas');
                if (isMMDLoadingSessionActive(failedMmdCanvas, activeMmdLoadingSessionId)) {
                    clearMMDCanvasLoadingSession(failedMmdCanvas);
                }
                if (mmdRequestSessionId === activeMmdLoadingSessionId) {
                    mmdRequestSessionId = '';
                }
                if (activeMMDReloadCanvasSessionId === activeMmdLoadingSessionId) {
                    activeMMDReloadCanvasSessionId = '';
                }
                activeMmdLoadingSessionId = '';
            }
            // 回滚提前写入的 config，防止残留错误的模型类型
            if (typeChanged && window.lanlan_config) {
                window.lanlan_config.model_type = oldModelType;
                window.lanlan_config.live3d_sub_type = oldLive3dSubType || '';
                console.warn('[Model] 已回滚 config:', { model_type: oldModelType, live3d_sub_type: oldLive3dSubType });
            }
            if (typeChanged && oldModelType === 'pngtuber') {
                try {
                    if (window.lanlan_config && window.lanlan_config.pngtuber && typeof window.loadPNGTuberAvatar === 'function') {
                        await window.loadPNGTuberAvatar(window.lanlan_config.pngtuber);
                    } else if (window.pngtuberManager && typeof window.pngtuberManager.show === 'function') {
                        window.pngtuberManager.show();
                    }
                    var restoredPngtuberContainer = document.getElementById('pngtuber-container');
                    if (restoredPngtuberContainer) {
                        restoredPngtuberContainer.classList.remove('hidden');
                        restoredPngtuberContainer.style.display = 'block';
                        restoredPngtuberContainer.style.visibility = 'visible';
                        restoredPngtuberContainer.style.pointerEvents = 'none';
                        var restoredPngtuberImage = restoredPngtuberContainer.querySelector('.pngtuber-image');
                        if (restoredPngtuberImage) {
                            restoredPngtuberImage.style.visibility = 'visible';
                            restoredPngtuberImage.style.pointerEvents = 'auto';
                        }
                    }
                } catch (restoreError) {
                    console.warn('[Model] PNGTuber restore after failed switch failed:', restoreError);
                }
            }
            if (!suppressToast) {
                window.showStatusToast(
                    window.t ? window.t('app.modelSwitchFailed') : '模型切换失败',
                    3000
                );
            }
            if (throwOnError) {
                throw error;
            }
        } finally {
            // Clear in-flight flag
            window._modelReloadInFlight = false;
            activeMMDReloadCanvasSessionId = '';
            if (reloadSucceeded) {
                window._lastModelReloadKey = reloadKey;
                window._lastModelReloadAt = Date.now();
                window._lastModelReloadResult = true;
                invalidateModelManagerOverlapBounds();
            } else {
                window._lastModelReloadResult = false;
            }
            window._modelReloadKey = '';
            resolveReload(window._lastModelReloadResult);

            // If the model manager is still open, keep the Pet UI hidden even
            // though the reload path briefly re-created containers/buttons.
            if (I.isMainUIHiddenByModelManager()) {
                console.log('[Model] 主界面处于模型管理隐藏状态，模型重载完成后重新隐藏 UI');
                I.handleHideMainUI({ preserveHiddenState: true });
                deferredMainUIShowAfterModelReload = null;
            } else if (deferredMainUIShowAfterModelReload) {
                var deferredShowOptions = deferredMainUIShowAfterModelReload;
                deferredMainUIShowAfterModelReload = null;
                console.log('[Model] 执行热重载期间延后的主界面恢复');
                I.handleShowMainUI(deferredShowOptions);
            }

            // Process any queued reload request
            if (window._pendingModelReload) {
                console.log('[Model] 执行待处理的模型重载请求');
                var pendingReload = window._pendingModelReload;
                window._pendingModelReload = null;
                setTimeout(function () {
                    I.handleModelReload(pendingReload.targetLanlanName, pendingReload.reloadOptions)
                        .then(function (result) {
                            if (typeof pendingReload.resolve === 'function') pendingReload.resolve(result);
                        })
                        .catch(function (error) {
                            if (typeof pendingReload.reject === 'function') pendingReload.reject(error);
                        });
                }, 100);
            }
        }
    }

    I.handleReloadModelParametersMessage = async function handleReloadModelParametersMessage(message) {
        var manager = window.live2dManager;
        if (!manager || typeof manager.reloadModelParameters !== 'function') {
            await I.handleModelReload(message && message.lanlan_name);
            return false;
        }

        try {
            var result = await manager.reloadModelParameters({
                model_name: message && message.model_name,
                model_path: message && message.model_path
            });
            if (result && result.reason === 'model_mismatch') {
                console.log('[Live2D] 忽略非当前模型的参数热刷新消息');
                return false;
            }
            return !!(result && result.applied);
        } catch (error) {
            console.warn('[Live2D] 参数轻量热刷新失败，降级为完整模型重载:', error);
            await I.handleModelReload(message && message.lanlan_name);
            return false;
        }
    }

    /**
     * [HACK/WORKAROUND] 动态向已加载的 Live2D 模型实例注入动作组。
     * 注意：这里直接修改了 pixi-live2d-display SDK 的内部私有/只读数据结构。
     * @deprecated-if-sdk-upgraded 如果未来升级了 live2d SDK，此函数极易崩溃，请优先寻找官方 API 替代。
     */
    function _injectMotionGroupSafely(live2dModel, groupName, motionFiles) {
        if (!live2dModel || !live2dModel.internalModel || !live2dModel.internalModel.motionManager) {
            console.warn('[_injectMotionGroup] 模型结构不完整，注入失败');
            return false;
        }

        const internalModel = live2dModel.internalModel;
        const motionManager = internalModel.motionManager;
        const motionsList = motionFiles.map(file => ({ File: file }));

        try {
            console.debug(`[_injectMotionGroup] 正在向内部结构注入动作组: ${groupName}`);

            // 1. 注入 MotionManager 配置
            if (!motionManager.definitions) motionManager.definitions = {};
            motionManager.definitions[groupName] = motionsList;

            // 2. 初始化实例缓存数组（关键：必须为空数组，避免跳过实际加载）
            if (!motionManager.motionGroups) motionManager.motionGroups = {};
            if (!motionManager.motionGroups[groupName]) motionManager.motionGroups[groupName] = [];

            // 3. 同步 fallback 的 Settings 树
            if (!internalModel.settings) internalModel.settings = {};
            if (!internalModel.settings.motions) internalModel.settings.motions = {};
            internalModel.settings.motions[groupName] = motionsList;

            // 4. 同步最外层的文件引用树
            if (!live2dModel.fileReferences) live2dModel.fileReferences = {};
            if (!live2dModel.fileReferences.Motions) live2dModel.fileReferences.Motions = {};
            live2dModel.fileReferences.Motions[groupName] = motionsList;

            console.debug('[_injectMotionGroup] 注入完成');
            return true;
        } catch (err) {
            console.error('[_injectMotionGroup] 篡改 SDK 内部结构时崩溃，可能是 SDK 已升级:', err);
            return false;
        }
    }

    // =====================================================================
    // Live2D 待机动作恢复功能
    //
    // 功能说明：
    // - 主页加载时自动读取 characters.json 中保存的 live2d_idle_animation
    // - 从 API 获取当前模型的动作文件列表
    // - 手动构建 motionManager.definitions 和 motionGroups（主页没有 PreviewAll 组）
    // - 加载并循环播放保存的待机动作
    //
    // 注意：motionGroups 必须初始化为空数组 []，不能放入配置对象！
    // 原因：SDK 会检查 motionGroups 是否已有内容来判断动作是否已加载。
    // 如果放入 JSON 配置对象，SDK 会误认为动作已加载，跳过网络请求和解析。
    // =====================================================================
    async function restoreLive2DIdleAnimationOnMainPage(options = {}) {
        try {
            const shouldContinue = options && typeof options.shouldContinue === 'function'
                ? options.shouldContinue
                : null;
            const canContinue = () => {
                if (!shouldContinue) return true;
                try {
                    return shouldContinue() !== false;
                } catch (guardError) {
                    console.warn('[Live2D Main] 待机动作恢复 guard 失败，跳过恢复:', guardError);
                    return false;
                }
            };

            // 1. 获取当前角色名称，并作为当前任务的标识（防竞态）
            const initialLanlanName = window.lanlan_config?.lanlan_name;
            if (!initialLanlanName) {
                console.log('[Live2D Main] 没有 lanlan_name，跳过恢复待机动作');
                return;
            }
            if (!canContinue()) {
                console.log('[Live2D Main] 待机动作恢复已被新的交互取消');
                return;
            }

            // 2. 从 characters.json 获取保存的待机动作路径
            const response = await fetch('/api/characters');
            const data = await response.json();

            // 【竞态防护】如果中途角色被切换了，立刻中止
            if (window.lanlan_config?.lanlan_name !== initialLanlanName) return;
            if (!canContinue()) {
                console.log('[Live2D Main] 待机动作恢复已被新的交互取消');
                return;
            }

            const charData = data['猫娘']?.[initialLanlanName];
            // 【修复】兼容新旧版字段，穿透 _reserved 读取 Live2D 待机动作
            const hasOwn = (obj, key) => !!obj && Object.prototype.hasOwnProperty.call(obj, key);
            const reservedLive2D = charData?._reserved?.avatar?.live2d;
            const avatarLive2D = charData?.avatar?.live2d;
            let live2dIdleAnimation;
            let hasExplicitIdleAnimation = false;
            if (hasOwn(reservedLive2D, 'idle_animation')) {
                live2dIdleAnimation = reservedLive2D.idle_animation;
                hasExplicitIdleAnimation = true;
            } else if (hasOwn(charData, 'live2d_idle_animation')) {
                live2dIdleAnimation = charData.live2d_idle_animation;
                hasExplicitIdleAnimation = true;
            } else if (hasOwn(avatarLive2D, 'idle_animation')) {
                live2dIdleAnimation = avatarLive2D.idle_animation;
                hasExplicitIdleAnimation = true;
            }
            const live2dModelName = charData?.live2d;

            if (!live2dModelName) {
                console.log('[Live2D Main] 没有模型名称');
                return;
            }

            // 3. 从 API 获取模型的动作文件列表（主页没有初始化 PreviewAll 组）
            let modelFilesData;
            try {
                const filesResponse = await fetch('/api/live2d/model_files/' + encodeURIComponent(live2dModelName));
                modelFilesData = await filesResponse.json();
            } catch (e) {
                console.warn('[Live2D Main] 获取模型文件列表失败:', e);
                return;
            }

            // 【竞态防护】如果中途角色被切换了，立刻中止
            if (window.lanlan_config?.lanlan_name !== initialLanlanName) return;
            if (!canContinue()) {
                console.log('[Live2D Main] 待机动作恢复已被新的交互取消');
                return;
            }

            const motionFiles = modelFilesData?.motion_files || [];
            if (!live2dIdleAnimation) {
                if (hasExplicitIdleAnimation) {
                    console.log('[Live2D Main] 待机动作已明确清空');
                    return;
                }
                if (motionFiles.length === 1) {
                    const singleMotion = typeof motionFiles[0] === 'string' ? motionFiles[0].trim() : '';
                    if (!singleMotion) {
                        console.log('[Live2D Main] 唯一的 motion 文件名为空，跳过恢复');
                        return;
                    }
                    live2dIdleAnimation = singleMotion;
                    console.log('[Live2D Main] 没有保存的待机动作，使用唯一 motion 作为默认待机动作:', live2dIdleAnimation);
                } else {
                    console.log('[Live2D Main] 没有保存的待机动作');
                    return;
                }
            }

            console.log('[Live2D Main] 开始恢复待机动作:', live2dIdleAnimation);

            const motionIndex = motionFiles.indexOf(live2dIdleAnimation);
            if (motionIndex < 0) {
                console.log('[Live2D Main] 待机动作不在当前模型的动作列表中:', live2dIdleAnimation);
                return;
            }

            // 4. 获取 Live2D 模型和 motionManager
            const live2dManager = window.live2dManager;
            const live2dModel = live2dManager?.getCurrentModel();
            if (!live2dModel) {
                console.log('[Live2D Main] Live2D 模型未加载，跳过恢复');
                return;
            }

            const internalModel = live2dModel.internalModel;
            if (!internalModel?.motionManager) {
                console.log('[Live2D Main] motionManager 不存在');
                return;
            }

            const motionManager = internalModel.motionManager;
            const groupName = 'PreviewAll';

            // 5. 使用隔离的 Helper 函数注入动作组配置
            const injectSuccess = _injectMotionGroupSafely(live2dModel, groupName, motionFiles);
            if (!injectSuccess) {
                console.log('[Live2D Main] 注入动作组失败，跳过动作恢复');
                return;
            }

            // 6. 加载动作（耗时操作）
            await motionManager.loadMotion(groupName, motionIndex);

            // 【最终竞态防护】加载完成后，确保角色没切走，且当前的 Live2D 模型实例还是我之前拿到的那个
            if (window.lanlan_config?.lanlan_name !== initialLanlanName || live2dManager?.getCurrentModel() !== live2dModel) {
                console.log('[Live2D Main] 模型或角色已切换，中止待机动作播放');
                return;
            }
            if (!canContinue()) {
                console.log('[Live2D Main] 待机动作恢复已被新的交互取消');
                return;
            }

            // 7. 设置循环播放
            const motionInstance = motionManager.motionGroups?.[groupName]?.[motionIndex];
            if (motionInstance) {
                if (typeof motionInstance.setIsLoop === 'function') {
                    motionInstance.setIsLoop(true);
                } else if (motionInstance._loop !== undefined) {
                    motionInstance._loop = true;
                }
            }

            // 8. 以 IDLE 优先级恢复；普通动作占用时不抢占
            if (live2dManager.hasActiveActionMotion(live2dModel)) return;
            const motionState = motionManager.state;
            if (
                (Number(motionState?.currentPriority || 0) === 1 || motionState?.reservedIdleGroup !== undefined)
                && typeof motionManager.stopAllMotions === 'function'
            ) {
                motionManager.stopAllMotions();
            }
            const started = await live2dModel.motion(groupName, motionIndex, 1);
            if (started) {
                console.log('[Live2D Main] 已恢复待机动作并循环播放:', live2dIdleAnimation);
            }

        } catch (error) {
            console.error('[Live2D Main] 恢复待机动作失败:', error);
        }
    }

    // 暴露给全局作用域，供 live2d-init.js 调用
    window.restoreLive2DIdleAnimationOnMainPage = restoreLive2DIdleAnimationOnMainPage;

    // =====================================================================
    // Hide / Show main UI (called when entering/leaving model manager)
    // =====================================================================

    /**
     * Hide main-page model rendering (entering model manager).
     */
    I.handleHideMainUI = function handleHideMainUI(options) {
        if (!_isModelHostPage()) return;
        options = options || {};
        var skipHiddenStateUpdate = options.skipHiddenStateUpdate || options.preserveHiddenState;
        if (!skipHiddenStateUpdate) {
            mainUIHideOwners[getMainUIHideOwner(options)] = true;
            setMainUIHiddenByModelManager(true);
        }
        console.log('[UI] 隐藏主界面并暂停渲染');

        try {
            // Hide Live2D
            var live2dContainer = document.getElementById('live2d-container');
            if (live2dContainer) {
                live2dContainer.style.display = 'none';
                live2dContainer.classList.add('hidden');
            }

            var live2dCanvas = document.getElementById('live2d-canvas');
            if (live2dCanvas) {
                live2dCanvas.style.visibility = 'hidden';
                live2dCanvas.style.pointerEvents = 'none';
            }

            // Hide VRM
            var vrmContainer = document.getElementById('vrm-container');
            if (vrmContainer) {
                vrmContainer.style.display = 'none';
                vrmContainer.classList.add('hidden');
            }

            var vrmCanvas = document.getElementById('vrm-canvas');
            if (vrmCanvas) {
                vrmCanvas.style.visibility = 'hidden';
                vrmCanvas.style.pointerEvents = 'none';
            }

            // Hide MMD
            var mmdContainer = document.getElementById('mmd-container');
            if (mmdContainer) {
                mmdContainer.style.display = 'none';
                mmdContainer.classList.add('hidden');
            }

            var mmdCanvas = document.getElementById('mmd-canvas');
            if (mmdCanvas) {
                // 热重载期间再次收到 hide/show 是模型管理窗口生命周期的正常竞态。
                // 此时只隐藏 Canvas，不清除会话 ID；否则重载收尾无法结束 overlay
                // 和恢复 Canvas，最终会留下已加载但不可见的 MMD 模型。
                var preserveMMDLoadingSession = !!activeMMDReloadCanvasSessionId
                    && mmdCanvas.dataset.mmdLoadingSessionId === activeMMDReloadCanvasSessionId;
                if (preserveMMDLoadingSession) {
                    mmdCanvas.style.visibility = 'hidden';
                    mmdCanvas.style.pointerEvents = 'none';
                } else {
                    clearMMDCanvasLoadingSession(mmdCanvas);
                }
            }

            // Pause render loops to save resources
            if (window.vrmManager && typeof window.vrmManager.pauseRendering === 'function') {
                window.vrmManager.pauseRendering();
            }

            if (window.live2dManager && typeof window.live2dManager.pauseRendering === 'function') {
                window.live2dManager.pauseRendering();
            }

            if (window.mmdManager && typeof window.mmdManager.pauseRendering === 'function') {
                window.mmdManager.pauseRendering();
            }

            // 停止 UI 更新循环（独立于渲染循环，pauseRendering 不会停止它们）
            // 如果不停止，UI 循环每帧会覆盖下面设置的 display: none，导致按钮重新出现
            if (window.vrmManager && window.vrmManager._uiUpdateLoopId != null) {
                cancelAnimationFrame(window.vrmManager._uiUpdateLoopId);
                window.vrmManager._uiUpdateLoopId = null;
            }
            if (window.mmdManager && window.mmdManager._uiUpdateLoopId != null) {
                cancelAnimationFrame(window.mmdManager._uiUpdateLoopId);
                window.mmdManager._uiUpdateLoopId = null;
            }
            // 空闲低频模式下 UI 循环可能停在 pending 再入定时器里：一并清掉
            for (const mgr of [window.vrmManager, window.mmdManager]) {
                if (mgr && mgr._uiLoopIdleTimeout) {
                    clearTimeout(mgr._uiLoopIdleTimeout);
                    mgr._uiLoopIdleTimeout = null;
                }
            }

            // 隐藏所有悬浮按钮、锁图标和返回按钮（它们挂载在 document.body 上，不随容器隐藏）。
            // 记录隐藏前的 display，避免恢复时清空 display 导致容器短暂按默认 block 布局显示，
            // 出现“语音控制/屏幕分享/猫爪/设置/请她离开”先挤在一起再分开的闪烁。
            document.querySelectorAll(
                '#live2d-floating-buttons, #vrm-floating-buttons, #mmd-floating-buttons, #pngtuber-floating-buttons, ' +
                '#live2d-lock-icon, #vrm-lock-icon, #mmd-lock-icon, #pngtuber-lock-icon, ' +
                '#live2d-return-button-container, #vrm-return-button-container, #mmd-return-button-container, #pngtuber-return-button-container'
            ).forEach(function (el) {
                if (!el.dataset.nekoPreHideDisplay) {
                    var isFloatingButtons = !!(el.id && /-floating-buttons$/.test(el.id));
                    var computedDisplay = '';
                    try {
                        computedDisplay = window.getComputedStyle(el).display || '';
                    } catch (_) {}
                    el.dataset.nekoPreHideDisplay = isFloatingButtons && !el.style.display && computedDisplay === 'none'
                        ? 'flex'
                        : (computedDisplay && computedDisplay !== 'none'
                            ? computedDisplay
                            : (el.style.display || 'none'));
                }
                el.style.display = 'none';
            });
        } catch (error) {
            console.error('[UI] 隐藏主界面失败:', error);
        }
    }

    /**
     * Show main-page model rendering (returning to main page).
     */
    I.handleShowMainUI = function handleShowMainUI(options) {
        if (!_isModelHostPage()) return;
        options = options || {};
        delete mainUIHideOwners[getMainUIHideOwner(options)];
        if (hasMainUIHideOwner()) return;
        setMainUIHiddenByModelManager(false);
        // 模型重载进行中时跳过：handleModelReload 自己会正确切换容器，
        // 此时 lanlan_config.model_type 尚未更新，handleShowMainUI 会
        // 错误地恢复旧模型类型的容器，导致需要切换两次才能成功。
        if (window._modelReloadInFlight) {
            console.log('[UI] 模型重载进行中，跳过显示主界面（避免覆盖正在切换的容器）');
            deferredMainUIShowAfterModelReload = Object.assign({}, options);
            return;
        }
        console.log('[UI] 显示主界面并恢复渲染');

        try {
            var currentModelType = window.lanlan_config?.model_type || 'live2d';
            var activeUiPrefix = currentModelType === 'pngtuber'
                ? 'pngtuber'
                : (currentModelType === 'live3d'
                    ? (((window.lanlan_config && window.lanlan_config.live3d_sub_type || '').toLowerCase() === 'mmd') ? 'mmd' : 'vrm')
                    : (currentModelType === 'vrm' ? 'vrm' : 'live2d'));

            function hideInactiveAvatarRuntime(prefix) {
                if (prefix === 'live2d') {
                    var live2dContainerToHide = document.getElementById('live2d-container');
                    if (live2dContainerToHide) {
                        live2dContainerToHide.style.display = 'none';
                        live2dContainerToHide.classList.add('hidden');
                    }
                    var live2dCanvasToHide = document.getElementById('live2d-canvas');
                    if (live2dCanvasToHide) {
                        live2dCanvasToHide.style.visibility = 'hidden';
                        live2dCanvasToHide.style.pointerEvents = 'none';
                    }
                } else if (prefix === 'vrm') {
                    var vrmContainerToHide = document.getElementById('vrm-container');
                    if (vrmContainerToHide) {
                        vrmContainerToHide.style.display = 'none';
                        vrmContainerToHide.classList.add('hidden');
                    }
                    var vrmCanvasToHide = document.getElementById('vrm-canvas');
                    if (vrmCanvasToHide) {
                        vrmCanvasToHide.style.visibility = 'hidden';
                        vrmCanvasToHide.style.pointerEvents = 'none';
                    }
                } else if (prefix === 'mmd') {
                    var mmdContainerToHide = document.getElementById('mmd-container');
                    if (mmdContainerToHide) {
                        mmdContainerToHide.style.display = 'none';
                        mmdContainerToHide.classList.add('hidden');
                    }
                    var mmdCanvasToHide = document.getElementById('mmd-canvas');
                    if (mmdCanvasToHide) {
                        mmdCanvasToHide.style.visibility = 'hidden';
                        mmdCanvasToHide.style.pointerEvents = 'none';
                    }
                } else if (prefix === 'pngtuber') {
                    if (window.pngtuberManager && typeof window.pngtuberManager.hide === 'function') {
                        window.pngtuberManager.hide();
                    }
                    if (typeof I.cleanupPNGTuberOverlayUI === 'function') {
                        I.cleanupPNGTuberOverlayUI();
                    }
                    var pngtuberContainerToHide = document.getElementById('pngtuber-container');
                    if (pngtuberContainerToHide) {
                        pngtuberContainerToHide.style.display = 'none';
                        pngtuberContainerToHide.style.visibility = 'hidden';
                        pngtuberContainerToHide.classList.add('hidden');
                        pngtuberContainerToHide.style.pointerEvents = 'none';
                    }
                    var pngtuberImageToHide = pngtuberContainerToHide ? pngtuberContainerToHide.querySelector('.pngtuber-image') : null;
                    if (pngtuberImageToHide) {
                        pngtuberImageToHide.style.visibility = 'hidden';
                        pngtuberImageToHide.style.pointerEvents = 'none';
                    }
                }
                document.querySelectorAll('#' + prefix + '-floating-buttons, #' + prefix + '-lock-icon, #' + prefix + '-return-button-container')
                    .forEach(function (el) {
                        el.style.display = 'none';
                        el.style.visibility = 'hidden';
                        el.style.opacity = '0';
                        delete el.dataset.nekoPreHideDisplay;
                    });
            }

            ['live2d', 'vrm', 'mmd', 'pngtuber'].forEach(function (prefix) {
                if (prefix !== activeUiPrefix) hideInactiveAvatarRuntime(prefix);
            });
            console.log('[UI] 当前模型类型:', currentModelType);

            if (currentModelType === 'vrm') {
                // Show VRM
                var vrmContainer = document.getElementById('vrm-container');
                if (vrmContainer) {
                    vrmContainer.style.display = 'block';
                    vrmContainer.classList.remove('hidden');
                    console.log('[UI] VRM 容器已显示，display:', vrmContainer.style.display);
                }

                var vrmCanvas = document.getElementById('vrm-canvas');
                if (vrmCanvas) {
                    vrmCanvas.style.visibility = 'visible';
                    vrmCanvas.style.pointerEvents = 'auto';
                    console.log('[UI] VRM canvas 已显示，visibility:', vrmCanvas.style.visibility);
                }

                // Resume VRM rendering
                if (window.vrmManager && typeof window.vrmManager.resumeRendering === 'function') {
                    window.vrmManager.resumeRendering();
                }
                // 重启 VRM UI 更新循环（被 handleHideMainUI 停止）
                if (window.vrmManager && window.vrmManager._uiUpdateLoopId == null
                    && typeof window.vrmManager._startUIUpdateLoop === 'function') {
                    window.vrmManager._snapUIPosition = true;
                    window.vrmManager._startUIUpdateLoop();
                }
            } else if (currentModelType === 'live3d') {
                // Live3D: determine sub-type from config
                var live3dSubType = (window.lanlan_config && window.lanlan_config.live3d_sub_type || '').toLowerCase();

                if (live3dSubType === 'mmd') {
                    var mmdContainerR = document.getElementById('mmd-container');
                    if (mmdContainerR) {
                        mmdContainerR.style.display = 'block';
                        mmdContainerR.classList.remove('hidden');
                    }
                    var mmdCanvasR = document.getElementById('mmd-canvas');
                    var hasActiveLoadingSession = mmdCanvasR && !!mmdCanvasR.dataset.mmdLoadingSessionId;
                    if (mmdCanvasR && !hasActiveLoadingSession) {
                        mmdCanvasR.style.visibility = 'visible';
                        mmdCanvasR.style.pointerEvents = 'auto';
                    }
                    if (window.mmdManager && typeof window.mmdManager.resumeRendering === 'function') {
                        window.mmdManager.resumeRendering();
                    }
                    // 重启 MMD UI 更新循环（被 handleHideMainUI 停止）
                    // UI 循环会自动管理浮动按钮和锁图标的显示/定位
                    if (window.mmdManager && window.mmdManager._uiUpdateLoopId == null
                        && typeof window.mmdManager._startUIUpdateLoop === 'function') {
                        window.mmdManager._snapUIPosition = true;
                        window.mmdManager._startUIUpdateLoop();
                    }
                } else {
                    var vrmContainerR = document.getElementById('vrm-container');
                    if (vrmContainerR) {
                        vrmContainerR.style.display = 'block';
                        vrmContainerR.classList.remove('hidden');
                    }
                    var vrmCanvasR = document.getElementById('vrm-canvas');
                    if (vrmCanvasR) {
                        vrmCanvasR.style.visibility = 'visible';
                        vrmCanvasR.style.pointerEvents = 'auto';
                    }
                    if (window.vrmManager && typeof window.vrmManager.resumeRendering === 'function') {
                        window.vrmManager.resumeRendering();
                    }
                    if (window.vrmManager && window.vrmManager._uiUpdateLoopId == null
                        && typeof window.vrmManager._startUIUpdateLoop === 'function') {
                        window.vrmManager._snapUIPosition = true;
                        window.vrmManager._startUIUpdateLoop();
                    }
                }
            } else if (currentModelType === 'pngtuber') {
                var pngtuberContainer = document.getElementById('pngtuber-container');
                if (pngtuberContainer) {
                    pngtuberContainer.style.display = 'block';
                    pngtuberContainer.style.visibility = 'visible';
                    pngtuberContainer.classList.remove('hidden');
                    pngtuberContainer.style.pointerEvents = 'none';
                }
                var pngtuberImage = pngtuberContainer ? pngtuberContainer.querySelector('.pngtuber-image') : null;
                if (pngtuberImage) {
                    pngtuberImage.style.visibility = 'visible';
                    pngtuberImage.style.pointerEvents = 'auto';
                }
                if (window.pngtuberManager && typeof window.pngtuberManager.show === 'function') {
                    window.pngtuberManager.show();
                } else if (window.pngtuberManager && typeof window.pngtuberManager.resumeRendering === 'function') {
                    window.pngtuberManager.resumeRendering();
                }
            } else {
                // Show Live2D
                var live2dContainer = document.getElementById('live2d-container');
                if (live2dContainer) {
                    live2dContainer.style.display = 'block';
                    live2dContainer.classList.remove('hidden');
                    console.log('[UI] Live2D 容器已显示，display:', live2dContainer.style.display);
                }

                var live2dCanvas = document.getElementById('live2d-canvas');
                if (live2dCanvas) {
                    live2dCanvas.style.visibility = 'visible';
                    live2dCanvas.style.pointerEvents = 'auto';
                    console.log('[UI] Live2D canvas 已显示，visibility:', live2dCanvas.style.visibility);
                }

                // Resume Live2D rendering
                if (window.live2dManager && typeof window.live2dManager.resumeRendering === 'function') {
                    window.live2dManager.resumeRendering();
                }
            }

            // 只恢复常规悬浮按钮与锁图标。
            // “请她回来”按钮默认由“请她离开”流程显示；从角色外形/模型管理窗口返回时
            // 如果在这里把 return-button-container 的 display 从 none 清空，会凭空多出一个返回按钮。
            // 恢复为隐藏前的 display（如 flex/block），并让浮动按钮首两帧保持不可见，
            // 等 UI 更新循环完成定位后再显露，避免按钮先挤在一起再分开。
            var restoringFloatingEls = Array.from(document.querySelectorAll(
                '#live2d-floating-buttons, #vrm-floating-buttons, #mmd-floating-buttons, #pngtuber-floating-buttons, ' +
                '#live2d-lock-icon, #vrm-lock-icon, #mmd-lock-icon, #pngtuber-lock-icon'
            )).filter(function (el) {
                return el && el.id && el.id.indexOf(activeUiPrefix + '-') === 0;
            });
            var hiddenFloatingButtonEls = [];
            restoringFloatingEls.forEach(function (el) {
                var restoreDisplay = el.dataset.nekoPreHideDisplay || '';
                var isFloatingButtons = !!(el.id && /-floating-buttons$/.test(el.id));
                if (restoreDisplay && restoreDisplay !== 'none') {
                    if (isFloatingButtons) {
                        el.style.visibility = 'hidden';
                        hiddenFloatingButtonEls.push(el);
                    }
                    el.style.display = isFloatingButtons ? 'flex' : restoreDisplay;
                }
                delete el.dataset.nekoPreHideDisplay;
            });
            if (hiddenFloatingButtonEls.length > 0) {
                requestAnimationFrame(function () {
                    requestAnimationFrame(function () {
                        hiddenFloatingButtonEls.forEach(function (el) {
                            if (!el || !el.isConnected || el.style.display === 'none') return;
                            el.style.removeProperty('visibility');
                        });
                    });
                });
            }
            document.querySelectorAll(
                '#live2d-return-button-container, #vrm-return-button-container, #mmd-return-button-container, #pngtuber-return-button-container'
            ).forEach(function (el) {
                if (!el || !el.id || el.id.indexOf(activeUiPrefix + '-') !== 0) {
                    if (el) el.style.display = 'none';
                    return;
                }
                if (!el.getAttribute('data-neko-return-visible')) {
                    el.style.display = 'none';
                }
            });
        } catch (error) {
            console.error('[UI] 显示主界面失败:', error);
        }
    }

    // =====================================================================
    // Voice chat composer sync (cross-window)
    // =====================================================================

    Object.assign(window.appInterpage, I.mod || {});
})();
