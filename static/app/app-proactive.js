/**
 * app-proactive.js — 主动搭话（Proactive Chat）模块
 *
 * 包含：
 *   - syncProactiveFlags (no-op, 由 app-state.js defineProperty 桥接代替)
 *   - hasAnyChatModeEnabled / canTriggerProactively
 *   - scheduleProactiveChat / stopProactiveChatSchedule
 *   - triggerProactiveChat / _showProactiveChatSourceLinks
 *   - resetProactiveChatBackoff
 *   - getAvailablePersonalPlatforms
 *   - sendOneProactiveVisionFrame
 *   - startProactiveVisionDuringSpeech / stopProactiveVisionDuringSpeech
 *   - captureProactiveChatScreenshot / acquireProactiveVisionStream / releaseProactiveVisionStream
 *   - isWindowsOS (helper)
 *   - captureCanvasFrame / captureFrameFromStream / acquireOrReuseCachedStream (screen-capture helpers)
 *   - fetchBackendScreenshot / scheduleScreenCaptureIdleCheck (screen-capture helpers)
 */
(function () {
    'use strict';

    const mod = {};
    const S = window.appState;
    const C = window.appConst;
    const NEW_USER_ICEBREAKER_STORAGE_KEY = 'neko.new_user_icebreaker.v1';
    const NEW_USER_ICEBREAKER_BLOCKING_WINDOW_MS = 2 * 60 * 60 * 1000;
    const MEME_LOAD_FAILED_STICKER_URL = '/static/icons/meme-image-load-failed-sticker.png';
    const MUSIC_CANDIDATE_FALLBACK_BUDGET_MS = 10000;
    const MUSIC_CANDIDATE_ATTEMPT_TIMEOUT_MS = 3000;

    function isMusicOccupiedNow() {
        if (typeof window.isMusicOccupied === 'function') return window.isMusicOccupied();
        return ((typeof window.isMusicPlaying === 'function') && window.isMusicPlaying())
            || ((typeof window.isMusicPending === 'function') && window.isMusicPending())
            || ((typeof window.isRemoteMusicActive === 'function') && window.isRemoteMusicActive());
    }

    function getDesktopProvider() {
        return typeof window.getDesktopCaptureProvider === 'function'
            ? window.getDesktopCaptureProvider()
            : null;
    }

    // ======================== proactive leader election ========================
    //
    // 背景：index.html（Pet 主窗口）和 chat.html（聊天浮窗）共用 app-proactive.js，
    // 各自跑 setTimeout 调度，会同时发 /api/proactive_chat 请求 / 推屏幕帧。
    // 后端把它们当两次独立请求处理，结果双倍 LLM 调用、双倍音乐推荐、双倍 vision 帧。
    //
    // 约定：Pet (index.html) 为主，chat.html 为从。同时存活时只有 Pet 跑调度；
    // Pet 关闭后 chat.html 通过 TTL 自动接班。
    //
    // 协议：广播 'neko_proactive_leader'。每 5s 心跳，15s TTL。
    // rank 越小越优先：Pet=0, chat.html=1, 其它页面=99（不参与）。
    //
    const PROACTIVE_LEADER_CHANNEL = 'neko_proactive_leader';
    const PROACTIVE_LEADER_HEARTBEAT_MS = 5000;
    const PROACTIVE_LEADER_TTL_MS = 15000;
    const PROACTIVE_LEADER_RECHECK_MS = 8000; // 非 leader 的自检周期
    const PROACTIVE_CHAT_INPUT_SLOW_THRESHOLD_SECONDS = 30;
    const PROACTIVE_CHAT_INPUT_MIN_DELAY_MS = 30000;

    const PROACTIVE_SELF_ID = (Date.now().toString(36) + Math.random().toString(36).slice(2, 10));

    function _isElectronFullChatHost(path) {
        const normalizedPath = path || '';
        if (normalizedPath !== '/chat_full' && normalizedPath !== '/chat_full/') return false;
        const body = document.body;
        return !!(
            body &&
            body.classList &&
            body.classList.contains('neko-electron-runtime') &&
            body.getAttribute('data-chat-host-kind') === 'full'
        );
    }

    function _computeSelfRank() {
        try {
            const path = (window.location && window.location.pathname) || '';
            // Electron full chat 使用独立 session，只显示/控制同步过来的聊天与播放器。
            // Web /chat_full 没有 neko-electron-runtime，仍需参与 proactive leader 选举。
            if (_isElectronFullChatHost(path)) return 99;
            // chat.html 浮窗 → 从节点
            if (path === '/chat') return 1;
            // 不参与 proactive 的页面（model_manager / jukebox / subtitle / agenthud / toast / cookies_login 等）
            // 它们本来就不加载 app-proactive.js，但保险起见显式归类为不参与
            if (
                path === '/model_manager' || path === '/l2d' ||
                path === '/live2d_parameter_editor' || path === '/jukebox' ||
                path === '/jukebox/manager' || path === '/subtitle' ||
                path === '/agenthud' || path === '/toast'
            ) return 99;
            // 其它（/、/{lanlan_name}）一律视为 Pet 主窗口
            return 0;
        } catch (_) {
            return 0;
        }
    }
    const PROACTIVE_SELF_RANK = _computeSelfRank();

    // peer_id -> { rank, expireAt }
    const _proactivePeers = new Map();
    let _proactiveLeaderChannel = null;
    let _proactiveLeaderHeartbeatTimer = null;
    let _wasLeaderLastTick = null; // 用于 leader 状态切换时主动 reschedule
    let _chatInputSlowdownUntil = 0;
    let _homeTutorialFeatureSuppressedByEvent = false;

    function isProactiveVisionEnabledNow() {
        // 跨窗口时 leader 可能还没收到 storage 事件；以 localStorage 的最新保存值兜底。
        try {
            const raw = localStorage.getItem('project_neko_settings');
            if (raw) {
                const settings = JSON.parse(raw);
                if (settings && typeof settings.proactiveVisionEnabled === 'boolean') {
                    return settings.proactiveVisionEnabled;
                }
            }
        } catch (_) { }

        if (typeof window.proactiveVisionEnabled !== 'undefined') {
            return !!window.proactiveVisionEnabled;
        }
        return !!S.proactiveVisionEnabled;
    }

    function isHomeTutorialFeatureSuppressed() {
        try {
            if (_homeTutorialFeatureSuppressedByEvent) {
                return true;
            }
            const controller = window.NekoHomeTutorialFeatureController;
            if (controller && typeof controller.isActive === 'function' && controller.isActive()) {
                return true;
            }
            return typeof window.isNekoHomeTutorialInteractionLocked === 'function'
                && window.isNekoHomeTutorialInteractionLocked() === true;
        } catch (_) {
            return false;
        }
    }

    function readNewUserIcebreakerStore() {
        try {
            if (typeof localStorage === 'undefined') return null;
            const raw = localStorage.getItem(NEW_USER_ICEBREAKER_STORAGE_KEY);
            if (!raw) return null;
            const parsed = JSON.parse(raw);
            return parsed && typeof parsed === 'object' ? parsed : null;
        } catch (_) {
            return null;
        }
    }

    function isRecentNewUserIcebreakerEntry(entry) {
        if (!entry || typeof entry !== 'object') return false;
        const timestamps = [
            Number(entry.triggeredAt || 0),
            Number(entry.updatedAt || 0),
            Number(entry.completedAt || 0),
            Number(entry.endedAt || 0)
        ].filter((value) => Number.isFinite(value) && value > 0);
        if (!timestamps.length) return false;
        const latest = Math.max.apply(Math, timestamps);
        return Date.now() - latest <= NEW_USER_ICEBREAKER_BLOCKING_WINDOW_MS;
    }

    function isNewUserIcebreakerEntryBlocking(entry) {
        return !!(entry && entry.completed !== true && isRecentNewUserIcebreakerEntry(entry));
    }

    function getNewUserIcebreakerBlockingRetryMs() {
        try {
            if (window.newUserIcebreaker && typeof window.newUserIcebreaker.getActiveSession === 'function') {
                if (window.newUserIcebreaker.getActiveSession()) {
                    return getNewUserIcebreakerRetryDelayMs();
                }
            }
        } catch (_) {}

        const store = readNewUserIcebreakerStore();
        const days = store && typeof store.days === 'object' ? store.days : null;
        if (!days) return 0;
        const finalDay = days['7'];
        if (finalDay && finalDay.completed === true) return 0;

        let latest = 0;
        for (let day = 1; day <= 7; day += 1) {
            const entry = days[String(day)];
            if (!entry || typeof entry !== 'object') continue;
            if (entry.completed === true) continue;
            [
                Number(entry.triggeredAt || 0),
                Number(entry.updatedAt || 0),
                Number(entry.completedAt || 0),
                Number(entry.endedAt || 0)
            ].forEach((value) => {
                if (Number.isFinite(value) && value > latest) {
                    latest = value;
                }
            });
        }
        if (!latest) return 0;
        const remaining = NEW_USER_ICEBREAKER_BLOCKING_WINDOW_MS - (Date.now() - latest);
        return remaining > 0 ? remaining : 0;
    }

    /**
     * Returns whether a new-user icebreaker is currently owning the greeting slot.
     *
     * A live icebreaker session wins immediately; persisted day entries only
     * suppress nearby reconnect/proactive work so older day history does not
     * mute normal sessions for the rest of the seven-day onboarding.
     *
     * @returns {boolean} True when proactive chat should be suppressed for onboarding.
     */
    function isNewUserIcebreakerPeriodActive() {
        try {
            if (window.newUserIcebreaker && typeof window.newUserIcebreaker.getActiveSession === 'function') {
                if (window.newUserIcebreaker.getActiveSession()) return true;
            }
        } catch (_) {}

        const store = readNewUserIcebreakerStore();
        const days = store && typeof store.days === 'object' ? store.days : null;
        if (!days) return false;
        const finalDay = days['7'];
        if (finalDay && finalDay.completed === true) return false;
        for (let day = 1; day <= 7; day += 1) {
            const entry = days[String(day)];
            if (isNewUserIcebreakerEntryBlocking(entry)) {
                return true;
            }
        }
        return false;
    }
    mod.isNewUserIcebreakerPeriodActive = isNewUserIcebreakerPeriodActive;

    function getNewUserIcebreakerRetryDelayMs() {
        let remainingMs = 0;
        const store = readNewUserIcebreakerStore();
        const days = store && typeof store.days === 'object' ? store.days : null;
        if (days) {
            for (let day = 1; day <= 7; day += 1) {
                const entry = days[String(day)];
                if (!entry || typeof entry !== 'object') continue;
                if (entry.completed === true) continue;
                const latest = Math.max(
                    Number(entry.triggeredAt || 0),
                    Number(entry.updatedAt || 0),
                    Number(entry.completedAt || 0),
                    Number(entry.endedAt || 0)
                );
                if (Number.isFinite(latest) && latest > 0) {
                    remainingMs = Math.max(remainingMs, NEW_USER_ICEBREAKER_BLOCKING_WINDOW_MS - (Date.now() - latest));
                }
            }
        }
        return Math.max(5000, Math.min(remainingMs || 5000, 30000));
    }

    try {
        if (typeof BroadcastChannel !== 'undefined' && PROACTIVE_SELF_RANK !== 99) {
            _proactiveLeaderChannel = new BroadcastChannel(PROACTIVE_LEADER_CHANNEL);
            _proactiveLeaderChannel.onmessage = function (event) {
                const data = event && event.data;
                if (!data || typeof data !== 'object') return;
                if (!data.id || data.id === PROACTIVE_SELF_ID) return;
                if (data.type === 'announce' || data.type === 'heartbeat') {
                    const isNewPeer = !_proactivePeers.has(data.id);
                    _proactivePeers.set(data.id, {
                        rank: typeof data.rank === 'number' ? data.rank : 99,
                        expireAt: Date.now() + PROACTIVE_LEADER_TTL_MS
                    });
                    // 新 peer 上线：立即回一个 heartbeat，让它在第一次决策前就能感知到我，
                    // 避免新窗口在 announce 后的"无人响应"窗口里误以为只有自己。
                    if (data.type === 'announce') {
                        _proactiveBroadcast('heartbeat');
                    }
                    // 拓扑变化（新 peer 或 announce）时重新评估自己的角色
                    if (isNewPeer || data.type === 'announce') {
                        _onProactiveLeadershipMaybeChanged();
                    }
                } else if (data.type === 'goodbye') {
                    _proactivePeers.delete(data.id);
                    _onProactiveLeadershipMaybeChanged();
                } else if (data.type === 'user_input_reset') {
                    // 分发环境（Electron）下 chat.html 承担文本输入，但 proactive 计时器
                    // 只在 index.html (leader) 运行。chat.html 本地调 resetProactiveChatBackoff
                    // 对 leader 的 S.proactiveChatBackoffLevel 不可见，因此转成 IPC 转发到
                    // 所有窗口，由 leader 真正重置退避级别 + 重排 timer。
                    // { _fromIpc: true } 阻止二次广播，靠 data.id !== SELF_ID 已避免回环。
                    try {
                        resetProactiveChatBackoff({ _fromIpc: true });
                    } catch (e) {
                        console.warn('[Proactive] 处理 user_input_reset IPC 失败:', e);
                    }
                } else if (data.type === 'chat_input_focus_slowdown'
                        && S.proactiveChatEnabled
                        && hasAnyChatModeEnabled()
                        && Number(S.proactiveChatInterval) <= PROACTIVE_CHAT_INPUT_SLOW_THRESHOLD_SECONDS) {
                    _markChatInputSlowdownWindow(S.proactiveChatInterval);
                    scheduleProactiveChat();
                }
            };
        }
    } catch (e) {
        console.log('[Proactive] BroadcastChannel 不可用，主备协调失效:', e);
    }

    function _proactiveBroadcast(type) {
        if (!_proactiveLeaderChannel) return;
        try {
            _proactiveLeaderChannel.postMessage({
                type: type || 'heartbeat',
                id: PROACTIVE_SELF_ID,
                rank: PROACTIVE_SELF_RANK,
                ts: Date.now()
            });
        } catch (_) { /* ignore */ }
    }

    /**
     * Was this 403 produced by ``_validate_local_mutation_request``
     * (i.e. CSRF/Origin guard), or by something else (future business
     * rule, reverse proxy, WAF, …)? The unified-guard contract is
     * ``error_code === "csrf_validation_failed"`` (see
     * ``static/app-prompt-shared.js`` 520-541 and
     * ``static/tutorial/core/universal-manager.js`` 120-134).
     *
     * Only the CSRF case warrants the ``refreshToken()`` + retry-once
     * recovery path; treating *every* 403 as benign-and-skip means a
     * token-expired heartbeat looks identical to a "real" 403 and the
     * caller never recovers — proactive chat would silently stall until
     * a full page reload (CodeRabbit Major on PR #1530).
     */
    async function _proactiveIsCsrfValidationFailure(resp) {
        if (!resp || resp.status !== 403) return false;
        try {
            var cloned = typeof resp.clone === 'function' ? resp.clone() : resp;
            var body = await cloned.json();
            return Boolean(body && body.error_code === 'csrf_validation_failed');
        } catch (_) {
            return false;
        }
    }

    function _applyProactiveLanguagePayload(payload, lanlanName) {
        // Durable preference and request-only rendering fallback are deliberately
        // separate. In particular, never promote the latter to i18n_language.
        var explicitConversationLanguage = '';
        var renderConversationLanguage = '';
        try {
            if (typeof window.getExplicitConversationLanguagePreference === 'function') {
                explicitConversationLanguage = window.getExplicitConversationLanguagePreference(lanlanName);
            }
        } catch (_) { explicitConversationLanguage = ''; }
        try {
            if (typeof window.getConversationLanguagePreference === 'function') {
                renderConversationLanguage = window.getConversationLanguagePreference(lanlanName);
            }
        } catch (_) { renderConversationLanguage = ''; }
        if (explicitConversationLanguage) {
            payload.i18n_language = explicitConversationLanguage;
        }
        if (renderConversationLanguage) {
            payload.render_language = renderConversationLanguage;
        }
        return payload;
    }

    function _isChatInputElement(element) {
        if (!element || element.nodeType !== 1 || typeof element.matches !== 'function') {
            return false;
        }
        if (!element.matches(
            '#textInputBox, ' +
            '#react-chat-window-shell textarea.composer-input, ' +
            '#react-chat-window-root textarea.composer-input'
        )) {
            return false;
        }
        return element.disabled !== true && element.readOnly !== true;
    }

    function _getFocusedChatInputElement() {
        try {
            const active = document.activeElement;
            if (!active || !_isChatInputElement(active)) return null;
            if (document.hasFocus && !document.hasFocus()) return null;
            return active;
        } catch (_) {
            return null;
        }
    }

    function _getChatInputSlowdownDelay(baseIntervalSeconds) {
        const interval = Number(baseIntervalSeconds);
        if (!isFinite(interval) || interval > PROACTIVE_CHAT_INPUT_SLOW_THRESHOLD_SECONDS) {
            return 0;
        }
        const minDelay = Math.max(PROACTIVE_CHAT_INPUT_MIN_DELAY_MS, interval * 1000);
        return _getFocusedChatInputElement()
            ? minDelay
            : Math.max(0, _chatInputSlowdownUntil - Date.now());
    }

    function _markChatInputSlowdownWindow(baseIntervalSeconds) {
        const interval = Number(baseIntervalSeconds);
        if (!isFinite(interval) || interval > PROACTIVE_CHAT_INPUT_SLOW_THRESHOLD_SECONDS) {
            return;
        }
        _chatInputSlowdownUntil = Date.now() + Math.max(PROACTIVE_CHAT_INPUT_MIN_DELAY_MS, interval * 1000);
    }

    document.addEventListener('focusin', function (event) {
        if (!_isChatInputElement(event.target)) return;
        if (!S.proactiveChatEnabled || !hasAnyChatModeEnabled()) return;
        if (Number(S.proactiveChatInterval) > PROACTIVE_CHAT_INPUT_SLOW_THRESHOLD_SECONDS) return;
        _markChatInputSlowdownWindow(S.proactiveChatInterval);
        scheduleProactiveChat();
        _proactiveBroadcast('chat_input_focus_slowdown');
    }, true);

    function _purgeStaleProactivePeers() {
        const now = Date.now();
        let removed = false;
        for (const [id, info] of _proactivePeers) {
            if (now > info.expireAt) {
                _proactivePeers.delete(id);
                removed = true;
            }
        }
        return removed;
    }

    function isProactiveLeader() {
        if (PROACTIVE_SELF_RANK === 99) return false; // 不参与的页面永远不是
        _purgeStaleProactivePeers();
        // 找出存活节点中最优 rank（含自己），同 rank 时 ID 字典序小者胜
        let bestRank = PROACTIVE_SELF_RANK;
        let bestId = PROACTIVE_SELF_ID;
        for (const [id, info] of _proactivePeers) {
            if (info.rank < bestRank || (info.rank === bestRank && id < bestId)) {
                bestRank = info.rank;
                bestId = id;
            }
        }
        return bestId === PROACTIVE_SELF_ID;
    }
    mod.isProactiveLeader = isProactiveLeader;

    function _onProactiveLeadershipMaybeChanged() {
        const nowLeader = isProactiveLeader();
        if (_wasLeaderLastTick === nowLeader) return;
        _wasLeaderLastTick = nowLeader;
        console.log('[Proactive] 主备状态切换：自己现在' + (nowLeader ? '是 leader（开始调度 proactive_chat / vision）' : '是 follower（停止调度，等待 leader 失联）'));
        if (nowLeader) {
            // 接班：立刻安排一次 proactive_chat
            try { scheduleProactiveChat(); } catch (e) {
                console.warn('[Proactive] 接班时调度 proactive_chat 失败:', e);
            }
            // 接班：如果当前正在录音，启动 vision-during-speech
            try {
                if (S.isRecording) startProactiveVisionDuringSpeech();
            } catch (e) {
                console.warn('[Proactive] 接班时启动 vision-during-speech 失败:', e);
            }
        } else {
            // 让位：清掉本地 proactive 定时器和 vision 心跳
            if (S.proactiveChatTimer) {
                clearTimeout(S.proactiveChatTimer);
                S.proactiveChatTimer = null;
            }
            try { stopProactiveVisionDuringSpeech(); } catch (e) {
                console.warn('[Proactive] 让位时停止 vision-during-speech 失败:', e);
            }
        }
    }

    // 启动：先 announce 一下，再周期性 heartbeat
    if (_proactiveLeaderChannel) {
        _proactiveBroadcast('announce');
        _proactiveLeaderHeartbeatTimer = setInterval(function () {
            _proactiveBroadcast('heartbeat');
            // 心跳节奏顺手扫一下过期 peer，防止 leader 被关掉后 follower 不知情
            if (_purgeStaleProactivePeers()) {
                _onProactiveLeadershipMaybeChanged();
            }
        }, PROACTIVE_LEADER_HEARTBEAT_MS);
        // 窗口关闭前广播 goodbye，让对端立即接班
        window.addEventListener('beforeunload', function () {
            _proactiveBroadcast('goodbye');
            if (_proactiveLeaderHeartbeatTimer) {
                clearInterval(_proactiveLeaderHeartbeatTimer);
                _proactiveLeaderHeartbeatTimer = null;
            }
        });
    }

    // ======================== screen-capture helpers (delegate to app-screen.js) ========================

    function captureCanvasFrame(video, jpegQuality, detectBlack) {
        return window.appScreen.captureCanvasFrame(video, jpegQuality, detectBlack);
    }

    function captureFrameFromStream(stream, jpegQuality) {
        return window.appScreen.captureFrameFromStream(stream, jpegQuality);
    }

    function acquireOrReuseCachedStream(opts) {
        return window.appScreen.acquireOrReuseCachedStream(opts);
    }

    function fetchBackendScreenshot() {
        return window.appScreen.fetchBackendScreenshot();
    }

    function scheduleScreenCaptureIdleCheck() {
        return window.appScreen.scheduleScreenCaptureIdleCheck();
    }

    function normalizeNativeCaptureDataUrlForStream(dataUrl) {
        return window.appScreen.normalizeNativeCaptureDataUrlForStream(dataUrl);
    }

    function buildStreamDataMessage(dataUrl, inputType, sourceId, explicitCaptureType) {
        return window.appScreen.buildStreamDataMessage(dataUrl, inputType, sourceId, explicitCaptureType);
    }

    // ======================== syncProactiveFlags (no-op) ========================
    // app-state.js 使用 Object.defineProperty 进行双向绑定，
    // 因此不再需要手动同步 window.xxx <-> 本地变量。
    function syncProactiveFlags() {
        // no-op: bridged by app-state.js defineProperty
    }

    // ======================== proactive chat core ========================

    /**
     * 检查是否处于「请她离开」状态
     */
    function isGoodbyeActive() {
        return (window.live2dManager && window.live2dManager._goodbyeClicked) ||
            (window.vrmManager && window.vrmManager._goodbyeClicked) ||
            (window.mmdManager && window.mmdManager._goodbyeClicked);
    }

    /**
     * 检查是否有任何搭话方式被选中
     */
    function hasAnyChatModeEnabled() {
        return S.proactiveVisionChatEnabled || S.proactiveNewsChatEnabled ||
            S.proactiveVideoChatEnabled || S.proactivePersonalChatEnabled ||
            S.proactiveMusicEnabled || S.proactiveMemeEnabled ||
            S.proactiveMiniGameInviteEnabled;
    }
    mod.hasAnyChatModeEnabled = hasAnyChatModeEnabled;

    /**
     * 检查主动搭话前置条件是否满足
     */
    // AI 是否正在播放语音：proactive timer 到点时如果还在播，就跳过本次 nudge
    // 并继续按固定间隔 poll（见下面 scheduleProactiveChat 的两处 speaking 分支）。
    //
    // 分支 1（isPlaying / speechActiveTurnId）：覆盖"首个 PCM chunk 入队 → drain 完"。
    // 分支 2（turnId !== completedId + 时间窗）：覆盖"text 已开始流、首个音频 chunk
    //   还没解码"那几百毫秒空窗。PR #839 原本只靠 `turnId !== completedId` 自释放，
    //   但 drain 完时 clearAssistantTurnCompletion 会把 completedId 清回 null 而
    //   turnId 仍留着，这条件会一直 TRUE、proactive 永不触发。
    //   加 `elapsed < PROACTIVE_TURN_STARTUP_GRACE_MS` 作为硬上限：那段空窗通常只有
    //   几百毫秒，5s 已经是数量级的余裕；超出就让分支 1 的自释放信号接管。
    var PROACTIVE_TURN_STARTUP_GRACE_MS = 5000;

    // C: 用户最近发声的窗口（ms）。和后端 _user_recent_activity_window (8s) 对齐，
    // 网络来回有延迟时前端守门先挡住，请求根本不发出去，省一个 round-trip。
    // `S.userRecentSpeechTime` 由 app-audio-capture.js 里的 monitorInputVolume 持续
    // 写入（RMS > 0.01 每帧打点），这里用一个稍宽于后端的窗口，保证"前端没挡住但
    // 后端挡住"的 race 不至于频繁发生（文本触发空跑成本低，但可以省则省）。
    var USER_RECENT_SPEECH_WINDOW_MS = 8000;

    function _isAssistantSpeaking() {
        try {
            if (!S) return false;
            if (S.isPlaying || S.assistantSpeechActiveTurnId) return true;
            if (S.assistantTurnId && S.assistantTurnId !== S.assistantTurnCompletedId) {
                var startedAt = S.assistantTurnStartedAt || 0;
                if (startedAt && Date.now() - startedAt < PROACTIVE_TURN_STARTUP_GRACE_MS) {
                    return true;
                }
            }
            return false;
        } catch (_) {
            return false;
        }
    }
    mod._isAssistantSpeaking = _isAssistantSpeaking;

    // 给 proactive skip 日志带上 _isAssistantSpeaking 用到的全部输入 + 音频队列长度。
    // gate 卡死时直接看 log 就能判断哪个 flag 粘住、队列是不是真的空，
    // 不用让用户手动到 DevTools 抓快照（手动解锁前一刷新就把证据擦了）。
    function _dumpSpeakingGateState() {
        try {
            if (!S) return { snapshot: 'no_state' };
            return {
                isPlaying: S.isPlaying,
                assistantSpeechActiveTurnId: S.assistantSpeechActiveTurnId,
                assistantTurnId: S.assistantTurnId,
                assistantTurnCompletedId: S.assistantTurnCompletedId,
                assistantTurnStartedAt: S.assistantTurnStartedAt || null,
                elapsedSinceStartMs: S.assistantTurnStartedAt ? Date.now() - S.assistantTurnStartedAt : null,
                scheduledSources: (S.scheduledSources && S.scheduledSources.length) || 0,
                audioBufferQueue: (S.audioBufferQueue && S.audioBufferQueue.length) || 0,
                incomingAudioBlobQueue: (S.incomingAudioBlobQueue && S.incomingAudioBlobQueue.length) || 0
            };
        } catch (e) {
            return { snapshot: 'error', error: String(e) };
        }
    }
    mod._dumpSpeakingGateState = _dumpSpeakingGateState;

    // C: 判断用户是否最近在发声。仅在 voice 模式下使用（文本模式没有麦克风打点），
    // 用来在前端层面拦住 proactive tick，与后端 prompt_ephemeral 的
    // _user_recent_activity_time 防线形成对称冗余。
    function _isUserRecentlySpeaking() {
        try {
            if (!S || !S.isRecording) return false;
            var last = S.userRecentSpeechTime || 0;
            if (!last) return false;
            return (Date.now() - last) < USER_RECENT_SPEECH_WINDOW_MS;
        } catch (_) {
            return false;
        }
    }
    mod._isUserRecentlySpeaking = _isUserRecentlySpeaking;

    function canTriggerProactively() {
        if (isHomeTutorialFeatureSuppressed()) {
            return false;
        }
        if (isNewUserIcebreakerPeriodActive()) {
            return false;
        }

        // 「请她离开」状态下禁止一切主动搭话
        if (isGoodbyeActive()) {
            return false;
        }

        // 游戏路由 active 期间，普通 proactive 会干扰游戏语音/文本接管。
        // 赛后 game_postgame 不走 /api/proactive_chat，不受这里影响。
        if (S.gameRouteActive) {
            return false;
        }

        // 必须开启主动搭话
        if (!S.proactiveChatEnabled) {
            return false;
        }

        // 必须选择至少一种搭话方式
        if (!S.proactiveVisionChatEnabled && !S.proactiveNewsChatEnabled &&
            !S.proactiveVideoChatEnabled && !S.proactivePersonalChatEnabled &&
            !S.proactiveMusicEnabled && !S.proactiveMemeEnabled &&
            !S.proactiveMiniGameInviteEnabled) {
            return false;
        }

        // 如果只选择了视觉搭话，需要同时开启自主视觉
        if (S.proactiveVisionChatEnabled && !S.proactiveNewsChatEnabled &&
            !S.proactiveVideoChatEnabled && !S.proactivePersonalChatEnabled &&
            !S.proactiveMusicEnabled && !S.proactiveMemeEnabled &&
            !S.proactiveMiniGameInviteEnabled) {
            return isProactiveVisionEnabledNow();
        }

        // 如果只选择了个人动态搭话，需要同时开启个人动态
        if (!S.proactiveVisionChatEnabled && !S.proactiveNewsChatEnabled &&
            !S.proactiveVideoChatEnabled && S.proactivePersonalChatEnabled &&
            !S.proactiveMusicEnabled && !S.proactiveMemeEnabled &&
            !S.proactiveMiniGameInviteEnabled) {
            return S.proactivePersonalChatEnabled;
        }

        // 音乐搭话和meme搭话不需要额外条件，总是允许
        return true;
    }
    mod.canTriggerProactively = canTriggerProactively;

    /**
     * 主动搭话定时触发功能
     */
    function scheduleProactiveChat() {
        // 清除现有定时器
        if (S.proactiveChatTimer) {
            clearTimeout(S.proactiveChatTimer);
            S.proactiveChatTimer = null;
        }

        // 明确不参与的页面不挂 recheck；/chat_full 独立 session 下会一直自封失败。
        if (PROACTIVE_SELF_RANK === 99) {
            console.log('[Proactive] 当前页面不参与 proactive 调度，跳过');
            return;
        }

        // 主备协调：非 leader 不调度，只挂一个轻量的 recheck，
        // 一旦 leader 失联（peer 过期）就自动接班。
        if (!isProactiveLeader()) {
            console.log('[Proactive] 当前不是 leader，跳过调度，等待接班 (rank=' + PROACTIVE_SELF_RANK + ')');
            S.proactiveChatTimer = setTimeout(scheduleProactiveChat, PROACTIVE_LEADER_RECHECK_MS);
            return;
        }
        _wasLeaderLastTick = true;

        // 必须开启主动搭话且选择至少一种搭话方式才启动调度
        if (!S.proactiveChatEnabled || !hasAnyChatModeEnabled()) {
            S.proactiveChatBackoffLevel = 0;
            return;
        }

        // 前置条件检查：如果不满足触发条件，不启动调度器并重置退避
        if (isNewUserIcebreakerPeriodActive()) {
            console.log('[Proactive] new-user icebreaker active, retry schedule later');
            S.proactiveChatBackoffLevel = 0;
            S.proactiveChatTimer = setTimeout(
                scheduleProactiveChat,
                getNewUserIcebreakerBlockingRetryMs() || getNewUserIcebreakerRetryDelayMs()
            );
            return;
        }
        if (!canTriggerProactively()) {
            console.log('主动搭话前置条件不满足，不启动调度器');
            S.proactiveChatBackoffLevel = 0;
            var icebreakerRetryMs = getNewUserIcebreakerBlockingRetryMs();
            if (icebreakerRetryMs > 0) {
                S.proactiveChatTimer = setTimeout(scheduleProactiveChat, icebreakerRetryMs + 250);
            }
            return;
        }

        // 如果主动搭话正在执行中，不安排新的定时器（等当前执行完成后自动安排）
        if (S.isProactiveChatRunning) {
            console.log('主动搭话正在执行中，延迟安排下一次');
            return;
        }

        // 语音模式：固定间隔（不退避），连续5轮无回复则停止
        if (S.isRecording) {
            if (S._voiceProactiveNoResponseCount >= 10) {
                console.log('[ProactiveChat] 语音模式连续5轮无回复，停止主动搭话');
                return;
            }
            var delay = S.proactiveChatInterval * 1000;
            console.log('[ProactiveChat] 语音模式：' + (delay / 1000) + '秒后触发（无退避，无回复计数：' + (S._voiceProactiveNoResponseCount || 0) + '/10）');

            S.proactiveChatTimer = setTimeout(async function () {
                // setTimeout firing does not clear our stored handle.
                S.proactiveChatTimer = null;
                if (S.isProactiveChatRunning) return;
                // 设计说明（by 用户意图）：
                // 这里不"rearm-after-playback"——那样每句话说完都要严格等满一个固定间隔
                // 才能接下一句，节奏太死板。改为"继续按固定间隔轮询"：
                // 轮询到时 AI 还在说 → 跳过本次 nudge，不累加 _voiceProactiveNoResponseCount
                // （没真发请求就不算无回复），但仍然 scheduleProactiveChat() 推进下一 tick。
                // 结果：播放完成到下一次 nudge 的等待 ∈ [0, interval)，带随机感，更自然。
                if (_isAssistantSpeaking()) {
                    console.log('[ProactiveChat] 语音模式：AI 正在播放语音，本次 nudge 跳过（不计数），继续下一 tick', _dumpSpeakingGateState());
                    scheduleProactiveChat();
                    return;
                }
                // C: 前端麦克风 RMS 最近 8s 内超过语音阈值 → 用户正在说话或
                // 刚说完，不发主动文本触发。与后端 _user_recent_activity_time guard
                // 对称（8s 窗口），请求根本不出门，省一次 round-trip。
                // 同 AI-speaking 分支：不计入 no-response 计数，仍推进下一 tick。
                if (_isUserRecentlySpeaking()) {
                    console.log('[ProactiveChat] 语音模式：用户最近在发声，本次 nudge 跳过（不计数），继续下一 tick');
                    scheduleProactiveChat();
                    return;
                }
                S.isProactiveChatRunning = true;
                var voiceTriggered = false;
                var voiceResetVersion = S._voiceProactiveBackoffResetVersion || 0;
                try {
                    voiceTriggered = await triggerProactiveChat();
                } finally {
                    S.isProactiveChatRunning = false;
                }
                // server 并发拒绝（HTTP 409）时 triggerProactiveChat 返回 false 表示
                // "根本没真正发起一次 proactive"——不消耗 no-response quota，否则连续
                // 409 会按 >=10 阈值熔断语音 nudge 直到下次 user 触发 reset。等同
                // _isAssistantSpeaking / _isUserRecentlySpeaking 这两个 frontend
                // guard 走的"跳过不计数"分支。Codex review on PR #1401。
                if (voiceTriggered
                    && (S._voiceProactiveBackoffResetVersion || 0) === voiceResetVersion) {
                    S._voiceProactiveNoResponseCount = (S._voiceProactiveNoResponseCount || 0) + 1;
                } else if (voiceTriggered) {
                    console.log('[ProactiveChat] 用户在本轮等待期间已回复，保留无回复计数 reset');
                }
                // 文本注入会一直 await 到 response.done；对应 turn end 可能在
                // isProactiveChatRunning 仍为 true 时先到，导致调度调用被抑制。
                // 请求完成、锁释放后兜底补排。若其它完成事件已排好 timer，
                // 保留原 timer，避免把间隔起点向后推。
                if (!S.proactiveChatTimer) {
                    scheduleProactiveChat();
                }
            }, delay);
            return;
        }

        // ── 文本模式：三段式自适应退避 ──
        //
        // 常量:
        //   BACKOFF_TARGET  = 120s (收敛目标)
        //   BACKOFF_M1      = 1.09167 (tier 1 固定倍率, = 1 + (120 - 10) / 1200)
        //   BACKOFF_M2      = 1.55 (tier 2/3 倍率)
        //   BACKOFF_SLOW    = 4 (慢区级数)
        //   BACKOFF_P_SLOW  = 0.09 (慢区升级概率)
        //   BACKOFF_HARD_CAP = 3600s (硬顶 60min)
        //
        // 自适应参数 (由 base 决定):
        //   cap1 = ceil(log(TARGET / base) / log(M1))   — tier 1 总级数
        //   cap2 = cap1 + SLOW                          — 慢区终点
        //
        // Delay 函数:
        //   level < cap1:  base × M1^level × (1 ± 12%)                   (tier 1: 每 tick 必升)
        //   level ≥ cap1:  base × M1^cap1 × M2^(level - cap1) × (1 ± 12%)  (tier 2/3)
        //   min(上式, HARD_CAP)
        //
        // Level 推进:
        //   level < cap1         → level++          (确定性)
        //   cap1 ≤ level < cap2  → 9% 概率 level++  (慢区)
        //   level ≥ cap2         → level++          (快区)
        //
        // 单调性: 固定 M1 使 delay(T) ≈ base + T×(M1-1)，
        //         ∂delay/∂base = 1 > 0，base 越高期望 delay 越高。
        //
        // ── 固定间隔分支 (proactiveFixedScheduleMode) ──
        // 当后端 propensity=restricted_screen_only（屏幕专注态：gaming /
        // focused_work）时，常规退避会让搭话间隔指数级增长，跟陪伴产品
        // 命题冲突。前端跳过 tier backoff，按 baseInterval 等间隔触发，
        // 后端在 /proactive_chat 入口注入 [0, 0.5×base] sleep 把实际间隔
        // 抹成 [base, 1.5×base] 均匀分布。详见 main_routers/system_router.py
        // 的 restricted_screen_only 处理段。

        var baseInterval = S.proactiveChatInterval;
        var BACKOFF_TARGET = 120;
        var BACKOFF_M1 = 1.09167;
        var BACKOFF_M2 = 1.55;
        var BACKOFF_SLOW = 4;
        var BACKOFF_P_SLOW = 0.09;
        var BACKOFF_HARD_CAP_MS = 3600 * 1000;

        function computeBackoffCaps(baseIntervalSeconds) {
            var c1 = (baseIntervalSeconds >= BACKOFF_TARGET) ? 0
                : Math.ceil(Math.log(BACKOFF_TARGET / baseIntervalSeconds) / Math.log(BACKOFF_M1));
            return { cap1: c1, cap2: c1 + BACKOFF_SLOW };
        }

        var caps = computeBackoffCaps(baseInterval);
        var cap1 = caps.cap1;
        var cap2 = caps.cap2;

        var fixedMode = !!S.proactiveFixedScheduleMode;
        var level = S.proactiveChatBackoffLevel;
        var delay;

        if (fixedMode) {
            // 屏幕专注态：跳过 tier backoff，重置 level，按 baseInterval 等间隔
            // 触发。抖动完全交给后端（[0, 0.5×base] sleep），前端不做乘性抖动，
            // 否则两层叠加会让方差大于设计目标。
            S.proactiveChatBackoffLevel = 0;
            level = 0;
            delay = baseInterval * 1000;
        } else if (level < cap1) {
            // Tier 1: base × M1^level，确定性爬升
            delay = (baseInterval * 1000) * Math.pow(BACKOFF_M1, level);
        } else {
            // Tier 2/3: 从收敛点开始用 M2 爬升
            var convergenceDelay = baseInterval * Math.pow(BACKOFF_M1, cap1);
            delay = convergenceDelay * 1000 * Math.pow(BACKOFF_M2, level - cap1);
            delay = Math.min(delay, BACKOFF_HARD_CAP_MS);
        }

        // 对 delay 做 ±12% 乘性随机抖动，避免节奏过于机械
        // 固定模式下抖动由后端注入，前端不再叠加
        if (!fixedMode) {
            delay *= 1 + (Math.random() - 0.5) * 0.24;
        }

        // 首次启动时额外等待 6 秒，避免程序刚启动就触发音乐推荐。
        // 用一次性 flag 而非 backoffLevel === 0 —— 后者在 user_input reset 或
        // speaking-skip 重排时也会命中，导致每次都重新叠 6s，把 skip 路径期望的
        // "等待 ∈ [0, interval)" 变成 "interval + 6s"。
        var startupDelay = 0;
        if (!S._proactiveStartupDelayApplied) {
            startupDelay = 6000;
            S._proactiveStartupDelayApplied = true;
        }
        delay += startupDelay;

        // 输入放缓 floor 跟 fixed/tier 模式正交：用户在打字时不该被主动搭话打断，
        // 不管处于屏幕专注态还是常规态。两边都套这个下限。
        var inputSlowdownDelay = _getChatInputSlowdownDelay(baseInterval);
        if (inputSlowdownDelay > 0) {
            delay = Math.max(delay, inputSlowdownDelay);
        }

        if (fixedMode) {
            console.log('主动搭话：' + (delay / 1000).toFixed(1) + '秒后触发（屏幕专注态固定间隔，base=' + baseInterval + 's，level 已重置，后端注入抖动，启动延迟：' + (startupDelay / 1000) + '秒，输入放缓：' + (inputSlowdownDelay ? ((inputSlowdownDelay / 1000) + '秒') : '无') + '）');
        } else {
            console.log('主动搭话：' + (delay / 1000).toFixed(1) + '秒后触发（基础间隔：' + baseInterval + '秒，退避级别：' + level + '，cap1：' + cap1 + '，cap2：' + cap2 + '，启动延迟：' + (startupDelay / 1000) + '秒，输入放缓：' + (inputSlowdownDelay ? ((inputSlowdownDelay / 1000) + '秒') : '无') + '）');
        }

        S.proactiveChatTimer = setTimeout(async function () {
            // setTimeout firing does not clear our stored handle.
            S.proactiveChatTimer = null;
            // 双重检查锁：定时器触发时再次检查是否正在执行
            if (S.isProactiveChatRunning) {
                console.log('主动搭话定时器触发时发现正在执行中，跳过本次');
                return;
            }

            // 设计说明（by 用户意图）：
            // 不 rearm-after-playback —— 那样每句话说完都要等满一个固定间隔，节奏太死。
            // 改为"继续按间隔轮询"：轮询到时 AI 还在说 → 跳过本次，不累加 backoffLevel
            // （没真发请求就不算一次尝试），但仍然 scheduleProactiveChat() 推进下一 tick。
            // 结果：播放完成到下一次 nudge 的等待 ∈ [0, interval)，带随机感，更自然。
            if (_isAssistantSpeaking()) {
                console.log('[ProactiveChat] 文本模式：AI 正在播放语音，本次跳过（不累加退避），继续下一 tick', _dumpSpeakingGateState());
                scheduleProactiveChat();
                return;
            }

            console.log('触发主动搭话...');
            S.isProactiveChatRunning = true; // 加锁

            var triggered = false;
            try {
                triggered = await triggerProactiveChat();
            } finally {
                S.isProactiveChatRunning = false; // 解锁
            }

            // 三段式 level 推进（仅在实际发送了请求时才推进）：
            //   tier 1 (level < cap1): 每次必升 — 确定性爬升阶段
            //   tier 2 (cap1 ≤ level < cap2): 9% 概率升级 — 慢区，长时间停留
            //   tier 3 (level ≥ cap2): 每次必升 — 快区，快速逼近 60min 硬顶
            // 屏幕专注态固定模式下不动 level（由 next_schedule_fixed_mode
            // 反向通知后续 reset），让用户离开屏幕态回到常规态时 backoff
            // 不会带着旧值。
            //
            // ⚠️ 用本轮调度时捕获的 ``fixedMode`` 而非已被响应同步过的
            // ``S.proactiveFixedScheduleMode``：本轮的 level 推进决策应该基于
            // 「这一 round 是按哪种模式调度的」而不是「返回后的最新模式」。
            // 否则 fixed → tier 切换的那一跳会误升一级，下一轮 tier 不能从
            // 干净的 base 起步。CodeRabbit Minor review: PR #1327。
            if (triggered && !fixedMode) {
                var currentCaps = computeBackoffCaps(S.proactiveChatInterval);
                var currentCap1 = currentCaps.cap1;
                var currentCap2 = currentCaps.cap2;

                if (S.proactiveChatBackoffLevel < currentCap1) {
                    S.proactiveChatBackoffLevel++;
                } else if (S.proactiveChatBackoffLevel < currentCap2) {
                    if (Math.random() < BACKOFF_P_SLOW) {
                        S.proactiveChatBackoffLevel++;
                        console.log('[ProactiveChat] 慢区概率升级命中，退避级别升至 ' + S.proactiveChatBackoffLevel);
                    }
                } else {
                    S.proactiveChatBackoffLevel++;
                }
            }

            // 安排下一次
            scheduleProactiveChat();
        }, delay);
    }
    mod.scheduleProactiveChat = scheduleProactiveChat;

    // ======================== getAvailablePersonalPlatforms ========================

    /**
     * 获取个人媒体cookies所有可用平台的函数
     */
    async function getAvailablePersonalPlatforms() {
        try {
            var response = await fetch('/api/auth/cookies/status');
            if (!response.ok) return [];

            var result = await response.json();
            var availablePlatforms = [];
            var personalFeedPlatforms = new Set(['bilibili', 'douyin', 'kuaishou', 'weibo', 'reddit', 'twitter']);

            if (result.success && result.data) {
                for (var _ref of Object.entries(result.data)) {
                    var platform = _ref[0];
                    var info = _ref[1];
                    // A credential can belong to a non-feed platform (for example
                    // NetEase Music or Xiaoheihe).  Presence alone must not enable
                    // the personal-dynamics source.
                    if (personalFeedPlatforms.has(platform) && info.has_cookies && info.supports_personal_dynamic === true) {
                        availablePlatforms.push(platform);
                    }
                }
            }
            return availablePlatforms;
        } catch (error) {
            console.error('获取可用平台列表失败:', error);
            return [];
        }
    }
    mod.getAvailablePersonalPlatforms = getAvailablePersonalPlatforms;

    // ======================== triggerProactiveChat ========================

    async function triggerProactiveChat() {
        var requestSent = false;
        try {
            if (isHomeTutorialFeatureSuppressed()) {
                console.log('[ProactiveChat] 首页新手教程接管中，跳过主动搭话');
                return false;
            }
            if (isNewUserIcebreakerPeriodActive()) {
                console.log('[ProactiveChat] 新用户破冰期未结束，跳过主动搭话');
                return false;
            }

            // 主备协调：本窗口非 leader 时不触发，避免和 Pet 主窗口重复发请求。
            // 这里再 guard 一次是为了防止 leader 切换后旧定时器仍然触发。
            if (!isProactiveLeader()) {
                console.log('[ProactiveChat] 当前不是 leader，跳过触发');
                return;
            }
            // 「请她离开」状态下不触发
            if (isGoodbyeActive()) {
                console.log('[ProactiveChat] goodbye 状态，跳过本次触发');
                return;
            }
            if (S.gameRouteActive) {
                console.log('[ProactiveChat] 游戏路由 active，跳过普通主动搭话');
                return;
            }
            // ── 语音模式快速路径：直接发 voice_mode 请求，后端注入文本触发 ──
            if (S.isRecording) {
                var lanlanName = (window.lanlan_config && window.lanlan_config.lanlan_name) || '';
                var voiceModes = [];
                if (S.proactiveVisionChatEnabled && S.proactiveChatEnabled && isProactiveVisionEnabledNow()) {
                    voiceModes.push('vision');
                }
                console.log('[ProactiveChat] 语音模式快速路径，modes: [' + voiceModes.join(', ') + ']');
                // 故意不带 base_interval_seconds / 不读 next_schedule_fixed_mode：
                // 语音模式在后端走 voice fast path（system_router.py 4222 行附近），
                // 在 propensity / restricted_screen_only / 抖动 sleep 这一整套门
                // 之前就早退；语音 scheduler 自己也是固定 baseInterval 不带 backoff。
                // 既然两边都不读，发了也是冗余字段。
                var voiceProactiveSec = window.nekoLocalMutationSecurity;
                var voiceProactivePayload = {
                    lanlan_name: lanlanName,
                    enabled_modes: voiceModes,
                    voice_mode: true,
                    // mini-game 邀请的用户级 toggle；后端 _maybe_deliver_mini_game_invite
                    // 与 source-driven sources 解耦，不进 enabled_modes 数组。
                    mini_game_invite_enabled: !!S.proactiveMiniGameInviteEnabled
                };
                _applyProactiveLanguagePayload(voiceProactivePayload, lanlanName);
                var voiceProactiveBody = JSON.stringify(voiceProactivePayload);

                async function _sendVoiceProactive() {
                    var hdrs = { 'Content-Type': 'application/json' };
                    if (voiceProactiveSec && typeof voiceProactiveSec.getMutationHeaders === 'function') {
                        try { Object.assign(hdrs, await voiceProactiveSec.getMutationHeaders()); } catch (_) { }
                    }
                    return fetch('/api/proactive_chat', {
                        method: 'POST',
                        headers: hdrs,
                        body: voiceProactiveBody,
                    });
                }

                var resp = await _sendVoiceProactive();

                // CSRF-403 retry-once: 只在 error_code === 'csrf_validation_failed'
                // 时调 refreshToken() + 重试一次。其它 403（业务规则、反代、WAF）走
                // 真实失败分支，避免把所有 403 当 benign pass，让 token 过期后
                // proactive 静默停摆到整页刷新为止（CodeRabbit Major on PR #1530）。
                if (resp.status === 403
                    && voiceProactiveSec
                    && typeof voiceProactiveSec.refreshToken === 'function'
                    && await _proactiveIsCsrfValidationFailure(resp)) {
                    try {
                        await voiceProactiveSec.refreshToken();
                        resp = await _sendVoiceProactive();
                    } catch (_) { /* fall through to 403 handling below */ }
                }

                // HTTP 409 = server try_start_proactive 因并发拒绝（AI 还在响应上一轮 /
                // 另一路 proactive 已占坑，见 main_routers/system_router.py:4241-4247）。
                // 本次请求**根本没真正发起**一次 proactive，标 'pass' 走上游"不计数"
                // 分支自行 schedule，否则 _voiceProactiveNoResponseCount 会被白白消耗
                // 一格、最坏 5 次 server 忙就触发"连续 5 轮无回复，停止主动搭话"。
                //
                // HTTP 403 csrf_validation_failed = 统一守卫拒绝。refresh+retry 仍失败
                // 时归入"server 拒绝"分支：token bootstrap 真没完成 / 配置错位的
                // 启动竞速窗口跟 409 同语义（server 早退、没跑业务）。当 attempt 算
                // 会让 _voiceProactiveNoResponseCount 在 token bootstrap 完成前就
                // 消耗光，触发"5 轮无回复，停止主动搭话"。
                //
                // 非 csrf 的 403（业务规则 / 反代 / WAF）会跳过上面的 retry，落到这里
                // ——也按"不消耗 attempt"处理：这些路径在 proactive 语义里同样表示
                // "server 没真正跑业务"，把它们计入 attempt 既无重试帮助又会污染调度。
                if (resp.status === 409 || resp.status === 403) {
                    console.log('[ProactiveChat] 语音模式 server 拒绝 ('
                        + resp.status + ')，不消耗 attempt');
                    S._voiceProactiveLastResult = 'pass';
                    return false;
                }
                requestSent = true;
                var result = await resp.json();
                S._voiceProactiveLastResult = result.action || 'unknown';
                console.log('[ProactiveChat] 语音模式结果:', S._voiceProactiveLastResult);
                return true;
            }

            var availableModes = [];
            // 收集所有启用的搭话方式
            // 视觉搭话：需要同时开启主动搭话和自主视觉
            // 同时触发 vision 和 window 模式
            if (S.proactiveVisionChatEnabled && S.proactiveChatEnabled && isProactiveVisionEnabledNow()) {
                availableModes.push('vision');
                availableModes.push('window');
            }

            // 新闻搭话：使用微博热议与小黑盒首页内容
            if (S.proactiveNewsChatEnabled && S.proactiveChatEnabled) {
                availableModes.push('news');
            }

            // 视频搭话：中文地区使用 B站，非中文地区使用 YouTube 首页 Feed
            if (S.proactiveVideoChatEnabled && S.proactiveChatEnabled) {
                availableModes.push('video');
            }

            // 个人动态搭话：聚合已登录社交平台的账号信息流。
            if (S.proactivePersonalChatEnabled && S.proactiveChatEnabled) {
                // 检查是否有可用的 Cookie 凭证
                var platforms = await getAvailablePersonalPlatforms();
                if (platforms.length > 0) {
                    availableModes.push('personal');
                    console.log('[个人动态] 模式已启用，平台: ' + platforms.join(', '));
                } else {
                    // 如果开关开了但没登录，不把 personal 发给后端，避免后端抓取失败报错
                    console.warn('[个人动态] 开关已开启但未检测到登录凭证，已忽略此模式');
                }
            }
            // 音乐搭话（正在播放或冷却期内不发送 music 模式，避免后端搜歌浪费 + 污染模型上下文）
            console.log('[ProactiveChat] 检查音乐模式: proactiveMusicEnabled=' + S.proactiveMusicEnabled + ', proactiveChatEnabled=' + S.proactiveChatEnabled);
            if (S.proactiveMusicEnabled && S.proactiveChatEnabled) {
                var musicOccupied = isMusicOccupiedNow();
                var musicRateLimited = (typeof window.isMusicRecommendRateLimited === 'function') && window.isMusicRecommendRateLimited();
                var musicCooldown = (typeof window.isMusicCooldown === 'function') && window.isMusicCooldown();
                if (musicOccupied || musicRateLimited || musicCooldown) {
                    console.log('[ProactiveChat] 音乐模式跳过: occupied=' + musicOccupied + ', rateLimited=' + musicRateLimited + ', cooldown=' + musicCooldown);
                } else {
                    console.log('[ProactiveChat] 音乐模式已启用');
                    availableModes.push('music');
                }
            }

            // Meme搭话
            if (S.proactiveMemeEnabled && S.proactiveChatEnabled) {
                console.log('[ProactiveChat] Meme模式已启用');
                availableModes.push('meme');
            }

            // 如果没有选择任何搭话方式，跳过本次搭话——除非 mini-game 邀请独立开着
            // （那条路径与 enabled_modes 解耦，后端短路通道仍可能掷骰投递邀请）。
            if (availableModes.length === 0) {
                if (!S.proactiveMiniGameInviteEnabled) {
                    console.log('未选择任何搭话方式，跳过本次搭话');
                    return;
                }
                console.log('availableModes 为空但 mini-game 邀请开着，让请求过去走短路通道');
            }

            console.log('主动搭话：启用模式 [' + availableModes.join(', ') + ']，将并行获取所有信息源');

            var lanlanName = (window.lanlan_config && window.lanlan_config.lanlan_name) || '';
            var requestBody = {
                lanlan_name: lanlanName,
                enabled_modes: availableModes,
                is_playing_music: (typeof window.isMusicPlaying === 'function') ? window.isMusicPlaying() : false,
                is_music_occupied: isMusicOccupiedNow(),
                current_track: (typeof window.getMusicCurrentTrack === 'function') ? window.getMusicCurrentTrack() : null,
                music_cooldown: (typeof window.isMusicCooldown === 'function') ? window.isMusicCooldown() : false,
                // mini-game 邀请的用户级 toggle；后端 _maybe_deliver_mini_game_invite
                // 与 source-driven sources 解耦，不进 enabled_modes 数组。
                mini_game_invite_enabled: !!S.proactiveMiniGameInviteEnabled,
                // 屏幕专注态后端会按 [0, 0.5×base] 注入间隔抖动，需要知道
                // 当前用户配置的 baseInterval。后端 propensity 非屏幕专注态
                // 时忽略此字段。
                base_interval_seconds: S.proactiveChatInterval
            };
            _applyProactiveLanguagePayload(requestBody, lanlanName);

            // 独立计时器：确保 vision/window 模式的屏幕感知间隔不低于 proactiveVisionInterval
            if (availableModes.includes('vision') || availableModes.includes('window')) {
                var now = Date.now();
                var minIntervalMs = S.proactiveVisionInterval * 1000;
                var elapsed = now - S._lastProactiveChatScreenTime;
                if (elapsed < minIntervalMs) {
                    console.log('[ProactiveChat] 屏幕感知间隔不足（已过 ' + Math.round(elapsed / 1000) + '秒，最低 ' + S.proactiveVisionInterval + '秒），本轮跳过 vision/window');
                    availableModes = availableModes.filter(function (m) { return m !== 'vision' && m !== 'window'; });
                    requestBody.enabled_modes = availableModes;
                    if (availableModes.length === 0) {
                        console.log('跳过屏幕感知后无其他可用模式，取消本次搭话');
                        return;
                    }
                }
            }

            // 如果包含 vision 模式，需要在前端获取截图和窗口标题
            if (availableModes.includes('vision') || availableModes.includes('window')) {
                var fetchTasks = [];
                var screenshotIndex = -1;
                var windowTitleIndex = -1;

                if (availableModes.includes('vision')) {
                    screenshotIndex = fetchTasks.length;
                    // 带来源版：下面要靠 via 区分「后端整屏兜底」与「流/原生源」，
                    // 光看发送时的 S.selectedScreenSourceId 会把整屏图误判成窗口截图。
                    fetchTasks.push(captureProactiveChatScreenshotWithSource());
                }

                if (availableModes.includes('window')) {
                    windowTitleIndex = fetchTasks.length;
                    fetchTasks.push(fetch('/api/get_window_title')
                        .then(function (r) { return r.json(); })
                        .catch(function () { return { success: false }; }));
                }

                var results = await Promise.all(fetchTasks);

                // await 期间检查状态
                if (!canTriggerProactively()) {
                    console.log('功能已关闭或前置条件不满足，取消本次搭话');
                    return;
                }

                // await 期间用户可能切换模式，重新过滤可用模式
                var latestModes = [];
                if (S.proactiveVisionChatEnabled && S.proactiveChatEnabled && isProactiveVisionEnabledNow()) {
                    latestModes.push('vision', 'window');
                }
                if (S.proactiveNewsChatEnabled && S.proactiveChatEnabled) {
                    latestModes.push('news');
                }
                if (S.proactiveVideoChatEnabled && S.proactiveChatEnabled) {
                    latestModes.push('video');
                }
                // 个人动态搭话：需要同时开启个人动态
                if (S.proactivePersonalChatEnabled && S.proactiveChatEnabled) {
                    latestModes.push('personal');
                }
                // 音乐搭话（重新检查冷却状态，await 期间可能变化）
                if (S.proactiveMusicEnabled && S.proactiveChatEnabled) {
                    var musicOccupiedNow = isMusicOccupiedNow();
                    var musicRateLimitedNow = (typeof window.isMusicRecommendRateLimited === 'function') && window.isMusicRecommendRateLimited();
                    var musicCooldownNow = (typeof window.isMusicCooldown === 'function') && window.isMusicCooldown();
                    if (!musicOccupiedNow && !musicRateLimitedNow && !musicCooldownNow) {
                        latestModes.push('music');
                    }
                }
                // Meme搭话
                if (S.proactiveMemeEnabled && S.proactiveChatEnabled) {
                    latestModes.push('meme');
                }
                availableModes = availableModes.filter(function (m) { return latestModes.includes(m); });
                requestBody.enabled_modes = availableModes;
                if (availableModes.length === 0) {
                    console.log('await后无可用模式，取消本次搭话');
                    return;
                }

                if (screenshotIndex !== -1 && availableModes.includes('vision')) {
                    var screenshotResult = results[screenshotIndex] || {};
                    var screenshotDataUrl = screenshotResult.dataUrl;
                    if (screenshotDataUrl) {
                        requestBody.screenshot_data = screenshotDataUrl;
                        // captureType 由截图函数在抓帧那一刻定好（见 resolveCaptureTypeFor）。
                        // 这里不再二次推断：抓帧是异步的，等到这一步 S 里的流 / 源
                        // 可能已经换人，拿它解释这张旧图会叠错或漏叠。
                        var captureType = screenshotResult.captureType || null;
                        var avatarPos = typeof window.getAvatarScreenPosition === 'function'
                            ? window.getAvatarScreenPosition(captureType) : null;
                        if (avatarPos) {
                            requestBody.avatar_position = avatarPos;
                        }
                        if (window.unlockAchievement) {
                            window.unlockAchievement('ACH_SEND_IMAGE').catch(function (err) {
                                console.error('解锁发送图片成就失败:', err);
                            });
                        }
                    } else {
                        // 截图失败，从 enabled_modes 中移除 vision
                        console.log('截图失败，移除 vision 模式');
                        availableModes = availableModes.filter(function (m) { return m !== 'vision'; });
                        requestBody.enabled_modes = availableModes;
                    }
                }

                if (windowTitleIndex !== -1 && availableModes.includes('window')) {
                    var windowTitleResult = results[windowTitleIndex];
                    if (windowTitleResult && windowTitleResult.success && windowTitleResult.window_title) {
                        requestBody.window_title = windowTitleResult.window_title;
                        console.log('视觉搭话附加窗口标题:', windowTitleResult.window_title);
                    } else {
                        // 窗口标题获取失败，从 enabled_modes 中移除 window
                        console.log('窗口标题获取失败，移除 window 模式');
                        availableModes = availableModes.filter(function (m) { return m !== 'window'; });
                        requestBody.enabled_modes = availableModes;
                    }
                }

                if (availableModes.length === 0) {
                    console.log('所有附加模式均失败，移除后无其他可用模式，跳过本次搭话');
                    return;
                }

                // 更新屏幕感知时间戳（仅当 vision/window 实际保留时才消耗冷却）
                if (availableModes.includes('vision') || availableModes.includes('window')) {
                    S._lastProactiveChatScreenTime = Date.now();
                }
            }

            // 发送请求前最终检查：确保功能状态未在 await 期间改变
            if (!canTriggerProactively()) {
                console.log('发送请求前检查失败，取消本次搭话');
                return;
            }

            // 检测用户是否在20秒内有过输入，有过输入则作废本次主动搭话
            var timeSinceLastInput = Date.now() - (window.lastUserInputTime || 0);
            if (timeSinceLastInput < 20000) {
                console.log('主动搭话作废：用户在' + Math.round(timeSinceLastInput / 1000) + '秒前有过输入');
                return;
            }

            // 前面的截图/窗口标题等待期间，播放器可能刚好开始加载或播放。
            // 在序列化请求前使用最新状态收口，避免后端为过期的 music 模式再次搜歌。
            var musicPlayingBeforeRequest = (typeof window.isMusicPlaying === 'function') && window.isMusicPlaying();
            var musicOccupiedBeforeRequest = isMusicOccupiedNow();
            var musicRateLimitedBeforeRequest = (typeof window.isMusicRecommendRateLimited === 'function') && window.isMusicRecommendRateLimited();
            var musicCooldownBeforeRequest = (typeof window.isMusicCooldown === 'function') && window.isMusicCooldown();
            requestBody.is_playing_music = !!musicPlayingBeforeRequest;
            requestBody.is_music_occupied = !!musicOccupiedBeforeRequest;
            requestBody.current_track = (typeof window.getMusicCurrentTrack === 'function') ? window.getMusicCurrentTrack() : null;
            requestBody.music_cooldown = !!musicCooldownBeforeRequest;
            if (musicOccupiedBeforeRequest || musicRateLimitedBeforeRequest || musicCooldownBeforeRequest) {
                requestBody.enabled_modes = requestBody.enabled_modes.filter(function (mode) { return mode !== 'music'; });
                if (requestBody.enabled_modes.length === 0 && !S.proactiveMiniGameInviteEnabled) {
                    return;
                }
            }

            var proactiveSec = window.nekoLocalMutationSecurity;
            var proactiveBody = JSON.stringify(requestBody);

            async function _sendProactive() {
                var hdrs = { 'Content-Type': 'application/json' };
                if (proactiveSec && typeof proactiveSec.getMutationHeaders === 'function') {
                    try { Object.assign(hdrs, await proactiveSec.getMutationHeaders()); } catch (_) { }
                }
                return fetch('/api/proactive_chat', {
                    method: 'POST',
                    headers: hdrs,
                    body: proactiveBody,
                });
            }

            var response = await _sendProactive();

            // CSRF-403 retry-once: 只在 error_code === 'csrf_validation_failed'
            // 时调 refreshToken() + 重试一次。其它 403 走真实失败分支，避免把所有
            // 403 当 benign pass，让 token 过期后 proactive 静默停摆（CodeRabbit
            // Major on PR #1530）。
            if (response.status === 403
                && proactiveSec
                && typeof proactiveSec.refreshToken === 'function'
                && await _proactiveIsCsrfValidationFailure(response)) {
                try {
                    await proactiveSec.refreshToken();
                    response = await _sendProactive();
                } catch (_) { /* fall through to 403 handling below */ }
            }

            // HTTP 409 = server try_start_proactive 因并发拒绝（AI 还在响应上一轮 /
            // 另一路 proactive 已占坑，见 main_routers/system_router.py:4241-4247）。
            // 本次请求**根本没真正发起**一次 proactive——server 在 claim 那一步就早退
            // 了，没跑过 phase 1/2 LLM、没消耗任何上下文资源。若返 true 让上游
            // scheduleProactiveChat 的 `if (triggered)` 判定通过、把 backoffLevel++，
            // 等于"server 一忙就被前端误判成 attempt 用掉一格"，惩罚性升级 backoff 把
            // 节奏整体往后拉，跟 server 实际状态正交、用户体验上变成"server 越忙、AI
            // 越沉默"。这里 requestSent 故意不翻 true、return false 让上游识别为"没
            // 真发"、下一轮按 base interval 重排，不动 level。
            //
            // HTTP 403 csrf_validation_failed = 统一守卫拒绝（refresh+retry 后仍失败）。
            // 跟 409 同语义——server 没跑业务逻辑、没消耗资源；非 csrf 的 403（业务
            // 规则 / 反代 / WAF）会跳过上面的 retry 落到这里，同样按"不消耗 attempt"
            // 处理，避免把"刚开机几秒里 token 没就位 / 部署中间态"惩罚性升级
            // backoff。下一轮 token 通常已到位，按 base interval 重排即可。
            if (response.status === 409 || response.status === 403) {
                console.log('[ProactiveChat] server 拒绝 ('
                    + response.status + ')，不消耗 backoff attempt');
                return false;
            }
            requestSent = true;

            var result = await response.json();

            // 同步下一轮调度模式：后端在 propensity=restricted_screen_only
            // 时会把这个字段置 true，前端 scheduleProactiveChat 据此跳过
            // tier backoff、按 baseInterval 等间隔触发。
            //
            // 字段缺席 ≠ 模式应该回退。短路响应路径（409 try_start_proactive
            // 冲突、voice fast path、game-route active 早退）都不走 _end_proactive，
            // 也就拿不到这个字段——但用户的活动状态没变，把模式硬重置成 false
            // 会让一次并发冲突就把客户端踢出 fixed 模式、被 tier backoff 吞几轮。
            // 改为：只有显式收到 boolean 才同步，缺席时保留旧状态。
            // Codex P2 review: PR #1327。
            if (typeof result.next_schedule_fixed_mode === 'boolean') {
                S.proactiveFixedScheduleMode = result.next_schedule_fixed_mode;
            }

            if (result.success) {
                if (result.action === 'chat') {
                    console.log('主动搭话已发送:', result.message, result.source_mode ? '(来源: ' + result.source_mode + ')' : '');

                    var dispatchedTrackUrl = null;
                    var proactiveMusicCardScopeId = 'proactive:' + (
                        result.turn_id || (Date.now() + '-' + Math.random().toString(36).slice(2, 8))
                    );

                    // 如果模式包含音乐信号，按顺序尝试音轨；候选 URL 或媒体自身
                    // 的错误（包括加载超时）才回退，播放器/调度错误结束本轮推荐。
                    if ((result.source_mode === 'music' || result.source_mode === 'both') && result.source_links && Array.isArray(result.source_links)) {
                        var normalizedLinks = result.source_links.filter(Boolean);
                        var musicLinks = normalizedLinks.filter(function (link) {
                            return link && link.url && (link.artist || link.source === '音乐推荐');
                        });
                        // 兼容未带 artist/source 标记的旧服务端响应。旧版本可能
                        // 同时返回多个普通 { title, url, cover } 候选。
                        if (musicLinks.length === 0) {
                            musicLinks = normalizedLinks.filter(function (link) {
                                return link && link.url;
                            });
                        }

                        if (musicLinks.length > 0) {
                            await new Promise(function (resolve) {
                                setTimeout(resolve, 50 + Math.floor(Math.random() * 120));
                            });
                            var musicBusyBeforeDispatch =
                                isMusicOccupiedNow() ||
                                ((typeof window.isMusicRecommendRateLimited === 'function') && window.isMusicRecommendRateLimited()) ||
                                ((typeof window.isMusicCooldown === 'function') && window.isMusicCooldown());
                            if (musicBusyBeforeDispatch) {
                                console.log('[ProactiveChat] 音乐 dispatch 前检测到播放器已占用，跳过本次音乐链接');
                            } else {
                                var unknownTrack = window.t ? window.t('music.unknownTrack') : 'Unknown Track';
                                var unknownArtist = window.t ? window.t('music.unknownArtist') : 'Unknown Artist';
                                if (!unknownTrack || unknownTrack === 'music.unknownTrack') unknownTrack = 'Unknown Track';
                                if (!unknownArtist || unknownArtist === 'music.unknownArtist') unknownArtist = 'Unknown Artist';

                                var proactiveMusicFallbackDeadlineAt = Date.now() + MUSIC_CANDIDATE_FALLBACK_BUDGET_MS;
                                var lastAttemptedMusicTrack = null;
                                try {
                                    for (var musicIndex = 0; musicIndex < musicLinks.length; musicIndex++) {
                                        var musicLink = musicLinks[musicIndex];
                                        var hasNextMusicCandidate = musicIndex < musicLinks.length - 1;
                                        var track = {
                                            name: musicLink.title || unknownTrack,
                                            artist: musicLink.artist || unknownArtist,
                                            url: musicLink.url,
                                            cover: musicLink.cover
                                        };
                                        lastAttemptedMusicTrack = track;
                                        console.log('[ProactiveChat] 尝试音乐候选 ' + (musicIndex + 1) + '/' + musicLinks.length + ':', track);
                                        var dispatchResult;
                                        if (typeof window.dispatchMusicPlayDetailed === 'function') {
                                            dispatchResult = await window.dispatchMusicPlayDetailed(track, {
                                                source: 'proactive',
                                                cardScopeId: proactiveMusicCardScopeId,
                                                hasNextCandidate: hasNextMusicCandidate,
                                                fallbackDeadlineAt: hasNextMusicCandidate
                                                    ? proactiveMusicFallbackDeadlineAt
                                                    : undefined,
                                                candidateTimeoutMs: hasNextMusicCandidate
                                                    ? MUSIC_CANDIDATE_ATTEMPT_TIMEOUT_MS
                                                    : undefined
                                            });
                                        } else {
                                            var legacyAccepted = await window.dispatchMusicPlay(track, { source: 'proactive' });
                                            dispatchResult = {
                                                ok: legacyAccepted === true,
                                                reason: legacyAccepted === true ? '' : 'player_error',
                                                canTryNextCandidate: false
                                            };
                                        }

                                        if (dispatchResult.ok === true) {
                                            dispatchedTrackUrl = musicLink.url;
                                            break;
                                        }
                                        if (dispatchResult.canTryNextCandidate !== true) {
                                            console.warn('[ProactiveChat] 音乐派发因非候选错误停止:', dispatchResult.reason, musicLink.url);
                                            break;
                                        }
                                        console.warn('[ProactiveChat] 音乐候选不可用，尝试下一条:', dispatchResult.reason, musicLink.url);
                                    }
                                } finally {
                                    if (
                                        !dispatchedTrackUrl
                                        && lastAttemptedMusicTrack
                                        && typeof window.finalizeMusicCandidateCardFailure === 'function'
                                    ) {
                                        window.finalizeMusicCandidateCardFailure(lastAttemptedMusicTrack, {
                                            source: 'proactive',
                                            cardScopeId: proactiveMusicCardScopeId
                                        });
                                    }
                                }
                            }
                        }
                    }

                    // 【重构】统一处理链接，使用服务端返回的 turn_id 绑定，解决 HTTP/WS 竞态
                    var captureTurnId = result.turn_id || 'fallback';
                    var processed = _processProactiveLinks(result.source_links || [], dispatchedTrackUrl);

                    // 暂存待展示附件，等待对应的 turn_id 建立后统一 flush
                    if (!window._proactiveAttachmentBuffer) {
                        window._proactiveAttachmentBuffer = {};
                    }
                    if (!window._proactiveAttachmentBuffer[captureTurnId]) {
                        window._proactiveAttachmentBuffer[captureTurnId] = { memes: [], links: [] };
                    }
                    
                    if (processed.memeLinks.length > 0) {
                        var MAX_MEME_BUBBLES = 2;
                        window._proactiveAttachmentBuffer[captureTurnId].memes = processed.memeLinks.slice(0, MAX_MEME_BUBBLES);
                    }
                    
                    if (processed.otherLinks.length > 0) {
                        window._proactiveAttachmentBuffer[captureTurnId].links = processed.otherLinks;
                    }

                    // 如果当前 turn 已经就绪（例如主动搭话回复极快），直接 flush
                    if (window.realisticGeminiCurrentTurnId === captureTurnId) {
                        _flushProactiveAttachments(captureTurnId);
                    }

                    // 后端会直接通过session发送消息和TTS，前端无需处理显示
                } else if (result.action === 'pass') {
                    console.log('AI选择不搭话');
                }
            } else {
                console.warn('主动搭话失败:', result.error);
            }
            return true;
        } catch (error) {
            console.error('主动搭话触发失败:', error);
            return requestSent;
        }
    }
    mod.triggerProactiveChat = triggerProactiveChat;

    // ======================== attachment buffering ========================

    /**
     * 统一 flush 对应 turn_id 的主动搭话附件（表情包、来源卡片）
     */
    function _flushProactiveAttachments(turnId) {
        if (!window._proactiveAttachmentBuffer || !window._proactiveAttachmentBuffer[turnId]) {
            return; // 没有待展示的附件
        }
        
        var attachments = window._proactiveAttachmentBuffer[turnId];
        
        if (attachments.memes && attachments.memes.length > 0) {
            _showMemeBubbles(attachments.memes, turnId);
        }
        
        if (attachments.links && attachments.links.length > 0) {
            setTimeout(function () {
                _showProactiveSourceCards(attachments.links, turnId);
            }, 3000);
        }
        
        // flush 后清理 buffer
        delete window._proactiveAttachmentBuffer[turnId];
    }
    mod._flushProactiveAttachments = _flushProactiveAttachments;

    // ======================== source link card ========================

    /**
     * 在聊天区域临时显示来源链接卡片（旁路，不进入 AI 记忆）
     */
    /**
     * 将原始链接处理为分类好的安全链接对象
     */
    function _processProactiveLinks(links, dispatchedUrl) {
        var isSameUrl = function (u1, u2) {
            if (!u1 || !u2) return false;
            if (u1 === u2) return true;
            try {
                var url1 = new URL(u1, window.location.origin);
                var url2 = new URL(u2, window.location.origin);
                var getRef = function (u) { return (u.hostname + u.pathname.replace(/\/$/, '') + u.search).toLowerCase(); };
                return getRef(url1) === getRef(url2);
            } catch (e) { return u1 === u2; }
        };

        var memeLinks = [];
        var otherLinks = [];

        for (var i = 0; i < links.length; i++) {
            var link = links[i];
            if (!link) continue;

            var isMusicLink = link.artist || link.source === '音乐推荐' || (dispatchedUrl && isSameUrl(link.url, dispatchedUrl));
            if (isMusicLink) continue;

            var isMemeLink = link.type === 'meme' || link.type === 'gif';
            if (!isMemeLink) {
                var memeSourceKeywords = ['表情包', '斗图吧', '发表情', 'Imgflip', 'meme', 'sticker'];
                var linkSource = String(link.source || '').toLowerCase();
                for (var k = 0; k < memeSourceKeywords.length; k++) {
                    if (linkSource.indexOf(memeSourceKeywords[k].toLowerCase()) !== -1) {
                        isMemeLink = true;
                        break;
                    }
                }
            }
            if (!isMemeLink && link.url) {
                // 2026-04-16: doutub.com 域名易主挂黑产，停用（'qn.doutub.com', 'doutub.com'）；新增 doutupk.com（斗图啦）
                var memeDomains = ['img.soutula.com', 'i.imgflip.com', 'fabiaoqing.com', 'soutula.com', 'img.doutupk.com', 'doutupk.com'];
                var linkHost = '';
                try {
                    var tempUrl = new URL(String(link.url), window.location.origin);
                    linkHost = tempUrl.hostname.toLowerCase();
                } catch (e) {}
                for (var m = 0; m < memeDomains.length; m++) {
                    if (linkHost === memeDomains[m] || linkHost.endsWith('.' + memeDomains[m])) {
                        isMemeLink = true;
                        break;
                    }
                }
            }

            var safeUrl = null;
            var rawUrl = String(link.url || '').trim();
            if (rawUrl && (rawUrl.startsWith('http://') || rawUrl.startsWith('https://'))) {
                try {
                    var u = new URL(rawUrl);
                    if (u.protocol === 'http:' || u.protocol === 'https:') {
                        safeUrl = u.href;
                    }
                } catch (e) {}
            }

            if (safeUrl) {
                if (isMemeLink) {
                    memeLinks.push(Object.assign({}, link, { safeUrl: safeUrl }));
                } else {
                    otherLinks.push(Object.assign({}, link, { safeUrl: safeUrl }));
                }
            }
        }
        return { memeLinks: memeLinks, otherLinks: otherLinks };
    }

    /**
     * 在聊天区域临时显示来源链接卡片
     */
    function _showProactiveSourceCards(otherLinks, targetTurnId) {
        try {
            if (window.realisticGeminiCurrentTurnId !== targetTurnId) return;
            var chatContent = document.getElementById('chat-content-wrapper');
            if (!chatContent || otherLinks.length === 0) return;

            var MAX_LINK_CARDS = 3;
            var existingCards = chatContent.querySelectorAll('.proactive-source-link-card');
            var overflow = existingCards.length - MAX_LINK_CARDS + 1;
            if (overflow > 0) {
                for (var j = 0; j < overflow; j++) {
                    existingCards[j].remove();
                }
            }

            var linkCard = document.createElement('div');
            linkCard.className = 'proactive-source-link-card';
            linkCard.style.cssText =
                'margin: 6px 12px; padding: 8px 14px; background: var(--bg-secondary, rgba(255,255,255,0.08));' +
                'border-left: 3px solid var(--accent-color, #6c8cff); border-radius: 8px;' +
                'font-size: 12px; opacity: 0; transition: opacity 0.4s ease; max-width: 320px; position: relative;';

            var closeBtn = document.createElement('span');
            closeBtn.textContent = '\u2715';
            closeBtn.style.cssText = 'position: absolute; top: 6px; right: 6px; cursor: pointer; color: var(--text-secondary, rgba(200,200,200,0.8)); font-size: 14px; font-weight: bold; line-height: 1; width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; border-radius: 50%; background: rgba(255,255,255,0.08); transition: color 0.2s, background 0.2s; z-index: 1;';
            closeBtn.addEventListener('click', function () { linkCard.style.opacity = '0'; setTimeout(function () { linkCard.remove(); }, 300); });
            linkCard.appendChild(closeBtn);

            for (var k = 0; k < otherLinks.length; k++) {
                (function (vl) {
                    var a = document.createElement('a');
                    a.href = vl.safeUrl;
                    a.textContent = '\uD83D\uDD17 ' + (vl.source ? '[' + vl.source + '] ' : '') + (vl.title || vl.url);
                    a.style.cssText = 'display: block; color: var(--accent-color, #6c8cff); text-decoration: none; padding: 3px 0; padding-right: 20px; word-break: break-all; font-size: 12px; cursor: pointer;';
                    a.addEventListener('click', function (e) {
                        e.preventDefault();
                        if (window.electronShell && window.electronShell.openExternal) { window.electronShell.openExternal(vl.safeUrl); }
                        else { window.open(vl.safeUrl, '_blank', 'noopener,noreferrer'); }
                    });
                    linkCard.appendChild(a);
                })(otherLinks[k]);
            }

            chatContent.appendChild(linkCard);
            chatContent.scrollTop = chatContent.scrollHeight;

            if (window.currentTurnGeminiAttachments) {
                window.currentTurnGeminiAttachments.push(linkCard);
            }

            requestAnimationFrame(function () { linkCard.style.opacity = '1'; });
            setTimeout(function () { linkCard.style.opacity = '0'; setTimeout(function () { linkCard.remove(); }, 500); }, 5 * 60 * 1000);
        } catch (e) {
            console.warn('显示来源链接失败:', e);
        }
    }

    /**
     * 向 React 聊天窗追加一条 assistant 图片气泡。
     *
     * 消息构造、头像解析、镜像广播（__nekoMirrorChatAppend 同步到
     * chat.html 等 follower 窗口，见 music_ui.js）与兜底路径都在这里，
     * 调用方只提供 url / 文案 / id 前缀，避免每个图片来源各抄一份会
     * 各自漂移的拷贝。
     * 已知边界（与后端 send_* 的 display-plane 约束一致，见 notify.py）：
     * WS 帧只送达最新连接的窗口，镜像广播经 BroadcastChannel、不跨
     * Electron partition——隔离 partition 内的窗口（如宠物窗）看不到该
     * 气泡，属既有限制。
     * 返回 false 表示 React host 未就绪，调用方走旧 DOM 兜底。
     */
    function _appendReactImageBubble(url, alt, turnId, idPrefix, logTag, logDetail) {
        var host = window.reactChatWindowHost;
        if (!host || typeof host.appendMessage !== 'function') {
            return false;
        }
        var mirrorAppend = window.__nekoMirrorChatAppend;
        var now = new Date();
        var timeStr = now.getHours().toString().padStart(2, '0') + ':' +
            now.getMinutes().toString().padStart(2, '0');
        var assistantName = '';
        if (window.lanlan_config && window.lanlan_config.lanlan_name) assistantName = window.lanlan_config.lanlan_name;
        else if (window._currentCatgirl) assistantName = window._currentCatgirl;
        else if (window.currentCatgirl) assistantName = window.currentCatgirl;
        assistantName = assistantName || 'Neko';
        var avatarUrl = '';
        if (window.appChatAvatar && typeof window.appChatAvatar.getCurrentAvatarDataUrl === 'function') {
            avatarUrl = window.appChatAvatar.getCurrentAvatarDataUrl() || '';
        }
        var msg = {
            id: idPrefix + Date.now() + '-' + Math.random().toString(36).slice(2, 8),
            role: 'assistant',
            author: assistantName,
            time: timeStr,
            createdAt: Date.now(),
            turnId: turnId,
            avatarLabel: assistantName.trim().slice(0, 1).toUpperCase(),
            avatarUrl: avatarUrl || undefined,
            blocks: [{ type: 'image', url: url, alt: alt }],
            status: 'sent'
        };
        if (typeof mirrorAppend === 'function') {
            mirrorAppend(host, msg);
        } else {
            // 兜底：music_ui.js 未就绪时退化为只在本窗口显示
            host.appendMessage(msg);
        }
        console.log(logTag, logDetail);
        return true;
    }
    mod._appendReactImageBubble = _appendReactImageBubble;

    /**
     * 构建旧 DOM 聊天窗的图片气泡脚手架。
     *
     * 气泡结构（message/gemini/attachment 容器、时间戳+🎀 头部、居中图片
     * 容器、currentTurnGeminiAttachments 登记、追加后滚动到底）与图片来源
     * 无关，抽在这里防止每个来源各抄一份后各自漂移。返回 { bubble, img }，
     * 调用方在 img 上挂各自差异化的 click/error 行为（meme 挂的是外链重试
     * + 失败贴纸）。容器存在但不可见（React 聊天窗为主显示时旧容器
     * display:none）时大声警告——否则"渲染成功"日志会制造假安全网。容器
     * 缺失返回 null，由调用方决定如何提示。
     */
    function _createLegacyImageBubble(chatContainer, imgSrc, altText, maxHeight) {
        // 与上方 docstring 及调用方的 `if (!parts)` 守卫对齐：容器缺失时
        // 返回 null（调用方决定如何提示），而不是无条件建气泡。
        if (!chatContainer) {
            return null;
        }
        var imgBubble = document.createElement('div');
        imgBubble.classList.add('message', 'gemini', 'attachment');
        imgBubble.style.padding = '12px';
        imgBubble.style.textAlign = 'left';

        // 复刻 createGeminiBubble 的头部（与 meme 兜底同一来源）
        var now = new Date();
        var timestamp = now.getHours().toString().padStart(2, '0') + ':' +
            now.getMinutes().toString().padStart(2, '0') + ':' +
            now.getSeconds().toString().padStart(2, '0');
        var headerSpan = document.createElement('span');
        headerSpan.textContent = "[" + (window.appChat ? window.appChat.getCurrentTimeString() : timestamp) + "] \uD83C\uDF80 ";
        imgBubble.appendChild(headerSpan);

        var imgOuter = document.createElement('div');
        imgOuter.style.marginTop = '8px';
        imgOuter.style.textAlign = 'center';

        var img = document.createElement('img');
        img.src = imgSrc;
        img.alt = altText;
        img.style.cssText = 'max-width: 100%; max-height: ' + maxHeight + 'px; border-radius: 8px; cursor: pointer; display: inline-block;';
        img.addEventListener('load', function () {
            chatContainer.scrollTop = chatContainer.scrollHeight;
        });

        imgOuter.appendChild(img);
        imgBubble.appendChild(imgOuter);
        chatContainer.appendChild(imgBubble);

        if (window.currentTurnGeminiAttachments) {
            window.currentTurnGeminiAttachments.push(imgBubble);
        }

        chatContainer.scrollTop = chatContainer.scrollHeight;

        if (!chatContainer.offsetParent) {
            console.warn('[LegacyChat] 旧 DOM 聊天容器当前不可见（React 聊天窗为主显示），图片气泡将不可见:', imgSrc);
        }

        return { bubble: imgBubble, img: img };
    }
    mod._createLegacyImageBubble = _createLegacyImageBubble;

    function _showMemeBubbles(memeLinks, targetTurnId) {
        if (window.realisticGeminiCurrentTurnId !== targetTurnId) return;
        // [优化] 不再此处手动 addToHistory，因为正向的对话流(response_text) 已经由 finish_proactive_delivery 记录。
        // 表情包作为 UI 侧挂件展示，无需单独污染 LLM 上下文。
        if (!memeLinks || !Array.isArray(memeLinks) || memeLinks.length === 0) {
            return;
        }

        // 表情包绑定它所属主动搭话轮的 turn_id（与同轮台词共享同一个值：flush 条件是
        // realisticGeminiCurrentTurnId === captureTurnId，而台词的 turnId 也是 String(turn_id)）。
        // compact overlay 靠它判定「新一轮发言才换场、同轮台词不顶掉」。turn_id 缺失（'fallback'/空）
        // 时留 undefined，React 侧回退到旧的「只有用户开口才收起」逻辑。
        var memeTurnId = (targetTurnId !== undefined && targetTurnId !== null
            && targetTurnId !== '' && targetTurnId !== 'fallback')
            ? String(targetTurnId) : undefined;

        // 优先通过 React 聊天窗口 API 显示表情包
        // （PR #780 之后 proactive 只在 leader 触发，meme 只会暂存在 leader
        // 的 _proactiveAttachmentBuffer 里；镜像广播由 _appendReactImageBubble
        // 内部的 __nekoMirrorChatAppend 同步到所有窗口。）
        var host = window.reactChatWindowHost;
        if (host && typeof host.appendMessage === 'function') {
            for (var i = 0; i < memeLinks.length; i++) {
                var meme = memeLinks[i];
                if (!meme || !meme.safeUrl) continue;
                var proxyUrl = '/api/meme/proxy-image?url=' + encodeURIComponent(meme.safeUrl);
                _appendReactImageBubble(
                    proxyUrl, meme.title || 'Meme', memeTurnId, 'meme-',
                    '[Meme] 已展示图片气泡 (React):', meme.title
                );
            }
            return;
        }

        // 回退：旧 DOM 方式（chatContainer 可见时）
        var chatContainer = S.dom.chatContainer || document.getElementById('chatContainer');
        if (!chatContainer) {
            console.warn('[Meme] chatContainer not found, cannot show meme bubbles');
            return;
        }

        for (var i = 0; i < memeLinks.length; i++) {
            (function (meme) {
                if (!meme || !meme.safeUrl) return;

                var proxyUrl = '/api/meme/proxy-image?url=' + encodeURIComponent(meme.safeUrl);
                var parts = _createLegacyImageBubble(chatContainer, proxyUrl, meme.title || 'Meme', 350);
                if (!parts) return;
                var img = parts.img;

                // 【修复】添加重试机制，最多重试 2 次
                var retryCount = 0;
                var maxRetries = 2;

                img.addEventListener('click', function (e) {
                    if (img.dataset.failed === 'true') return;
                    e.preventDefault();
                    if (window.electronShell && window.electronShell.openExternal) {
                        window.electronShell.openExternal(meme.safeUrl);
                    } else {
                        window.open(meme.safeUrl, '_blank', 'noopener,noreferrer');
                    }
                });
                img.addEventListener('error', function () {
                    if (img.dataset.failed) return;
                    retryCount++;
                    if (retryCount <= maxRetries) {
                        console.log('[Meme] 加载失败，重试第', retryCount, '次:', meme.title);
                        // 添加随机参数避免缓存（proxyUrl 已包含 ?url=，所以用 &）
                        img.src = proxyUrl + '&retry=' + retryCount + '&t=' + Date.now();
                    } else {
                        img.dataset.failed = "true";
                        img.src = MEME_LOAD_FAILED_STICKER_URL;
                        Object.assign(img.style, {
                            objectFit: "contain",
                            backgroundColor: "#fff",
                            border: "none",
                            minWidth: "160px",
                            minHeight: "160px",
                            cursor: "default"
                        });
                    }
                });

                // 【修复】拦截图片失效后的点击事件
                img.addEventListener('click', function (e) {
                    if (img.dataset.failed === "true") {
                        e.preventDefault();
                        e.stopPropagation();
                        return false;
                    }
                });

                console.log('[Meme] 已展示图片气泡:', meme.title);
            })(memeLinks[i]);
        }
    }
    mod._showMemeBubbles = _showMemeBubbles;

    // ======================== backoff reset ========================

    /**
     * 重置主动搭话退避级别 + 语音无回复计数，并 reschedule timer；
     * 同时通过 BroadcastChannel 广播，让所有窗口（包括 leader）同步 reset。
     * @param {Object} [opts]
     * @param {boolean} [opts._fromIpc] 标记本次调用源自 IPC 消息，避免回环广播。
     */
    function resetProactiveChatBackoff(opts) {
        // 重置退避级别
        S.proactiveChatBackoffLevel = 0;
        // 语音模式：用户说话了，重置无回复计数
        S._voiceProactiveNoResponseCount = 0;
        S._voiceProactiveBackoffResetVersion =
            (S._voiceProactiveBackoffResetVersion || 0) + 1;
        // 重新安排定时器
        scheduleProactiveChat();
        // 跨窗口同步：分发环境下 chat.html 输入只会 reset 它自己这份无用的 state，
        // proactive 真正的计时器在 index.html (leader)。广播 user_input_reset，
        // 让所有窗口（包括 leader）本地再跑一次 reset。_fromIpc 表示本次调用源自
        // IPC 消息，不再回广播，避免回环。
        if (!opts || !opts._fromIpc) {
            _proactiveBroadcast('user_input_reset');
        }
    }
    mod.resetProactiveChatBackoff = resetProactiveChatBackoff;

    // ======================== proactive vision during speech ========================

    var proactiveVisionFrameInFlight = false;

    /**
     * 发送单帧屏幕数据（统一使用 acquireOrReuseCachedStream → captureFrameFromStream → 后端兜底）
     */
    async function sendOneProactiveVisionFrame() {
        if (proactiveVisionFrameInFlight) return;
        proactiveVisionFrameInFlight = true;
        try {
            if (!isProactiveVisionEnabledNow() || !S.isRecording) {
                stopProactiveVisionDuringSpeech();
                return;
            }
            if (!S.socket || S.socket.readyState !== WebSocket.OPEN) return;

            var rememberedWindowCapture = { required: false, allowed: true };
            if (typeof window.prepareRememberedWindowCapture === 'function') {
                rememberedWindowCapture = await window.prepareRememberedWindowCapture();
                if (rememberedWindowCapture && rememberedWindowCapture.required
                    && !rememberedWindowCapture.allowed) {
                    console.warn('[ProactiveVision] 记忆窗口无法确认，停止本次语音视觉帧');
                    return;
                }
            }

            var dataUrl = null;
            // 这一帧来自哪种画面来源，决定 Avatar 坐标怎么映射到截图坐标系。
            // 三条来源判据各不相同且逐级回退，必须在取到画面的那一步就定下来——
            // 发送时的 S.screenCaptureStream / S.selectedScreenSourceId 描述的是
            // 「本会话配置抓什么」，不是「这一帧抓到了什么」。
            var captureType = null;

            // 优先前端流（缓存流 → Electron源 → 不弹窗）
            var stream = await acquireOrReuseCachedStream({ allowPrompt: false });
            if (stream) {
                // 同上：抓帧是异步的，源 ID 要跟这条流一起钉住，事后再读可能已经换人。
                // 必须在 acquireOrReuseCachedStream 之后取——它自己就会改写这个字段。
                var streamSourceId = S.selectedScreenSourceId;
                var frame = await captureFrameFromStream(stream, 0.8);
                if (frame && frame.dataUrl) {
                    dataUrl = frame.dataUrl;
                    captureType = window.detectScreenshotCaptureType(stream, streamSourceId);
                } else if (S.screenCaptureStream === stream) {
                    // 空帧（黑帧或空壳流），废弃缓存流
                    console.warn('[ProactiveVision] 缓存流提取帧失败，废弃该流');
                    try { stream.getTracks().forEach(function (t) { try { t.stop(); } catch (e) { } }); } catch (e) { }
                    S.screenCaptureStream = null;
                    S.screenCaptureStreamLastUsed = null;
                }
            }

            // Native desktop shells return encoded frames instead of a
            // Chromium MediaStream.
            if (!dataUrl && S.selectedScreenSourceId) {
                var desktopProvider = getDesktopProvider();
                if (desktopProvider
                    && desktopProvider.nativeFrameCapture
                    && typeof desktopProvider.captureSourceAsDataUrl === 'function') {
                    // 捕获前先钉住源 ID：下面隔着两个 await，期间用户换源会让
                    // S.selectedScreenSourceId 变掉，事后再读就会拿新源去解释旧帧
                    // （窗口换成屏幕 → 给一张没有 Avatar 的窗口图叠字）。
                    var nativeSourceId = S.selectedScreenSourceId;
                    try {
                        var direct = await window.captureDesktopSourceWithTimeout(
                            desktopProvider,
                            'captureSourceAsDataUrl',
                            nativeSourceId
                        );
                        if (direct && direct.success && direct.dataUrl) {
                            // 桌面壳的 NativeImage.toDataURL() 通常出 PNG，而后端屏幕数据
                            // 校验只接受 data:image/jpeg;base64,（utils/screenshot_utils.py:120），
                            // 不转码会让整帧被判为「无效的屏幕数据格式」直接丢掉。
                            var nativeDataUrl = await normalizeNativeCaptureDataUrlForStream(direct.dataUrl);
                            if (nativeDataUrl) {
                                dataUrl = nativeDataUrl;
                                captureType = window.detectScreenshotCaptureType(null, nativeSourceId);
                            } else {
                                console.warn('[ProactiveVision] 原生帧转 JPEG 失败，尝试后端兜底');
                            }
                        } else if (typeof window.maybeClearSourceOnNotFound === 'function') {
                            window.maybeClearSourceOnNotFound(direct, '主动视觉原生捕获源已失效');
                        }
                    } catch (directError) {
                        if (typeof window.maybeClearSourceOnNotFound === 'function') {
                            window.maybeClearSourceOnNotFound(
                                { error: directError && directError.message },
                                '主动视觉原生捕获源已失效'
                            );
                        }
                        console.warn('[ProactiveVision] 原生捕获失败，尝试后端兜底:', directError);
                    }
                }
            }

            // Remember-window is a fail-closed boundary. A validated window whose
            // stream/native capture failed must not widen to the full desktop.
            if (!dataUrl && rememberedWindowCapture
                && rememberedWindowCapture.required) {
                console.warn('[ProactiveVision] 记忆窗口捕获失败，停止整桌面兜底');
                return;
            }

            // 后端 pyautogui 兜底
            if (!dataUrl) {
                var backendResult = await fetchBackendScreenshot();
                dataUrl = backendResult.dataUrl;
                if (dataUrl) {
                    // pyautogui 抓的是整屏桌面，与已选窗口源 / 缓存流都无关，必须显式
                    // 定成 'screen'，否则会被残留的 window:* 源判成不叠加。
                    // 多屏下由 getAvatarScreenPosition 的闸门兜底返回 null。
                    captureType = 'screen';
                }
                // macOS 403 权限提示
                if (backendResult.status === 403 && !S.screenRecordingPermissionHintShown) {
                    S.screenRecordingPermissionHintShown = true;
                    if (typeof window.showStatusToast === 'function') {
                        window.showStatusToast(window.t ? window.t('app.screenRecordingPermissionDenied') : '\u26A0\uFE0F 屏幕录制权限未授权，请在系统设置中允许屏幕录制', 6000);
                    }
                    console.warn('[ProactiveVision] 后端截图返回 403，请在"系统设置 → 隐私与安全性 → 屏幕录制"中授权 N.E.K.O');
                }
            }

            if (!isProactiveVisionEnabledNow() || !S.isRecording) {
                stopProactiveVisionDuringSpeech();
                return;
            }
            if (rememberedWindowCapture && rememberedWindowCapture.required
                && typeof rememberedWindowCapture.isCurrent === 'function'
                && !rememberedWindowCapture.isCurrent()) {
                console.warn('[ProactiveVision] 记忆窗口身份已变化，丢弃过期语音视觉帧');
                return;
            }
            if (dataUrl && S.socket && S.socket.readyState === WebSocket.OPEN) {
                var visionInputType = (window.appUtils && window.appUtils.isMobile) ? (window.appUtils.isMobile() ? 'camera' : 'screen') : 'screen';
                // 与持续屏幕分享共用同一个构造函数，避免两条链路的 avatar_position 口径分叉。
                // 第 3 参传 null：来源已由 captureType 显式给出，不需要再按 sourceId 推断。
                S.socket.send(JSON.stringify(
                    buildStreamDataMessage(dataUrl, visionInputType, null, captureType)
                ));
                console.log('[ProactiveVision] 发送单帧屏幕数据');
            }
        } catch (e) {
            console.error('sendOneProactiveVisionFrame 失败:', e);
        } finally {
            proactiveVisionFrameInFlight = false;
        }
    }
    mod.sendOneProactiveVisionFrame = sendOneProactiveVisionFrame;

    function startProactiveVisionDuringSpeech() {
        // 如果已有定时器先清理
        if (S.proactiveVisionFrameTimer) {
            clearInterval(S.proactiveVisionFrameTimer);
            S.proactiveVisionFrameTimer = null;
        }

        // 主备协调：proactive vision 也由 Pet 主窗口负责，chat.html 不参与。
        // 否则两个窗口都会向后端推屏幕帧，带宽和 LLM 调用翻倍。
        if (!isProactiveLeader()) {
            console.log('[ProactiveVision] 当前不是 leader，跳过启动');
            return;
        }

        // 「请她离开」状态下禁止启动
        if (isGoodbyeActive()) {
            return;
        }

        // 仅在条件满足时启动：已开启主动视觉 && 正在录音 && 未手动屏幕共享
        if (!isProactiveVisionEnabledNow() || !S.isRecording) return;
        var screenButton = document.getElementById('screenButton');
        if (screenButton && screenButton.classList.contains('active')) return; // 手动共享时不启动

        S.proactiveVisionFrameTimer = setInterval(async function () {
            // 在每次执行前再做一次检查，避免竞态
            if (!isProactiveVisionEnabledNow() || !S.isRecording || isGoodbyeActive()) {
                stopProactiveVisionDuringSpeech();
                return;
            }
            // leader 切换的兜底：发帧前再核对一次
            if (!isProactiveLeader()) {
                stopProactiveVisionDuringSpeech();
                return;
            }

            // 如果手动开启了屏幕共享，重置计数器（即跳过发送）
            var sb = document.getElementById('screenButton');
            if (sb && sb.classList.contains('active')) {
                // do nothing this tick, just wait for next interval
                return;
            }

            await sendOneProactiveVisionFrame();
        }, S.proactiveVisionInterval * 1000);
    }
    mod.startProactiveVisionDuringSpeech = startProactiveVisionDuringSpeech;

    function stopProactiveVisionDuringSpeech() {
        if (S.proactiveVisionFrameTimer) {
            clearInterval(S.proactiveVisionFrameTimer);
            S.proactiveVisionFrameTimer = null;
        }
    }
    mod.stopProactiveVisionDuringSpeech = stopProactiveVisionDuringSpeech;

    function stopProactiveChatSchedule() {
        if (S.proactiveChatTimer) {
            clearTimeout(S.proactiveChatTimer);
            S.proactiveChatTimer = null;
        }
        if (S._voiceSessionInitialTimer) {
            clearTimeout(S._voiceSessionInitialTimer);
            S._voiceSessionInitialTimer = null;
        }
    }
    mod.stopProactiveChatSchedule = stopProactiveChatSchedule;

    // ======================== isWindowsOS ========================

    /**
     * 安全的Windows系统检测函数
     * 优先使用 navigator.userAgentData，然后 fallback 到 navigator.userAgent，最后才用已弃用的 navigator.platform
     * @returns {boolean} 是否为Windows系统
     */
    function isWindowsOS() {
        try {
            // 优先使用现代 API（如果支持）
            if (navigator.userAgentData && navigator.userAgentData.platform) {
                var platform = navigator.userAgentData.platform.toLowerCase();
                return platform.includes('win');
            }

            // Fallback 到 userAgent 字符串检测
            if (navigator.userAgent) {
                var ua = navigator.userAgent.toLowerCase();
                return ua.includes('win');
            }

            // 最后的兼容方案：使用已弃用的 platform API
            if (navigator.platform) {
                var plat = navigator.platform.toLowerCase();
                return plat.includes('win');
            }

            // 如果所有方法都不可用，默认返回false
            return false;
        } catch (error) {
            console.error('Windows检测失败:', error);
            return false;
        }
    }
    mod.isWindowsOS = isWindowsOS;

    // ======================== captureProactiveChatScreenshot ========================

    /**
     * 主动搭话截图函数
     * 优先级：
     *   0a. 复用有效缓存流（屏幕共享活跃时零成本）
     *   0b. 主进程 desktopCapturer 直接对选中源做快照（Electron 桌面 + 用户已选源；最可靠）
     *   1.  acquireOrReuseCachedStream（创建新流：Electron chromeMediaSourceId / getDisplayMedia）
     *   2.  后端 pyautogui 兜底
     *
     * 0b 解决聊天框截图按钮在 Electron 41/Win11 + useSystemPicker 下对窗口源总是
     * 返回整屏的问题；同时也改善此函数走 WS_HOOK / CHAT_CHANNELS.REQUEST_SCREENSHOT
     * 路径时的准确性。
     */
    /**
     * 把「这一帧是怎么抓来的」翻译成 Avatar 坐标的映射方式。
     *
     * 必须在**抓帧的那一刻**用当时的流 / 源算出来：抓帧是异步的，调用方事后再读
     * S.screenCaptureStream / S.selectedScreenSourceId 可能已经换人，会拿新源去
     * 解释旧帧。原生帧的调用点传 stream=null——那条缓存流描述的是另一次捕获，
     * 源被清空时会替原生帧答出 'screen'，给一张窗口图叠上不存在的 Avatar。
     */
    function resolveCaptureTypeFor(via, stream, sourceId) {
        // pyautogui 抓的是整屏桌面，与流 / 已选源都无关。
        if (via === 'backend') return 'screen';
        if (typeof window.detectScreenshotCaptureType === 'function') {
            return window.detectScreenshotCaptureType(stream, sourceId);
        }
        // detect 因加载顺序拿不到时的降级兜底：无流无源即整屏。这条不能删——
        // 后端把「不带坐标」当成明确的否定信号，删了会从「默认整屏叠」静默
        // 变成「永久不叠」。
        return (!stream && !sourceId) ? 'screen' : null;
    }

    async function captureProactiveChatScreenshotWithSource() {
        var rememberedWindowCapture = { required: false, allowed: true };
        if (typeof window.prepareRememberedWindowCapture === 'function') {
            rememberedWindowCapture = await window.prepareRememberedWindowCapture();
            if (rememberedWindowCapture && rememberedWindowCapture.required
                && !rememberedWindowCapture.allowed) {
                console.warn('[主动搭话截图] 记忆窗口无法唯一确认，停止本次截图');
                return { dataUrl: null, via: null, captureType: null };
            }
        }

        function rememberedCaptureStillCurrent() {
            return !rememberedWindowCapture
                || !rememberedWindowCapture.required
                || typeof rememberedWindowCapture.isCurrent !== 'function'
                || rememberedWindowCapture.isCurrent();
        }

        function discardSupersededRememberedFrame(path) {
            if (rememberedCaptureStillCurrent()) return false;
            console.warn('[主动搭话截图] ' + path + ' 完成时记忆窗口已变化，丢弃旧帧');
            return true;
        }

        // 策略 0a: 复用有效缓存流（避免打扰正在进行的屏幕共享）
        if (S.screenCaptureStream && S.screenCaptureStream.active) {
            try {
                var cachedStream = S.screenCaptureStream;
                var cachedSourceId = S.selectedScreenSourceId;
                var tracks = cachedStream.getVideoTracks();
                if (tracks.length > 0 && tracks.some(function (t) { return t.readyState === 'live'; })) {
                    var cachedFrame = await captureFrameFromStream(cachedStream, 0.85);
                    if (cachedFrame && cachedFrame.dataUrl) {
                        if (discardSupersededRememberedFrame('缓存流截图')) {
                            return { dataUrl: null, via: null, captureType: null };
                        }
                        S.screenCaptureStreamLastUsed = Date.now();
                        if (window.scheduleScreenCaptureIdleCheck) window.scheduleScreenCaptureIdleCheck();
                        console.log('[主动搭话截图] 缓存流截图成功');
                        return {
                            dataUrl: cachedFrame.dataUrl,
                            via: 'stream',
                            captureType: resolveCaptureTypeFor('stream', cachedStream, cachedSourceId)
                        };
                    }
                }
            } catch (e) { console.warn('[主动搭话截图] 缓存流截图失败，继续:', e); }
        }

        // 策略 0b: 桌面壳直接捕获选中源（Electron / Tauri）
        var desktopProvider = getDesktopProvider();
        if (S.selectedScreenSourceId && desktopProvider
            && typeof desktopProvider.captureSourceAsDataUrl === 'function') {
            // 捕获前钉住源 ID：下面是异步的，事后再读会拿新源解释旧帧。
            var nativeSourceId = S.selectedScreenSourceId;
            try {
                var direct = await window.captureDesktopSourceWithTimeout(
                    desktopProvider,
                    'captureSourceAsDataUrl',
                    nativeSourceId
                );
                if (direct && direct.success && direct.dataUrl) {
                    if (discardSupersededRememberedFrame('主进程直接捕获')) {
                        return { dataUrl: null, via: null, captureType: null };
                    }
                    console.log('[主动搭话截图] 主进程直接捕获成功:', nativeSourceId);
                    return {
                        dataUrl: direct.dataUrl,
                        via: 'native',
                        captureType: resolveCaptureTypeFor('native', null, nativeSourceId)
                    };
                } else if (direct && direct.error) {
                    console.warn('[主动搭话截图] 主进程直接捕获失败，将回退到流路径:', direct.error);
                    if (typeof window.maybeClearSourceOnNotFound === 'function') {
                        window.maybeClearSourceOnNotFound(direct, '主动搭话截图 capture-source-as-dataurl Source not found');
                    }
                }
            } catch (e) { console.warn('[主动搭话截图] 主进程直接捕获抛错，将回退到流路径:', e); }
        }

        // 策略1: 缓存流 / Electron窗口ID / getDisplayMedia（非user gesture不弹窗）
        var stream = await acquireOrReuseCachedStream({ allowPrompt: false });
        if (stream) {
            // 快照必须取在 acquireOrReuseCachedStream 之后——它自己就会改写这个字段。
            var streamSourceId = S.selectedScreenSourceId;
            var frame = await captureFrameFromStream(stream, 0.85);
            if (frame && frame.dataUrl) {
                if (discardSupersededRememberedFrame('前端流截图')) {
                    return { dataUrl: null, via: null, captureType: null };
                }
                console.log('[主动搭话截图] 前端截图成功');
                return {
                    dataUrl: frame.dataUrl,
                    via: 'stream',
                    captureType: resolveCaptureTypeFor('stream', stream, streamSourceId)
                };
            }
            // 黑帧或抓帧失败 → 废弃流，重试一次
            console.warn('[主动搭话截图] 帧提取失败或纯黑帧，废弃缓存流并重试');
            if (S.screenCaptureStream === stream) {
                try { stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) { }
                S.screenCaptureStream = null;
                S.screenCaptureStreamLastUsed = null;
            }
            // 重试：会走 Electron sourceId 路径
            stream = await acquireOrReuseCachedStream({ allowPrompt: false });
            if (stream) {
                var retrySourceId = S.selectedScreenSourceId;
                frame = await captureFrameFromStream(stream, 0.85);
                if (frame && frame.dataUrl) {
                    if (discardSupersededRememberedFrame('前端流重试截图')) {
                        return { dataUrl: null, via: null, captureType: null };
                    }
                    return {
                        dataUrl: frame.dataUrl,
                        via: 'stream',
                        captureType: resolveCaptureTypeFor('stream', stream, retrySourceId)
                    };
                }
                // 二次重试仍然失败，废弃这个流
                console.warn('[主动搭话截图] 二次重试仍失败，废弃流');
                if (S.screenCaptureStream === stream) {
                    try { stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) { }
                    S.screenCaptureStream = null;
                    S.screenCaptureStreamLastUsed = null;
                }
            }
        }

        // Remember-window is a fail-closed boundary. A validated window whose
        // direct/stream capture failed must not widen to a desktop screenshot.
        if (rememberedWindowCapture && rememberedWindowCapture.required) {
            console.warn('[主动搭话截图] 记忆窗口捕获失败，停止整桌面兜底');
            return { dataUrl: null, via: null, captureType: null };
        }

        // 策略2: 后端 pyautogui 兜底
        var backendResult = await fetchBackendScreenshot();
        if (backendResult.dataUrl) {
            console.log('[主动搭话截图] 后端截图成功');
            return {
                dataUrl: backendResult.dataUrl,
                via: 'backend',
                captureType: resolveCaptureTypeFor('backend', null, null)
            };
        }

        console.warn('[主动搭话截图] 所有截图方式均失败');
        return { dataUrl: null, via: null, captureType: null };
    }
    mod.captureProactiveChatScreenshotWithSource = captureProactiveChatScreenshotWithSource;

    // 旧契约薄封装：只要 dataUrl 的调用方继续用这个。
    async function captureProactiveChatScreenshot() {
        var res = await captureProactiveChatScreenshotWithSource();
        return res && res.dataUrl ? res.dataUrl : null;
    }
    mod.captureProactiveChatScreenshot = captureProactiveChatScreenshot;

    // ======================== acquireProactiveVisionStream ========================

    /**
     * 主动视觉开关切换时的流生命周期管理
     * 开启时：优先测试后端 pyautogui（静默无弹窗），不可用则通过前端流获取（用户手势上下文可弹 getDisplayMedia）
     */
    async function acquireProactiveVisionStream() {
        var rememberedWindowCapture = { required: false, allowed: true };
        if (typeof window.prepareRememberedWindowCapture === 'function') {
            rememberedWindowCapture = await window.prepareRememberedWindowCapture();
            if (rememberedWindowCapture && rememberedWindowCapture.required
                && !rememberedWindowCapture.allowed) {
                console.warn('[主动视觉] 记忆窗口无法确认，停止启用');
                return false;
            }
        }

        // 策略1: 无记忆窗口约束时测试后端 pyautogui（静默，无弹窗）
        if (!rememberedWindowCapture || !rememberedWindowCapture.required) {
            var backendResult = await fetchBackendScreenshot();
            if (backendResult.dataUrl) {
                console.log('[主动视觉] 后端 pyautogui 可用，无需前端流');
                return true;
            }
        }

        // 策略2: 后端不可用，尝试前端流（用户手势上下文，可弹 getDisplayMedia）
        var stream = await acquireOrReuseCachedStream({ allowPrompt: true });
        if (stream) {
            if (rememberedWindowCapture && rememberedWindowCapture.required
                && typeof rememberedWindowCapture.isCurrent === 'function'
                && !rememberedWindowCapture.isCurrent()) {
                console.warn('[主动视觉] 记忆窗口身份已变化，忽略过期流');
                return false;
            }
            console.log('[主动视觉] 前端流获取/复用成功');
            return true;
        }

        console.warn('[主动视觉] 无可用的截图方式');
        return false;
    }
    mod.acquireProactiveVisionStream = acquireProactiveVisionStream;

    // ======================== releaseProactiveVisionStream ========================

    function releaseProactiveVisionStream() {
        // 如果用户手动开启了屏幕共享，不要释放流
        var screenButton = document.getElementById('screenButton');
        if (screenButton && screenButton.classList.contains('active')) {
            console.log('[主动视觉] 手动屏幕共享活跃中，不释放流');
            return;
        }

        var privacyBlocksVision = !isProactiveVisionEnabledNow();

        // 如果正在录音（语音模式），流可能正在被使用，不释放
        if (S.isRecording && !privacyBlocksVision) {
            console.log('[主动视觉] 语音模式活跃中，不释放流');
            return;
        }

        // 如果主动搭话+主动视觉Chat仍活跃，保留流
        if (S.proactiveVisionChatEnabled && S.proactiveChatEnabled && !privacyBlocksVision) {
            console.log('[主动视觉] 主动搭话视觉仍活跃，不释放流');
            return;
        }

        if (S.screenCaptureStream) {
            try {
                if (typeof S.screenCaptureStream.getTracks === 'function') {
                    S.screenCaptureStream.getTracks().forEach(function (track) {
                        try { track.stop(); } catch (e) { }
                    });
                }
            } catch (e) {
                console.warn('[主动视觉] 停止 tracks 失败:', e);
            }
            S.screenCaptureStream = null;
            S.screenCaptureStreamLastUsed = null;
            if (S.screenCaptureStreamIdleTimer) {
                clearTimeout(S.screenCaptureStreamIdleTimer);
                S.screenCaptureStreamIdleTimer = null;
            }
            console.log('[主动视觉] 屏幕流已释放');
        }
    }
    mod.releaseProactiveVisionStream = releaseProactiveVisionStream;

    window.addEventListener('neko:home-tutorial-features-suppressed', function (event) {
        var detail = event && event.detail ? event.detail : {};
        if (detail.active === true) {
            _homeTutorialFeatureSuppressedByEvent = true;
            stopProactiveChatSchedule();
            stopProactiveVisionDuringSpeech();
        } else if (detail.active === false && S.proactiveChatEnabled && hasAnyChatModeEnabled()) {
            _homeTutorialFeatureSuppressedByEvent = false;
            scheduleProactiveChat();
        } else if (detail.active === false) {
            _homeTutorialFeatureSuppressedByEvent = false;
        }
    });

    // ======================== backward-compat window exports ========================

    window.hasAnyChatModeEnabled = hasAnyChatModeEnabled;
    window.resetProactiveChatBackoff = resetProactiveChatBackoff;
    window.stopProactiveChatSchedule = stopProactiveChatSchedule;
    window.startProactiveVisionDuringSpeech = startProactiveVisionDuringSpeech;
    window.stopProactiveVisionDuringSpeech = stopProactiveVisionDuringSpeech;
    window.acquireProactiveVisionStream = acquireProactiveVisionStream;
    window.releaseProactiveVisionStream = releaseProactiveVisionStream;
    window.scheduleProactiveChat = scheduleProactiveChat;
    window.isProactiveLeader = isProactiveLeader;
    window.captureCanvasFrame = captureCanvasFrame;
    window.fetchBackendScreenshot = fetchBackendScreenshot;
    window.scheduleScreenCaptureIdleCheck = scheduleScreenCaptureIdleCheck;
    window.captureProactiveChatScreenshot = captureProactiveChatScreenshot;
    window.isWindowsOS = isWindowsOS;
    window.getAvailablePersonalPlatforms = getAvailablePersonalPlatforms;

    // ======================== module export ========================

    window.appProactive = mod;
})();
