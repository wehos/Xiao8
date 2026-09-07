(function () {
    'use strict';

    const namespace = window.__YuiGuideDirector = window.__YuiGuideDirector || {};

    const YUI_GUIDE_CHAT_BRIDGE_QUEUE_KEY = 'neko_yui_guide_chat_bridge_queue_v1';
    const YUI_GUIDE_CHAT_BRIDGE_QUEUE_LIMIT = 160;
    const TutorialVisualControllers = window.TutorialVisualControllers || {};
    const TutorialResistanceControllers = window.TutorialResistanceControllers || {};
    const ResistanceController = TutorialResistanceControllers.ResistanceController;
    const SidebarPauseController = TutorialResistanceControllers.SidebarPauseController;
    const PauseCoordinator = TutorialResistanceControllers.PauseCoordinator;
    const TutorialTerminationRouter = TutorialResistanceControllers.TutorialTerminationRouter;
    const TutorialOperationRegistry = window.TutorialOperationRegistry || {};
    const OperationRegistry = TutorialOperationRegistry.OperationRegistry;
    const TutorialSceneOrchestrator = window.TutorialSceneOrchestrator || {};
    const TutorialSettingsTourFlow = window.TutorialSettingsTourFlow || {};

    function createYuiGuideChatBridgeCommandBus(options) {
        if (
            window.YuiGuideCommon
            && typeof window.YuiGuideCommon.createTutorialBridgeCommandBus === 'function'
        ) {
            return window.YuiGuideCommon.createTutorialBridgeCommandBus(Object.assign({
                window: window,
                storageKey: YUI_GUIDE_CHAT_BRIDGE_QUEUE_KEY,
                queueLimit: YUI_GUIDE_CHAT_BRIDGE_QUEUE_LIMIT
            }, options || {}));
        }

        return {
            readQueue: readYuiGuideChatBridgeQueue,
            enqueue: enqueueYuiGuideChatBridgeMessage,
            post(message) {
                if (!message || typeof message !== 'object' || !message.action) {
                    return false;
                }
                enqueueYuiGuideChatBridgeMessage(message);
                let posted = false;
                const channel = options && typeof options.channelProvider === 'function'
                    ? options.channelProvider()
                    : null;
                if (channel && typeof channel.postMessage === 'function') {
                    try {
                        channel.postMessage(message);
                        posted = true;
                    } catch (error) {
                        console.warn('[YuiGuide] BroadcastChannel 转发独立聊天窗消息失败:', error);
                    }
                }
                const nativeRelay = options && typeof options.nativeRelayProvider === 'function'
                    ? options.nativeRelayProvider()
                    : null;
                if (nativeRelay && typeof nativeRelay.relayToChat === 'function') {
                    try {
                        nativeRelay.relayToChat(message);
                        posted = true;
                    } catch (error) {
                        console.warn('[YuiGuide] PC 原生转发独立聊天窗消息失败:', error);
                    }
                }
                return posted;
            }
        };
    }

    function createYuiGuideTargetGeometryRegistry() {
        if (
            window.YuiGuideCommon
            && typeof window.YuiGuideCommon.createTutorialTargetGeometryRegistry === 'function'
        ) {
            return window.YuiGuideCommon.createTutorialTargetGeometryRegistry();
        }

        const externalKinds = {
            'chat-capsule-input': 'capsule-input',
            'chat-input': 'input',
            'chat-history-handle': 'history',
            'chat-tool-toggle': 'tool-toggle',
            'chat-avatar-tools': 'avatar-tools',
            'chat-galgame': 'galgame',
            'chat-avatar-tool-items': 'avatar-tool-items'
        };
        return {
            resolve(key) {
                const normalizedKey = typeof key === 'string' ? key.trim() : '';
                const externalKind = externalKinds[normalizedKey] || '';
                return externalKind ? {
                    key: normalizedKey,
                    externalKind,
                    localSelectors: []
                } : null;
            },
            getExternalKind(key) {
                const entry = this.resolve(key);
                return entry ? entry.externalKind : '';
            },
            getLocalSelectors(key) {
                const entry = this.resolve(key);
                return entry ? entry.localSelectors : [];
            }
        };
    }

    function createYuiGuideChatWindowAdapter(options) {
        if (
            window.YuiGuideCommon
            && typeof window.YuiGuideCommon.createChatWindowAdapter === 'function'
        ) {
            return window.YuiGuideCommon.createChatWindowAdapter(options || {});
        }
        return {
            isExternalized: () => false,
            getExternalKind(targetKey) {
                const registry = options && options.registry;
                return registry && typeof registry.getExternalKind === 'function'
                    ? registry.getExternalKind(targetKey)
                    : '';
            },
            resolveTarget(targetKey) {
                return options && typeof options.resolveLocalTarget === 'function'
                    ? options.resolveLocalTarget(targetKey)
                    : null;
            },
            setSpotlight: () => false,
            setCursor: () => false,
            lockInput: () => false
        };
    }

    function createYuiGuideScopedTutorialResources() {
        if (
            window.YuiGuideCommon
            && typeof window.YuiGuideCommon.createScopedTutorialResources === 'function'
        ) {
            return window.YuiGuideCommon.createScopedTutorialResources({ window: window });
        }

        const timers = [];
        return {
            setTimeout(callback, delayMs) {
                const timerId = window.setTimeout(callback, delayMs);
                timers.push(timerId);
                return timerId;
            },
            clearTimeout(timerId) {
                if (!timerId) {
                    return;
                }
                window.clearTimeout(timerId);
                const index = timers.indexOf(timerId);
                if (index !== -1) {
                    timers.splice(index, 1);
                }
            },
            destroy() {
                while (timers.length) {
                    window.clearTimeout(timers.pop());
                }
            }
        };
    }

    function readYuiGuideChatBridgeQueue() {
        try {
            const raw = window.localStorage && window.localStorage.getItem(YUI_GUIDE_CHAT_BRIDGE_QUEUE_KEY);
            const parsed = raw ? JSON.parse(raw) : [];
            return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
        } catch (_) {
            return [];
        }
    }

    function enqueueYuiGuideChatBridgeMessage(message) {
        if (!message || typeof message !== 'object' || !message.action) {
            return;
        }
        try {
            const queue = readYuiGuideChatBridgeQueue();
            queue.push(message);
            const trimmed = queue.slice(-YUI_GUIDE_CHAT_BRIDGE_QUEUE_LIMIT);
            window.localStorage.setItem(YUI_GUIDE_CHAT_BRIDGE_QUEUE_KEY, JSON.stringify(trimmed));
        } catch (error) {
            console.warn('[YuiGuide] 缓存教程聊天消息失败:', error);
        }
    }

    function postYuiGuideChatBridgeMessage(channel, message) {
        if (!message || typeof message !== 'object' || !message.action) {
            return false;
        }
        enqueueYuiGuideChatBridgeMessage(message);
        if (!channel || typeof channel.postMessage !== 'function') {
            return false;
        }
        channel.postMessage(message);
        return true;
    }

    function translateGuideText(textKey, fallbackText, interpolation) {
        const normalizedKey = typeof textKey === 'string' ? textKey.trim() : '';
        const normalizedFallback = typeof fallbackText === 'string' ? fallbackText : '';
        if (!normalizedKey || typeof window.t !== 'function') {
            return normalizedFallback;
        }

        const hasInterpolation = interpolation && typeof interpolation === 'object';
        try {
            const translated = hasInterpolation
                ? window.t(normalizedKey, interpolation)
                : window.t(normalizedKey);
            if (typeof translated === 'string' && translated.trim() && translated !== normalizedKey) {
                return translated;
            }
        } catch (_) {}

        return normalizedFallback;
    }

    function normalizeGuideLocale(locale) {
        const current = String(locale || '').trim().toLowerCase();
        if (!current || current === 'auto') {
            return 'zh';
        }

        if (current.indexOf('ja') === 0) return 'ja';
        if (current.indexOf('en') === 0) return 'en';
        if (current.indexOf('es') === 0) return 'es';
        if (current.indexOf('ko') === 0) return 'ko';
        if (current.indexOf('pt') === 0) return 'pt';
        if (current.indexOf('ru') === 0) return 'ru';
        return 'zh';
    }

    function resolveGuidePreferredLanguage() {
        const candidates = [
            window.i18n && window.i18n.language,
            window.localStorage && window.localStorage.getItem('i18nextLng'),
            document && document.documentElement && document.documentElement.lang,
            navigator && navigator.language,
            window.localStorage && window.localStorage.getItem('locale')
        ];

        for (let index = 0; index < candidates.length; index += 1) {
            const candidate = String(candidates[index] || '').trim();
            if (!candidate || candidate.toLowerCase() === 'auto') {
                continue;
            }

            const lowered = candidate.toLowerCase();
            if (lowered.indexOf('ja') === 0) return 'ja';
            if (lowered.indexOf('en') === 0) return 'en';
            if (lowered.indexOf('ko') === 0) return 'ko';
            if (lowered.indexOf('ru') === 0) return 'ru';
            if (lowered.indexOf('zh-tw') === 0 || lowered.indexOf('zh-hk') === 0 || lowered.indexOf('zh-hant') === 0) {
                return 'zh-TW';
            }
            if (lowered.indexOf('zh') === 0) {
                return 'zh-CN';
            }
        }

        return '';
    }

    function isGuideI18nReady() {
        const i18nInstance = window.i18n;
        return typeof window.t === 'function' && !!(i18nInstance && i18nInstance.isInitialized);
    }

    function waitForGuideI18nReady(timeoutMs) {
        const normalizedTimeoutMs = Number.isFinite(timeoutMs) ? timeoutMs : 5000;
        if (isGuideI18nReady()) {
            return Promise.resolve(true);
        }

        return new Promise((resolve) => {
            let settled = false;
            let timeoutId = 0;
            let pollId = 0;

            const finish = (ready) => {
                if (settled) {
                    return;
                }
                settled = true;
                if (timeoutId) {
                    window.clearTimeout(timeoutId);
                    timeoutId = 0;
                }
                if (pollId) {
                    window.clearInterval(pollId);
                    pollId = 0;
                }
                window.removeEventListener('localechange', handleLocaleReady);
                resolve(!!ready);
            };

            const handleLocaleReady = () => {
                if (isGuideI18nReady()) {
                    finish(true);
                }
            };

            pollId = window.setInterval(() => {
                if (isGuideI18nReady()) {
                    finish(true);
                }
            }, 120);
            timeoutId = window.setTimeout(() => {
                finish(isGuideI18nReady());
            }, normalizedTimeoutMs);

            window.addEventListener('localechange', handleLocaleReady);
        });
    }

    function resolveGuideLocale() {
        const candidates = [
            window.i18n && window.i18n.language,
            window.localStorage && window.localStorage.getItem('i18nextLng'),
            document && document.documentElement && document.documentElement.lang,
            navigator && navigator.language,
            window.localStorage && window.localStorage.getItem('locale')
        ];

        for (let index = 0; index < candidates.length; index += 1) {
            const candidate = String(candidates[index] || '').trim();
            if (!candidate || candidate.toLowerCase() === 'auto') {
                continue;
            }
            return normalizeGuideLocale(candidate);
        }

        return 'zh';
    }

    function guideSpeechLang() {
        const locale = resolveGuideLocale();
        if (locale === 'ja') return 'ja-JP';
        if (locale === 'en') return 'en-US';
        if (locale === 'es') return 'es-ES';
        if (locale === 'ko') return 'ko-KR';
        if (locale === 'pt') return 'pt-PT';
        if (locale === 'ru') return 'ru-RU';
        return 'zh-CN';
    }

    function resolveGuideAudioLocale(locale) {
        const candidates = locale
            ? [locale]
            : [
                window.i18n && window.i18n.language,
                window.localStorage && window.localStorage.getItem('i18nextLng'),
                document && document.documentElement && document.documentElement.lang,
                navigator && navigator.language,
                window.localStorage && window.localStorage.getItem('locale')
            ];

        for (let index = 0; index < candidates.length; index += 1) {
            const candidate = String(candidates[index] || '').trim().toLowerCase();
            if (!candidate || candidate === 'auto') {
                continue;
            }
            if (candidate.indexOf('ja') === 0) return 'ja';
            if (candidate.indexOf('en') === 0) return 'en';
            if (candidate.indexOf('ko') === 0) return 'ko';
            if (candidate.indexOf('ru') === 0) return 'ru';
            if (candidate.indexOf('zh') === 0) return 'zh';
            return 'en';
        }

        return 'en';
    }

    const AVATAR_FLOATING_GUIDE_USAGE_STORAGE_KEY = 'neko_avatar_floating_guide_usage_v1';
    const YUI_GUIDE_EXTERNAL_CHAT_CURSOR_SCREEN_POINT_KEY = 'neko_yui_guide_external_chat_cursor_screen_point_v1';

    function readAvatarFloatingGuideUsageState() {
        try {
            const raw = window.localStorage && window.localStorage.getItem(AVATAR_FLOATING_GUIDE_USAGE_STORAGE_KEY);
            return raw ? JSON.parse(raw) || {} : {};
        } catch (_) {
            return {};
        }
    }

    function writeAvatarFloatingGuideUsageState(patch) {
        if (!patch || typeof patch !== 'object') {
            return;
        }
        try {
            const next = Object.assign({}, readAvatarFloatingGuideUsageState(), patch, {
                updatedAt: Date.now()
            });
            window.localStorage.setItem(AVATAR_FLOATING_GUIDE_USAGE_STORAGE_KEY, JSON.stringify(next));
        } catch (_) {}
    }

    function normalizeAvatarFloatingGuideUsageTimestamp(value) {
        const number = Number(value);
        if (Number.isFinite(number) && number > 0) {
            return number;
        }
        if (typeof value === 'string' && value.trim()) {
            const parsed = Date.parse(value);
            return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
        }
        return 0;
    }

    function getAvatarFloatingGuideActiveRound() {
        const memoryRound = Number(window.__avatarFloatingGuideCurrentRound || 0);
        if (Number.isFinite(memoryRound) && memoryRound > 0) {
            return Math.floor(memoryRound);
        }
        const state = readAvatarFloatingGuideUsageState();
        const persistedRound = Number(state && state.currentRound);
        return Number.isFinite(persistedRound) && persistedRound > 0 ? Math.floor(persistedRound) : 0;
    }

    function recordAvatarFloatingGuideRoundStart(round) {
        const normalizedRound = Number(round);
        if (!Number.isFinite(normalizedRound) || normalizedRound <= 0) {
            return;
        }
        const day = Math.floor(normalizedRound);
        const startedAt = Date.now();
        window.__avatarFloatingGuideCurrentRound = day;
        const patch = {
            currentRound: day,
            currentRoundStartedAt: startedAt
        };
        patch['day' + day + 'StartedAt'] = startedAt;
        writeAvatarFloatingGuideUsageState(patch);
    }

    function recordAvatarFloatingGuideRoundEnd(round) {
        const normalizedRound = Number(round);
        if (!Number.isFinite(normalizedRound) || normalizedRound <= 0) {
            return;
        }
        const day = Math.floor(normalizedRound);
        const endedAt = Date.now();
        const patch = {};
        patch['day' + day + 'EndedAt'] = endedAt;
        writeAvatarFloatingGuideUsageState(patch);
    }

    function markAvatarFloatingGuideUsage(key) {
        const normalizedKey = typeof key === 'string' ? key.trim() : '';
        if (!normalizedKey) {
            return;
        }
        const activeRound = getAvatarFloatingGuideActiveRound();
        const patch = {};
        patch[normalizedKey] = true;
        patch[normalizedKey + 'At'] = Date.now();
        if (activeRound) {
            patch[normalizedKey + 'Round'] = activeRound;
        }
        writeAvatarFloatingGuideUsageState(patch);
    }

    function hasAvatarFloatingGuideUsage(key) {
        const state = readAvatarFloatingGuideUsageState();
        return !!(state && state[key]);
    }

    function hasAvatarFloatingGuideVoiceUsedAfterRoundStart(round) {
        const normalizedRound = Number(round);
        if (!Number.isFinite(normalizedRound) || normalizedRound <= 0) {
            return false;
        }
        const state = readAvatarFloatingGuideUsageState();
        if (!state || !state.voiceUsed) {
            return false;
        }
        const voiceUsedAt = normalizeAvatarFloatingGuideUsageTimestamp(state.voiceUsedAt);
        const day = Math.floor(normalizedRound);
        const roundStartKey = 'day' + day + 'StartedAt';
        const roundStartedAt = normalizeAvatarFloatingGuideUsageTimestamp(state[roundStartKey]);
        if (!voiceUsedAt) {
            return false;
        }
        if (roundStartedAt) {
            return voiceUsedAt >= roundStartedAt;
        }

        const voiceUsedRound = Number(state.voiceUsedRound);
        if (Number.isFinite(voiceUsedRound) && Math.floor(voiceUsedRound) === day) {
            return true;
        }

        const nextRoundStartedAt = normalizeAvatarFloatingGuideUsageTimestamp(state['day' + (day + 1) + 'StartedAt']);
        return !!(day === 1 && nextRoundStartedAt && voiceUsedAt < nextRoundStartedAt);
    }

    function hasAvatarFloatingGuideVoiceUsedAfterDay1EndBeforeRoundStart(round) {
        const normalizedRound = Number(round);
        if (!Number.isFinite(normalizedRound) || normalizedRound <= 0) {
            return false;
        }
        const state = readAvatarFloatingGuideUsageState();
        if (!state || !state.voiceUsed) {
            return false;
        }
        const voiceUsedAt = normalizeAvatarFloatingGuideUsageTimestamp(state.voiceUsedAt);
        const day1EndedAt = normalizeAvatarFloatingGuideUsageTimestamp(state.day1EndedAt);
        const day = Math.floor(normalizedRound);
        const roundStartedAt = normalizeAvatarFloatingGuideUsageTimestamp(state['day' + day + 'StartedAt']);
        return !!(
            voiceUsedAt
            && day1EndedAt
            && roundStartedAt
            && voiceUsedAt >= day1EndedAt
            && voiceUsedAt < roundStartedAt
        );
    }

    if (!window.__avatarFloatingGuideUsageListenersInstalled) {
        window.__avatarFloatingGuideUsageListenersInstalled = true;
        window.addEventListener('live2d-mic-toggle', function (event) {
            if (event && event.detail && event.detail.active === true) {
                markAvatarFloatingGuideUsage('voiceUsed');
            }
        }, true);
        window.addEventListener('live2d-screen-toggle', function () {
            markAvatarFloatingGuideUsage('screenShareButtonUsed');
        }, true);
        window.addEventListener('click', function (event) {
            const target = event && event.target && typeof event.target.closest === 'function'
                ? event.target
                : null;
            if (!target) {
                return;
            }
            if (target.closest('[id$="-btn-agent"], [id$="-toggle-agent-master"], [id$="-toggle-agent-keyboard"], [id$="-toggle-agent-browser"], [id$="-toggle-agent-user-plugin"]')) {
                markAvatarFloatingGuideUsage('agentUsed');
            }
            if (target.closest('[class*="trigger-icon-screen"], [id$="-popup-screen"]')) {
                markAvatarFloatingGuideUsage('screenSourcePopupUsed');
            }
            if (target.closest('[id$="-toggle-proactive-chat"]')) {
                markAvatarFloatingGuideUsage('proactiveChatOpened');
            }
            if (target.closest('#micButton')) {
                markAvatarFloatingGuideUsage('voiceUsed');
            }
        }, true);
    }

    const DEFAULT_USER_CURSOR_REVEAL_DISTANCE = 14;
    const DEFAULT_USER_CURSOR_REVEAL_INTERVAL_MS = 160;
    const DEFAULT_USER_CURSOR_REVEAL_MOVES = 2;
    const DEFAULT_INTERRUPT_COUNT_CURSOR_REVEAL_MS = 3000;
    const DEFAULT_STEP_DELAY_MS = 120;
    const DEFAULT_SCENE_SETTLE_MS = 260;
    const DEFAULT_CURSOR_DURATION_MS = 520;
    const DEFAULT_CURSOR_CLICK_VISIBLE_MS = 420;
    const DAY6_PLUGIN_AGENT_PANEL_CURSOR_MOVE_MS = 2800;
    const DAY6_PLUGIN_AGENT_PANEL_CURSOR_START_DELAY_MS = 500;
    const DAY6_PLUGIN_AGENT_PANEL_CLICK_VISIBLE_MS = 620;
    const DAY6_PLUGIN_CAT_PAW_CURSOR_OFFSET_Y = 8;
    const DAY6_PLUGIN_SIDE_PANEL_CURSOR_MOVE_MS = 1120;
    const DAY6_PLUGIN_SIDE_PANEL_CURSOR_START_DELAY_MS = 500;
    const DAY6_PLUGIN_SIDE_PANEL_CLICK_VISIBLE_MS = 480;
    const DAY6_PLUGIN_SIDE_PANEL_ACTION_TIMEOUT_MS = 1200;
    const DAY6_PLUGIN_SIDE_PANEL_DASHBOARD_WAIT_MS = 900;
    const DAY6_PLUGIN_DASHBOARD_DONE_GRACE_MS = 120;
    const INTRO_GREETING_REPLY_TEXT = '微风、阳光，还有刚刚好出现的你。初次见面，我是林悠怡，未来的日子请多关照喵！我把关于这里的一切都写进新手指南里啦！就当作是我们相遇的第一份小礼物，请查收吧！';
    const INTRO_GREETING_REPLY_TEXT_KEY = 'tutorial.yuiGuide.lines.introGreetingReply';
    const TAKEOVER_PLUGIN_DASHBOARD_TEXT = '有了它们，我不光能看 B 站弹幕，还能帮你关灯开空调…… 本喵就是无所不能的超级猫猫神！哼哼！';
    const TAKEOVER_PLUGIN_DASHBOARD_TEXT_KEY = 'tutorial.yuiGuide.lines.takeoverPluginPreviewDashboard';
    const PLUGIN_DASHBOARD_POPUP_BLOCKED_TEXT = '浏览器需要你亲自点一下这里打开插件面板。点一下这个“管理面板”，我就继续带你看。';
    const PLUGIN_DASHBOARD_POPUP_BLOCKED_TEXT_KEY = 'tutorial.yuiGuide.lines.pluginDashboardPopupBlocked';
    const TAKEOVER_SETTINGS_DETAIL_TEXT_PART_1 = '不管是说话的温度、相处的小脾气，还是我每天那些细腻的小心思，都可以一点一点调成你喜欢的样子。';
    const TAKEOVER_SETTINGS_DETAIL_TEXT_PART_2 = '这个小按钮也很重要哦，只要你轻轻点一下，我就能在合适的时候跑过去找你啦。';
    const TAKEOVER_SETTINGS_DETAIL_TEXT = TAKEOVER_SETTINGS_DETAIL_TEXT_PART_1 + TAKEOVER_SETTINGS_DETAIL_TEXT_PART_2;
    const TAKEOVER_SETTINGS_DETAIL_TEXT_KEY = 'tutorial.yuiGuide.lines.takeoverSettingsPeekDetail';
    const TAKEOVER_SETTINGS_DETAIL_TEXT_PART_1_KEY = 'tutorial.yuiGuide.lines.takeoverSettingsPeekDetailPart1';
    const TAKEOVER_SETTINGS_DETAIL_TEXT_PART_2_KEY = 'tutorial.yuiGuide.lines.takeoverSettingsPeekDetailPart2';
    const INTRO_ACTIVATION_HINT_KEY = 'tutorial.yuiGuide.lines.introActivationHint';
    const INTRO_ACTIVATION_HINT = '稍等一下，我马上开始说话啦～';
    const INTRO_ACTIVATION_AUTO_ADVANCE_MS = 2600;
    const INTRO_ACTIVATION_REDUCED_MOTION_AUTO_ADVANCE_MS = 720;
    const DEFAULT_SPOTLIGHT_PADDING = 6;
    const PLUGIN_MANAGEMENT_ENTRY_SPOTLIGHT_EXTRA_X = 18;
    const PLUGIN_MANAGEMENT_ENTRY_SPOTLIGHT_EXTRA_Y = 10;
    const NARRATION_RESUME_BACKTRACK_MS = 320;
    const NARRATION_RESUME_MIN_REMAINING_MS = 1400;
    const PLUGIN_DASHBOARD_WINDOW_NAME = 'plugin_dashboard';
    const PLUGIN_DASHBOARD_HANDOFF_EVENT = 'neko:yui-guide:plugin-dashboard:start';
    const PLUGIN_DASHBOARD_READY_EVENT = 'neko:yui-guide:plugin-dashboard:ready';
    const PLUGIN_DASHBOARD_DONE_EVENT = 'neko:yui-guide:plugin-dashboard:done';
    const PLUGIN_DASHBOARD_TERMINATE_EVENT = 'neko:yui-guide:plugin-dashboard:terminate';
    const PLUGIN_DASHBOARD_NARRATION_FINISHED_EVENT = 'neko:yui-guide:plugin-dashboard:narration-finished';
    const PLUGIN_DASHBOARD_INTERRUPT_REQUEST_EVENT = 'neko:yui-guide:plugin-dashboard:interrupt-request';
    const PLUGIN_DASHBOARD_INTERRUPT_ACK_EVENT = 'neko:yui-guide:plugin-dashboard:interrupt-ack';
    const PLUGIN_DASHBOARD_SYSTEM_CURSOR_TEMPORARY_REVEAL_EVENT = 'neko:yui-guide:plugin-dashboard:system-cursor-temporary-reveal';
    const DESKTOP_PLUGIN_DASHBOARD_INTERRUPT_ACK_EVENT = 'neko:yui-guide:desktop-interrupt-ack';
    const DESKTOP_PLUGIN_DASHBOARD_NARRATION_FINISHED_EVENT = 'neko:yui-guide:desktop-narration-finished';
    const DESKTOP_PLUGIN_DASHBOARD_SYSTEM_CURSOR_TEMPORARY_REVEAL_EVENT = 'neko:yui-guide:desktop-system-cursor-temporary-reveal';
    const DESKTOP_PLUGIN_DASHBOARD_SKIP_REQUEST_EVENT = 'neko:yui-guide:desktop-skip-request';
    const PLUGIN_DASHBOARD_SKIP_REQUEST_EVENT = 'neko:yui-guide:plugin-dashboard:skip-request';
    const DEFAULT_TUTORIAL_MODEL_MANAGER_LANLAN_NAME = 'ATLS';
    const GUIDE_AUDIO_BASE_URL = '/static/assets/tutorial/guide-audio/';
    const RETURN_PETAL_SEQUENCE_URL = '/static/assets/tutorial/petals/yui-guide-petal-transition.webp';
    function getYuiGuideDailyGuide(day) {
        const normalizedDay = Number(day);
        const registry = window.YuiGuideDailyGuides || {};
        return registry[normalizedDay] || null;
    }

    function collectGuideAudioFilesByKey() {
        const registry = window.YuiGuideDailyGuides || {};
        const result = {};
        Object.keys(registry).forEach((day) => {
            const guide = registry[day];
            if (guide && guide.audioFilesByKey) {
                Object.assign(result, guide.audioFilesByKey);
            }
        });
        return result;
    }

    const DAY1_HOME_GUIDE = getYuiGuideDailyGuide(1) || {};
    const GUIDE_AUDIO_FILES_BY_KEY = Object.freeze(collectGuideAudioFilesByKey());
    const GUIDE_AUDIO_FILE_OVERRIDES_BY_KEY = Object.freeze(Object.assign({}, DAY1_HOME_GUIDE.audioFileOverridesByKey || {}));
    const GUIDE_AUDIO_VERSION_BY_KEY = Object.freeze({
        avatar_floating_day4_model_lock: '20260701'
    });

    function guideAudioSrc(key) {
        const files = key
            ? (GUIDE_AUDIO_FILE_OVERRIDES_BY_KEY[key] || GUIDE_AUDIO_FILES_BY_KEY[key] || null)
            : null;
        if (!files) {
            return '';
        }

        // 当前 locale 没有对应语音文件时（如 es / pt 等未提供录音的语言），
        // 默认 fallback 是英文，避免回退到中文给非中文用户带来违和感。
        const locale = resolveGuideAudioLocale();
        const hasLocaleFile = Object.prototype.hasOwnProperty.call(files, locale);
        const fileName = hasLocaleFile ? files[locale] : (files.en || '');
        const fileLocale = hasLocaleFile ? locale : 'en';
        const version = GUIDE_AUDIO_VERSION_BY_KEY[key] || '';
        const versionQuery = version ? ('?v=' + encodeURIComponent(version)) : '';
        return fileName ? (GUIDE_AUDIO_BASE_URL + fileLocale + '/' + encodeURIComponent(fileName) + versionQuery) : '';
    }

    function shouldGuideAudioDriveMouth(voiceKey) {
        const normalizedKey = typeof voiceKey === 'string' ? voiceKey.trim() : '';
        return !!normalizedKey;
    }

    const TAKEOVER_CAPTURE_SELECTORS = Object.freeze({
        voiceControl: '[alt="语音控制"]',
        catPaw: '[alt="猫爪"]',
        agentMaster: '#${p}-toggle-agent-master',
        keyboardControl: '#${p}-toggle-agent-keyboard',
        userPlugin: '#${p}-toggle-agent-user-plugin',
        managementPanel: 'div#neko-sidepanel-action-agent-user-plugin-management-panel'
    });

    const AVATAR_FLOATING_GUIDE_INTERRUPT_STEP = Object.freeze({
        id: 'avatar_floating_guide_interruptible',
        performance: Object.freeze({
            interruptible: true
        }),
        interrupts: Object.freeze({
            mode: 'theatrical_abort',
            threshold: 4,
            throttleMs: 500,
            resetOnStepAdvance: false
        })
    });

    function wait(ms) {
        return new Promise((resolve) => {
            window.setTimeout(resolve, ms);
        });
    }

    function fetchWithTimeout(resource, options, timeoutMs) {
        const normalizedTimeoutMs = Math.max(1000, Math.round(Number.isFinite(timeoutMs) ? timeoutMs : 5000));
        const normalizedOptions = Object.assign({}, options || {});
        if (typeof AbortController === 'function') {
            const controller = new AbortController();
            const timeoutId = window.setTimeout(() => controller.abort(), normalizedTimeoutMs);
            normalizedOptions.signal = controller.signal;
            return fetch(resource, normalizedOptions).finally(() => {
                window.clearTimeout(timeoutId);
            });
        }

        return Promise.race([
            fetch(resource, normalizedOptions),
            new Promise((resolve, reject) => {
                window.setTimeout(() => reject(new Error('fetch_timeout')), normalizedTimeoutMs);
            })
        ]);
    }

    function resolveWithTimeout(promise, timeoutMs, fallbackValue, label) {
        const normalizedTimeoutMs = Math.max(300, Math.round(Number.isFinite(timeoutMs) ? timeoutMs : 3000));
        let timeoutId = 0;
        return Promise.race([
            Promise.resolve(promise).then(
                (value) => ({ status: 'fulfilled', value: value }),
                (error) => ({ status: 'rejected', error: error })
            ),
            new Promise((resolve) => {
                timeoutId = window.setTimeout(() => {
                    timeoutId = 0;
                    resolve({ status: 'timeout' });
                }, normalizedTimeoutMs);
            })
        ]).then((result) => {
            if (timeoutId) {
                window.clearTimeout(timeoutId);
            }
            if (result.status === 'timeout') {
                if (label) {
                    console.warn('[YuiGuide] 等待超时，使用兜底:', label);
                }
                return fallbackValue;
            }
            if (result.status === 'rejected') {
                throw result.error;
            }
            return result.value;
        });
    }

    function clamp(value, min, max) {
        return Math.max(min, Math.min(max, value));
    }

    const DAY4_LOCK_SPOTLIGHT_SAFE_BOTTOM_PX = 112;

    const HOME_TUTORIAL_PLATFORM_PROFILES = Object.freeze({
        windows: Object.freeze({
            supportsExternalChat: true,
            supportsSystemTrayHint: true,
            supportsPluginDashboardWindow: true,
            pointerProfile: 'mouse',
            browserSkipHitPadding: 28,
            electronSkipHitPadding: 20,
            browserSkipForwardingTolerance: 10,
            electronSkipForwardingToleranceRatio: 0.2,
            electronSkipForwardingToleranceMin: 4
        }),
        macos: Object.freeze({
            supportsExternalChat: true,
            supportsSystemTrayHint: true,
            supportsPluginDashboardWindow: true,
            pointerProfile: 'trackpad',
            browserSkipHitPadding: 36,
            electronSkipHitPadding: 28,
            browserSkipForwardingTolerance: 14,
            electronSkipForwardingToleranceRatio: 0.25,
            electronSkipForwardingToleranceMin: 6
        }),
        linux: Object.freeze({
            supportsExternalChat: true,
            supportsSystemTrayHint: true,
            supportsPluginDashboardWindow: true,
            pointerProfile: 'mouse',
            browserSkipHitPadding: 44,
            electronSkipHitPadding: 32,
            browserSkipForwardingTolerance: 18,
            electronSkipForwardingToleranceRatio: 0.35,
            electronSkipForwardingToleranceMin: 8
        }),
        web: Object.freeze({
            supportsExternalChat: false,
            supportsSystemTrayHint: false,
            supportsPluginDashboardWindow: true,
            pointerProfile: 'pointer',
            browserSkipHitPadding: 18,
            electronSkipHitPadding: 18,
            browserSkipForwardingTolerance: 6,
            electronSkipForwardingToleranceRatio: 0.2,
            electronSkipForwardingToleranceMin: 4
        })
    });

    function detectHomeTutorialPlatform() {
        const rawPlatform = (
            (navigator.userAgentData && navigator.userAgentData.platform)
            || navigator.platform
            || navigator.userAgent
            || ''
        ).toString().toLowerCase();
        if (rawPlatform.indexOf('mac') >= 0) return 'macos';
        if (rawPlatform.indexOf('win') >= 0) return 'windows';
        if (rawPlatform.indexOf('linux') >= 0 || rawPlatform.indexOf('x11') >= 0) return 'linux';
        return 'web';
    }

    function createHomeTutorialPlatformCapabilities(overrides) {
        const normalizedOverrides = overrides && typeof overrides === 'object' ? overrides : {};
        const platform = typeof normalizedOverrides.platform === 'string' && normalizedOverrides.platform.trim()
            ? normalizedOverrides.platform.trim().toLowerCase()
            : detectHomeTutorialPlatform();
        const profile = HOME_TUTORIAL_PLATFORM_PROFILES[platform] || HOME_TUTORIAL_PLATFORM_PROFILES.web;
        const hasElectronBounds = !!(
            window.nekoPetDrag
            && typeof window.nekoPetDrag.getBounds === 'function'
        );
        const windowBoundsSource = hasElectronBounds ? 'electron-window-bounds' : 'browser-screen-origin';
        const preferredSkipHitPadding = windowBoundsSource === 'electron-window-bounds'
            ? profile.electronSkipHitPadding
            : profile.browserSkipHitPadding;

        return Object.freeze({
            version: 1,
            platform: HOME_TUTORIAL_PLATFORM_PROFILES[platform] ? platform : 'web',
            windowBoundsSource: windowBoundsSource,
            supportsExternalChat: normalizedOverrides.supportsExternalChat === true || (
                normalizedOverrides.supportsExternalChat !== false && profile.supportsExternalChat
            ),
            supportsSystemTrayHint: normalizedOverrides.supportsSystemTrayHint === true || (
                normalizedOverrides.supportsSystemTrayHint !== false && profile.supportsSystemTrayHint
            ),
            supportsPluginDashboardWindow: normalizedOverrides.supportsPluginDashboardWindow === true || (
                normalizedOverrides.supportsPluginDashboardWindow !== false && profile.supportsPluginDashboardWindow
            ),
            pointerProfile: typeof normalizedOverrides.pointerProfile === 'string' && normalizedOverrides.pointerProfile.trim()
                ? normalizedOverrides.pointerProfile.trim()
                : profile.pointerProfile,
            preferredSkipHitPadding: preferredSkipHitPadding,
            getSkipHitPadding: function (boundsSource) {
                return boundsSource === 'electron-window-bounds'
                    ? profile.electronSkipHitPadding
                    : profile.browserSkipHitPadding;
            },
            getSkipForwardingTolerance: function (screenRect) {
                const rect = screenRect && typeof screenRect === 'object' ? screenRect : {};
                const coordinateSpace = String(rect.coordinateSpace || windowBoundsSource || '').toLowerCase();
                const rawPadding = Number(rect.hitPadding);
                const basePadding = Number.isFinite(rawPadding) ? Math.max(0, rawPadding) : preferredSkipHitPadding;
                if (coordinateSpace === 'electron-window-bounds') {
                    return Math.max(
                        profile.electronSkipForwardingToleranceMin,
                        Math.round(basePadding * profile.electronSkipForwardingToleranceRatio)
                    );
                }
                return profile.browserSkipForwardingTolerance;
            }
        });
    }

    const HOME_TUTORIAL_PLATFORM_CAPABILITIES_API = Object.freeze({
        create: createHomeTutorialPlatformCapabilities,
        detectPlatform: detectHomeTutorialPlatform,
        profiles: HOME_TUTORIAL_PLATFORM_PROFILES
    });

    window.homeTutorialPlatformCapabilities = window.homeTutorialPlatformCapabilities || HOME_TUTORIAL_PLATFORM_CAPABILITIES_API;

    const HOME_TUTORIAL_EXPERIENCE_METRICS_STORAGE_KEY = 'neko_home_tutorial_experience_metrics_v1';
    const HOME_TUTORIAL_EXPERIENCE_METRICS_LIMIT = 300;

    function readHomeTutorialExperienceMetrics() {
        try {
            const raw = window.localStorage && window.localStorage.getItem(HOME_TUTORIAL_EXPERIENCE_METRICS_STORAGE_KEY);
            const parsed = raw ? JSON.parse(raw) : [];
            return Array.isArray(parsed) ? parsed : [];
        } catch (_) {
            return [];
        }
    }

    function writeHomeTutorialExperienceMetrics(events) {
        if (!window.localStorage) {
            return false;
        }

        try {
            const boundedEvents = (Array.isArray(events) ? events : [])
                .slice(-HOME_TUTORIAL_EXPERIENCE_METRICS_LIMIT);
            window.localStorage.setItem(
                HOME_TUTORIAL_EXPERIENCE_METRICS_STORAGE_KEY,
                JSON.stringify(boundedEvents)
            );
            return true;
        } catch (_) {
            return false;
        }
    }

    function createHomeTutorialExperienceMetrics() {
        return Object.freeze({
            storageKey: HOME_TUTORIAL_EXPERIENCE_METRICS_STORAGE_KEY,
            record: function (type, detail) {
                const eventType = typeof type === 'string' ? type.trim() : '';
                if (!eventType) {
                    return null;
                }

                const event = Object.assign({
                    type: eventType,
                    timestamp: Date.now()
                }, detail && typeof detail === 'object' ? detail : {});
                const current = readHomeTutorialExperienceMetrics();
                current.push(event);
                writeHomeTutorialExperienceMetrics(current);

                try {
                    window.dispatchEvent(new CustomEvent('neko:yui-guide:experience-metric', {
                        detail: event
                    }));
                } catch (_) {}

                return event;
            },
            list: function () {
                return readHomeTutorialExperienceMetrics();
            },
            clear: function () {
                return writeHomeTutorialExperienceMetrics([]);
            },
            export: function () {
                return JSON.stringify(readHomeTutorialExperienceMetrics(), null, 2);
            }
        });
    }

    window.homeTutorialExperienceMetrics = window.homeTutorialExperienceMetrics || createHomeTutorialExperienceMetrics();

    const GUIDE_NARRATION_TIMELINES_BY_KEY = Object.freeze({
        intro_greeting_reply: Object.freeze({
            fallbackDurationMs: 15020,
            cues: Object.freeze({
                showIntroGiftHeart: Object.freeze({
                    at: 57 / 78,
                    atByLocale: Object.freeze({
                        zh: 57 / 78,
                        ja: 88 / 117,
                        en: 211 / 283,
                        ko: 88 / 127,
                        ru: 188 / 270
                    })
                })
            })
        }),
        takeover_settings_peek_intro: Object.freeze({
            fallbackDurationMs: 11877,
            cues: Object.freeze({
                openSettingsPanel: Object.freeze({ at: 9000 / 11877 })
            })
        }),
        takeover_settings_peek_detail: Object.freeze({
            fallbackDurationMs: 13923,
            cues: Object.freeze({
                showSecondLine: Object.freeze({ at: 7450 / 13923 })
            })
        }),
        takeover_return_control: Object.freeze({
            fallbackDurationMs: 11938,
            cues: Object.freeze({
                returnPetalTransition: Object.freeze({ at: 0.7 })
            })
        })
    });

    // 修改原因：教程演出会按这里的语种时长排布转场和光标动作，数值需与真实 mp3 时长同步。
    const GUIDE_AUDIO_DURATIONS_BY_KEY = Object.freeze({
        avatar_floating_day2_avatar_tools_intro: Object.freeze({ zh: 4400, ja: 5904, en: 4336, ko: 6060, ru: 5120 }),
        avatar_floating_day2_avatar_tools_props: Object.freeze({ zh: 13320, ja: 16144, en: 14681, ko: 14420, ru: 14942 }),
        avatar_floating_day2_tool_wheel_rotation: Object.freeze({ zh: 8832, ja: 10893, en: 9247, ko: 11729, ru: 7915 }),
        avatar_floating_day2_galgame_choices: Object.freeze({ zh: 9800, ja: 12382, en: 9639, ko: 11755, ru: 12931 }),
        avatar_floating_day2_galgame_intro: Object.freeze({ zh: 6640, ja: 8333, en: 7262, ko: 8803, ru: 7393 }),
        avatar_floating_day2_intro: Object.freeze({ zh: 12960, ja: 17711, en: 14054, ko: 17241, ru: 16535 }),
        avatar_floating_day2_wrap_intro: Object.freeze({ zh: 5700, ja: 6531, en: 5877, ko: 7210, ru: 6896 }),
        avatar_floating_day2_wrap_ready: Object.freeze({ zh: 5840, ja: 7993, en: 6374, ko: 7366, ru: 7210 }),
        avatar_floating_day3_intro: Object.freeze({ zh: 12768, ja: 17371, en: 14602, ko: 17711, ru: 15125 }),
        avatar_floating_day3_intro_voice_used: Object.freeze({ zh: 18336, ja: 22544, en: 20114, ko: 25260, ru: 20637 }),
        avatar_floating_day3_personalization_detail: Object.freeze({ zh: 9540, ja: 11337, en: 12042, ko: 11206, ru: 10240 }),
        avatar_floating_day3_personalization_space: Object.freeze({ zh: 7680, ja: 8882, en: 10841, ko: 10240, ru: 11729 }),
        avatar_floating_day3_proactive_chat: Object.freeze({ zh: 6800, ja: 8829, en: 9169, ko: 9352, ru: 8098 }),
        avatar_floating_day3_wrap: Object.freeze({ zh: 8500, ja: 9874, en: 8882, ko: 9535, ru: 8934 }),
        avatar_floating_day3_wrap_companion: Object.freeze({ zh: 7920, ja: 10893, en: 10371, ko: 9404, ru: 9639 }),
        avatar_floating_day3_wrap_intro: Object.freeze({ zh: 2840, ja: 2534, en: 2664, ko: 2482, ru: 2664 }),
        avatar_floating_day4_chat_settings: Object.freeze({ zh: 11880, ja: 13636, en: 12382, ko: 14472, ru: 12016 }),
        avatar_floating_day4_gaze_follow: Object.freeze({ zh: 9780, ja: 13401, en: 9352, ko: 10971, ru: 10762 }),
        avatar_floating_day4_intro: Object.freeze({ zh: 8380, ja: 8281, en: 7497, ko: 9822, ru: 8699 }),
        avatar_floating_day4_model_behavior: Object.freeze({ zh: 13600, ja: 15909, en: 16144, ko: 14785, ru: 14524 }),
        avatar_floating_day4_model_lock: Object.freeze({ zh: 18480, ja: 24137, en: 23771, ko: 26305, ru: 21473 }),
        avatar_floating_day4_privacy_mode: Object.freeze({ zh: 14880, ja: 15386, en: 14263, ko: 14472, ru: 16091 }),
        avatar_floating_day4_return_home: Object.freeze({ zh: 10940, ja: 14472, en: 13949, ko: 13819, ru: 13479 }),
        avatar_floating_day4_wrap: Object.freeze({ zh: 17520, ja: 17606, en: 16326, ko: 19670, ru: 18495 }),
        avatar_floating_day5_character_panic: Object.freeze({ zh: 10760, ja: 14367, en: 13427, ko: 15438, ru: 11206 }),
        avatar_floating_day5_character_settings: Object.freeze({ zh: 11320, ja: 12983, en: 11442, ko: 14002, ru: 10919 }),
        avatar_floating_day5_memory_entry: Object.freeze({ zh: 13340, ja: 18939, en: 14968, ko: 16353, ru: 14446 }),
        avatar_floating_day5_wrap: Object.freeze({ zh: 16680, ja: 17345, en: 17528, ko: 17424, ru: 16640 }),
        avatar_floating_day6_intro: Object.freeze({ zh: 11580, ja: 15255, en: 12382, ko: 14367, ru: 10423 }),
        avatar_floating_day6_plugin_dashboard: Object.freeze({ zh: 9400, ja: 14080, en: 11807, ko: 12565, ru: 13009 }),
        avatar_floating_day6_plugin_side_panel: Object.freeze({ zh: 3780, ja: 6374, en: 6243, ko: 7131, ru: 5721 }),
        avatar_floating_day6_status_master: Object.freeze({ zh: 4020, ja: 6374, en: 5538, ko: 5904, ru: 5721 }),
        avatar_floating_day6_task_hud: Object.freeze({ zh: 8640, ja: 9717, en: 8202, ko: 8751, ru: 8934 }),
        avatar_floating_day6_task_hud_control: Object.freeze({ zh: 9540, ja: 12695, en: 11206, ko: 12042, ru: 12147 }),
        avatar_floating_day6_wrap: Object.freeze({ zh: 11340, ja: 15438, en: 13949, ko: 16326, ru: 12330 }),
        avatar_floating_day6_wrap_cleanup: Object.freeze({ zh: 4920, ja: 6740, en: 5407, ko: 7366, ru: 5538 }),
        avatar_floating_day7_memory_control: Object.freeze({ zh: 13000, ja: 17162, en: 15203, ko: 16274, ru: 16666 }),
        avatar_floating_day7_memory_review: Object.freeze({ zh: 15500, ja: 20820, en: 19095, ko: 20219, ru: 17241 }),
        avatar_floating_day7_wrap: Object.freeze({ zh: 22100, ja: 23301, en: 26958, ko: 25443, ru: 25469 }),
        day1_avatar_zoom_hint: Object.freeze({ zh: 11760, ja: 13949, en: 12460, ko: 15491, ru: 12513 }),
        day1_capsule_drag_hint: Object.freeze({ zh: 6936, ja: 11076, en: 9900, ko: 10736, ru: 10423 }),
        day1_history_handle: Object.freeze({ zh: 5580, ja: 8385, en: 5460, ko: 6792, ru: 5877 }),
        day1_screen_entry: Object.freeze({ zh: 6080, ja: 7157, en: 5172, ko: 6896, ru: 6713 }),
        day1_screen_entry_invite: Object.freeze({ zh: 7440, ja: 11259, en: 11259, ko: 10475, ru: 9587 }),
        interrupt_angry_exit: Object.freeze({ zh: 9864, ja: 11860, en: 8646, ko: 13636, ru: 9117 }),
        interrupt_resist_light_1: Object.freeze({ zh: 7440, ja: 11624, en: 7993, ko: 10240, ru: 8464 }),
        interrupt_resist_light_2: Object.freeze({ zh: 7176, ja: 9300, en: 8150, ko: 7863, ru: 7967 }),
        interrupt_resist_light_3: Object.freeze({ zh: 6480, ja: 10188, en: 7393, ko: 8882, ru: 8620 }),
        intro_basic: Object.freeze({ zh: 13296, ja: 19984, en: 13166, ko: 17424, ru: 17032 }),
        intro_greeting_reply: Object.freeze({ zh: 15680, ja: 18965, en: 22596, ko: 19957, ru: 18991 }),
        takeover_capture_cursor: Object.freeze({ zh: 22580, ja: 28238, en: 24712, ko: 23040, ru: 25966 }),
        takeover_return_control: Object.freeze({ zh: 7500, ja: 9822, en: 9770, ko: 11024, ru: 7993 }),
        takeover_settings_peek_detail: Object.freeze({ zh: 9540, ja: 11337, en: 12042, ko: 11206, ru: 10240 }),
        takeover_settings_peek_detail_part_2: Object.freeze({ zh: 6800, ja: 8829, en: 9169, ko: 9352, ru: 8098 }),
        takeover_settings_peek_intro: Object.freeze({ zh: 7680, ja: 8882, en: 10841, ko: 10240, ru: 11729 })
    });

    function getGuideAudioCueConfig(voiceKey) {
        const normalizedKey = typeof voiceKey === 'string' ? voiceKey.trim() : '';
        if (!normalizedKey) {
            return null;
        }

        return GUIDE_NARRATION_TIMELINES_BY_KEY[normalizedKey] || null;
    }

    function getGuideAudioDurationConfig(voiceKey) {
        const normalizedKey = typeof voiceKey === 'string' ? voiceKey.trim() : '';
        if (!normalizedKey) {
            return null;
        }

        return GUIDE_AUDIO_DURATIONS_BY_KEY[normalizedKey] || null;
    }

    function formatGuideDebugText(textKey, text) {
        const content = typeof text === 'string' ? text.trim() : '';
        return content;
    }

    function unionRects(rects) {
        const items = Array.isArray(rects) ? rects.filter(Boolean) : [];
        if (items.length === 0) {
            return null;
        }

        const left = Math.min.apply(null, items.map((rect) => rect.left));
        const top = Math.min.apply(null, items.map((rect) => rect.top));
        const right = Math.max.apply(null, items.map((rect) => rect.right));
        const bottom = Math.max.apply(null, items.map((rect) => rect.bottom));
        const width = Math.max(0, right - left);
        const height = Math.max(0, bottom - top);

        if (width <= 0 || height <= 0) {
            return null;
        }

        return {
            left: left,
            top: top,
            right: right,
            bottom: bottom,
            width: width,
            height: height
        };
    }

    function estimateSpeechDurationMs(text) {
        const message = typeof text === 'string' ? text.trim() : '';
        if (!message) {
            return 0;
        }

        return clamp(Math.round(message.length * 280), 2200, 24000);
    }

    function estimateGuideChatStreamDurationMs(text) {
        const units = Array.from(typeof text === 'string' ? text.trim() : '');
        if (units.length === 0) {
            return 0;
        }

        return clamp(Math.round(units.length * 40), 720, 9600);
    }

    async function resumeKnownAudioContexts() {
        const tasks = [];

        if (window.AM && typeof window.AM.unlock === 'function') {
            try {
                window.AM.unlock();
            } catch (_) {}
        }

        const playerContext = window.appState && window.appState.audioPlayerContext;
        if (playerContext && playerContext.state === 'suspended' && typeof playerContext.resume === 'function') {
            tasks.push(playerContext.resume().catch(() => {}));
        }

        if (window.lanlanAudioContext && window.lanlanAudioContext.state === 'suspended' && typeof window.lanlanAudioContext.resume === 'function') {
            tasks.push(window.lanlanAudioContext.resume().catch(() => {}));
        }

        if (tasks.length > 0) {
            await Promise.all(tasks);
        }
    }

    function normalizeVoiceLang(voice) {
        const lang = voice && typeof voice.lang === 'string' ? voice.lang.trim().toLowerCase() : '';
        return lang.replace('_', '-');
    }

    function scoreSpeechVoice(voice) {
        if (!voice) {
            return 0;
        }

        const name = typeof voice.name === 'string' ? voice.name.trim().toLowerCase() : '';
        const lang = normalizeVoiceLang(voice);
        let score = 0;

        if (lang === 'zh-cn') {
            score += 100;
        } else if (lang.indexOf('zh') === 0) {
            score += 80;
        } else if (lang === 'cmn-cn') {
            score += 90;
        }

        if (name.indexOf('chinese') >= 0 || name.indexOf('mandarin') >= 0 || name.indexOf('中文') >= 0) {
            score += 20;
        }

        if (voice.default) {
            score += 5;
        }

        return score;
    }

    Object.assign(namespace, {
        YUI_GUIDE_CHAT_BRIDGE_QUEUE_KEY,
        YUI_GUIDE_CHAT_BRIDGE_QUEUE_LIMIT,
        TutorialVisualControllers,
        TutorialResistanceControllers,
        ResistanceController,
        SidebarPauseController,
        PauseCoordinator,
        TutorialTerminationRouter,
        TutorialOperationRegistry,
        OperationRegistry,
        TutorialSceneOrchestrator,
        TutorialSettingsTourFlow,
        createYuiGuideChatBridgeCommandBus,
        createYuiGuideTargetGeometryRegistry,
        createYuiGuideChatWindowAdapter,
        createYuiGuideScopedTutorialResources,
        readYuiGuideChatBridgeQueue,
        enqueueYuiGuideChatBridgeMessage,
        postYuiGuideChatBridgeMessage,
        translateGuideText,
        normalizeGuideLocale,
        resolveGuidePreferredLanguage,
        isGuideI18nReady,
        waitForGuideI18nReady,
        resolveGuideLocale,
        guideSpeechLang,
        resolveGuideAudioLocale,
        AVATAR_FLOATING_GUIDE_USAGE_STORAGE_KEY,
        YUI_GUIDE_EXTERNAL_CHAT_CURSOR_SCREEN_POINT_KEY,
        readAvatarFloatingGuideUsageState,
        writeAvatarFloatingGuideUsageState,
        normalizeAvatarFloatingGuideUsageTimestamp,
        getAvatarFloatingGuideActiveRound,
        recordAvatarFloatingGuideRoundStart,
        recordAvatarFloatingGuideRoundEnd,
        markAvatarFloatingGuideUsage,
        hasAvatarFloatingGuideUsage,
        hasAvatarFloatingGuideVoiceUsedAfterRoundStart,
        hasAvatarFloatingGuideVoiceUsedAfterDay1EndBeforeRoundStart,
        DEFAULT_USER_CURSOR_REVEAL_DISTANCE,
        DEFAULT_USER_CURSOR_REVEAL_INTERVAL_MS,
        DEFAULT_USER_CURSOR_REVEAL_MOVES,
        DEFAULT_INTERRUPT_COUNT_CURSOR_REVEAL_MS,
        DEFAULT_STEP_DELAY_MS,
        DEFAULT_SCENE_SETTLE_MS,
        DEFAULT_CURSOR_DURATION_MS,
        DEFAULT_CURSOR_CLICK_VISIBLE_MS,
        DAY6_PLUGIN_AGENT_PANEL_CURSOR_MOVE_MS,
        DAY6_PLUGIN_AGENT_PANEL_CURSOR_START_DELAY_MS,
        DAY6_PLUGIN_AGENT_PANEL_CLICK_VISIBLE_MS,
        DAY6_PLUGIN_CAT_PAW_CURSOR_OFFSET_Y,
        DAY6_PLUGIN_SIDE_PANEL_CURSOR_MOVE_MS,
        DAY6_PLUGIN_SIDE_PANEL_CURSOR_START_DELAY_MS,
        DAY6_PLUGIN_SIDE_PANEL_CLICK_VISIBLE_MS,
        DAY6_PLUGIN_SIDE_PANEL_ACTION_TIMEOUT_MS,
        DAY6_PLUGIN_SIDE_PANEL_DASHBOARD_WAIT_MS,
        DAY6_PLUGIN_DASHBOARD_DONE_GRACE_MS,
        INTRO_GREETING_REPLY_TEXT,
        INTRO_GREETING_REPLY_TEXT_KEY,
        TAKEOVER_PLUGIN_DASHBOARD_TEXT,
        TAKEOVER_PLUGIN_DASHBOARD_TEXT_KEY,
        PLUGIN_DASHBOARD_POPUP_BLOCKED_TEXT,
        PLUGIN_DASHBOARD_POPUP_BLOCKED_TEXT_KEY,
        TAKEOVER_SETTINGS_DETAIL_TEXT_PART_1,
        TAKEOVER_SETTINGS_DETAIL_TEXT_PART_2,
        TAKEOVER_SETTINGS_DETAIL_TEXT,
        TAKEOVER_SETTINGS_DETAIL_TEXT_KEY,
        TAKEOVER_SETTINGS_DETAIL_TEXT_PART_1_KEY,
        TAKEOVER_SETTINGS_DETAIL_TEXT_PART_2_KEY,
        INTRO_ACTIVATION_HINT_KEY,
        INTRO_ACTIVATION_HINT,
        INTRO_ACTIVATION_AUTO_ADVANCE_MS,
        INTRO_ACTIVATION_REDUCED_MOTION_AUTO_ADVANCE_MS,
        DEFAULT_SPOTLIGHT_PADDING,
        PLUGIN_MANAGEMENT_ENTRY_SPOTLIGHT_EXTRA_X,
        PLUGIN_MANAGEMENT_ENTRY_SPOTLIGHT_EXTRA_Y,
        NARRATION_RESUME_BACKTRACK_MS,
        NARRATION_RESUME_MIN_REMAINING_MS,
        PLUGIN_DASHBOARD_WINDOW_NAME,
        PLUGIN_DASHBOARD_HANDOFF_EVENT,
        PLUGIN_DASHBOARD_READY_EVENT,
        PLUGIN_DASHBOARD_DONE_EVENT,
        PLUGIN_DASHBOARD_TERMINATE_EVENT,
        PLUGIN_DASHBOARD_NARRATION_FINISHED_EVENT,
        PLUGIN_DASHBOARD_INTERRUPT_REQUEST_EVENT,
        PLUGIN_DASHBOARD_INTERRUPT_ACK_EVENT,
        PLUGIN_DASHBOARD_SYSTEM_CURSOR_TEMPORARY_REVEAL_EVENT,
        DESKTOP_PLUGIN_DASHBOARD_INTERRUPT_ACK_EVENT,
        DESKTOP_PLUGIN_DASHBOARD_NARRATION_FINISHED_EVENT,
        DESKTOP_PLUGIN_DASHBOARD_SYSTEM_CURSOR_TEMPORARY_REVEAL_EVENT,
        DESKTOP_PLUGIN_DASHBOARD_SKIP_REQUEST_EVENT,
        PLUGIN_DASHBOARD_SKIP_REQUEST_EVENT,
        DEFAULT_TUTORIAL_MODEL_MANAGER_LANLAN_NAME,
        GUIDE_AUDIO_BASE_URL,
        RETURN_PETAL_SEQUENCE_URL,
        getYuiGuideDailyGuide,
        collectGuideAudioFilesByKey,
        DAY1_HOME_GUIDE,
        GUIDE_AUDIO_FILES_BY_KEY,
        GUIDE_AUDIO_FILE_OVERRIDES_BY_KEY,
        GUIDE_AUDIO_VERSION_BY_KEY,
        guideAudioSrc,
        shouldGuideAudioDriveMouth,
        TAKEOVER_CAPTURE_SELECTORS,
        AVATAR_FLOATING_GUIDE_INTERRUPT_STEP,
        wait,
        resumeKnownAudioContexts,
        fetchWithTimeout,
        resolveWithTimeout,
        clamp,
        DAY4_LOCK_SPOTLIGHT_SAFE_BOTTOM_PX,
        HOME_TUTORIAL_PLATFORM_PROFILES,
        detectHomeTutorialPlatform,
        createHomeTutorialPlatformCapabilities,
        HOME_TUTORIAL_PLATFORM_CAPABILITIES_API,
        HOME_TUTORIAL_EXPERIENCE_METRICS_STORAGE_KEY,
        HOME_TUTORIAL_EXPERIENCE_METRICS_LIMIT,
        readHomeTutorialExperienceMetrics,
        writeHomeTutorialExperienceMetrics,
        createHomeTutorialExperienceMetrics,
        GUIDE_NARRATION_TIMELINES_BY_KEY,
        GUIDE_AUDIO_DURATIONS_BY_KEY,
        getGuideAudioCueConfig,
        getGuideAudioDurationConfig,
        formatGuideDebugText,
        unionRects,
        estimateSpeechDurationMs,
        estimateGuideChatStreamDurationMs,
        normalizeVoiceLang,
        scoreSpeechVoice
    });

    namespace.extendDirector = function extendDirector(methods) {
        Object.keys(methods).forEach(function (name) {
            Object.defineProperty(namespace.YuiGuideDirector.prototype, name, {
                value: methods[name],
                configurable: true,
                writable: true
            });
        });
    };
})();
