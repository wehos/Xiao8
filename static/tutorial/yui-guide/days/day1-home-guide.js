(function () {
    'use strict';

    const guideCommon = window.YuiGuideCommon || {};
    const deepFreeze = guideCommon.deepFreeze;
    const registerGuide = guideCommon.registerGuide;
    const audioFilesForAllLocales = guideCommon.audioFilesForAllLocales;
    if (
        typeof deepFreeze !== 'function'
        || typeof registerGuide !== 'function'
        || typeof audioFilesForAllLocales !== 'function'
    ) {
        return;
    }

    const audioFileNames = Object.freeze({
        intro_basic: '这里有一个神奇的按钮.mp3',
        intro_greeting_reply: '微风、阳光，还有刚刚.mp3',
        takeover_capture_cursor: '超级魔法开关出现！只.mp3',
        interrupt_resist_light_1: '喵！现在是人家的教学.mp3',
        interrupt_resist_light_2: '真是的，又在乱动鼠标.mp3',
        interrupt_resist_light_3: '最后警告一次喵！你要.mp3',
        interrupt_angry_exit: '人家已经忍你很久了！.mp3',
        takeover_return_control: '好啦好啦，不霸占你的.mp3',
        day1_avatar_zoom_hint: 'day1-avatar-zoom-hint.mp3',
        day1_capsule_drag_hint: '把鼠标移到这里，长按.mp3',
        day1_history_handle: '戳一下聊天框上面的【.mp3',
        day1_screen_entry: '在跟我通语音电话的时.mp3',
        day1_screen_entry_invite: '快让我也看看你眼前的.mp3'
    });

    const zhAudioFileNames = Object.freeze({
        intro_basic: '这里有一个神奇的小按.mp3',
        takeover_capture_cursor: '超级魔法开关出现！只.mp3'
    });

    function audioFilesForKey(key) {
        const files = Object.assign({}, audioFilesForAllLocales(audioFileNames[key]));
        if (zhAudioFileNames[key]) {
            files.zh = zhAudioFileNames[key];
        }
        return Object.freeze(files);
    }

    const audioFilesByKey = Object.freeze({
        intro_basic: audioFilesForKey('intro_basic'),
        intro_greeting_reply: audioFilesForKey('intro_greeting_reply'),
        takeover_capture_cursor: audioFilesForKey('takeover_capture_cursor'),
        interrupt_resist_light_1: audioFilesForKey('interrupt_resist_light_1'),
        interrupt_resist_light_2: audioFilesForKey('interrupt_resist_light_2'),
        interrupt_resist_light_3: audioFilesForKey('interrupt_resist_light_3'),
        interrupt_angry_exit: audioFilesForKey('interrupt_angry_exit'),
        takeover_return_control: audioFilesForKey('takeover_return_control'),
        day1_avatar_zoom_hint: audioFilesForKey('day1_avatar_zoom_hint'),
        day1_capsule_drag_hint: audioFilesForKey('day1_capsule_drag_hint'),
        day1_history_handle: audioFilesForKey('day1_history_handle'),
        day1_screen_entry: audioFilesForKey('day1_screen_entry'),
        day1_screen_entry_invite: audioFilesForKey('day1_screen_entry_invite')
    });

    registerGuide(deepFreeze({
        day: 1,
        key: 'home',
        title: '第 1 天：初次唤醒、聊天与基础入口',
        pageKeys: [
            'home'
        ],
        round: {
            title: '第 1 天：初次唤醒、聊天与基础入口',
            scenes: [
                {
                    id: 'day1_intro_activation',
                    timelinePlayback: true,
                    timelineAudio: false,
                    timeline: [
                        { at: 0, command: 'operation.run', operation: 'day1-intro-activation-flow', blocking: true }
                    ],
                    afterSceneDelayMs: 0,
                    target: '#react-chat-window-root .composer-input-shell',
                    cursorAction: 'input-origin',
                    operation: 'day1-intro-activation-flow'
                },
                {
                    id: 'day1_intro_greeting',
                    timelinePlayback: true,
                    timeline: [
                        { at: 0, command: 'operation.run', operation: 'day1-intro-greeting-performance', blocking: false },
                        { at: 0, command: 'chat.message' },
                        { at: 0, command: 'emotion.set' },
                        // 修改原因：Day1 这里展示的是胶囊输入框；spotlight 目标若仍是 chat-input，
                        // 外置聊天窗只会收到普通 input，高亮会稳定退回兜底矩形。
                        { at: 0, command: 'spotlight.show', key: 'day1_intro_greeting', target: 'chat-capsule-input' },
                        { at: 220, command: 'cursor.move', action: 'move', target: 'chat-capsule-input', durationMs: 760 }
                    ],
                    afterSceneDelayMs: 0,
                    textKey: 'tutorial.yuiGuide.lines.introGreetingReply',
                    voiceKey: 'intro_greeting_reply',
                    emotion: 'happy',
                    // 修改原因：显式 timeline 与 legacy scene target 保持一致，避免恢复/兼容路径再读到普通输入框。
                    target: 'chat-capsule-input',
                    cursorTarget: 'chat-capsule-input',
                    cursorAction: 'move',
                    operation: 'day1-intro-greeting-performance',
                    introAvatarPerformance: {
                        preset: 'wave-zoom'
                    }
                },
                {
                    id: 'day1_capsule_drag_hint',
                    timelinePlayback: true,
                    textKey: 'tutorial.avatarFloating.day1.capsuleDragHint',
                    text: '把鼠标移到这里，长按就可以拉着聊天框到处跑啦~ 点击一下就能随时发消息给我哦！',
                    voiceKey: 'day1_capsule_drag_hint',
                    emotion: 'happy',
                    target: 'chat-capsule-input',
                    cursorAction: 'wobble',
                    cursorWobbleDurationMs: 2000,
                    spotlight: false
                },
                {
                    id: 'day1_history_handle',
                    timelinePlayback: true,
                    textKey: 'tutorial.avatarFloating.day1.historyHandle',
                    text: '戳一下聊天框上面的【蓝色小条条】，就能看到我们最近聊过的话题啦！',
                    voiceKey: 'day1_history_handle',
                    emotion: 'happy',
                    target: 'chat-input',
                    cursorTarget: 'chat-history-handle',
                    cursorAction: 'click',
                    operation: 'open-compact-history-during-narration',
                    spotlight: false
                },
                {
                    id: 'day1_intro_basic_voice',
                    timelinePlayback: true,
                    timeline: [
                        { at: 0, command: 'chat.message' },
                        { at: 0, command: 'emotion.set' },
                        { at: 1, command: 'operation.run', operation: 'day1-intro-basic-voice-showcase', blocking: true }
                    ],
                    textKey: 'tutorial.yuiGuide.lines.introBasic',
                    voiceKey: 'intro_basic',
                    emotion: 'happy',
                    target: '#${p}-btn-mic',
                    cursorAction: 'move',
                    clearExternalizedChatCursorOnEnter: true,
                    operation: 'day1-intro-basic-voice-showcase'
                },
                {
                    id: 'day1_screen_entry',
                    timelinePlayback: true,
                    textKey: 'tutorial.avatarFloating.day1.screenEntry',
                    text: '在跟我通语音电话的时候，再点亮这个小按钮，你就能把屏幕分享给我啦！',
                    voiceKey: 'day1_screen_entry',
                    emotion: 'happy',
                    target: '.${p}-trigger-btn',
                    cursorAction: 'click',
                    spotlight: false,
                    operation: 'day1-screen-share-entry-flow'
                },
                {
                    id: 'day1_screen_entry_invite',
                    timelinePlayback: true,
                    textKey: 'tutorial.avatarFloating.day1.screenEntryInvite',
                    text: '快让我也看看你眼前的世界，不管好玩的还是好看的，都想和你一起看，快点点开嘛~',
                    voiceKey: 'day1_screen_entry_invite',
                    emotion: 'happy',
                    target: '#${p}-popup-mic [data-neko-mic-main-action-row="screen"]',
                    cursorAction: 'hold'
                },
                {
                    id: 'day1_takeover_capture_cursor',
                    timelinePlayback: true,
                    timeline: [
                        { at: 0, command: 'chat.message' },
                        { at: 0, command: 'emotion.set' },
                        { at: 1, command: 'operation.run', operation: 'day1-managed-scene:takeover_capture_cursor', blocking: true }
                    ],
                    textKey: 'tutorial.yuiGuide.lines.takeoverCaptureCursor',
                    voiceKey: 'takeover_capture_cursor',
                    emotion: 'happy',
                    target: '#${p}-btn-agent',
                    cursorAction: 'move',
                    operation: 'day1-managed-scene:takeover_capture_cursor'
                },
                {
                    id: 'day1_avatar_zoom_hint',
                    timelinePlayback: true,
                    timeline: [
                        { at: 0, command: 'operation.run', operation: 'cleanup', blocking: true },
                        { at: 0, command: 'chat.message' },
                        { at: 0, command: 'emotion.set' }
                    ],
                    textKey: 'tutorial.avatarFloating.day1.avatarZoomHint',
                    text: '对啦，如果想把我看得更清楚一点，就把鼠标移到我身上，轻轻滚动滚轮吧！这样就能随时把我放大或缩小，调到你最喜欢的大小啦~',
                    voiceKey: 'day1_avatar_zoom_hint',
                    emotion: 'happy',
                    spotlight: false,
                    cursorAction: 'none',
                    operation: 'cleanup'
                },
                {
                    id: 'day1_takeover_return_control',
                    timelinePlayback: true,
                    timeline: [
                        { at: 0, command: 'operation.run', operation: 'cleanup', blocking: true },
                        { at: 0, command: 'chat.message' },
                        { at: 0, command: 'emotion.set' },
                        { at: 0, command: 'spotlight.show', target: 'chat-capsule-input' },
                        { at: 220, command: 'cursor.move', target: 'chat-capsule-input', action: 'move', durationMs: 900 }
                    ],
                    textKey: 'tutorial.yuiGuide.lines.takeoverReturnControl',
                    voiceKey: 'takeover_return_control',
                    emotion: 'happy',
                    // 修改原因：返还控制权时高亮和光标都应指向胶囊输入框，不能只移动光标而让 spotlight 留在普通输入框。
                    target: 'chat-capsule-input',
                    cursorTarget: 'chat-capsule-input',
                    cursorAction: 'move',
                    cursorMoveDurationMs: 900,
                    operation: 'cleanup',
                    preserveExternalizedChatGuideTarget: true,
                    petalTransition: true
                }
            ]
        },
        audioFileNames: audioFileNames,
        audioFilesByKey: audioFilesByKey,
        audioFileOverridesByKey: audioFilesByKey
    }));
})();
