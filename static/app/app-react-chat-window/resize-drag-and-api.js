/**
 * app-react-chat-window/resize-drag-and-api.js
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
    var CLICK_THRESHOLD = 5; // px – 移动距离低于此值视为点击

    function getCurrentIdleChatLifecycleSequence() {
        var sequence = Number(window.__nekoIdleChatLifecycleSequence);
        return Number.isSafeInteger(sequence) && sequence > 0 ? sequence : 0;
    }

    function dispatchMinimizedYarnDragPhase(phase, dragState, shell) {
        if (!dragState || !dragState.minimizedYarn || !shell || typeof window.dispatchEvent !== 'function') return;
        var rect = shell.getBoundingClientRect();
        if (!rect || rect.width <= 0 || rect.height <= 0) return;
        window.dispatchEvent(new CustomEvent('neko:chat-yarn-user-drag', {
            detail: {
                phase: phase,
                sessionId: dragState.yarnSessionId,
                source: 'react-chat-window',
                coordinateSpace: 'viewport',
                moved: dragState.moved === true,
                lifecycleSequence: dragState.yarnLifecycleSequence,
                screenRect: {
                    left: rect.left,
                    top: rect.top,
                    width: rect.width,
                    height: rect.height
                },
                timestamp: Date.now()
            }
        }));
    }

    function isCompactDragSurfaceTarget(target) {
        if (!target || typeof target.closest !== 'function') return false;
        if (target.closest('[data-compact-no-drag="true"]')) return false;
        return !!target.closest('[data-compact-drag-surface="true"]');
    }

    function startDrag(clientX, clientY, options) {
        var shell = I.getShell();
        if (!shell) return;
        if (I.isYuiGuideDragLocked()) return;

        var opts = options || {};
        var compactSurface = !!(opts.compactSurface && I.getCurrentChatSurfaceMode() === 'compact' && !I.minimized);
        var rect = shell.getBoundingClientRect();
        I.dragState = {
            pointerOffsetX: clientX - rect.left,
            pointerOffsetY: clientY - rect.top,
            startClientX: clientX,
            startClientY: clientY,
            compactSurface: compactSurface,
            moved: false,
            minimizedYarn: !!(I.minimized && !I.isElectronChatWindow()),
            yarnLifecycleSequence: getCurrentIdleChatLifecycleSequence(),
            yarnSessionId: `react-yarn:${Date.now()}:${(I.yarnDragSequence = (I.yarnDragSequence || 0) + 1)}`
        };

        shell.classList.add('is-dragging');
        document.body.classList.add('react-chat-window-dragging');
        if (compactSurface) {
            I.scheduleCompactMinimizeBallTracking();
        }
        dispatchMinimizedYarnDragPhase('start', I.dragState, shell);
    }

    function updateDrag(clientX, clientY) {
        if (!I.dragState) return;
        if (I.isYuiGuideDragLocked()) {
            // 教程接管期强制中断拖拽：抑制后续 toggleMinimized，避免最小化球被误展开
            I.stopDrag({ suppressClick: true });
            return;
        }

        var dx = clientX - I.dragState.startClientX;
        var dy = clientY - I.dragState.startClientY;
        if (Math.abs(dx) > CLICK_THRESHOLD || Math.abs(dy) > CLICK_THRESHOLD) {
            I.dragState.moved = true;
        }

        if (I.dragState.compactSurface && !I.dragState.moved) return;

        var left = clientX - I.dragState.pointerOffsetX;
        var top = clientY - I.dragState.pointerOffsetY;
        if (I.dragState.compactSurface) {
            I.applyCompactSurfacePosition(left, top);
            return;
        }
        var clamped = I.clampPosition(left, top);
        I.applyPosition(clamped.left, clamped.top);
        dispatchMinimizedYarnDragPhase('move', I.dragState, I.getShell());
    }

    I.stopDrag = function stopDrag(options) {
        if (!I.dragState) return;
        var opts = options || {};
        var changedTouch = opts.changedTouches && opts.changedTouches.length > 0 ? opts.changedTouches[0] : null;

        var wasMoved = I.dragState.moved;
        var wasCompactSurface = !!I.dragState.compactSurface;
        var completedDragState = I.dragState;

        var shell = I.getShell();
        if (shell) {
            shell.classList.remove('is-dragging');
            var rect = shell.getBoundingClientRect();
            // 最小化态下不持久化悬浮球坐标到展开态存储，
            // 否则 restorePosition 会把完整窗口放到悬浮球位置
            // 移动端坐标也不持久化，避免污染桌面端保存的位置
            // full 是可拖动的完整窗口：拖完记住位置，刷新/展开后回到这里（不再每次居中）。
            if (!I.minimized && !I.isMobileWidth() && I.getCurrentChatSurfaceMode() === 'full') {
                I.rememberExpandedShellPosition(rect.left, rect.top);
                I.persistPosition(rect.left, rect.top);
            }
            dispatchMinimizedYarnDragPhase(opts.suppressClick ? 'cancel' : 'end', completedDragState, shell);
        }

        I.dragState = null;
        document.body.classList.remove('react-chat-window-dragging');
        if (wasCompactSurface && wasMoved) {
            var compactRect = I.getCurrentCompactSurfaceRect();
            if (compactRect) {
                if (typeof I.noteCompactSurfaceManualDragRelease === 'function') {
                    I.noteCompactSurfaceManualDragRelease(compactRect);
                }
                I.dispatchCompactSurfaceLayoutChange(compactRect);
            }
        }

        // 移动过的拖拽不该再变成点击：吞掉 mouseup 落点补发的那一次 click。
        if (wasMoved) {
            I.armDragReleaseClickGuard();
        }

        // 最小化状态下，未发生拖拽移动 → 视为点击，恢复窗口
        // 但 suppressClick=true（如教程接管强制中断）时不触发，避免误展开
        if (I.minimized && !wasMoved && !opts.suppressClick) {
            if (changedTouch && I.isMobileWidth()) {
                I.armMobileExpandClickGuard(changedTouch.clientX, changedTouch.clientY);
            }
            I.toggleMinimized();
        }
    }

    function bindDragging() {
        var header = I.getHeader();
        if (!header) return;

        header.addEventListener('mousedown', function (event) {
            if (event.button !== 0) return;
            var closeButton = I.$('reactChatWindowCloseButton');
            if (closeButton && closeButton.contains(event.target)) return;
            var minimizeButton = I.$('reactChatWindowMinimizeButton');
            if (minimizeButton && minimizeButton.contains(event.target)) return;
            var avatarHeaderBtn = I.$('avatarPreviewHeaderButton');
            if (avatarHeaderBtn && avatarHeaderBtn.contains(event.target)) return;
            startDrag(event.clientX, event.clientY);
            event.preventDefault();
        });

        // touchstart 不 preventDefault：让浏览器自行决定是滚动还是点击，
        // 真正进入拖拽后由 touchmove（passive: false）阻止滚动即可。
        header.addEventListener('touchstart', function (event) {
            var closeButton = I.$('reactChatWindowCloseButton');
            if (closeButton && closeButton.contains(event.target)) return;
            var minimizeButton = I.$('reactChatWindowMinimizeButton');
            if (minimizeButton && minimizeButton.contains(event.target)) return;
            var avatarHeaderBtn = I.$('avatarPreviewHeaderButton');
            if (avatarHeaderBtn && avatarHeaderBtn.contains(event.target)) return;
            if (!event.touches || event.touches.length === 0) return;
            startDrag(event.touches[0].clientX, event.touches[0].clientY);
        }, { passive: true });

        document.addEventListener('mousedown', function (event) {
            if (event.button !== 0) return;
            if (I.isElectronChatWindow()) return;
            if (!isCompactDragSurfaceTarget(event.target)) return;
            startDrag(event.clientX, event.clientY, {
                compactSurface: true
            });
            event.preventDefault();
            event.stopPropagation();
        }, true);

        document.addEventListener('touchstart', function (event) {
            if (I.isElectronChatWindow()) return;
            if (!isCompactDragSurfaceTarget(event.target)) return;
            if (!event.touches || event.touches.length === 0) return;
            startDrag(event.touches[0].clientX, event.touches[0].clientY, {
                compactSurface: true
            });
            // Do not preventDefault on touchstart: a stationary tap on the
            // compact capsule must still synthesize click so React can enter
            // input mode. Real drags are blocked in touchmove below.
        }, { capture: true, passive: true });

        document.addEventListener('mousemove', function (event) {
            if (!I.dragState) return;
            updateDrag(event.clientX, event.clientY);
        });

        document.addEventListener('touchmove', function (event) {
            if (!I.dragState || !event.touches || event.touches.length === 0) return;
            // chat.html 不走 mobile 路径，保留原 passive: true 语义，不吞原生滚动。
            if (!I.isElectronChatWindow()) event.preventDefault();
            updateDrag(event.touches[0].clientX, event.touches[0].clientY);
        }, { passive: false });

        document.addEventListener('mouseup', I.stopDrag);
        document.addEventListener('touchend', function (event) {
            I.stopDrag({ changedTouches: event.changedTouches });
        });
        document.addEventListener('touchcancel', function (event) {
            I.stopDrag({ changedTouches: event.changedTouches, suppressClick: true });
        });

        // React 侧工具轮盘原点「按住拖动文本框」手势：轮盘 / toggle 是 no-drag，宿主的
        // mousedown 命中判定不会自动起拖，所以由 React 检测到拖动意图后派发该事件，这里
        // 以按下点为锚启动 compact surface 拖拽，复用上面的全局 mousemove/mouseup（含落点
        // click 守卫）。Electron 下由 preload-chat-react.js 用原生窗口拖拽接管，这里早退。
        window.addEventListener('neko:compact-surface-drag-grab', function (event) {
            if (I.isElectronChatWindow()) return;
            if (I.getCurrentChatSurfaceMode() !== 'compact' || I.minimized) return;
            var detail = event && event.detail ? event.detail : {};
            if (!Number.isFinite(detail.clientX) || !Number.isFinite(detail.clientY)) return;
            startDrag(detail.clientX, detail.clientY, { compactSurface: true });
        });
    }

    var MIN_WIDTH = 320;
    var MIN_HEIGHT = 280;
    var GALGAME_MIN_HEIGHT = 420;
    var RESIZE_DIRECTIONS = ['n', 's', 'w', 'e', 'nw', 'ne', 'sw', 'se'];

    function getDesktopMinHeight() {
        if (!I.getEffectiveGalgameEnabled()) return MIN_HEIGHT;
        // 与 CSS 的 galgame min-height 对齐，避免拖拽时 JS 先把高度压到 280px。
        return Math.min(GALGAME_MIN_HEIGHT, Math.max(MIN_HEIGHT, window.innerHeight - 22));
    }

    function createResizeEdges() {
        var shell = I.getShell();
        if (!shell) return;

        RESIZE_DIRECTIONS.forEach(function (dir) {
            var edge = document.createElement('div');
            edge.className = 'react-chat-resize-edge react-chat-resize-' + dir;
            edge.dataset.resizeDir = dir;
            shell.appendChild(edge);
        });
    }

    function startResize(clientX, clientY, direction) {
        var shell = I.getShell();
        if (!shell) return;
        // 教程接管期禁止 resize，否则用户拉伸会让教程锚点和高亮错位
        if (I.isYuiGuideDragLocked()) return;
        // 手机端仅允许向上拖动调整高度（北侧边缘）
        if (I.isMobileWidth() && direction !== 'n') return;
        if (I.minimized) return;

        var rect = shell.getBoundingClientRect();
        I.resizeState = {
            dir: direction,
            startX: clientX,
            startY: clientY,
            origLeft: rect.left,
            origTop: rect.top,
            origWidth: rect.width,
            origHeight: rect.height
        };

        document.body.classList.add('react-chat-window-resizing');
    }

    function updateResize(clientX, clientY) {
        if (!I.resizeState) return;
        // 教程接管期强制中断 resize，与 updateDrag 的 lock 行为对称
        if (I.isYuiGuideDragLocked()) {
            stopResize();
            return;
        }

        var shell = I.getShell();
        if (!shell) return;

        var dx = clientX - I.resizeState.startX;
        var dy = clientY - I.resizeState.startY;
        var dir = I.resizeState.dir;

        var newLeft = I.resizeState.origLeft;
        var newTop = I.resizeState.origTop;
        var newWidth = I.resizeState.origWidth;
        var newHeight = I.resizeState.origHeight;

        // 手机端仅处理高度变化
        var mobile = I.isMobileWidth();

        if (!mobile && dir.indexOf('e') !== -1) {
            newWidth = Math.max(MIN_WIDTH, I.resizeState.origWidth + dx);
        }
        if (!mobile && dir.indexOf('w') !== -1) {
            var proposedWidth = I.resizeState.origWidth - dx;
            if (proposedWidth >= MIN_WIDTH) {
                newWidth = proposedWidth;
                newLeft = I.resizeState.origLeft + dx;
            } else {
                newWidth = MIN_WIDTH;
                newLeft = I.resizeState.origLeft + I.resizeState.origWidth - MIN_WIDTH;
            }
        }
        var desktopMinHeight = getDesktopMinHeight();

        if (!mobile && dir.indexOf('s') !== -1) {
            newHeight = Math.max(desktopMinHeight, I.resizeState.origHeight + dy);
        }
        if (dir.indexOf('n') !== -1) {
            var minH = mobile ? I.MOBILE_MIN_HEIGHT : desktopMinHeight;
            var proposedHeight = I.resizeState.origHeight - dy;
            if (proposedHeight >= minH) {
                newHeight = proposedHeight;
                newTop = I.resizeState.origTop + dy;
            } else {
                newHeight = minH;
                newTop = I.resizeState.origTop + I.resizeState.origHeight - minH;
            }
        }

        // Clamp to viewport
        newLeft = Math.max(0, Math.min(newLeft, window.innerWidth - 50));
        newTop = Math.max(0, Math.min(newTop, window.innerHeight - 50));
        newWidth = Math.min(newWidth, window.innerWidth);
        newHeight = Math.min(newHeight, window.innerHeight);

        if (mobile) {
            // 手机端：更新高度和 top，保持 CSS 控制的 left/width
            var maxMobileH = I.getMobileMaxHeight();
            var clampedH = Math.min(newHeight, maxMobileH);
            // 高度被截断时重新计算 top，保持面板底部不动
            if (clampedH < newHeight) {
                newTop = window.innerHeight - clampedH;
            }
            shell.style.height = clampedH + 'px';
            // 设置 top 并清除 bottom，使北侧拖拽正确向上扩展
            shell.style.top = newTop + 'px';
            shell.style.bottom = 'auto';
        } else {
            shell.style.width = newWidth + 'px';
            shell.style.height = newHeight + 'px';
            shell.style.left = newLeft + 'px';
            shell.style.top = newTop + 'px';
            shell.style.transform = 'none';
        }
    }

    function stopResize() {
        if (!I.resizeState) return;

        var shell = I.getShell();
        if (shell) {
            var rect = shell.getBoundingClientRect();
            if (I.isMobileWidth()) {
                // 手机端：保存用户设置的高度，恢复底部锚定
                I.mobileUserHeight = Math.round(rect.height);
                shell.style.removeProperty('top');
                shell.style.removeProperty('bottom');
                try {
                    localStorage.setItem(I.MOBILE_HEIGHT_STORAGE_KEY, String(I.mobileUserHeight));
                } catch (_) {}
            } else if (I.getCurrentChatSurfaceMode() === 'full') {
                // full 是可调整大小的完整窗口：记住尺寸与位置，刷新/展开后还原。
                I.persistPosition(rect.left, rect.top);
                I.persistSize(rect.width, rect.height);
                I.rememberExpandedShellPosition(rect.left, rect.top);
            }
        }

        I.resizeState = null;
        document.body.classList.remove('react-chat-window-resizing');
    }

    function bindResizing() {
        var shell = I.getShell();
        if (!shell) return;

        shell.addEventListener('mousedown', function (event) {
            if (event.button !== 0) return;
            var target = event.target;
            if (!target || !target.dataset || !target.dataset.resizeDir) return;
            startResize(event.clientX, event.clientY, target.dataset.resizeDir);
            event.preventDefault();
        });

        shell.addEventListener('touchstart', function (event) {
            var target = event.target;
            if (!target || !target.dataset || !target.dataset.resizeDir) return;
            if (!event.touches || event.touches.length === 0) return;
            startResize(event.touches[0].clientX, event.touches[0].clientY, target.dataset.resizeDir);
            // chat.html 保留原 passive 语义；只在真正进入 resize（非 chat.html）才吞事件。
            if (I.resizeState && !I.isElectronChatWindow()) event.preventDefault();
        }, { passive: false });

        document.addEventListener('mousemove', function (event) {
            if (!I.resizeState) return;
            updateResize(event.clientX, event.clientY);
        });

        document.addEventListener('touchmove', function (event) {
            if (!I.resizeState || !event.touches || event.touches.length === 0) return;
            if (!I.isElectronChatWindow()) event.preventDefault();
            updateResize(event.touches[0].clientX, event.touches[0].clientY);
        }, { passive: false });

        document.addEventListener('mouseup', stopResize);
        document.addEventListener('touchend', stopResize);
        document.addEventListener('touchcancel', stopResize);
    }

    function bindBridgeEvents() {
        window.addEventListener(I.EVENT_PREFIX + 'set-messages', function (event) {
            I.setMessages(event.detail && event.detail.messages);
        });

        window.addEventListener(I.EVENT_PREFIX + 'append-message', function (event) {
            I.appendMessage(event.detail && event.detail.message);
        });

        window.addEventListener(I.EVENT_PREFIX + 'update-message', function (event) {
            var detail = event.detail || {};
            I.updateMessage(detail.messageId, detail.patch);
        });

        window.addEventListener(I.EVENT_PREFIX + 'remove-message', function (event) {
            I.removeMessage(event.detail && event.detail.messageId);
        });

        window.addEventListener(I.EVENT_PREFIX + 'clear-messages', function () {
            I.clearMessages();
        });

        window.addEventListener('chat-avatar-preview-updated', I.refreshAssistantAvatarUrls);
        window.addEventListener('chat-avatar-preview-cleared', I.refreshAssistantAvatarUrls);
        window.addEventListener('neko:tutorial-chat-identity-changed', I.refreshAssistantAvatarUrls);

        window.addEventListener(I.EVENT_PREFIX + 'set-view-props', function (event) {
            I.setViewProps(event.detail && event.detail.viewProps);
        });

        window.addEventListener(I.EVENT_PREFIX + 'set-composer-attachments', function (event) {
            I.setComposerAttachments(event.detail && event.detail.attachments);
        });

        window.addEventListener(I.EVENT_PREFIX + 'set-composer-hidden', function (event) {
            I.setComposerHidden(event.detail && event.detail.hidden);
        });

        window.addEventListener(I.EVENT_PREFIX + 'set-goodbye-composer-hidden', function (event) {
            var detail = event && event.detail && typeof event.detail === 'object' ? event.detail : {};
            I.setGoodbyeComposerHidden(!!detail.hidden, detail.reason || 'bridge');
        });

        window.addEventListener(I.EVENT_PREFIX + 'set-galgame-mode', function (event) {
            var detail = event.detail || {};
            I.setGalgameModeEnabled(!!detail.enabled, { persist: detail.persist !== false });
        });

        ['live2d-floating-buttons-ready', 'vrm-model-loaded', 'mmd-model-loaded'].forEach(function (eventName) {
            window.addEventListener(eventName, I.revealPendingCompactSurfaceOpen);
        });

        window.addEventListener('neko:tutorial-started', function (event) {
            var detail = event && event.detail ? event.detail : {};
            if (detail.page !== 'home') return;
            I.setHomeTutorialInteractionLocked(true, 'tutorial-started');
            I.setHomeTutorialInputLocked(true, 'tutorial-started');
            I.setGalgameModeTemporarilyDisabled(true);
        });

        window.addEventListener('neko:tutorial-completed', function (event) {
            var detail = event && event.detail ? event.detail : {};
            if (detail.page !== 'home') return;
            I.setHomeTutorialInputLocked(false, 'tutorial-completed');
            I.setHomeTutorialInteractionLocked(false, 'tutorial-completed');
            I.setGalgameModeTemporarilyDisabled(false);
        });

        window.addEventListener('neko:tutorial-skipped', function (event) {
            var detail = event && event.detail ? event.detail : {};
            if (detail.page !== 'home') return;
            I.setHomeTutorialInputLocked(false, 'tutorial-skipped');
            I.setHomeTutorialInteractionLocked(false, 'tutorial-skipped');
            I.setGalgameModeTemporarilyDisabled(false);
        });

        window.addEventListener('neko:tutorial-ended-without-completion', function (event) {
            var detail = event && event.detail ? event.detail : {};
            if (detail.page !== 'home') return;
            I.setHomeTutorialInputLocked(false, 'tutorial-ended-without-completion');
            I.setHomeTutorialInteractionLocked(false, 'tutorial-ended-without-completion');
            I.setGalgameModeTemporarilyDisabled(false);
        });

        window.addEventListener('neko:new-user-icebreaker-reset', function () {
            I.clearChoicePromptBySource('new_user_icebreaker', 'new-user-icebreaker-reset');
        });

        function isNewUserIcebreakerTurnEndEvent(event) {
            var detail = event && event.detail && typeof event.detail === 'object'
                ? event.detail
                : {};
            if (detail.source === 'new_user_icebreaker') return true;
            var meta = detail.meta && typeof detail.meta === 'object' ? detail.meta : {};
            if (meta.source === 'new_user_icebreaker' || meta.kind === 'new_user_icebreaker') {
                return true;
            }
            var metaEvent = meta.event && typeof meta.event === 'object' ? meta.event : {};
            return metaEvent.source === 'new_user_icebreaker';
        }

        // Refresh option list whenever an ordinary assistant turn finishes streaming.
        window.addEventListener('neko-assistant-turn-end', function (event) {
            if (isNewUserIcebreakerTurnEndEvent(event)) return;
            if (!I.state.galgameModeEnabled) return;
            // Skip when the chat overlay is hidden — otherwise galgame mode's
            // default-on flag would spam /api/galgame/options (and summary-tier
            // inference) on every assistant turn even for users who never
            // opened the React chat window (voice-only / proactive paths).
            var overlay = I.getOverlay();
            if (!overlay || overlay.hidden) return;
            // app-chat-adapter's processRealisticQueue can still be sleeping
            // 1-2s between bubble flushes when turn-end fires, so the message
            // list may not yet contain the final assistant sentences. Wait
            // until the queue is drained and the lock is released before
            // building the request, with a hard cap so a stuck queue can't
            // permanently block the option fetch.
            //
            // Snapshot _galgameRequestSeq before waiting: invalidatePending…
            // (called by setMessages / clearMessages / handleComposerSubmit)
            // bumps the seq when the conversation switches or the user moves
            // on. If that happens during the wait window, we drop this fetch
            // so a stale turn-end can't render A/B/C into the new context.
            var seqAtSchedule = I.state._galgameRequestSeq;
            I.waitForAssistantBubblesFlushed(4000).then(function () {
                if (!I.state.galgameModeEnabled) return;
                if (I.state._galgameRequestSeq !== seqAtSchedule) return;
                // The overlay may have been closed during the 4s wait — re-check
                // before firing the fetch so closing the chat mid-turn doesn't
                // still kick off a background summary-tier inference.
                var overlayNow = I.getOverlay();
                if (!overlayNow || overlayNow.hidden) return;
                I.fetchGalgameOptionsForLatestTurn();
            });
        });
    }

    function init() {
        // initAfterStorageBarrier() is the only caller, so the active storage
        // namespace is settled before the font preference is restored. This is
        // still before any React chat surface can mount.
        I.restoreChatFontPresetPreference();
        var trigger = I.$('reactChatWindowButton');
        var closeButton = I.$('reactChatWindowCloseButton');
        var minimizeButton = I.getMinimizeButton();
        var backdrop = I.$('react-chat-window-backdrop');
        var avatarHeaderButton = I.$('avatarPreviewHeaderButton');

        I.ensureViewProps();
        var tutorialLockActive = I.isHomeTutorialRunning();
        I.state.homeTutorialInteractionLocked = tutorialLockActive;
        I.state.homeTutorialInputLocked = false;
        I.state.chatSurfaceMode = I.readInitialChatSurfaceMode();
        if (tutorialLockActive) {
            I.setHomeTutorialInputLocked(true, 'tutorial-startup');
        }
        I.lastRestorableChatSurfaceMode = I.state.chatSurfaceMode;
        I.resetCompactChatState();
        I.state.viewProps = Object.assign({}, I.ensureViewProps(), {
            chatSurfaceMode: I.getCurrentChatSurfaceMode(),
            compactChatState: I.getCurrentCompactChatState(),
            composerDisabled: !!(I.state.homeTutorialInteractionLocked || I.state.homeTutorialInputLocked),
            compactInputLocked: !!I.state.homeTutorialInputLocked
        });
        I.bindSubtitleSettingsSync();
        I.syncChatSurfaceModeUI();
        I.prewarmUserDisplayName();
        // Resolve the persisted GalGame preference now that the storage-location
        // barrier has settled (initAfterStorageBarrier has awaited it before
        // calling init). Reading at module-eval would risk capturing the value
        // from a storage namespace the barrier is about to remap.
        // setGalgameModeEnabled idempotently syncs state + body class + fires
        // the change event when the resolved pref differs from the safe default.
        if (I.isHomeTutorialInteractionLocked()) {
            I.setGalgameModeTemporarilyDisabled(true);
        } else {
            I.setGalgameModeEnabled(I.readGalgameModePreference(), { persist: false });
        }

        if (trigger) {
            trigger.addEventListener('click', I.openWindow);
        }
        if (closeButton) {
            closeButton.addEventListener('click', I.closeWindow);
        }
        if (minimizeButton) {
            minimizeButton.addEventListener('click', function (event) {
                event.stopPropagation();
                I.toggleMinimized();
            });
        }
        // Note: the avatarPreviewHeaderButton click is bound by app-chat-avatar.js
        // (it owns the standalone avatar preview popup and toggling behavior).
        // We only fire the host event here for external listeners/analytics.
        if (avatarHeaderButton) {
            avatarHeaderButton.addEventListener('click', function () {
                I.dispatchHostEvent('avatar-generator-click', {});
            });
        }
        if (backdrop) {
            // When chat adapter is active (primary mode), backdrop should not
            // block interaction with the model behind it.
            if (!window._chatAdapterActive) {
                backdrop.addEventListener('click', I.closeWindow);
            } else {
                backdrop.style.pointerEvents = 'none';
            }
        }

        document.addEventListener('mousedown', I.blockMobileExpandSyntheticPointerEvent, true);
        document.addEventListener('mouseup', I.blockMobileExpandSyntheticPointerEvent, true);
        document.addEventListener('click', I.blockMobileExpandSyntheticPointerEvent, true);
        document.addEventListener('click', I.consumeDragReleaseClickGuard, true);
        bindDragging();
        createResizeEdges();
        bindResizing();
        bindBridgeEvents();
        setTimeout(I.refreshAssistantAvatarUrls, 0);
        setTimeout(I.refreshAssistantAvatarUrls, 500);
        I.ensureElectronChatMinimizedStateBridge();

        // 恢复手机端用户设置的高度
        try {
            var storedMobileHeight = localStorage.getItem(I.MOBILE_HEIGHT_STORAGE_KEY);
            if (storedMobileHeight) {
                var parsed = Number(storedMobileHeight);
                if (Number.isFinite(parsed) && parsed >= I.MOBILE_MIN_HEIGHT) {
                    I.mobileUserHeight = parsed;
                }
            }
        } catch (_) {}

        // 悬浮球 hover 效果（参考原版 #chat-container 实现）
        var header = I.getHeader();
        if (header) {
            header.addEventListener('mouseenter', function () {
                if (!I.minimized) return;
                var shell = I.getShell();
                var ico = shell && shell.querySelector('.react-chat-minimized-icon');
                I.applyMinimizedBallSkin(ico);
            });
            header.addEventListener('mouseleave', function () {
                if (!I.minimized) return;
                var shell = I.getShell();
                var ico = shell && shell.querySelector('.react-chat-minimized-icon');
                I.applyMinimizedBallSkin(ico);
            });
        }

        window.addEventListener('keydown', function (event) {
            if (window._chatAdapterActive) return;
            var overlay = I.getOverlay();
            if (event.key === 'Escape' && overlay && !overlay.hidden) {
                I.closeWindow();
            }
        });

        window.addEventListener('resize', function () {
            I.scheduleCompactMinimizeBallTracking();
            var overlay = I.getOverlay();
            if (overlay && !overlay.hidden) {
                if (I.minimized) {
                    // 最小化态下，根据当前布局（桌面圆球 / 手机胶囊）重新贴到视口内。
                    // 手机胶囊宽度由 CSS !important 控制（width: calc(100vw - 12px)），
                    // 这里只需修正左上角坐标，避免旋转屏或拖窗后溢出。
                    var shell = I.getShell();
                    if (shell) {
                        var r = shell.getBoundingClientRect();
                        var minW = r.width || I.MINIMIZED_SIZE;
                        var minH = r.height || I.MINIMIZED_SIZE;
                        var safeLeft, safeTop;
                        if (I.isMobileWidth()) {
                            // 圆形悬浮球：保持用户拖拽位置，仅 clamp 到视口内
                            safeLeft = Math.max(0, Math.min(r.left, window.innerWidth - minW));
                            safeTop = Math.max(0, Math.min(r.top, window.innerHeight - minH));
                        } else {
                            safeLeft = Math.max(0, Math.min(r.left, window.innerWidth - minW));
                            safeTop = Math.max(0, Math.min(r.top, window.innerHeight - minH));
                        }
                        if (safeLeft !== r.left || safeTop !== r.top) {
                            shell.style.left = safeLeft + 'px';
                            shell.style.top = safeTop + 'px';
                        }
                    }
                } else {
                    // full 走 restoreFullPosition()：有记忆位置才还原、无记忆保持居中，
                    // 不能像 compact/minimized 那样走 restorePosition() → 被甩回左中。
                    if (I.getCurrentChatSurfaceMode() === 'full') {
                        I.restoreFullPosition();
                    } else {
                        I.restorePosition();
                    }
                    I.syncCompactSurfaceAnchor();
                    I.scheduleMobileContentLayout();
                }
            }
        });

        window.addEventListener('localechange', function () {
            I.state.viewProps = I.createBaseViewProps();
            I.renderWindow();
        });

        window.addEventListener('neko:auto-goodbye:state-change', function (event) {
            var detail = event && event.detail && typeof event.detail === 'object' ? event.detail : null;
            if (!detail || detail.type !== 'visual-tier') return;

            I.setGoodbyeComposerHidden(detail.tier !== I.IDLE_DOCK_TIER_NONE, detail.source || 'visual-tier');

            I.idleDockTier = !I.isGoodbyeIdleBallAppearanceActive() &&
                (detail.tier === I.IDLE_DOCK_TIER_CAT2 || detail.tier === I.IDLE_DOCK_TIER_CAT3)
                ? detail.tier
                : I.IDLE_DOCK_TIER_NONE;

            var overlay = I.getOverlay();
            if (!overlay || overlay.hidden || I.isElectronChatWindow()) return;

            if (I.isIdleDockTierActive()) {
                if (!I.idleDockActive) {
                    I.enterIdleDock();
                }
                return;
            }

            if (I.hasIdleDockPendingOrActive()) {
                I.exitIdleDock({
                    preserveCurrentPosition: I.idleDockActive && detail.source === 'return-ball-drag-demotion',
                });
                return;
            }

            I.clearIdleDockState();
        });
        window.addEventListener('neko:goodbye-idle-appearance', function (event) {
            var detail = event && event.detail && typeof event.detail === 'object' ? event.detail : null;
            var mode = detail && typeof detail.mode === 'string' ? detail.mode : '';
            if (mode === 'ball') {
                I.idleDockTier = I.IDLE_DOCK_TIER_NONE;
            } else {
                // 切回 cat 不会再有 visual-tier 事件补发，须按当前 tier 立即恢复贴靠状态
                var currentTier = I.readAutoGoodbyeVisualTier();
                I.idleDockTier = currentTier === I.IDLE_DOCK_TIER_CAT2 || currentTier === I.IDLE_DOCK_TIER_CAT3
                    ? currentTier
                    : I.IDLE_DOCK_TIER_NONE;
            }

            var overlay = I.getOverlay();
            if (!overlay || overlay.hidden || I.isElectronChatWindow()) return;

            if (I.isIdleDockTierActive()) {
                if (!I.idleDockActive) {
                    I.enterIdleDock();
                }
                return;
            }

            if (I.hasIdleDockPendingOrActive()) {
                I.exitIdleDock({});
                return;
            }

            I.clearIdleDockState();
        });
        window.addEventListener('neko:idle-return-ball-state', function (event) {
            var detail = event && event.detail && typeof event.detail === 'object' ? event.detail : null;
            if (!detail) return;
            I.handleElectronIdleReturnBallState(detail);
        });
        window.addEventListener('neko:idle-chat-pair-move-bounds', function (event) {
            var detail = event && event.detail && typeof event.detail === 'object' ? event.detail : null;
            if (!detail) return;
            I.scheduleElectronCat1PairMoveBounds(detail.screenRect || detail.bounds, {
                force: !!detail.force,
                reason: detail.reason || detail.source || 'cat1-pair-move'
            });
        });
        window.addEventListener('neko:idle-cat1-compact-mirror-state', I.handleIdleCat1CompactMirrorState);
        window.addEventListener('neko:idle-cat1-play-yarn-visibility', I.handleIdleCat1PlayYarnVisibility);
        window.addEventListener('neko:idle-cat1-playground-yarn-request', I._handleIdleCat1PlaygroundYarnRequest);
        window.addEventListener('live2d-goodbye-click', function () {
            I.setGoodbyeComposerHidden(true, 'live2d-goodbye-click');
        });
        window.addEventListener('neko:cat-return-complete', function (event) {
            const source = event && event.detail ? event.detail.source : '';
            if (source !== 'live2d-return-click' && source !== 'vrm-return-click' &&
                source !== 'mmd-return-click' && source !== 'pngtuber-return-click') {
                return;
            }
            I.setGoodbyeComposerHidden(false, 'return-complete');
            if (I.hasElectronIdleDockPendingOrActive()) { I.exitElectronIdleDock(); }
            if (I.hasIdleDockPendingOrActive()) { I.exitIdleDock(); return; }
            I.clearIdleDockState();
        });

        window.addEventListener('neko:desktop-compact-layout-change', function (event) {
            var layout = event && event.detail ? event.detail : window.__nekoDesktopCompactLayout;
            I.handleDesktopCompactLayoutChange(layout || null);
            I.refreshIdleCat1CompactMirrorPosition();
        });
        if (window.__nekoDesktopCompactLayout) {
            I.handleDesktopCompactLayoutChange(window.__nekoDesktopCompactLayout);
        }
        window.addEventListener('neko:desktop-avatar-bounds-change', function () {
            I.scheduleCompactMinimizeBallTracking();
        });
        window.addEventListener('neko:compact-surface-resize-request', function (event) {
            I.applyCompactSurfaceResizeRequest(event.detail || {});
        }, true);
        window.addEventListener('neko:compact-surface-resize-width-change', function () {
            I.syncCompactInteractionGeometry();
        });
        window.addEventListener('neko:compact-interaction-geometry-refresh', function () {
            I.syncCompactInteractionGeometry();
        });
    }

    function applyInitialComposerHiddenState() {
        // 独立 Chat 刷新时，语音态广播可能早于 React host 初始化到达。
        // 初始化完成后补读一次共享状态，避免 composer 以默认显示态首绘。
        try {
            var initialComposerShouldHide = false;
            if (typeof window.shouldKeepVoiceComposerHidden === 'function') {
                initialComposerShouldHide = window.shouldKeepVoiceComposerHidden();
            } else if (window.appState) {
                initialComposerShouldHide = !!(
                    window.appState.isRecording ||
                    window.appState.voiceChatActive ||
                    window.appState.voiceStartPending ||
                    window.isMicStarting
                );
            }
            if (initialComposerShouldHide) {
                I.setComposerHidden(true);
            }
            if (window.__nekoGoodbyeChatComposerHidden
                && typeof window.__nekoGoodbyeChatComposerHidden === 'object') {
                I.setGoodbyeComposerHidden(
                    !!window.__nekoGoodbyeChatComposerHidden.hidden,
                    window.__nekoGoodbyeChatComposerHidden.reason || 'initial-goodbye-cache'
                );
            } else {
                I.syncGoodbyeComposerHidden('initial-goodbye-state');
            }
            I.requestGoodbyeComposerHiddenState('initial-goodbye-state');
        } catch (_) {
            // 首绘兜底失败不影响后续 session_started 同步
        }
    }

    async function initAfterStorageBarrier() {
        if (typeof window.waitForStorageLocationStartupBarrier === 'function') {
            try {
                await window.waitForStorageLocationStartupBarrier();
            } catch (_) {}
        } else if (window.__nekoStorageLocationStartupBarrier
            && typeof window.__nekoStorageLocationStartupBarrier.then === 'function') {
            try {
                await window.__nekoStorageLocationStartupBarrier;
            } catch (_) {}
        }
        init();
        applyInitialComposerHiddenState();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAfterStorageBarrier);
    } else {
        initAfterStorageBarrier();
    }

    Object.assign(window.reactChatWindowHost, {
        ensureBundleLoaded: I.ensureBundleLoaded,
        openWindow: I.openWindow,
        closeWindow: I.closeWindow,
        setViewProps: I.setViewProps,
        setMessages: I.setMessages,
        setComposerAttachments: I.setComposerAttachments,
        setComposerHidden: I.setComposerHidden,
        setHomeTutorialInteractionLocked: I.setHomeTutorialInteractionLocked,
        setHomeTutorialInputLocked: I.setHomeTutorialInputLocked,
        setAvatarToolMenuOpen: I.setAvatarToolMenuOpen,
        setCompactToolFanOpen: I.setCompactToolFanOpen,
        rotateCompactToolWheel: I.rotateCompactToolWheel,
        setCompactToolWheelIndex: I.setCompactToolWheelIndex,
        setCompactHistoryOpen: I.setCompactHistoryOpen,
        deactivateAvatarTool: I.deactivateAvatarTool,
        appendMessage: I.appendMessage,
        updateMessage: I.updateMessage,
        removeMessage: I.removeMessage,
        clearGuideMessages: I.clearGuideMessages,
        clearMessages: I.clearMessages,
        getState: I.getStateSnapshot,
        setOnMessageAction: function (handler) {
            I.state.onMessageAction = typeof handler === 'function' ? handler : null;
        },
        setOnComposerImportImage: function (handler) {
            I.state.onComposerImportImage = typeof handler === 'function' ? handler : null;
        },
        setOnComposerScreenshot: function (handler) {
            I.state.onComposerScreenshot = typeof handler === 'function' ? handler : null;
        },
        triggerComposerScreenshot: I.handleComposerScreenshot,
        setOnComposerRemoveAttachment: function (handler) {
            I.state.onComposerRemoveAttachment = typeof handler === 'function' ? handler : null;
        },
        setOnComposerSubmit: function (handler) {
            I.state.onComposerSubmit = typeof handler === 'function' ? handler : null;
        },
        prepareCompactHistoryDropSubmit: I.prepareCompactHistoryDropSubmit,
        setOnAvatarInteraction: function (handler) {
            I.state.onAvatarInteraction = typeof handler === 'function' ? handler : null;
            if (I.state.onAvatarInteraction && I.state.pendingAvatarInteractions.length) {
                var avatarInteractionHandler = I.state.onAvatarInteraction;
                var pendingInteractions = I.state.pendingAvatarInteractions.slice();
                I.state.pendingAvatarInteractions = [];
                pendingInteractions.forEach(function (detail) {
                    try {
                        avatarInteractionHandler(detail);
                    } catch (error) {
                        console.error('[ReactChatWindow] queued onAvatarInteraction failed:', error);
                    }
                });
            }
        },
        setOnAvatarToolStateChange: function (handler) {
            I.state.onAvatarToolStateChange = typeof handler === 'function' ? handler : null;
        },
        rollbackLastDraft: I.rollbackLastDraft,
        clearPendingRollbackDraft: I.clearPendingRollbackDraft,
        ensureChatSurfaceVisible: I.ensureChatSurfaceVisible,
        setChatSurfaceMode: I.setChatSurfaceMode,
        cycleChatSurfaceMode: I.cycleChatSurfaceMode,
        setCompactChatState: I.setCompactChatState,
        setGoodbyeComposerHidden: I.setGoodbyeComposerHidden,
        syncGoodbyeComposerHidden: I.syncGoodbyeComposerHidden,
        setGalgameModeEnabled: function (enabled, options) {
            I.setGalgameModeEnabled(enabled, options || {});
        },
        setTranslateEnabled: function (enabled, options) {
            return I.setTranslateEnabled(enabled, options || {});
        },
        isGalgameModeEnabled: function () { return !!I.state.galgameModeEnabled; },
        getChatSurfaceMode: function () { return I.getCurrentChatSurfaceMode(); },
        republishCompactSurfaceLayoutChange: I.republishCompactSurfaceLayoutChange,
        scheduleCompactMinimizeBallTracking: I.scheduleCompactMinimizeBallTracking,
        refreshGalgameOptions: I.fetchGalgameOptionsForLatestTurn,
        // Mini-game invite ChoicePrompt：app-websocket.js 收到对应 WS message 时调
        setMiniGameInvitePrompt: I.setMiniGameInvitePrompt,
        setIcebreakerChoicePrompt: I.setIcebreakerChoicePrompt,
        setChoicePrompt: I.setChoicePrompt,
        clearChoicePromptBySource: I.clearChoicePromptBySource,
        setNewUserIcebreakerPrompt: I.setNewUserIcebreakerPrompt,
        clearIcebreakerChoicePrompt: I.clearIcebreakerChoicePrompt,
        // unified resolved handler：accept 兼 launch / decline / suppress 都通过
        // 这条入口分发——前端 dismiss prompt UI + accept 时 window.open。替代了
        // 旧 launchMiniGame（accept-only）路径，让 codex P2 的 cross-window
        // dismiss 一致性能 cover decline / later 路径。
        handleMiniGameInviteResolved: I.handleMiniGameInviteResolved,
        isMounted: function () { return I.mounted; }
    });

    delete window.__appReactChatWindowParts;
})();
