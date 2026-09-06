/**
 * Live2D UI Drag - 拖拽和搭话方式控件
 * 包含按钮事件传播管理、返回按钮拖拽、搭话方式选项控件、折叠功能
 * 弹出框管理（showPopup/closePopupById 等）由 AvatarPopupMixin 提供
 * （avatar-ui-popup.js，经 avatar-ui-popup-config.js 应用到 Live2DManager）
 */

// ===== 拖拽辅助工具 - 按钮事件传播管理 =====
// 使用 body class 控制全局拖动屏蔽，替代逐元素 inline style 遍历。
// 优势：不受 CSS !important 优先级竞争影响，新增 UI 组件只需在 CSS 中加选择器。
(function() {
    'use strict';

    var DRAGGING_CLASS = 'neko-model-dragging';

    // 注入全局 CSS：拖动期间禁用所有按钮、容器、弹窗、侧面板的 pointer-events
    // 【维护注意】新增可交互 UI 组件时，需在此选择器列表中追加对应选择器，
    //  否则拖动模型经过该组件时会出现「粘手」卡顿。
    //  容器类选择器需同时加 * 后代通配符（因为按钮包装器是无 class 的匿名 div）。
    var styleId = 'neko-drag-helpers-styles';
    if (!document.getElementById(styleId)) {
        var style = document.createElement('style');
        style.id = styleId;
        style.textContent = [
            'body.' + DRAGGING_CLASS + ' .live2d-floating-btn,',
            'body.' + DRAGGING_CLASS + ' .live2d-trigger-btn,',
            'body.' + DRAGGING_CLASS + ' [id^="live2d-btn-"],',
            'body.' + DRAGGING_CLASS + ' .vrm-floating-btn,',
            'body.' + DRAGGING_CLASS + ' [id^="vrm-btn-"],',
            'body.' + DRAGGING_CLASS + ' .mmd-floating-btn,',
            'body.' + DRAGGING_CLASS + ' [id^="mmd-btn-"],',
            // 四种模型的锁图标统一按 id 后缀匹配（含 pngtuber-lock-icon），与
            // index.css 的 [id$="-lock-icon"]{pointer-events:auto} 兜底对偶；少一个 prefix
            // 就会让该模型拖拽时锁仍可点而粘手。
            'body.' + DRAGGING_CLASS + ' [id$="-lock-icon"],',
            'body.' + DRAGGING_CLASS + ' #live2d-floating-buttons,',
            'body.' + DRAGGING_CLASS + ' #live2d-floating-buttons *,',
            'body.' + DRAGGING_CLASS + ' #vrm-floating-buttons,',
            'body.' + DRAGGING_CLASS + ' #vrm-floating-buttons *,',
            'body.' + DRAGGING_CLASS + ' #mmd-floating-buttons,',
            'body.' + DRAGGING_CLASS + ' #mmd-floating-buttons *,',
            'body.' + DRAGGING_CLASS + ' .live2d-popup,',
            'body.' + DRAGGING_CLASS + ' .live2d-popup *,',
            'body.' + DRAGGING_CLASS + ' [id^="live2d-popup-"],',
            'body.' + DRAGGING_CLASS + ' [id^="live2d-popup-"] *,',
            'body.' + DRAGGING_CLASS + ' .vrm-popup,',
            'body.' + DRAGGING_CLASS + ' .vrm-popup *,',
            'body.' + DRAGGING_CLASS + ' [id^="vrm-popup-"],',
            'body.' + DRAGGING_CLASS + ' [id^="vrm-popup-"] *,',
            'body.' + DRAGGING_CLASS + ' .mmd-popup,',
            'body.' + DRAGGING_CLASS + ' .mmd-popup *,',
            'body.' + DRAGGING_CLASS + ' [id^="mmd-popup-"],',
            'body.' + DRAGGING_CLASS + ' [id^="mmd-popup-"] *,',
            'body.' + DRAGGING_CLASS + ' [data-neko-sidepanel],',
            'body.' + DRAGGING_CLASS + ' [data-neko-sidepanel] * {',
            '    pointer-events: none !important;',
            '}',
            '',
            '/* 排除返回按钮容器——它们有自己的拖拽行为 */',
            'body.' + DRAGGING_CLASS + ' [id$="-return-button-container"],',
            'body.' + DRAGGING_CLASS + ' [id$="-return-button-container"] * {',
            '    pointer-events: auto !important;',
            '}'
        ].join('\n');
        document.head.appendChild(style);
    }

    /**
     * 禁用按钮的 pointer-events
     * 在拖动开始时调用，通过 body class 让 CSS 规则生效
     */
    function disableButtonPointerEvents() {
        document.body.classList.add(DRAGGING_CLASS);
        // 拖动开始时关闭所有已展开的弹窗
        [window.live2dManager, window.vrmManager, window.mmdManager].forEach(function(m) {
            if (m && typeof m.closeAllPopups === 'function') m.closeAllPopups();
        });
    }

    /**
     * 恢复按钮的 pointer-events
     * 在拖动结束时调用，移除 body class 让 CSS 规则失效
     */
    function restoreButtonPointerEvents() {
        document.body.classList.remove(DRAGGING_CLASS);
    }

    // 挂载到全局 window 对象，供其他脚本使用
    window.DragHelpers = {
        disableButtonPointerEvents: disableButtonPointerEvents,
        restoreButtonPointerEvents: restoreButtonPointerEvents
    };
})();

// 为"请她回来"按钮容器设置拖动功能
Live2DManager.prototype.setupReturnButtonContainerDrag = function (returnButtonContainer) {
    let isDragging = false;
    let dragStartX = 0;
    let dragStartY = 0;
    let containerStartX = 0;
    let containerStartY = 0;
    let isClick = false; // 标记是否为点击操作

    // 鼠标按下事件
    returnButtonContainer.addEventListener('mousedown', (e) => {
        // 允许在按钮容器本身和按钮元素上都能开始拖动
        // 这样就能在按钮正中心位置进行拖拽操作
        if (e.target === returnButtonContainer || e.target.classList.contains('live2d-return-btn')) {
            isDragging = true;
            isClick = true;
            dragStartX = e.clientX;
            dragStartY = e.clientY;

            const rect = returnButtonContainer.getBoundingClientRect();
            containerStartX = rect.left;
            containerStartY = rect.top;
            returnButtonContainer.style.right = '';
            returnButtonContainer.style.bottom = '';
            returnButtonContainer.style.left = `${containerStartX}px`;
            returnButtonContainer.style.top = `${containerStartY}px`;

            returnButtonContainer.setAttribute('data-dragging', 'false');
            returnButtonContainer.style.cursor = 'grabbing';
            e.preventDefault();
        }
    });

    // 鼠标移动事件
    document.addEventListener('mousemove', (e) => {
        if (isDragging) {
            const deltaX = e.clientX - dragStartX;
            const deltaY = e.clientY - dragStartY;

            const dragThreshold = 5;
            if (Math.abs(deltaX) > dragThreshold || Math.abs(deltaY) > dragThreshold) {
                isClick = false;
                returnButtonContainer.setAttribute('data-dragging', 'true');
            }

            const newX = containerStartX + deltaX;
            const newY = containerStartY + deltaY;

            // 边界检查 - 使用窗口尺寸（窗口只覆盖当前屏幕）
            const containerWidth = returnButtonContainer.offsetWidth || 64;
            const containerHeight = returnButtonContainer.offsetHeight || 64;

            const boundedX = Math.max(0, Math.min(newX, window.innerWidth - containerWidth));
            const boundedY = Math.max(0, Math.min(newY, window.innerHeight - containerHeight));

            returnButtonContainer.style.left = `${boundedX}px`;
            returnButtonContainer.style.top = `${boundedY}px`;
        }
    });

    // 鼠标释放事件
    document.addEventListener('mouseup', (e) => {
        if (isDragging) {
            setTimeout(() => {
                returnButtonContainer.setAttribute('data-dragging', 'false');
            }, 10);

            isDragging = false;
            isClick = false;
            returnButtonContainer.style.cursor = 'grab';
        }
    });

    // 设置初始鼠标样式
    returnButtonContainer.style.cursor = 'grab';

    // 触摸事件支持
    returnButtonContainer.addEventListener('touchstart', (e) => {
        // 允许在按钮容器本身和按钮元素上都能开始拖动
        if (e.target === returnButtonContainer || e.target.classList.contains('live2d-return-btn')) {
            isDragging = true;
            isClick = true;
            const touch = e.touches[0];
            dragStartX = touch.clientX;
            dragStartY = touch.clientY;

            const rect = returnButtonContainer.getBoundingClientRect();
            containerStartX = rect.left;
            containerStartY = rect.top;
            returnButtonContainer.style.right = '';
            returnButtonContainer.style.bottom = '';
            returnButtonContainer.style.left = `${containerStartX}px`;
            returnButtonContainer.style.top = `${containerStartY}px`;

            returnButtonContainer.setAttribute('data-dragging', 'false');
            e.preventDefault();
        }
    });

    document.addEventListener('touchmove', (e) => {
        if (isDragging) {
            const touch = e.touches[0];
            const deltaX = touch.clientX - dragStartX;
            const deltaY = touch.clientY - dragStartY;

            const dragThreshold = 5;
            if (Math.abs(deltaX) > dragThreshold || Math.abs(deltaY) > dragThreshold) {
                isClick = false;
                returnButtonContainer.setAttribute('data-dragging', 'true');
            }

            const newX = containerStartX + deltaX;
            const newY = containerStartY + deltaY;

            // 边界检查 - 使用窗口尺寸
            const containerWidth = returnButtonContainer.offsetWidth || 64;
            const containerHeight = returnButtonContainer.offsetHeight || 64;

            const boundedX = Math.max(0, Math.min(newX, window.innerWidth - containerWidth));
            const boundedY = Math.max(0, Math.min(newY, window.innerHeight - containerHeight));

            returnButtonContainer.style.left = `${boundedX}px`;
            returnButtonContainer.style.top = `${boundedY}px`;
            e.preventDefault();
        }
    });

    document.addEventListener('touchend', (e) => {
        if (isDragging) {
            setTimeout(() => {
                returnButtonContainer.setAttribute('data-dragging', 'false');
            }, 10);

            isDragging = false;
            isClick = false;
        }
    });
};

// 全局函数：更新圆形指示器样式
window.updateChatModeStyle = function(checkbox) {
    if (!checkbox) return;
    const wrapper = checkbox.parentElement;
    if (!wrapper) return;
    const indicator = wrapper.querySelector('.chat-mode-indicator');
    const checkmark = indicator?.querySelector('.chat-mode-checkmark');
    if (!indicator || !checkmark) return;
    if (checkbox.checked) {
        indicator.style.backgroundColor = 'var(--neko-popup-accent, #44b7fe)';
        indicator.style.borderColor = 'var(--neko-popup-accent, #44b7fe)';
        checkmark.style.opacity = '1';
    } else {
        indicator.style.backgroundColor = 'transparent';
        indicator.style.borderColor = 'var(--neko-popup-indicator-border, #ccc)';
        checkmark.style.opacity = '0';
    }

    const hovered = wrapper.matches(':hover');
    wrapper.style.background = hovered
        ? (checkbox.checked
            ? 'var(--neko-popup-selected-hover, rgba(68,183,254,0.15))'
            : 'var(--neko-popup-hover-subtle, rgba(68,183,254,0.08))')
        : 'transparent';
};

// 兼容旧函数名
window.updateVisionOnlyStyle = window.updateChatModeStyle;

// 全局工厂函数：创建搭话方式选项控件
window.createChatModeToggle = function(options) {
    const { checkboxId, labelKey, tooltipKey, globalVarName } = options;
    
    const wrapper = document.createElement('div');
    const tooltipText = window.t ? window.t(tooltipKey) : '';
    wrapper.title = tooltipText;
    Object.assign(wrapper.style, {
        display: 'flex',
        alignItems: 'center',
        gap: '4px',
        width: '100%',
        padding: '6px 10px',
        marginTop: '0',
        cursor: 'pointer',
        borderRadius: '6px',
        boxSizing: 'border-box',
        transition: 'background 0.2s ease'
    });

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.id = checkboxId;
    console.log(`[ChatModeToggle] 初始化 checkbox: ${checkboxId}, globalVarName=${globalVarName}, window值=${window[globalVarName]}`);
    if (typeof window[globalVarName] !== 'undefined') {
        checkbox.checked = window[globalVarName];
    }
    Object.assign(checkbox.style, {
        position: 'absolute',
        opacity: '0',
        width: '0',
        height: '0'
    });

    const indicator = document.createElement('div');
    indicator.classList.add('chat-mode-indicator');
    Object.assign(indicator.style, {
        width: '16px',
        height: '16px',
        borderRadius: '50%',
        border: '2px solid var(--neko-popup-indicator-border, #ccc)',
        backgroundColor: 'transparent',
        cursor: 'pointer',
        flexShrink: '0',
        transition: 'all 0.2s ease',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center'
    });

    const checkmark = document.createElement('div');
    checkmark.classList.add('chat-mode-checkmark');
    checkmark.innerHTML = '✓';
    Object.assign(checkmark.style, {
        color: '#fff',
        fontSize: '11px',
        fontWeight: 'bold',
        lineHeight: '1',
        opacity: '0',
        transition: 'opacity 0.2s ease',
        pointerEvents: 'none',
        userSelect: 'none'
    });
    indicator.appendChild(checkmark);

    const label = document.createElement('label');
    label.textContent = window.t ? window.t(labelKey) : '';
    label.setAttribute('data-i18n', labelKey);
    label.htmlFor = checkboxId;
    Object.assign(label.style, {
        fontSize: '12px',
        color: 'var(--neko-popup-text, #333)',
        cursor: 'pointer',
        whiteSpace: 'nowrap'
    });

    checkbox.addEventListener('change', (e) => {
        e.stopPropagation();
        window.updateChatModeStyle(checkbox);
        window[globalVarName] = checkbox.checked;
        if (typeof window.saveNEKOSettings === 'function') {
            window.saveNEKOSettings();
        }
        if (checkbox.checked) {
            // 开启时，如果主动搭话已开启，重置并启动调度
            if (window.proactiveChatEnabled && typeof window.resetProactiveChatBackoff === 'function') {
                window.resetProactiveChatBackoff();
            }
        } else {
            // 关闭时的逻辑：区分主开关和子模式
            const isMainSwitch = globalVarName === 'proactiveChatEnabled';
            
            if (isMainSwitch) {
                // 主开关关闭：停止调度
                if (typeof window.stopProactiveChatSchedule === 'function') {
                    window.stopProactiveChatSchedule();
                }
            } else {
                // 子模式关闭：如果没有其他子模式开启，停止调度
                const hasOtherSubMode = (window.CHAT_MODE_CONFIG || []).some(config =>
                    config.globalVarName !== globalVarName && Boolean(window[config.globalVarName])
                );
                if (!hasOtherSubMode && typeof window.stopProactiveChatSchedule === 'function') {
                    window.stopProactiveChatSchedule();
                }
            }
        }
        console.log(`${label.textContent}已${checkbox.checked ? '开启' : '关闭'}`);
    });

    checkbox.addEventListener('click', (e) => e.stopPropagation());
    wrapper.addEventListener('mouseenter', () => {
        window.updateChatModeStyle(checkbox);
    });
    wrapper.addEventListener('mouseleave', () => {
        window.updateChatModeStyle(checkbox);
    });
    wrapper.addEventListener('click', (e) => {
        if (e.target === checkbox) return;
        e.preventDefault();
        e.stopPropagation();
        checkbox.click();
    });
    label.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        checkbox.click();
    });
    indicator.addEventListener('click', (e) => {
        e.stopPropagation();
        checkbox.click();
    });

    wrapper.appendChild(checkbox);
    wrapper.appendChild(indicator);
    wrapper.appendChild(label);

    window.updateChatModeStyle(checkbox);

    return wrapper;
};

// 聊天模式配置（单一数据源）
window.CHAT_MODE_CONFIG = [
    {
        mode: 'vision',
        labelKey: 'settings.toggles.proactiveVisionChat',
        tooltipKey: 'settings.toggles.proactiveVisionChatTooltip',
        globalVarName: 'proactiveVisionChatEnabled'
    },
    {
        mode: 'news',
        labelKey: 'settings.toggles.proactiveNewsChat',
        tooltipKey: 'settings.toggles.proactiveNewsChatTooltip',
        globalVarName: 'proactiveNewsChatEnabled'
    },
    {
        mode: 'community',
        labelKey: 'settings.toggles.proactiveCommunityChat',
        tooltipKey: 'settings.toggles.proactiveCommunityChatTooltip',
        globalVarName: 'proactiveCommunityChatEnabled'
    },
    {
        mode: 'video',
        labelKey: 'settings.toggles.proactiveVideoChat',
        tooltipKey: 'settings.toggles.proactiveVideoChatTooltip',
        globalVarName: 'proactiveVideoChatEnabled'
    },
    {
        mode: 'personal',
        labelKey: 'settings.toggles.proactivePersonalChat',
        tooltipKey: 'settings.toggles.proactivePersonalChatTooltip',
        globalVarName: 'proactivePersonalChatEnabled'
    },
    {
        mode: 'music',
        labelKey: 'settings.toggles.proactiveMusicChat',
        tooltipKey: 'settings.toggles.proactiveMusicChatTooltip',
        globalVarName: 'proactiveMusicEnabled'
    },
    {
        mode: 'meme',
        labelKey: 'settings.toggles.proactiveMemeChat',
        tooltipKey: 'settings.toggles.proactiveMemeChatTooltip',
        globalVarName: 'proactiveMemeEnabled'
    },
    {
        mode: 'mini_game',
        labelKey: 'settings.toggles.proactiveMiniGameInviteChat',
        tooltipKey: 'settings.toggles.proactiveMiniGameInviteChatTooltip',
        globalVarName: 'proactiveMiniGameInviteEnabled'
    }
];

// 全局工厂函数：创建所有搭话方式选项
window.createChatModeToggles = function(prefix) {
    const container = document.createElement('div');
    Object.assign(container.style, {
        display: 'flex',
        flexDirection: 'column',
        gap: '1px',
        width: '100%'
    });

    // 使用共享配置创建搭话方式选项
    window.CHAT_MODE_CONFIG.forEach(config => {
        const toggle = window.createChatModeToggle({
            checkboxId: `${prefix}-proactive-${config.mode}-chat`,
            labelKey: config.labelKey,
            tooltipKey: config.tooltipKey,
            globalVarName: config.globalVarName
        });
        container.appendChild(toggle);
    });

    return container;
};

// 兼容旧函数名
window.createVisionOnlyToggle = function(checkboxId) {
    return window.createChatModeToggle({
        checkboxId: checkboxId,
        labelKey: 'settings.toggles.proactiveVisionChat',
        tooltipKey: 'settings.toggles.proactiveVisionChatTooltip',
        globalVarName: 'proactiveVisionChatEnabled'
    });
};

// 设置折叠功能
Live2DManager.prototype._setupCollapseFunctionality = function (emptyState, collapseButton, emptyContent) {
    // 获取折叠状态
    const getCollapsedState = () => {
        try {
            const saved = localStorage.getItem('agent-task-empty-collapsed');
            return saved === 'true';
        } catch (error) {
            console.warn('Failed to read collapse state from localStorage:', error);
            return false;
        }
    };

    // 保存折叠状态
    const saveCollapsedState = (collapsed) => {
        try {
            localStorage.setItem('agent-task-empty-collapsed', collapsed.toString());
        } catch (error) {
            console.warn('Failed to save collapse state to localStorage:', error);
        }
    };

    // 初始化状态
    let isCollapsed = getCollapsedState();
    let touchProcessed = false; // 防止触摸设备双重切换的标志

    // 更新折叠状态
    const updateCollapseState = (collapsed) => {
        isCollapsed = collapsed;

        if (collapsed) {
            // 折叠状态
            emptyState.classList.add('collapsed');
            collapseButton.classList.add('collapsed');
            collapseButton.innerHTML = '▶';
        } else {
            // 展开状态
            emptyState.classList.remove('collapsed');
            collapseButton.classList.remove('collapsed');
            collapseButton.innerHTML = '▼';
        }

        // 保存状态
        saveCollapsedState(collapsed);
    };

    // 应用初始状态
    updateCollapseState(isCollapsed);

    // 点击事件处理
    collapseButton.addEventListener('click', (e) => {
        e.stopPropagation();
        // 如果是触摸设备刚刚处理过，则忽略click事件
        if (touchProcessed) {
            touchProcessed = false; // 重置标志
            return;
        }
        updateCollapseState(!isCollapsed);
    });

    // 悬停效果
    collapseButton.addEventListener('mouseenter', () => {
        collapseButton.style.background = 'rgba(100, 116, 139, 0.6)';
        collapseButton.style.transform = 'scale(1.1)';
    });

    collapseButton.addEventListener('mouseleave', () => {
        collapseButton.style.background = isCollapsed ?
            'rgba(100, 116, 139, 0.5)' : 'rgba(100, 116, 139, 0.3)';
        collapseButton.style.transform = 'scale(1)';
    });

    // 触摸设备优化
    collapseButton.addEventListener('touchstart', (e) => {
        e.stopPropagation();
        // 阻止默认行为，防止后续click事件
        e.preventDefault();
        collapseButton.style.background = 'rgba(100, 116, 139, 0.7)';
        collapseButton.style.transform = 'scale(1.1)';
    }, { passive: false });

    collapseButton.addEventListener('touchend', (e) => {
        e.stopPropagation();
        // 阻止click事件的触发
        e.preventDefault();

        // 设置标志，阻止后续的click事件
        touchProcessed = true;

        updateCollapseState(!isCollapsed);
        collapseButton.style.background = isCollapsed ?
            'rgba(100, 116, 139, 0.5)' : 'rgba(100, 116, 139, 0.3)';
        collapseButton.style.transform = 'scale(1)';

        // 短时间后重置标志，允许后续的点击操作
        setTimeout(() => {
            touchProcessed = false;
        }, 100);
    }, { passive: false });
};
