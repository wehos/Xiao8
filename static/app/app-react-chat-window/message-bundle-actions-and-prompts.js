/**
 * app-react-chat-window/message-bundle-actions-and-prompts.js
 * Host-side controller for the exported React chat window.
 *
 * Contract copied from the original entrypoint:
 * - Dynamically loads the React bundle if needed
 * - Owns window open/close/minimize/drag state
 * - Owns chat view props + messages state
 * - Exposes a stable bridge for host code / IPC adapters
 * Load all parts in filename order; this is a classic global script (no import/export).
 */
(function () {
    'use strict';

    window.reactChatWindowHost = window.reactChatWindowHost || {};
    const I = window.__appReactChatWindowParts || (window.__appReactChatWindowParts = {});
    I.ensureBundleLoaded = function ensureBundleLoaded() {
        if (window.NekoChatWindow && (typeof window.NekoChatWindow.mount === 'function' || typeof window.NekoChatWindow.mountChatWindow === 'function')) {
            return Promise.resolve(window.NekoChatWindow);
        }

        if (I.loadedPromise) return I.loadedPromise;

        I.loadedPromise = new Promise(function (resolve, reject) {
            var existing = document.querySelector('script[data-react-chat-window-bundle="true"]');
            if (existing) {
                // Script already finished loading but API is missing — re-create it
                if (existing.readyState === 'loaded' || existing.readyState === 'complete' || existing.dataset.loaded === 'true') {
                    if (window.NekoChatWindow && (typeof window.NekoChatWindow.mount === 'function' || typeof window.NekoChatWindow.mountChatWindow === 'function')) {
                        resolve(window.NekoChatWindow);
                    } else {
                        existing.parentNode.removeChild(existing);
                        // Fall through to create a fresh script element below
                    }
                } else if (existing.dataset.error === 'true') {
                    // Script previously failed to load — remove stale element and recreate
                    existing.parentNode.removeChild(existing);
                    // Fall through to create a fresh script element below
                } else {
                    existing.addEventListener('load', function () {
                        existing.dataset.loaded = 'true';
                        if (window.NekoChatWindow && (typeof window.NekoChatWindow.mount === 'function' || typeof window.NekoChatWindow.mountChatWindow === 'function')) {
                            resolve(window.NekoChatWindow);
                        } else {
                            reject(new Error('React chat bundle loaded but API is missing'));
                        }
                    }, { once: true });
                    existing.addEventListener('error', function () {
                        existing.dataset.error = 'true';
                        reject(new Error('React chat bundle failed to load'));
                    }, { once: true });
                    return;
                }
            }

            var script = document.createElement('script');
            script.src = I.BUNDLE_SRC + '?v=' + Date.now();
            script.async = true;
            script.dataset.reactChatWindowBundle = 'true';

            script.onload = function () {
                if (window.NekoChatWindow && (typeof window.NekoChatWindow.mount === 'function' || typeof window.NekoChatWindow.mountChatWindow === 'function')) {
                    resolve(window.NekoChatWindow);
                } else {
                    reject(new Error('React chat bundle loaded but API is missing'));
                }
            };

            script.onerror = function () {
                script.dataset.error = 'true';
                reject(new Error('React chat bundle failed to load'));
            };

            document.body.appendChild(script);
        }).catch(function (error) {
            I.loadedPromise = null;
            throw error;
        });

        return I.loadedPromise;
    }

    I.getStoredPosition = function getStoredPosition() {
        try {
            var rawLeft = localStorage.getItem(I.STORAGE_LEFT_KEY);
            var rawTop = localStorage.getItem(I.STORAGE_TOP_KEY);
            if (rawLeft === null || rawTop === null) return null;
            var left = Number(rawLeft);
            var top = Number(rawTop);
            if (Number.isFinite(left) && Number.isFinite(top)) {
                return { left: left, top: top };
            }
        } catch (_) {}
        return null;
    }

    I.persistPosition = function persistPosition(left, top) {
        try {
            localStorage.setItem(I.STORAGE_LEFT_KEY, String(Math.round(left)));
            localStorage.setItem(I.STORAGE_TOP_KEY, String(Math.round(top)));
        } catch (_) {}
    }

    I.rememberExpandedShellPosition = function rememberExpandedShellPosition(left, top) {
        if (I.isMobileWidth()) return;
        if (!Number.isFinite(left) || !Number.isFinite(top)) return;
        I.savedExpandedShellPosition = {
            left: Math.round(left),
            top: Math.round(top)
        };
    }

    function snapshotExpandedShellPositionFromShell() {
        var shell = I.getShell();
        if (!shell || I.isMobileWidth()) return;
        var rect = shell.getBoundingClientRect();
        I.rememberExpandedShellPosition(rect.left, rect.top);
    }

    I.persistSize = function persistSize(width, height) {
        try {
            localStorage.setItem(I.STORAGE_WIDTH_KEY, String(Math.round(width)));
            localStorage.setItem(I.STORAGE_HEIGHT_KEY, String(Math.round(height)));
        } catch (_) {}
    }

    function getStoredSize() {
        try {
            var rawWidth = localStorage.getItem(I.STORAGE_WIDTH_KEY);
            var rawHeight = localStorage.getItem(I.STORAGE_HEIGHT_KEY);
            if (rawWidth === null || rawHeight === null) return null;
            var width = Number(rawWidth);
            var height = Number(rawHeight);
            if (Number.isFinite(width) && Number.isFinite(height) && width >= 320 && height >= 280) {
                return { width: width, height: height };
            }
        } catch (_) {}
        return null;
    }

    function restoreSize() {
        var shell = I.getShell();
        if (!shell || I.isMobileWidth()) return;

        var stored = getStoredSize();
        if (stored) {
            shell.style.width = stored.width + 'px';
            shell.style.height = stored.height + 'px';
        }
    }

    I.clampPosition = function clampPosition(left, top, options) {
        var shell = I.getShell();
        if (!shell) {
            return { left: left, top: top };
        }

        var rect = shell.getBoundingClientRect();
        var width = rect.width || 960;
        var headerHeight = 52;
        var visibleHeight = options && options.fullHeight === true
            ? (rect.height || headerHeight)
            : headerHeight;
        var maxLeft = Math.max(0, window.innerWidth - width);
        var maxTop = Math.max(0, window.innerHeight - visibleHeight);

        return {
            left: Math.max(0, Math.min(maxLeft, left)),
            top: Math.max(0, Math.min(maxTop, top))
        };
    }

    I.ensureChatSurfaceVisible = function ensureChatSurfaceVisible() {
        var mode = I.getCurrentChatSurfaceMode();
        if (mode === 'compact' && !I.minimized) {
            var currentCompactRect = I.getCurrentCompactSurfaceRect();
            var compactTarget = I.getCompactSurfaceTarget();
            if (!currentCompactRect || !compactTarget) return false;

            var compactHeight = compactTarget.height || I.COMPACT_SURFACE_DEFAULT_HEIGHT;
            var compactChanged = Math.abs(compactTarget.left - currentCompactRect.left) >= 0.5
                || Math.abs(compactTarget.top - currentCompactRect.top) >= 0.5
                || Math.abs(compactTarget.width - currentCompactRect.width) >= 0.5
                || Math.abs(compactHeight - currentCompactRect.height) >= 0.5;
            if (!compactChanged) return false;

            return !!I.applyCompactSurfaceRect(
                compactTarget.left,
                compactTarget.top,
                compactTarget.width,
                compactHeight,
                { persist: false }
            );
        }

        if (mode !== 'full' || I.minimized) return false;
        var shell = I.getShell();
        if (!shell) return false;

        var rect = shell.getBoundingClientRect();
        if (!(rect.width > 0) || !(rect.height > 0)) return false;

        var clamped = I.clampPosition(rect.left, rect.top, { fullHeight: true });
        if (Math.abs(clamped.left - rect.left) < 0.5 && Math.abs(clamped.top - rect.top) < 0.5) {
            return false;
        }

        shell.style.left = clamped.left + 'px';
        shell.style.top = clamped.top + 'px';
        shell.style.transform = 'none';
        return true;
    }

    I.applyPosition = function applyPosition(left, top) {
        var shell = I.getShell();
        if (!shell) return;

        var clamped = I.clampPosition(left, top);
        shell.style.left = clamped.left + 'px';
        shell.style.top = clamped.top + 'px';
        shell.style.transform = 'none';
    }

    I.applyCompactSurfacePosition = function applyCompactSurfacePosition(left, top) {
        var shell = I.getShell();
        if (!shell) return;

        var rect = shell.getBoundingClientRect();
        var metrics = I.getCompactSurfaceMetrics();
        var width = metrics.width || rect.width || I.COMPACT_SURFACE_MAX_WIDTH;
        var height = metrics.height || I.COMPACT_SURFACE_DEFAULT_HEIGHT;
        I.applyCompactSurfaceRect(left, top, width, height, { persist: true });
    }

    function positionWindowAtLeftMiddle() {
        var shell = I.getShell();
        if (!shell || I.isMobileWidth()) return;

        var rect = shell.getBoundingClientRect();
        var left = Math.max(0, Math.round(window.innerWidth * I.DESKTOP_DEFAULT_LEFT_RATIO));
        var top = Math.max(0, Math.round((window.innerHeight - rect.height) / 2));
        I.applyPosition(left, top);
        I.rememberExpandedShellPosition(left, top);
        I.persistPosition(left, top);
    }

    I.restorePosition = function restorePosition() {
        var shell = I.getShell();
        if (!shell) return;

        if (I.isMobileWidth()) {
            // 宽度由 CSS calc(100vw - 12px) 控制；transform 的 translate 会污染 applyPosition 坐标。
            shell.style.removeProperty('width');
            shell.style.removeProperty('transform');
            // 不清 height：清掉会让 shell 瞬间回到 CSS 的 `height:auto;max-height:85vh`，
            // grid `auto 1fr auto` 父容器塌缩会把 .message-list 的 clientHeight 挤到几十 px，
            // 浏览器 clamp scrollTop → 0，下一帧 syncMobileContentLayout() 恢复 height 时已经来不及。
            // 保留旧像素值，让紧随其后的 syncMobileContentLayout() 直接覆写，避免中间态。
            // 不清 left/top：手机端允许 expanded 在任意位置飘；只按新视口 clamp 一次，避免旋屏/键盘后溢出。
            if (shell.style.left || shell.style.top) {
                var rect = shell.getBoundingClientRect();
                var clampedLeft = Math.max(0, Math.min(rect.left, window.innerWidth - rect.width));
                var clampedTop = Math.max(0, Math.min(rect.top, window.innerHeight - rect.height));
                shell.style.left = clampedLeft + 'px';
                shell.style.top = clampedTop + 'px';
            }
            return;
        }

        restoreSize();

        var stored = I.getStoredPosition();
        if (stored) {
            I.applyPosition(stored.left, stored.top);
            I.rememberExpandedShellPosition(stored.left, stored.top);
        } else {
            positionWindowAtLeftMiddle();
        }
    }

    // full 专属位置恢复（桌面）：有用户拖/缩后记住的位置就还原过去，否则保持 base
    // CSS 的居中（top/left:50% + translate）。绝不退回 restorePosition() 的
    // positionWindowAtLeftMiddle() —— 那会把完整窗口甩到屏幕左中。
    I.restoreFullPosition = function restoreFullPosition() {
        if (I.isMobileWidth()) return;
        var shell = I.getShell();
        if (!shell) return;
        var stored = I.getStoredPosition();
        if (!stored) return; // 无记忆位置 → 维持 CSS 居中
        restoreSize();
        I.applyPosition(stored.left, stored.top);
        I.rememberExpandedShellPosition(stored.left, stored.top);
    }

    I.mountWindow = function mountWindow() {
        var root = I.getRoot();
        if (!root) return false;

        var api = window.NekoChatWindow;
        var mount = api && (api.mount || api.mountChatWindow);
        if (typeof mount !== 'function') return false;

        mount(root, I.buildRenderProps());
        I.mounted = true;
        return true;
    }

    I.renderWindow = function renderWindow() {
        var overlay = I.getOverlay();
        if (!overlay || overlay.hidden) return;
        if (!I.mounted && I.getCurrentChatSurfaceMode() === 'compact') {
            I.seedCompactSurfaceAnchorForRender();
        }
        I.mountWindow();
        I.scheduleMobileContentLayout();
    }

    I.dispatchHostEvent = function dispatchHostEvent(name, detail) {
        window.dispatchEvent(new CustomEvent(I.EVENT_PREFIX + name, { detail: detail }));
    }

    I.handleMessageAction = function handleMessageAction(message, action) {
        var detail = {
            message: message,
            action: action
        };

        if (typeof I.state.onMessageAction === 'function') {
            try {
                I.state.onMessageAction(message, action);
            } catch (error) {
                console.error('[ReactChatWindow] onMessageAction failed:', error);
            }
        }

        I.dispatchHostEvent('action', detail);
    }

    I.handleComposerSubmit = function handleComposerSubmit(payload) {
        if (
            I.state.homeTutorialInteractionLocked
            || I.state.homeTutorialInputLocked
            || I.isHomeTutorialInteractionLocked()
            || I.getEffectiveComposerHidden()
        ) {
            return;
        }
        var requestId = payload && typeof payload.requestId === 'string' && payload.requestId
            ? payload.requestId
            : ('req-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8));
        var detail = {
            text: payload && typeof payload.text === 'string' ? payload.text : '',
            requestId: requestId
        };

        if (typeof I.isCatLocalChatActive === 'function' && I.isCatLocalChatActive()) {
            if (!detail.text.trim()) return;
            if (typeof I.submitCatLocalChatText === 'function') {
                I.submitCatLocalChatText(detail);
            }
            return;
        }

        var hasAttachments = I.state.composerAttachments && I.state.composerAttachments.length > 0;
        if (!detail.text.trim() && !hasAttachments) return;

        // Clear stale GalGame options as soon as the user sends anything; the
        // next turn-end will trigger a fresh fetch if the mode is still on.
        // invalidatePendingGalgameRequest unconditionally bumps the seq + aborts
        // the in-flight fetch (so a still-pending wait callback or response
        // can't land into the new turn context); we only re-render when the
        // visible state actually changed.
        if (I.invalidatePendingGalgameRequest()) {
            I.renderWindow();
        }

        // Store last submitted text for rollback on RESPONSE_TOO_LONG
        // Preserve original whitespace so rollback restores exactly what the user typed
        if (detail.text.trim()) {
            I.state.pendingRollbackDrafts[detail.requestId] = detail.text;
        } else {
            delete I.state.pendingRollbackDrafts[detail.requestId];
        }
        // Clear any stale rollback so it won't overwrite this new draft
        if (I.state.rollbackDraft) {
            console.log('[ROLLBACK] handleComposerSubmit: clearing rollbackDraft length=' + I.state.rollbackDraft.length + ' key=' + I.state._rollbackKey);
        }
        I.state.rollbackDraft = '';

        if (I.state.choicePrompt && I.state.choicePrompt.source === 'new_user_icebreaker') {
            var prompt = I.state.choicePrompt;
            var icebreakerDetail = {
                sessionId: prompt.sessionId || '',
                text: detail.text,
                requestId: detail.requestId
            };
            I.state.choicePrompt = null;
            delete I.state.pendingRollbackDrafts[detail.requestId];
            I.renderWindow();
            window.dispatchEvent(new CustomEvent('neko:icebreaker-free-text-submitted', { detail: icebreakerDetail }));
            I.dispatchHostEvent('icebreaker-free-text-submit', icebreakerDetail);
            try {
                var interpage = window.appInterpage;
                if (interpage && typeof interpage.postIcebreakerFreeTextSubmitted === 'function') {
                    interpage.postIcebreakerFreeTextSubmitted({
                        sessionId: icebreakerDetail.sessionId,
                        text: icebreakerDetail.text,
                        requestId: icebreakerDetail.requestId
                    });
                }
            } catch (error) {
                console.warn('[NewUserIcebreaker] free text broadcast failed:', error);
            }
            return;
        }

        if (typeof I.state.onComposerSubmit === 'function') {
            try {
                I.state.onComposerSubmit(detail);
            } catch (error) {
                console.error('[ReactChatWindow] onComposerSubmit failed:', error);
            }
        } else if (window.appButtons && typeof window.appButtons.sendTextPayload === 'function') {
            window.appButtons.sendTextPayload(detail.text, { source: 'react-chat-window', requestId: detail.requestId });
        } else {
            var input = I.$('textInputBox');
            var sendButton = I.$('textSendButton');
            if (input && sendButton) {
                input.value = detail.text;
                sendButton.click();
            } else {
                console.warn('[ReactChatWindow] no composer submit handler available');
            }
        }

        I.dispatchHostEvent('submit', detail);
    }

    I.prepareCompactHistoryDropSubmit = function prepareCompactHistoryDropSubmit(payload) {
        var detail = payload || {};
        var text = typeof detail.text === 'string' ? detail.text.trim() : '';
        var images = Array.isArray(detail.images) ? detail.images : [];
        if (!text && images.length === 0) return false;

        if (I.invalidatePendingGalgameRequest()) {
            I.renderWindow();
        }

        var requestId = typeof detail.requestId === 'string' ? detail.requestId : '';
        if (requestId) {
            if (text) {
                I.state.pendingRollbackDrafts[requestId] = text;
            } else {
                delete I.state.pendingRollbackDrafts[requestId];
            }
        }
        if (I.state.rollbackDraft) {
            console.log('[ROLLBACK] prepareCompactHistoryDropSubmit: clearing rollbackDraft length=' + I.state.rollbackDraft.length + ' key=' + I.state._rollbackKey);
        }
        I.state.rollbackDraft = '';
        return true;
    }

    I.queuePendingAvatarInteraction = function queuePendingAvatarInteraction(detail) {
        var interactionId = String(
            detail && (detail.interactionId || detail.interaction_id) || ''
        ).trim();
        if (interactionId && I.state.pendingAvatarInteractions.some(function (pending) {
            return String(pending && (pending.interactionId || pending.interaction_id) || '').trim() === interactionId;
        })) {
            return;
        }
        I.state.pendingAvatarInteractions.push(detail);
        if (I.state.pendingAvatarInteractions.length > 8) {
            I.state.pendingAvatarInteractions.shift();
        }
    };

    I.handleAvatarInteraction = function handleAvatarInteraction(payload) {
        var detail = payload || {};

        if (typeof I.state.onAvatarInteraction === 'function') {
            try {
                I.state.onAvatarInteraction(detail);
            } catch (error) {
                console.error('[ReactChatWindow] onAvatarInteraction failed:', error);
            }
        } else {
            // React can become interactive before app-buttons binds the
            // authoritative Host callback. Preserve the committed interaction;
            // the auxiliary DOM event is not a delivery acknowledgement.
            I.queuePendingAvatarInteraction(detail);
            console.warn('[ReactChatWindow] avatar interaction handler not ready; queued for host binding');
        }

        I.dispatchHostEvent('avatar-interaction', detail);
    }

    I.handleAvatarToolStateChange = function handleAvatarToolStateChange(payload) {
        var detail = payload || {};

        if (typeof I.state.onAvatarToolStateChange === 'function') {
            try {
                I.state.onAvatarToolStateChange(detail);
            } catch (error) {
                console.error('[ReactChatWindow] onAvatarToolStateChange failed:', error);
            }
        }

        I.dispatchHostEvent('avatar-tool-state', detail);
    }

    I.handleComposerImportImage = function handleComposerImportImage() {
        if (typeof I.state.onComposerImportImage === 'function') {
            try {
                I.state.onComposerImportImage();
            } catch (error) {
                console.error('[ReactChatWindow] onComposerImportImage failed:', error);
            }
        } else if (window.appButtons && typeof window.appButtons.openImageImportPicker === 'function') {
            window.appButtons.openImageImportPicker();
        } else {
            console.warn('[ReactChatWindow] no import image handler available');
        }

        I.dispatchHostEvent('import-image', {});
    }

    I.handleComposerScreenshot = function handleComposerScreenshot() {
        var handled = false;
        if (typeof I.state.onComposerScreenshot === 'function') {
            try {
                I.state.onComposerScreenshot();
                handled = true;
            } catch (error) {
                console.error('[ReactChatWindow] onComposerScreenshot failed:', error);
                handled = false;
            }
        } else if (window.appButtons && typeof window.appButtons.captureScreenshotToPendingList === 'function') {
            try {
                window.appButtons.captureScreenshotToPendingList();
                handled = true;
            } catch (error) {
                console.error('[ReactChatWindow] captureScreenshotToPendingList failed:', error);
                handled = false;
            }
        } else {
            console.warn('[ReactChatWindow] no screenshot handler available');
        }

        I.dispatchHostEvent('screenshot', { handled: handled });
        return handled;
    }

    I.handleComposerRemoveAttachment = function handleComposerRemoveAttachment(attachmentId) {
        if (typeof I.state.onComposerRemoveAttachment === 'function') {
            try {
                I.state.onComposerRemoveAttachment(String(attachmentId || ''));
            } catch (error) {
                console.error('[ReactChatWindow] onComposerRemoveAttachment failed:', error);
            }
        } else if (window.appButtons && typeof window.appButtons.removePendingAttachmentById === 'function') {
            window.appButtons.removePendingAttachmentById(String(attachmentId || ''));
        } else {
            console.warn('[ReactChatWindow] no remove attachment handler available');
        }

        I.dispatchHostEvent('remove-attachment', { attachmentId: attachmentId });
    }

    /**
     * Rollback last submitted text to the React composer input.
     * Called when backend discards response due to RESPONSE_TOO_LONG.
     */
    I.rollbackLastDraft = function rollbackLastDraft(requestId) {
        var rollbackText = (requestId && Object.prototype.hasOwnProperty.call(I.state.pendingRollbackDrafts, requestId))
            ? I.state.pendingRollbackDrafts[requestId]
            : '';
        if (!rollbackText) return;
        // Use a unique key each time so React useEffect can distinguish invocations
        I.state.rollbackDraft = rollbackText;
        I.state._rollbackKey = 'rb-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6);
        delete I.state.pendingRollbackDrafts[requestId];
        console.log('[ROLLBACK] rollbackLastDraft: rollbackDraftPresent=true length=' + I.state.rollbackDraft.length + ' key=' + I.state._rollbackKey);
        I.renderWindow();
    }

    I.clearPendingRollbackDraft = function clearPendingRollbackDraft(requestId) {
        if (!requestId) return;
        delete I.state.pendingRollbackDrafts[requestId];
    }

    I.handleJukeboxClick = function handleJukeboxClick() {
        try {
            if (typeof window.__nekoJukeboxToggle === 'function') {
                // Electron 多窗口模式：通过 IPC 打开独立 Jukebox 窗口
                window.__nekoJukeboxToggle();
            } else if (typeof window.Jukebox !== 'undefined' && typeof window.Jukebox.toggle === 'function') {
                window.Jukebox.toggle();
            } else {
                console.warn('[ReactChatWindow] Jukebox not available');
            }
        } finally {
            I.dispatchHostEvent('jukebox-click', {});
        }
    }

    function captureAvatarDirect() {
        if (!window.avatarPortrait || typeof window.avatarPortrait.capture !== 'function') {
            // Electron 多窗口模式：通过 IPC 请求 Pet 窗口截取头像
            if (window.__NEKO_MULTI_WINDOW__ && typeof window.__nekoRequestAvatarPreview === 'function') {
                // 优先使用已缓存的外部头像
                if (window.appChatAvatar && typeof window.appChatAvatar.getCurrentAvatarDataUrl === 'function') {
                    var cached = window.appChatAvatar.getCurrentAvatarDataUrl();
                    if (cached) {
                        window.dispatchEvent(new CustomEvent('chat-avatar-preview-updated', {
                            detail: { dataUrl: cached, source: 'cached' }
                        }));
                        I.showToast(I.getI18nText('chat.avatarPreviewReady', '头像已更新'), 2500);
                        return;
                    }
                }
                I.showToast(I.getI18nText('chat.avatarPreviewGenerating', '正在生成当前头像...'), 2000);
                var finished = false;
                var timerId = null;
                var finish = function (success) {
                    if (finished) return;
                    finished = true;
                    window.removeEventListener('neko:avatar-preview-ipc-result', onResult);
                    if (timerId) { clearTimeout(timerId); timerId = null; }
                    if (success) {
                        I.showToast(I.getI18nText('chat.avatarPreviewReady', '头像已更新'), 2500);
                    } else {
                        I.showToast(I.getI18nText('chat.avatarPreviewFailed', '生成头像失败'), 3000);
                    }
                };
                var onResult = function (e) {
                    finish(!!(e.detail && e.detail.dataUrl));
                };
                window.addEventListener('neko:avatar-preview-ipc-result', onResult);
                timerId = setTimeout(function () { finish(false); }, 10000);
                try {
                    window.__nekoRequestAvatarPreview();
                } catch (err) {
                    console.error('[ReactChatWindow] __nekoRequestAvatarPreview threw:', err);
                    finish(false);
                }
                return;
            }
            I.showToast(I.getI18nText('chat.avatarPreviewUnavailable', '头像预览功能尚未就绪。'), 3000);
            return;
        }

        I.showToast(I.getI18nText('chat.avatarPreviewGenerating', '正在生成当前头像...'), 2000);

        window.avatarPortrait.capture({
            width: 320, height: 320, padding: 0.035,
            shape: 'rounded', radius: 40,
            background: 'rgba(255, 255, 255, 0.96)',
            includeDataUrl: true
        }).then(function (result) {
            if (result && result.dataUrl) {
                // Dispatch the same event that app-chat-adapter.js already listens to
                window.dispatchEvent(new CustomEvent('chat-avatar-preview-updated', {
                    detail: {
                        dataUrl: result.dataUrl,
                        modelType: result.modelType || '',
                        source: 'react-chat-window'
                    }
                }));
                I.showToast(I.getI18nText('chat.avatarPreviewReady', '头像已更新'), 2500);
            } else {
                console.warn('[ReactChatWindow] Avatar capture completed without dataUrl');
                I.showToast(I.getI18nText('chat.avatarPreviewFailed', '生成头像失败'), 3000);
            }
        }).catch(function (error) {
            console.error('[ReactChatWindow] Avatar capture failed:', error);
            I.showToast(I.getI18nText('chat.avatarPreviewFailed', '生成头像失败'), 3000);
        });
    }

    I.handleAvatarGeneratorClick = function handleAvatarGeneratorClick() {
        try {
            // 统一走独立头像预览弹窗；弹窗模块自行处理缓存与 IPC 回退。
            if (window.appChatAvatar && typeof window.appChatAvatar.showPopup === 'function') {
                var anchor = document.getElementById('avatarPreviewHeaderButton')
                    || document.getElementById('avatarPreviewButton')
                    || null;
                window.appChatAvatar.showPopup(anchor);
                return;
            }
            // 极端兜底：弹窗模块加载失败时仍保持原有直采逻辑。
            captureAvatarDirect();
        } finally {
            I.dispatchHostEvent('avatar-generator-click', {});
        }
    }

    I.handleExportConversationClick = function handleExportConversationClick() {
        try {
            if (window.appChatExport && typeof window.appChatExport.open === 'function') {
                window.appChatExport.open();
                return;
            }
            var exportButton = document.getElementById('exportConversationButton');
            if (exportButton && typeof exportButton.click === 'function') {
                exportButton.click();
                return;
            }
            I.showToast(I.getI18nText('chat.exportPreviewFailed', '导出预览生成失败'), 3000);
        } finally {
            I.dispatchHostEvent('chat-export-click', {});
        }
    }

    function syncSubtitleWindowFromTranslateToggle(enabled) {
        var subtitleWindow = window.nekoSubtitleWindow;
        if (!subtitleWindow) return;
        try {
            if (typeof subtitleWindow.setEnabled === 'function') {
                subtitleWindow.setEnabled(!!enabled);
            } else if (enabled && typeof subtitleWindow.show === 'function') {
                subtitleWindow.show();
            } else if (!enabled && typeof subtitleWindow.hide === 'function') {
                subtitleWindow.hide();
            }
        } catch (error) {
            console.warn('[ReactChatWindow] subtitle window visibility sync failed:', error);
        }
    }

    I.setTranslateEnabled = function setTranslateEnabled(enabled, options) {
        var requestOptions = options || {};
        var next = !!enabled;
        var shouldPersist = requestOptions.persist !== false;
        var syncSource = requestOptions.source || 'react-chat-host-set-enabled';
        if (requestOptions.syncBridge !== false) {
            try {
                var bridge = window.subtitleBridge;
                if (bridge && typeof bridge.setSubtitleEnabled === 'function') {
                    bridge.setSubtitleEnabled(next, {
                        persist: shouldPersist,
                        source: syncSource
                    });
                } else {
                    throw new Error('subtitleBridge.setSubtitleEnabled unavailable');
                }
            } catch (err) {
                console.warn('[ReactChatWindow] bridge set enabled failed, using fallback:', err);
                var appSt = window.appState;
                var subtitleStore = window.nekoSubtitleShared;
                if (appSt) appSt.subtitleEnabled = next;
                var synced = false;
                if (subtitleStore && typeof subtitleStore.updateSettings === 'function') {
                    try {
                        subtitleStore.updateSettings({
                            subtitleEnabled: next
                        }, {
                            persist: shouldPersist,
                            source: syncSource
                        });
                        synced = true;
                    } catch (storeErr) {
                        console.warn('[ReactChatWindow] subtitle shared update failed:', storeErr);
                    }
                }
                if (!synced && shouldPersist) {
                    try {
                        localStorage.setItem('subtitleEnabled', String(next));
                    } catch (storageErr) {
                        console.warn('[ReactChatWindow] localStorage subtitleEnabled persist failed:', storageErr);
                    }
                }
            }
        }

        if (shouldPersist
            && window.appSettings
            && typeof window.appSettings.saveSettings === 'function') {
            try {
                window.appSettings.saveSettings();
            } catch (saveErr) {
                console.warn('[ReactChatWindow] appSettings.saveSettings failed:', saveErr);
            }
        }

        I.state.viewProps = Object.assign({}, I.ensureViewProps(), { translateEnabled: next });
        syncSubtitleWindowFromTranslateToggle(next);
        I.renderWindow();

        if (requestOptions.suppressHostEvent !== true) {
            I.dispatchHostEvent('translate-toggle', { enabled: next });
        }
        return next;
    }

    I.handleTranslateToggle = function handleTranslateToggle() {
        var bridge = window.subtitleBridge;
        var next;

        try {
            if (bridge && typeof bridge.toggle === 'function') {
                // Use full toggle with runtime side effects (hide/show subtitle, clear timers, re-translate)
                next = bridge.toggle();
            } else {
                throw new Error('subtitleBridge.toggle unavailable');
            }
        } catch (err) {
            console.warn('[ReactChatWindow] bridge.toggle failed, using fallback:', err);
            // Fallback: flip flag manually if bridge not loaded or threw
            var appSt = window.appState;
            var subtitleStore = window.nekoSubtitleShared;
            var subtitleState = subtitleStore && typeof subtitleStore.getSettings === 'function'
                ? subtitleStore.getSettings()
                : null;
            var viewProps = I.ensureViewProps();
            var current = (viewProps && typeof viewProps.translateEnabled !== 'undefined')
                ? !!viewProps.translateEnabled
                : (
                    appSt && typeof appSt.subtitleEnabled !== 'undefined'
                        ? !!appSt.subtitleEnabled
                        : (subtitleState ? !!subtitleState.subtitleEnabled : (localStorage.getItem('subtitleEnabled') === 'true'))
                );
            next = !current;
            if (appSt) appSt.subtitleEnabled = next;
            if (subtitleStore && typeof subtitleStore.updateSettings === 'function') {
                subtitleStore.updateSettings({
                    subtitleEnabled: next
                }, {
                    source: 'react-chat-fallback-toggle'
                });
            } else {
                localStorage.setItem('subtitleEnabled', String(next));
            }
            if (window.appSettings && typeof window.appSettings.saveSettings === 'function') {
                window.appSettings.saveSettings();
            }
        }

        // Update React prop to reflect new state
        I.state.viewProps = Object.assign({}, I.ensureViewProps(), { translateEnabled: next });
        syncSubtitleWindowFromTranslateToggle(next);
        I.renderWindow();

        I.dispatchHostEvent('translate-toggle', { enabled: next });
    }

    function syncTranslateEnabledFromSubtitleState(enabled) {
        var next = !!enabled;
        var props = I.ensureViewProps();
        if (props.translateEnabled === next) {
            return;
        }
        I.state.viewProps = Object.assign({}, props, { translateEnabled: next });
        I.renderWindow();
    }

    function handleSubtitleSettingsChange(event) {
        var detail = event && event.detail ? event.detail : {};
        var changedKeys = Array.isArray(detail.changedKeys) ? detail.changedKeys : [];
        if (changedKeys.length && changedKeys.indexOf('subtitleEnabled') === -1) {
            return;
        }
        if (!detail.state || typeof detail.state.subtitleEnabled === 'undefined') {
            return;
        }
        syncTranslateEnabledFromSubtitleState(detail.state.subtitleEnabled);
    }

    I.bindSubtitleSettingsSync = function bindSubtitleSettingsSync() {
        var subtitleStore = window.nekoSubtitleShared;
        if (subtitleStore && typeof subtitleStore.subscribeSettings === 'function') {
            subtitleStore.subscribeSettings(function(stateValue, detail) {
                var syncDetail = detail || {};
                if (!syncDetail.state) {
                    syncDetail = Object.assign({}, syncDetail, { state: stateValue });
                }
                handleSubtitleSettingsChange({ detail: syncDetail });
            });
            return;
        }
        window.addEventListener('neko-subtitle-settings-change', handleSubtitleSettingsChange);
    }

    // ============================ GalGame mode ============================
    function isGalgameModeTemporarilyDisabled() {
        return !!I.state.galgameTemporarilyDisabled;
    }

    I.isHomeTutorialRunning = function isHomeTutorialRunning() {
        var manager = window.universalTutorialManager;
        return !!(
            manager
            && manager.currentPage === 'home'
            && manager.isTutorialRunning
        );
    }

    I.isHomeTutorialInteractionLocked = function isHomeTutorialInteractionLocked() {
        if (I.state.homeTutorialInteractionLocked || I.isHomeTutorialRunning()) {
            return true;
        }
        try {
            return typeof window.isNekoHomeTutorialInteractionLocked === 'function'
                && window.isNekoHomeTutorialInteractionLocked() === true;
        } catch (_) {
            return false;
        }
    }

    I.setGalgameModeTemporarilyDisabled = function setGalgameModeTemporarilyDisabled(disabled, options) {
        var requestOptions = options || {};
        var next = !!disabled;
        var changed = I.state.galgameTemporarilyDisabled !== next;
        I.state.galgameTemporarilyDisabled = next;

        if (next) {
            I.setGalgameModeEnabled(false, {
                persist: false,
                skipRender: requestOptions.skipRender === true
            });
        } else if (changed) {
            I.setGalgameModeEnabled(I.readGalgameModePreference(), {
                persist: false,
                suppressRefetch: true,
                skipRender: requestOptions.skipRender === true
            });
        }
    }

    function syncTutorialGalgameSuppression() {
        I.setGalgameModeTemporarilyDisabled(
            I.state.homeTutorialInputLocked || I.isHomeTutorialInteractionLocked(),
            { skipRender: true }
        );
    }

    I.setGalgameModeEnabled = function setGalgameModeEnabled(enabled, options) {
        var requestOptions = options || {};
        var next = !!enabled;
        if (next && !requestOptions.force && (isGalgameModeTemporarilyDisabled() || I.isHomeTutorialInteractionLocked())) {
            next = false;
        }
        var changed = I.state.galgameModeEnabled !== next;
        I.state.galgameModeEnabled = next;
        if (!next) {
            I.state.galgameOptions = [];
            I.state.galgameOptionsLoading = false;
            I.state._galgameRequestSeq += 1;
            // Toggling off mid-fetch must also kill the in-flight request so
            // the summary-tier inference doesn't keep running uselessly until
            // the 30s timeout (or finishes and is silently discarded).
            abortPendingGalgameFetch();
        }
        I.applyGalgameBodyClass();
        if ((!requestOptions || requestOptions.persist !== false) && !isGalgameModeTemporarilyDisabled()) {
            I.persistGalgameModePreference(next);
        }
        if (!requestOptions.skipRender) {
            I.renderWindow();
        }
        if (changed) {
            // 派发 effective 值（与 body class 一致）：composer 隐藏期间即使
            // setGalgameModeEnabled(true) 也广播 enabled=false，避免监听器
            // (chat.html syncWindowToGalgameMin 等) 与 body class 状态分歧。
            I.dispatchHostEvent('galgame-mode-change', { enabled: I.getEffectiveGalgameEnabled() });
            // OFF→ON: if the chat overlay is currently visible, refetch the
            // latest turn's options so the user sees A/B/C immediately rather
            // than waiting for the next turn-end. Gating on overlay visibility
            // avoids wasting a summary-tier call during init() (where the
            // window is still hidden) and respects the same skip rule the
            // turn-end handler uses for voice-only / proactive paths.
            if (next && !requestOptions.suppressRefetch) {
                var overlay = I.getOverlay();
                if (overlay && !overlay.hidden) {
                    I.fetchGalgameOptionsForLatestTurn();
                }
            }
        }
    }

    I.waitForAssistantBubblesFlushed = function waitForAssistantBubblesFlushed(maxWaitMs) {
        // Resolve as soon as app-chat-adapter's realistic-mode queue is empty
        // and not in the middle of processing a sentence. In merge / non-Gemini
        // paths the queue is never populated and the predicate is true on the
        // first check, so this just collapses to a microtask.
        return new Promise(function (resolve) {
            var deadline = Date.now() + (typeof maxWaitMs === 'number' ? maxWaitMs : 4000);
            function isDrained() {
                var q = window._realisticGeminiQueue;
                var processing = !!window._isProcessingRealisticQueue;
                var queueEmpty = !Array.isArray(q) || q.length === 0;
                return queueEmpty && !processing;
            }
            if (isDrained()) {
                resolve();
                return;
            }
            var pollId = setInterval(function () {
                if (isDrained() || Date.now() >= deadline) {
                    clearInterval(pollId);
                    resolve();
                }
            }, 100);
        });
    }

    function isNewUserIcebreakerChatMessage(message) {
        if (!message) return false;
        var messageId = typeof message.id === 'string' ? message.id : '';
        if (messageId.indexOf('icebreaker-user-') === 0
                || messageId.indexOf('icebreaker-assistant-') === 0) {
            return true;
        }
        if (message.source === 'new_user_icebreaker') return true;
        var icebreaker = message.icebreaker && typeof message.icebreaker === 'object'
            ? message.icebreaker
            : {};
        return icebreaker.source === 'new_user_icebreaker';
    }

    function getRecentGalgameMessageHistory() {
        var msgs = Array.isArray(I.state.messages) ? I.state.messages : [];
        var collected = [];
        for (var i = msgs.length - 1; i >= 0 && collected.length < I.GALGAME_HISTORY_LIMIT; i--) {
            var m = msgs[i];
            if (!m) continue;
            if (isYuiGuideChatMessage(m)) continue;
            if (m.role !== 'assistant' && m.role !== 'user') continue;
            if (isNewUserIcebreakerChatMessage(m)) {
                // While the latest conversation turn belongs to the scripted
                // icebreaker, do not fall back to an older ordinary assistant
                // turn and generate unrelated GalGame choices for it.
                if (!collected.length) return [];
                continue;
            }
            var text = '';
            if (Array.isArray(m.blocks)) {
                for (var j = 0; j < m.blocks.length; j++) {
                    var block = m.blocks[j];
                    if (block && block.type === 'text' && typeof block.text === 'string') {
                        text += (text ? '\n' : '') + block.text;
                    }
                }
            }
            text = text.replace(/\[play_music:[^\]]*(\]|$)/g, '').trim();
            if (!text) continue;
            collected.push({ role: m.role, text: text });
        }
        return collected.reverse();
    }

    function pickAcceptLanguage() {
        // GalGame option copy is part of the visible UI, so its template locale
        // follows the interface instead of the active character's conversation
        // language preference.
        try {
            if (typeof window.getCurrentLocale === 'function') {
                var loc = window.getCurrentLocale();
                if (loc) return String(loc);
            }
        } catch (_) {}
        if (window.i18next && typeof window.i18next.language === 'string') return window.i18next.language;
        if (typeof navigator !== 'undefined' && typeof navigator.language === 'string') return navigator.language;
        return '';
    }

    var GALGAME_FETCH_TIMEOUT_MS = 30000;
    var _galgameAbortController = null;

    function abortPendingGalgameFetch() {
        if (_galgameAbortController) {
            try { _galgameAbortController.abort(); } catch (_) {}
            _galgameAbortController = null;
        }
    }

    I.fetchGalgameOptionsForLatestTurn = function fetchGalgameOptionsForLatestTurn() {
        if (isGalgameModeTemporarilyDisabled()) return;
        if (!I.state.galgameModeEnabled) return;
        // icebreaker 脚本选项激活期间不抢选项槽——含揭示延迟内 prompt 已就位、按钮尚未
        // 露出（choicePrompt 非 null 但 getRevealedChoicePrompt 返回 null）的那段。否则
        // icebreaker 台词的 turn-end 会触发 galgame A/B/C，在脚本选项露出前挤进同一槽位
        // （Codex P2）。icebreaker 运行在 home tutorial 之外，galgameTemporarilyDisabled
        // 此时并不覆盖它，故须单独按 choicePrompt 拦。
        if (I.state.choicePrompt && I.state.choicePrompt.source === 'new_user_icebreaker') return;
        var history = getRecentGalgameMessageHistory();
        if (!history.length) return;
        if (history[history.length - 1].role !== 'assistant') return;

        // Cancel any prior in-flight request — keeps summary-tier load down
        // when turns arrive faster than the model can answer, and ensures a
        // hung server side isn't held open while the panel is no longer
        // listening for it.
        abortPendingGalgameFetch();
        var controller = (typeof AbortController === 'function') ? new AbortController() : null;
        _galgameAbortController = controller;
        var requestSeq = ++I.state._galgameRequestSeq;
        I.state.galgameOptions = [];
        I.state.galgameOptionsLoading = true;
        I.renderWindow();

        // 30s timeout cleanup: clears loading state in addition to aborting,
        // so the catch's blanket AbortError swallow doesn't leave the panel
        // stuck. Aborts triggered by invalidation paths instead bump the seq
        // *and* clear state up front, so the catch's seq-mismatch return is
        // still the right thing for those.
        var timeoutId = controller ? setTimeout(function () {
            timeoutId = null;
            if (_galgameAbortController === controller) {
                _galgameAbortController = null;
            }
            try { controller.abort(); } catch (_) {}
            if (requestSeq !== I.state._galgameRequestSeq) return;
            I.state.galgameOptions = [];
            I.state.galgameOptionsLoading = false;
            I.renderWindow();
        }, GALGAME_FETCH_TIMEOUT_MS) : null;

        var payload = {
            messages: history,
            language: pickAcceptLanguage()
        };
        try {
            if (window.appState && typeof window.appState.lanlan_name === 'string' && window.appState.lanlan_name) {
                payload.lanlan_name = window.appState.lanlan_name;
            }
        } catch (_) {}
        try {
            // getCurrentUserName() returns the literal English placeholder 'You'
            // when no real user name can be resolved. Sending that overrides the
            // backend's localized GALGAME_DEFAULT_MASTER_PLACEHOLDER fallback,
            // so we only forward a name when it's a genuine user-set value.
            var currentUserName = I.getCurrentUserName();
            if (typeof currentUserName === 'string' && currentUserName && currentUserName !== 'You') {
                payload.master_name = currentUserName;
            }
        } catch (_) {}

        function clearTimer() {
            if (timeoutId !== null) {
                clearTimeout(timeoutId);
                timeoutId = null;
            }
            if (_galgameAbortController === controller) {
                _galgameAbortController = null;
            }
        }

        fetch('/api/galgame/options', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            signal: controller ? controller.signal : undefined
        }).then(function (resp) {
            if (!resp || !resp.ok) throw new Error('HTTP ' + (resp && resp.status));
            return resp.json();
        }).then(function (data) {
            clearTimer();
            if (requestSeq !== I.state._galgameRequestSeq) return;
            var opts = (data && Array.isArray(data.options)) ? data.options.slice(0, 3) : [];
            opts = opts.filter(function (o) {
                return o && typeof o.label === 'string' && typeof o.text === 'string';
            }).map(function (o) {
                return { label: String(o.label).slice(0, 4), text: String(o.text) };
            });
            I.state.galgameOptions = opts;
            I.state.galgameOptionsLoading = false;
            I.renderWindow();
        }).catch(function (err) {
            clearTimer();
            if (requestSeq !== I.state._galgameRequestSeq) return;
            // Aborts come from invalidation paths that have already cleared
            // visible state, so swallow them silently.
            if (err && err.name === 'AbortError') return;
            console.warn('[ReactChatWindow] galgame options fetch failed:', err);
            I.state.galgameOptions = [];
            I.state.galgameOptionsLoading = false;
            I.renderWindow();
        });
    }

    I.handleGalgameModeToggle = function handleGalgameModeToggle() {
        if (I.isHomeTutorialInteractionLocked()) {
            I.setGalgameModeEnabled(false, { persist: false });
            return;
        }
        if (isGalgameModeTemporarilyDisabled()) {
            I.setGalgameModeEnabled(false, { persist: false });
            return;
        }
        // setGalgameModeEnabled handles the OFF→ON refetch internally.
        I.setGalgameModeEnabled(!I.state.galgameModeEnabled);
    }

    I.handleGalgameOptionSelect = function handleGalgameOptionSelect(option) {
        if (I.isHomeTutorialInteractionLocked() || I.getEffectiveComposerHidden()) return;
        if (!option || typeof option.text !== 'string') return;
        var text = option.text.trim();
        if (!text) return;
        // Clear options immediately so the panel doesn't keep stale entries while
        // the next turn streams in.
        I.state.galgameOptions = [];
        I.state.galgameOptionsLoading = false;
        I.state._galgameRequestSeq += 1;
        I.renderWindow();

        var detail = {
            text: text,
            requestId: 'galgame-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8),
            source: 'galgame-option',
            label: option.label
        };
        I.handleComposerSubmit(detail);
        I.dispatchHostEvent('galgame-option-select', detail);
    }

    // ---- 通用 ChoicePrompt：mini-game invite / new-user icebreaker ----
    // React 组件 onChoice 回调把 option + source 一起传上来。source==='galgame'
    // 走旧路径（dummy fallback，正常不会到这里——galgame 仍然走 onGalgameOptionSelect
    // 直接 callback；这里只是 BC 兜底）。

    I.handleChoiceSelect = function handleChoiceSelect(option, source) {
        if (!option || typeof option.choice !== 'string') return;
        if (source === 'new_user_icebreaker') {
            var prompt = I.state.choicePrompt;
            if (!prompt || prompt.source !== 'new_user_icebreaker') return;
            var detail = {
                sessionId: option.sessionId || prompt.sessionId || '',
                gameType: prompt.gameType || 'new_user_icebreaker',
                choice: option.choice,
                label: option.label || '',
                option: option
            };
            I.state.choicePrompt = null;
            I.renderWindow();
            window.dispatchEvent(new CustomEvent('neko:icebreaker-choice-selected', {
                detail: detail
            }));
            I.dispatchHostEvent('icebreaker-choice-selected', detail);
            try {
                var interpage = window.appInterpage;
                if (interpage && typeof interpage.postIcebreakerChoiceSelected === 'function') {
                    interpage.postIcebreakerChoiceSelected({
                        sessionId: detail.sessionId,
                        gameType: detail.gameType,
                        choice: detail.choice,
                        label: detail.label,
                        option: option
                    });
                }
            } catch (error) {
                console.warn('[NewUserIcebreaker] choice broadcast failed:', error);
            }
            return;
        }
        if (I.isHomeTutorialInteractionLocked() || I.getEffectiveComposerHidden()) return;
        if (source === 'galgame') {
            // Forward to legacy galgame handler if it shows up here
            if (typeof option.text === 'string') {
                I.handleGalgameOptionSelect(option);
            }
            return;
        }
        if (source === 'mini_game_invite') {
            handleMiniGameInviteChoice(option);
            return;
        }
    }

    I.handleCompactChatStateChange = function handleCompactChatStateChange(nextCompactChatState) {
        var normalized = I.normalizeCompactChatState(nextCompactChatState);
        if (normalized === 'input' && (I.state.homeTutorialInputLocked || I.isHomeTutorialInteractionLocked())) {
            return;
        }
        if (typeof I.isCatLocalChatActive === 'function'
            && I.isCatLocalChatActive()
            && normalized !== 'input') {
            return;
        }
        I.setCompactChatState(normalized);
    }

    // React compact 输入框/胶囊左侧毛绒球点按 → 折叠为 minimized。最小化控制权在宿主，
    // 走 setChatSurfaceMode('minimized')（既有的 setMinimized + 位置持久化 + chat-surface-mode-change
    // 派发都在其中）；不用 toggleMinimized——毛绒球只在非 minimized 态出现，语义恒为「收起」。
    //
    // 说明：曾尝试「在 compact 态把 root 原位慢速淡出 + 提前异步显示独立球」做收起 overlap，但
    // (a) 淡出期间 compact 仍可交互/未冻结，提前显示的球与 relayout/hit-test/moveTop 互相打架 →
    //     球在光标下抖、cursor 在箭头/手之间闪；
    // (b) 给 root 设 inline opacity:0 做淡出，频繁点击时 setMinimized 因状态 desync 提前 return、
    //     跳过 opacity 复位 → root 卡在 0 = 输入框隐身。
    // 这套机制竞态太重，已回退。现仅保留一个安全增强：给按下的毛绒球补「按下挤压」动画（CSS），
    // 留一个短延时让挤压/擦除动画播完，再直接提交 minimized 最终态；不要复用 full surface 的整窗缩放。
    // 独立球出现时的「放大长出」靠球自身入场动画（neko-ball-appear），与此处解耦。
    var COMPACT_MINIMIZE_PRESS_MS = 280; // = neko-compact-collapse-wipe 擦除时长：擦完再瞬时折叠
    var compactMinimizePressTimer = 0;
    I.compactMinimizeCancelSeq = 0; // 折叠取消序号（见 clearCompactMinimizePressTimer），传给 React
    I.compactMinimizeBallTargetAnchor = null;
    // 清掉挂起的「按下挤压→瞬时折叠」延时。窗口关闭/动画取消时必须调用，否则这个
    // 跨 280ms 存活的回调会在窗口已关闭/重开后仍 setChatSurfaceMode('minimized')，
    // 把更新后的状态覆盖成「幽灵最小化」（CodeRabbit Minor / Codex P2）。
    I.clearCompactMinimizePressTimer = function clearCompactMinimizePressTimer() {
        if (!compactMinimizePressTimer) return;
        window.clearTimeout(compactMinimizePressTimer);
        compactMinimizePressTimer = 0;
        I.compactMinimizeBallTargetAnchor = null;
        // 真清掉一个 pending 折叠延时 = 一次进行中的折叠被取消（如 280ms 内 closeWindow）。
        // 递增序号；buildRenderProps 把它当 prop 传给 React，让组件在重开时立即复位
        // compactCollapsing，而不必等 600ms 兜底——否则快速「关→重开」会让 compact 表面带着擦除
        // mask 的不可见末态 + 历史区临时关闭重新出现（Codex P2）。
        I.compactMinimizeCancelSeq += 1;
        I.pendingChatSurfaceMode = null;
    }
    I.handleCompactMinimizeRequest = function handleCompactMinimizeRequest() {
        if (I.isMinimizeTransitioning) return;
        I.rememberCompactMinimizeBallTargetAnchor();
        // reduced-motion：折叠擦除/按压挤压动画已被 CSS（@media prefers-reduced-motion: reduce）
        // 禁用，此时再延时 COMPACT_MINIMIZE_PRESS_MS(280ms) 才折叠，只会让窗口「点了没反应」一段，
        // 无任何动画反馈。无障碍模式下直接立即折叠，保持即时响应（Codex P2）。
        var reduceMotion = false;
        try {
            reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
        } catch (e) {}
        if (reduceMotion) {
            I.setChatSurfaceMode('minimized', { skipShellCollapseAnimation: true });
            return;
        }
        // 给按下的毛绒球补「按下挤压」弹性动画（CSS index.css neko-compact-minimize-press）。
        try {
            var pressIcons = document.querySelectorAll('.compact-chat-minimize-ball-icon');
            for (var pi = 0; pi < pressIcons.length; pi++) {
                var pressIcon = pressIcons[pi];
                pressIcon.classList.remove('neko-minimize-ball-pressing');
                void pressIcon.offsetWidth;
                pressIcon.classList.add('neko-minimize-ball-pressing');
            }
        } catch (e) {}
        // 防重入：擦除途中再次点最小化忽略。
        if (compactMinimizePressTimer) return;
        // #1 折叠方向性擦除（右→左收）由 App.tsx 的 compactCollapsing state 驱动 className 实现
        //    （onClick 里已 setCompactCollapsing(true)）—— 不再在此用 classList 加类（会被 React 重渲染覆盖）。
        // web 与 Electron 统一走这条 = 擦除时长的延时再瞬时折叠：web 若同步 setChatSurfaceMode
        // ('minimized') 会在下一帧前就让 isCompactSurface=false、卸载 .compact-chat-surface-shell，
        // 新加的 neko-compact-collapsing 擦除遮罩与蓝条淡出根本来不及渲染（Codex P2）。
        compactMinimizePressTimer = window.setTimeout(function () {
            compactMinimizePressTimer = 0;
            // 守卫：擦除期间宿主可能把 surface mode 切走（关闭/重开经
            // closeWindow→cancelActiveAnimation 清掉本 timer；或经公开 API setChatSurfaceMode
            // ('full') / setViewProps 直写 state 绕过清理）。只有「仍处于 compact」才执行这次
            // 延迟折叠，否则会用陈旧的 minimized 覆盖更新后的模式（Codex P2）。两端通用。
            if (I.getCurrentChatSurfaceMode() !== 'compact') return;
            I.setChatSurfaceMode('minimized', { skipShellCollapseAnimation: true });
        }, COMPACT_MINIMIZE_PRESS_MS);
    }

    function handleMiniGameInviteChoice(option) {
        if (I.isHomeTutorialInteractionLocked()) return;
        var prompt = I.state.choicePrompt;
        if (!prompt || prompt.source !== 'mini_game_invite') return;
        var sessionId = prompt.sessionId || '';
        // 暂存原 prompt 用于失败回滚——网络异常时让用户能再点一次（CodeRabbit
        // Major 指出原版 fetch fail 仅 console.warn，用户看着空 UI 不知道发生
        // 啥）。立即清 prompt 防连点；fail catch 里恢复。
        var rollbackPrompt = prompt;
        I.state.choicePrompt = null;
        I.renderWindow();

        var lanlanName = '';
        try {
            // 优先读 window.appState.lanlan_name —— 角色切换时 appState 先更新，
            // window.lanlan_config 可能滞后；用旧 lanlan_name 调 endpoint 会被
            // 后端按错误角色查 pending invite 直接 expired。同 GalGame 请求路径
            // 保持一致（CodeRabbit Major 指出）。
            lanlanName = (window.appState && window.appState.lanlan_name)
                || (window.lanlan_config && window.lanlan_config.lanlan_name)
                || '';
        } catch (_) {}

        var requestBody = {
            lanlan_name: lanlanName,
            choice: option.choice,
            session_id: sessionId
        };

        // accept 路径预开 popup（**仍在用户点击的同步上下文**）保留 user-gesture
        // 上下文。后续 fetch resolve 后再 window.open 会被浏览器 popup blocker
        // 识别为非手势触发拦截——pre-open 后 .location.href 注入 URL 不会被拦
        // （codex P2 指出原版 fetch 后 window.open 失败时 state 已 responded
        // 用户失去重试入口）。decline / later 路径不开窗口，无此处理。
        var preOpenedWindow = null;
        if (option.choice === 'accept') {
            try {
                preOpenedWindow = window.open('', '_blank');
                if (preOpenedWindow) {
                    // 给个临时占位文本免得用户看到 about:blank 一闪
                    try {
                        preOpenedWindow.document.write(
                            '<title>Loading…</title><body style="background:#111;color:#888;font:14px sans-serif;padding:20px">Loading mini-game…</body>'
                        );
                    } catch (_) {}
                }
            } catch (_) {
                preOpenedWindow = null;
            }
        }
        var closePreOpened = function () {
            if (preOpenedWindow && !preOpenedWindow.closed) {
                try { preOpenedWindow.close(); } catch (_) {}
            }
        };

        // 必须带 CSRF token：后端 endpoint 用 _validate_local_mutation_request
        // 拒绝缺 token 的请求，否则所有合法点击都会被 403 reject、prompt 已清掉
        // 但 invite state 没更新 —— codex P1 指出。沿用 nekoLocalMutationSecurity
        // 共享 helper（其它 prompt endpoint 同款），含 token 缺失时 refresh + 重
        // 试一次的协议。
        var bodyJson = JSON.stringify(requestBody);
        var doFetch = function (headers) {
            return fetch('/api/mini_game/invite/respond', {
                method: 'POST',
                headers: Object.assign({ 'Content-Type': 'application/json' }, headers || {}),
                body: bodyJson
            });
        };
        var sec = window.nekoLocalMutationSecurity;
        var firstHeadersPromise = sec && typeof sec.getMutationHeaders === 'function'
            ? sec.getMutationHeaders()
            : Promise.resolve({});
        firstHeadersPromise.then(doFetch).then(function (resp) {
            // 403 + csrf_validation_failed → refresh token 重试一次（与 prompt
            // endpoint 同协议）
            if (resp.status === 403 && sec && typeof sec.refreshToken === 'function') {
                return resp.clone().json().catch(function () { return null; }).then(function (errBody) {
                    var code = errBody && errBody.error_code;
                    if (code === 'csrf_validation_failed') {
                        return sec.refreshToken().then(function () {
                            return sec.getMutationHeaders();
                        }).then(doFetch);
                    }
                    return resp;
                });
            }
            return resp;
        }).then(function (resp) {
            return resp.ok ? resp.json() : Promise.reject(new Error('HTTP ' + resp.status));
        }).then(function (data) {
            if (!data || data.action !== 'open_game' || !data.game_url) {
                // 非 accept outcome（cooldown / suppress / expired）→ 关掉占位 popup
                closePreOpened();
                return;
            }
            // accept：优先注入 URL 进 pre-opened popup（保留用户手势上下文，
            // 浏览器 popup blocker 不拦）；pre-open 失败时 fallback 调
            // launchMiniGameInternal（可能被拦但留个 console.warn 兜底）。
            if (preOpenedWindow && !preOpenedWindow.closed) {
                try {
                    preOpenedWindow.location.href = data.game_url;
                    if (sessionId) {
                        I.state._launchedMiniGameSessionIds[sessionId] = true;
                    }
                    return;
                } catch (err) {
                    console.warn('[MiniGameInvite] pre-opened window navigation failed:', err);
                    closePreOpened();
                }
            }
            // pre-open 失败 fallback：直接 window.open（有被 popup blocker 拦
            // 的风险，但 accept-path-via-pre-open 已是主路径，到此处罕见）。
            launchMiniGameInternal({
                sessionId: sessionId,
                gameType: data.game_type || rollbackPrompt.gameType || '',
                url: data.game_url,
                source: 'button'
            });
        }).catch(function (err) {
            console.warn('[MiniGameInvite] respond endpoint failed:', err);
            closePreOpened();
            // 网络/服务异常 → 回滚 prompt 让用户能再试。但只在当前 prompt 仍是
            // null（即用户没在 fetch 期间触发新 prompt）且会话仍未被 launch 过的
            // 情况下回滚——否则强复活旧 UI 可能撞新邀请。
            if (I.state.choicePrompt === null
                    && sessionId
                    && !I.state._launchedMiniGameSessionIds[sessionId]) {
                I.state.choicePrompt = rollbackPrompt;
                I.renderWindow();
            }
        });
    }

    I.setMiniGameInvitePrompt = function setMiniGameInvitePrompt(payload) {
        if (!payload) return;
        var sessionId = String(payload.sessionId || '');
        if (!sessionId) return;
        // 已经为该 session 开过游戏了 → 忽略 stale options（罕见：邀请 push 比
        // 用户键盘/按钮路径慢，但为了对偶仍 guard 一下）
        if (I.state._launchedMiniGameSessionIds[sessionId]) return;
        var rawOptions = Array.isArray(payload.options) ? payload.options : [];
        if (!rawOptions.length) return;
        // map → filter，再 recheck 长度——后端数据异常导致全部 filter 掉时不
        // 渲染空按钮 prompt（CodeRabbit Minor 指出原版只检 raw 长度漏了这条）。
        var cleanedOptions = rawOptions.map(function (o) {
            return {
                choice: String((o && o.choice) || ''),
                label: String((o && o.label) || '')
            };
        }).filter(function (o) { return o.choice && o.label; });
        if (!cleanedOptions.length) {
            console.warn('[MiniGameInvite] all options filtered out, skipping render', payload);
            return;
        }
        I.state.choicePrompt = {
            source: 'mini_game_invite',
            sessionId: sessionId,
            gameType: String(payload.gameType || ''),
            options: cleanedOptions
        };
        // mini-game invite 占用 composer 底部 slot 视觉位 → galgame options
        // 让位（App.tsx 已把 galgame slot 在 choicePromptHasOptions 下不挂树）。
        // 这里同步 abort 任何 in-flight / pending wait 的 galgame fetch + 清掉
        // 残留 loading/options state：
        //   1) 不再浪费 summary tier 推理（proactive invite 文本基本是
        //      sudden-context，galgame option 生成大概率 timeout / unparseable
        //      → 全是 fallback，纯浪费）
        //   2) 防止 fetch 在 invite 解决前才返回，写回 state.galgameOptions
        //      让 invite dismiss 后老结果突然冒出来（A/B/C 选项是基于 invite
        //      文本生成的，与后续对话无关）
        I.invalidatePendingGalgameRequest();
        I.renderWindow();
    }

    var choicePromptRevealTimer = null;

    // icebreaker choicePrompt 的「视觉揭示」延迟：state.choicePrompt 一旦设置就立刻生效
    // （handleComposerSubmit 据此把间隙内的自由文本判为 icebreaker free-text 而非普通
    // 聊天），但带 revealAt 时，按钮要等到该时刻才传给 React 组件（见 getRevealedChoicePrompt
    // + buildRenderProps）。延迟只藏按钮、不扣状态，这样既保留「选项晚于台词露出」的观感，
    // 又不会重新打开间隙内输入落到普通聊天的窗口。
    function scheduleChoicePromptReveal() {
        if (choicePromptRevealTimer) {
            window.clearTimeout(choicePromptRevealTimer);
            choicePromptRevealTimer = null;
        }
        var prompt = I.state.choicePrompt;
        if (!prompt || !prompt.revealAt) return;
        var remaining = prompt.revealAt - Date.now();
        if (remaining <= 0) {
            prompt.revealAt = 0;
            return;
        }
        choicePromptRevealTimer = window.setTimeout(function () {
            choicePromptRevealTimer = null;
            // 仅当同一个 prompt 仍在台上才揭示——期间被新 prompt 覆盖或清空则什么都不做。
            if (I.state.choicePrompt === prompt) {
                prompt.revealAt = 0;
                I.renderWindow();
            }
        }, remaining);
    }

    // 渲染层取用的 choicePrompt：揭示时刻未到的 icebreaker prompt 先把按钮藏起来（返回
    // null，视觉等同于尚未下发），其他 source 或已到点的照常返回。
    I.getRevealedChoicePrompt = function getRevealedChoicePrompt() {
        var prompt = I.state.choicePrompt;
        if (!prompt) return null;
        if (prompt.revealAt && Date.now() < prompt.revealAt) return null;
        return prompt;
    }

    I.setNewUserIcebreakerPrompt = function setNewUserIcebreakerPrompt(payload) {
        if (!payload) return;
        var sessionId = String(payload.sessionId || '');
        if (!sessionId) return;
        var rawOptions = Array.isArray(payload.options) ? payload.options : [];
        if (!rawOptions.length) return;
        var cleanedOptions = rawOptions.map(function (o) {
            return {
                choice: String((o && o.choice) || ''),
                label: String((o && o.label) || ''),
                sessionId: sessionId
            };
        }).filter(function (o) { return o.choice && o.label; });
        if (!cleanedOptions.length) {
            console.warn('[NewUserIcebreaker] all options filtered out, skipping render', payload);
            return;
        }
        var revealDelayMs = Number(payload.revealDelayMs) || 0;
        I.state.choicePrompt = {
            source: 'new_user_icebreaker',
            sessionId: sessionId,
            gameType: String(payload.gameType || 'new_user_icebreaker'),
            options: cleanedOptions,
            revealAt: revealDelayMs > 0 ? Date.now() + revealDelayMs : 0
        };
        I.invalidatePendingGalgameRequest();
        scheduleChoicePromptReveal();
        I.renderWindow();
    }

    I.setChoicePrompt = function setChoicePrompt(payload) {
        if (!payload || typeof payload !== 'object') return;
        var source = String(payload.source || '');
        if (source === 'new_user_icebreaker') {
            I.setNewUserIcebreakerPrompt(payload);
            return;
        }
        if (source === 'mini_game_invite') {
            I.setMiniGameInvitePrompt(payload);
        }
    }

    I.setIcebreakerChoicePrompt = function setIcebreakerChoicePrompt(payload) {
        I.setNewUserIcebreakerPrompt(payload);
    }

    I.clearChoicePromptBySource = function clearChoicePromptBySource(source, reason) {
        var normalizedSource = String(source || '');
        if (!normalizedSource) return false;
        if (normalizedSource !== 'new_user_icebreaker') return false;
        if (!I.state.choicePrompt || I.state.choicePrompt.source !== normalizedSource) return false;
        if (window.console && typeof window.console.debug === 'function') {
            window.console.debug('[NewUserIcebreaker] clearChoicePromptBySource:', normalizedSource, reason || '');
        }
        I.state.choicePrompt = null;
        if (choicePromptRevealTimer) {
            window.clearTimeout(choicePromptRevealTimer);
            choicePromptRevealTimer = null;
        }
        I.invalidatePendingGalgameRequest();
        I.renderWindow();
        return true;
    }

    I.clearIcebreakerChoicePrompt = function clearIcebreakerChoicePrompt(sessionId) {
        if (!I.state.choicePrompt || I.state.choicePrompt.source !== 'new_user_icebreaker') return false;
        if (sessionId && I.state.choicePrompt.sessionId !== String(sessionId)) return false;
        I.state.choicePrompt = null;
        if (choicePromptRevealTimer) {
            window.clearTimeout(choicePromptRevealTimer);
            choicePromptRevealTimer = null;
        }
        I.renderWindow();
        return true;
    }

    function dismissChoicePromptIfMatches(sessionId) {
        if (!sessionId) return;
        if (I.state.choicePrompt
                && I.state.choicePrompt.source === 'mini_game_invite'
                && I.state.choicePrompt.sessionId === sessionId) {
            I.state.choicePrompt = null;
            I.renderWindow();
        }
    }

    I.handleMiniGameInviteResolved = function handleMiniGameInviteResolved(payload) {
        if (!payload) return;
        var sessionId = String(payload.sessionId || '');
        // 任一 outcome（open_game / cooldown / suppress）都 dismiss 当前 prompt——
        // 跨窗口一致性。即便本 page 不是触发方，也保持 UI 同步。
        dismissChoicePromptIfMatches(sessionId);
        // launch path（仅 keyword 触发会带 game_url，button path backend 已不推
        // game_url）：多窗口 Electron 模式下 backend 通过 RAW_MESSAGE IPC 把
        // event 转给所有 page (pet + chat.html mirrors)，每个 page 都执行此函数。
        // 不分 ownership 直接 window.open 会让所有 page 各自开一个 game 窗口
        // （codex P2 指出，per-page _launchedMiniGameSessionIds 跨 page 不 dedupe）。
        // 约定：only **non-follower** owner page (pet / 单窗口) 处理 WS-trigger
        // launch；chat.html follower (window.__NEKO_MULTI_WINDOW__ === true) 仅
        // dismiss UI。Button path 不走这条 WS launch（HTTP 响应里 chat.html 自己
        // launch），所以不会双开。
        if (payload.action === 'open_game' && payload.url) {
            if (window.__NEKO_MULTI_WINDOW__) {
                return;  // chat.html follower：let pet leader 处理 launch
            }
            launchMiniGameInternal({
                sessionId: sessionId,
                gameType: String(payload.gameType || ''),
                url: payload.url,
                source: 'ws'
            });
        }
    }

    function launchMiniGameInternal(payload) {
        if (!payload || !payload.url) return;
        var sessionId = String(payload.sessionId || '');
        // 同一 session 只 open 一次：按钮 endpoint 直接 open 后，backend 还会 push
        // mini_game_invite_resolved（cross-window 一致性广播）；不 dedupe 会双开。
        if (sessionId && I.state._launchedMiniGameSessionIds[sessionId]) return;
        // window.open 在 Electron 模式下被主进程 setWindowOpenHandler 拦截开独立
        // BrowserWindow；普通浏览器是新 tab。'_blank' target 让浏览器治理一致。
        // dedupe flag 只在成功后设——popup blocker / throw 时让用户能再触发一次
        // (codex P2 + CodeRabbit Major 指出原版 set-before-open 会让失败的 session
        // 永远被 dedupe 锁死，prompt 已清掉用户彻底失去入口)。
        var opened = false;
        try {
            var w = window.open(payload.url, '_blank');
            if (!w) {
                console.warn('[MiniGameInvite] window.open returned null (popup blocked?)');
            } else {
                opened = true;
            }
        } catch (err) {
            console.warn('[MiniGameInvite] window.open failed:', err);
        }
        if (opened && sessionId) {
            I.state._launchedMiniGameSessionIds[sessionId] = true;
        }
    }

    I.setViewProps = function setViewProps(nextViewProps) {
        var nextProps = nextViewProps || {};
        var surfaceModeChanged = false;
        if (Object.prototype.hasOwnProperty.call(nextProps, 'chatSurfaceMode')) {
            var normalizedChatSurfaceMode = I.coerceChatSurfaceModeForHost(nextProps.chatSurfaceMode);
            var previousChatSurfaceMode = I.getCurrentChatSurfaceMode();
            surfaceModeChanged = normalizedChatSurfaceMode !== previousChatSurfaceMode;
            if (surfaceModeChanged
                && !Object.prototype.hasOwnProperty.call(nextProps, 'compactChatState')) {
                I.resetCompactChatState();
            }
            if (normalizedChatSurfaceMode !== 'minimized') {
                I.lastRestorableChatSurfaceMode = normalizedChatSurfaceMode;
            } else if (previousChatSurfaceMode !== 'minimized') {
                I.lastRestorableChatSurfaceMode = previousChatSurfaceMode;
            }
            I.state.chatSurfaceMode = normalizedChatSurfaceMode;
            if (surfaceModeChanged) {
                // 同 setChatSurfaceMode：mode 经此公开入口变更时取消挂起的延时折叠，防 full→compact
                // hop 内陈旧折叠回调误折叠刚恢复的表面（Codex P2）。timer 已=0 时为 no-op。
                I.clearCompactMinimizePressTimer();
                // setViewProps is a public entry point (exposed + the
                // `set-view-props` event), so it can land a real surface change —
                // e.g. an external `{chatSurfaceMode:'full'}`. Mirror
                // setChatSurfaceMode and persist the restorable preference, else
                // the switch is lost on reload (readChatSurfaceModePreference
                // returns the stale value). persistChatSurfaceModePreference
                // no-ops for minimized, which still restores via lastRestorable.
                I.persistChatSurfaceModePreference(normalizedChatSurfaceMode);
            }
        }
        if (Object.prototype.hasOwnProperty.call(nextProps, 'compactChatState')) {
            I.state.compactChatState = I.normalizeCompactChatState(nextProps.compactChatState);
        }
        I.state.viewProps = Object.assign({}, I.ensureViewProps(), nextProps, {
            chatSurfaceMode: I.getCurrentChatSurfaceMode(),
            compactChatState: I.getCurrentCompactChatState()
        });
        I.renderWindow();
        // setViewProps can now land a real surface change (e.g. compact -> the
        // revived `full`) because normalizeChatSurfaceMode preserves all three
        // modes. renderWindow only updates the React tree; the host shell's
        // data-chat-surface-mode attribute — which the compact CSS rules key off —
        // is owned by syncChatSurfaceModeUI, so sync it here too or the shell keeps
        // the stale mode and compact chrome leaks onto the new surface. (Minimize
        // transitions still route through setChatSurfaceMode/setMinimized.)
        if (surfaceModeChanged) {
            I.syncChatSurfaceModeUI();
        }
        return I.state.viewProps;
    }

    I.invalidatePendingGalgameRequest = function invalidatePendingGalgameRequest() {
        // Conversation advanced / switched / cleared — drop any in-flight
        // options fetch (or pending wait callback that hasn't fired yet) so
        // its response can't render stale A/B/C into the new context.
        // The seq bump must be UNCONDITIONAL: callers like
        // waitForAssistantBubblesFlushed snapshot _galgameRequestSeq before
        // their fetch goes out, so even when the panel is idle (loading
        // false, options empty) we still need to advance the seq to invalidate
        // those pending callbacks. The fetch itself is also aborted.
        I.state._galgameRequestSeq += 1;
        abortPendingGalgameFetch();
        var hadVisibleState = I.state.galgameOptionsLoading
            || (I.state.galgameOptions && I.state.galgameOptions.length > 0);
        if (hadVisibleState) {
            I.state.galgameOptions = [];
            I.state.galgameOptionsLoading = false;
        }
        return hadVisibleState;
    }

    I.setMessages = function setMessages(messages) {
        // Compute fallback start past any explicit sortKey in incoming batch
        var maxIncomingSortKey = Array.isArray(messages)
            ? messages.reduce(function (max, message) {
                var key = message && typeof message.sortKey === 'number' && Number.isFinite(message.sortKey)
                    ? message.sortKey : null;
                return (key !== null && key > max) ? key : max;
            }, -1)
            : -1;
        var nextSortKey = Math.max(I._sortKeySeq, maxIncomingSortKey + 1);
        var normalized = Array.isArray(messages)
            ? messages.map(function (message) {
                return I.normalizeMessage(message, nextSortKey++);
            }).filter(Boolean)
            : [];
        I.state.messages = I.sortMessages(normalized);
        I._sortKeySeq = nextSortKey;
        if (I.state.messages.length > MAX_MESSAGES) {
            I.state.messages = I.state.messages.slice(-MAX_MESSAGES);
        }
        I.invalidatePendingGalgameRequest();
        I.renderWindow();
        return I.state.messages;
    }

    I.setComposerHidden = function setComposerHidden(hidden) {
        var next = !!hidden;
        var wasEffectiveHidden = I.getEffectiveComposerHidden();
        var previousAttachmentsVisible = I.getEffectiveComposerAttachmentsVisible();
        I.state.composerHidden = next;
        var effectiveChanged = wasEffectiveHidden !== I.getEffectiveComposerHidden();
        if (effectiveChanged) {
            I.syncComposerAttachmentsVisibility(previousAttachmentsVisible);
            // composer 隐藏/显示切换会改变 effective galgame body class（参见
            // applyGalgameBodyClass），同步刷新一次；否则在 galgame ON 期间
            // 触发请她离开，body 仍带 galgame-mode-enabled，min-height:385px 撑住
            // 窗口底部一片空白，被用户感知为"输入框没隐藏"。
            I.applyGalgameBodyClass();
            // 复用现有 change 事件通知 chat.html 的 syncWindowToGalgameMin 等监听器
            // 重新评估窗口最小高度；effective OFF 时它会跳过撑高（b.height >= minH 兜底）。
            I.dispatchHostEvent('galgame-mode-change', { enabled: I.getEffectiveGalgameEnabled() });
        }
        I.renderWindow();
    }

    I.setGoodbyeComposerHidden = function setGoodbyeComposerHidden(hidden, reason) {
        var next = !!hidden;
        var wasEffectiveHidden = I.getEffectiveComposerHidden();
        var previousAttachmentsVisible = I.getEffectiveComposerAttachmentsVisible();
        try {
            window.__nekoGoodbyeChatComposerHidden = {
                hidden: next,
                reason: reason || (next ? 'goodbye' : 'return'),
                timestamp: Date.now()
            };
        } catch (_) {}
        if (I.state.goodbyeComposerHidden === next) {
            if (wasEffectiveHidden !== I.getEffectiveComposerHidden()) {
                I.syncComposerAttachmentsVisibility(previousAttachmentsVisible);
                I.renderWindow();
            }
            return;
        }
        I.state.goodbyeComposerHidden = next;
        var isEffectiveHidden = I.getEffectiveComposerHidden();
        var restoredEffectiveComposer = wasEffectiveHidden && !isEffectiveHidden;
        I.syncComposerAttachmentsVisibility(previousAttachmentsVisible);
        if (next) {
            if (typeof I.isCatLocalChatActive === 'function' && I.isCatLocalChatActive()) {
                I.setCompactChatState('input');
            } else {
                I.resetCompactChatState();
            }
            I.invalidatePendingGalgameRequest();
        }
        if (wasEffectiveHidden !== isEffectiveHidden) {
            I.applyGalgameBodyClass();
            I.dispatchHostEvent('galgame-mode-change', { enabled: I.getEffectiveGalgameEnabled() });
            if (restoredEffectiveComposer && I.getEffectiveGalgameEnabled()) {
                var overlay = I.getOverlay();
                if (overlay && !overlay.hidden) {
                    I.fetchGalgameOptionsForLatestTurn();
                }
            }
        }
        I.renderWindow();
    }

    I.syncGoodbyeComposerHidden = function syncGoodbyeComposerHidden(reason, options) {
        if (options && options.localOnly && !I.hasLocalGoodbyeModeSource()) {
            return;
        }
        I.setGoodbyeComposerHidden(I.getNekoGoodbyeModeActive(), reason || 'sync');
    }

    I.requestGoodbyeComposerHiddenState = function requestGoodbyeComposerHiddenState(reason) {
        var resolvedReason = reason || 'react-chat-window';
        try {
            if (typeof window.requestGoodbyeChatComposerHiddenState === 'function') {
                if (window.requestGoodbyeChatComposerHiddenState(resolvedReason)) {
                    return true;
                }
            }
        } catch (_) {}
        try {
            window.dispatchEvent(new CustomEvent('neko:request-goodbye-chat-composer-hidden-state', {
                detail: {
                    reason: resolvedReason,
                    timestamp: Date.now()
                }
            }));
            return true;
        } catch (_) {
            return false;
        }
    }

    I.setHomeTutorialInteractionLocked = function setHomeTutorialInteractionLocked(locked, reason) {
        var next = !!locked;
        if (I.state.homeTutorialInteractionLocked === next) {
            return;
        }
        if (next && I.getCurrentCompactChatState() === 'input') {
            I.resetCompactChatState();
        }
        I.state.homeTutorialInteractionLocked = next;
        if (!next && !I.state.homeTutorialInputLocked) {
            I.restoreHomeTutorialCompactChatState(true);
        }
        I.state.viewProps = Object.assign({}, I.ensureViewProps(), {
            compactChatState: I.getCurrentCompactChatState(),
            composerDisabled: !!next
        });
        syncTutorialGalgameSuppression();
        I.renderWindow();
    }

    I.setHomeTutorialInputLocked = function setHomeTutorialInputLocked(locked, reason) {
        var next = locked === true;
        if (I.state.homeTutorialInputLocked === next) {
            return;
        }
        var previousAttachmentsVisible = I.getEffectiveComposerAttachmentsVisible();
        if (next && I.getCurrentCompactChatState() === 'input') {
            I.resetCompactChatState();
        }
        I.state.homeTutorialInputLocked = next;
        if (!next && !I.state.homeTutorialInteractionLocked) {
            I.restoreHomeTutorialCompactChatState(true);
        }
        I.state.viewProps = Object.assign({}, I.ensureViewProps(), {
            compactChatState: I.getCurrentCompactChatState(),
            compactInputLocked: next,
            composerDisabled: !!I.state.homeTutorialInteractionLocked
        });
        I.syncComposerAttachmentsVisibility(previousAttachmentsVisible);
        syncTutorialGalgameSuppression();
        I.renderWindow();
    }

    I.rotateCompactToolWheel = function rotateCompactToolWheel(direction, stepCount, options) {
        var normalizedDirection = direction === -1 ? -1 : 1;
        var normalizedStepCount = Number.isFinite(stepCount)
            ? Math.max(1, Math.min(7, Math.floor(stepCount)))
            : 1;
        var normalizedOptions = typeof options === 'string'
            ? { reason: options }
            : (options || {});
        return I.setTutorialChatRequest('compactToolWheelRotateRequest', {
            direction: normalizedDirection,
            stepCount: normalizedStepCount,
            reason: typeof normalizedOptions.reason === 'string' ? normalizedOptions.reason : '',
            forceFast: normalizedOptions.forceFast !== false
        });
    }

    I.setCompactToolWheelIndex = function setCompactToolWheelIndex(index, reason) {
        var normalizedIndex = Number.isFinite(index)
            ? Math.max(0, Math.min(6, Math.floor(index)))
            : 0;
        return I.setTutorialChatRequest('compactToolWheelIndexRequest', {
            index: normalizedIndex,
            reason: typeof reason === 'string' ? reason : ''
        });
    }

    I.setCompactHistoryOpen = function setCompactHistoryOpen(open, reason) {
        return I.setTutorialChatRequest('compactHistoryOpenRequest', {
            open: open === true,
            reason: typeof reason === 'string' ? reason : ''
        });
    }

    I.deactivateAvatarTool = function deactivateAvatarTool() {
        I.state._avatarToolDeactivationKey = 'atd-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6);
        I.renderWindow();
    }

    I.setComposerAttachments = function setComposerAttachments(attachments) {
        var previousVisible = I.getEffectiveComposerAttachmentsVisible();
        I.state.composerAttachments = Array.isArray(attachments)
            ? attachments.map(function (attachment, index) {
                if (!attachment || typeof attachment !== 'object' || !attachment.url) return null;
                return {
                    id: String(attachment.id || ('attachment-' + index)),
                    url: String(attachment.url),
                    alt: attachment.alt ? String(attachment.alt) : ''
                };
            }).filter(Boolean)
            : [];
        I.syncComposerAttachmentsVisibility(previousVisible);
        I.renderWindow();
        return I.state.composerAttachments;
    }

    var MAX_MESSAGES = 50;

    function getNextAppendSortKey() {
        var maxExistingSortKey = Array.isArray(I.state.messages)
            ? I.state.messages.reduce(function (max, message) {
                var key = message && typeof message.sortKey === 'number' && Number.isFinite(message.sortKey)
                    ? message.sortKey : null;
                return (key !== null && key > max) ? key : max;
            }, -1)
            : -1;
        var nextSortKey = Math.max(I._sortKeySeq, maxExistingSortKey + 1, Date.now());
        I._sortKeySeq = nextSortKey + 1;
        return nextSortKey;
    }

    I.appendMessage = function appendMessage(message) {
        var normalized = I.normalizeMessage(message, getNextAppendSortKey());
        if (!normalized) return null;

        I.state.messages = I.sortMessages(I.state.messages.concat([normalized]));
        if (I.state.messages.length > MAX_MESSAGES) {
            I.state.messages = I.state.messages.slice(-MAX_MESSAGES);
        }
        // A new user-role message means the conversation has advanced — even
        // when the message came in via voice / proactive / sendTextPayload
        // rather than the React composer. Invalidate any pending GalGame fetch
        // so its response can't render against the old turn context.
        if (normalized.role === 'user' || isNewUserIcebreakerChatMessage(normalized)) {
            I.invalidatePendingGalgameRequest();
        }
        I.renderWindow();
        return normalized;
    }

    I.updateMessage = function updateMessage(messageId, patch) {
        var updatedMessage = null;

        I.state.messages = I.state.messages.map(function (message, index) {
            if (String(message.id) !== String(messageId)) return message;
            updatedMessage = I.normalizeMessage(Object.assign({}, message, patch || {}), index);
            return updatedMessage || message;
        });

        I.state.messages = I.sortMessages(I.state.messages);
        I.renderWindow();
        return updatedMessage;
    }

    I.removeMessage = function removeMessage(messageId) {
        var beforeLength = I.state.messages.length;
        I.state.messages = I.state.messages.filter(function (message) {
            return String(message.id) !== String(messageId);
        });
        var changed = I.state.messages.length !== beforeLength;
        if (changed) {
            I.renderWindow();
        }
        return changed;
    }

    function isYuiGuideChatMessage(message) {
        if (!message) return false;
        if (typeof message.id === 'string' && message.id.indexOf('yui-guide-') === 0) return true;
        var source = typeof message.source === 'string' ? message.source : '';
        return source === 'yui_guide' || source === 'yui-guide-director';
    }

    I.clearGuideMessages = function clearGuideMessages() {
        var beforeLength = I.state.messages.length;
        I.state.messages = I.state.messages.filter(function (message) {
            return !isYuiGuideChatMessage(message);
        });
        var changed = I.state.messages.length !== beforeLength;
        if (changed) {
            I.renderWindow();
        }
        return changed;
    }

    I.clearMessages = function clearMessages() {
        I.state.messages = [];
        I._sortKeySeq = 0;
        I.invalidatePendingGalgameRequest();
        // 角色切换 / cloud reload 等触发 clearMessages 的路径也必须清掉 mini-game
        // invite prompt——否则旧角色的按钮残留在新 context 里，用户点了 endpoint
        // 会按新 lanlan_name 查旧 session_id 直接 expired。dedupe set 也清，防止
        // 上一会话的 launched 标记错误地阻断新会话同 session_id 的 launch（虽然
        // session_id 是 uuid 实际撞概率几乎 0，对偶清理更干净）。codex P2 指出。
        I.state.choicePrompt = null;
        I.state._launchedMiniGameSessionIds = Object.create(null);
        I.renderWindow();
    }

    I.getStateSnapshot = function getStateSnapshot() {
        return {
            mounted: I.mounted,
            minimized: I.minimized,
            chatSurfaceMode: I.getCurrentChatSurfaceMode(),
            compactChatState: I.getCurrentCompactChatState(),
            viewProps: Object.assign({}, I.ensureViewProps()),
            messages: I.state.messages.map(I.cloneMessage),
            composerAttachments: I.state.composerAttachments.slice(),
            composerHidden: I.getEffectiveComposerHidden(),
            composerHiddenRequested: I.state.composerHidden,
            goodbyeComposerHidden: I.state.goodbyeComposerHidden
        };
    }

})();
