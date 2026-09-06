/**
 * app-state.js — 共享状态对象 & 常量
 * 所有 app-*.js 模块通过 window.appState (S) 和 window.appConst (C) 访问
 */
(function () {
    'use strict';

    function isDesktopLinuxX11Runtime() {
        return !!(window.__NEKO_DESKTOP_RUNTIME__ && window.__NEKO_DESKTOP_RUNTIME__.isLinuxX11);
    }

    const DEFAULT_RENDER_QUALITY = isDesktopLinuxX11Runtime() ? 'low' : 'medium';

    // ======================== 常量 ========================
    window.appConst = Object.freeze({
        HEARTBEAT_INTERVAL: 30000,           // WebSocket 心跳间隔 (ms)
        DEFAULT_MIC_GAIN_DB: 0,              // 麦克风增益默认值 (dB)
        MAX_MIC_GAIN_DB: 25,                 // 麦克风增益上限 (dB ≈ 18x)
        MIN_MIC_GAIN_DB: -5,                 // 麦克风增益下限 (dB ≈ 0.56x)
        DEFAULT_SPEAKER_VOLUME: 100,         // 扬声器默认音量
        DEFAULT_SPEAKER_DEVICE_ID: 'default', // Chromium 的系统默认多媒体输出（不是 communications）
        MAX_SPEAKER_VOLUME: 200,             // 扬声器音量上限（200% ≈ +6 dB 增益）
        SPEAKER_VOLUME_KNEE_RATIO: 0.75,     // 100% 锚点落在轨道 75% 处：前 3/4 给 0-100%，后 1/4 给 100-200% 增强区
        DEFAULT_SPATIAL_AUDIO_ENABLED: true, // 空间音频默认开启
        SPATIAL_AUDIO_MIN_GAIN: 0.4,         // 副屏远端最低音量保底（防止猫娘飞远后听不见）
        SPATIAL_AUDIO_MAX_PAN: 0.6,          // pan 绝对值上限（防止完全单声道，另一边留 ~31% 信号）
        SPATIAL_AUDIO_FALLOFF_RATE: 0.35,    // 超出主屏后每个 refDist 衰减比例
        SPATIAL_AUDIO_RAMP_SECONDS: 0.12,    // pan/gain 平滑过渡时长，避免突变 click
        SPATIAL_AUDIO_POLL_MS: 500,          // 位置轮询周期（兜底，事件驱动为主）
        DEFAULT_PROACTIVE_CHAT_INTERVAL: 15, // 默认搭话间隔 (秒)
        DEFAULT_PROACTIVE_VISION_INTERVAL: 10, // 默认视觉间隔 (秒)
        MAX_SCREENSHOT_WIDTH: 1280,
        MAX_SCREENSHOT_HEIGHT: 720,
        VOICE_TRANSCRIPT_MERGE_WINDOW: 3000, // 语音转录合并时间窗 (ms)
        SCREEN_IDLE_TIMEOUT: 5 * 60 * 1000, // 屏幕流闲置超时 (ms)
        SCREEN_CHECK_INTERVAL: 60 * 1000,    // 屏幕流检查间隔 (ms)
    });

    // ======================== 共享状态 ========================
    const S = {
        // --- DOM 元素引用 (init 时填充) ---
        dom: {},

        // --- Audio (播放) ---
        audioPlayerContext: null,
        globalAnalyser: null,
        speakerGainNode: null,
        audioBufferQueue: [],
        scheduledSources: [],
        isPlaying: false,
        scheduleAudioChunksRunning: false,
        scheduleAudioChunksTimer: null,
        audioStartTime: 0,
        nextChunkTime: 0,
        lipSyncActive: false,
        animationFrameId: null,
        seqCounter: 0,
        speakerVolume: 100,
        selectedSpeakerId: 'default',
        effectiveSpeakerId: 'default',
        selectedSpeakerAvailable: true,

        // --- Audio (空间音频，多屏立体声 + 距离衰减) ---
        spatialAudioEnabled: true,
        spatialPannerNode: null,         // StereoPannerNode：水平 L/R 定位
        spatialDistanceGainNode: null,   // GainNode：距离衰减
        spatialPollTimer: null,          // 位置轮询 timer 句柄
        spatialPrimaryDisplay: null,     // 缓存的主屏信息 { bounds, workArea }

        // --- Audio (打断/解码) ---
        interruptedSpeechId: null,
        currentPlayingSpeechId: null,
        currentPlayingSpeechCorrelationId: '',
        pendingDecoderReset: false,
        skipNextAudioBlob: false,
        incomingAudioBlobQueue: [],
        pendingAudioChunkMetaQueue: [],
        incomingAudioEpoch: 0,
        isProcessingIncomingAudioBlob: false,
        // 正在解码中的那个 blob 属于哪一轮。processIncomingAudioBlobQueue 是
        // 先 shift 出队再 await 解码，这段窗口里该 chunk 不在任何一个队列里，
        // 只有这个字段能证明"这一轮还有音频在路上"。缺了它，解码期间进来的
        // turn-end / source.onended 会把本轮判成已放完 → 提前收尾。
        processingAudioBlobTurnId: null,
        decoderResetPromise: null,

        // --- Audio (录音/麦克风) ---
        audioContext: null,
        workletNode: null,
        stream: null,
        micGainNode: null,
        inputAnalyser: null,
        selectedMicrophoneId: null,
        microphoneGainDb: 0,
        noiseReductionEnabled: true,
        independentAsrEnabled: false,
        // Current Core capability loaded from /api/config/core_api. Keep this
        // tri-state: only an explicit false may disable the effective UI;
        // null means unknown and leaves the user's persisted preference alone.
        coreApiProvider: '',
        coreApiSupportsIndependentAsr: null,
        voiceInputResourceOptimizationEnabled: true,
        // 设置是否已"水合"：server GET 合并成功或用户显式改过设置后才为 true。
        // 在此之前两个 true 都只是启动默认值，不代表服务器权威偏好；
        // independentAsrEnabled 尤其不能提前进入会话握手，
        // start_session 握手（app-websocket.js attachStartSessionHandshake）不得携带它，
        // 否则新浏览器 profile 首个会话会覆盖后端持久化的显式 false。
        settingsHydrated: false,
        // independentAsrEnabled 的按键权威位：settingsHydrated 在任何一次用户改
        // 设置时都会翻真，而那与 ASR 的值毫无关系。只有「server GET 合并成功」
        // 「用户显式改过 ASR 开关」「跨窗口 ASR 翻转」这三种事件才让它变权威；
        // 在此之前 start_session 握手必须省略该字段，由后端持久化值兜底。
        independentAsrAuthoritative: false,
        // 资源优化同样参与会话路由启动，必须独立证明该键来自 server merge、
        // 本窗口显式修改或可信的跨窗口修改，不能用启动默认值覆盖持久化选择。
        voiceInputResourceOptimizationAuthoritative: false,
        // 跨 popup generation 保存「下次会话生效」状态与当前会话的实际 ASR
        // route。否则跨窗口设置事件更新偏好后，重渲染会把偏好误报成当前 route。
        voiceSettingsPendingUntilEpoch: null,
        pendingVoiceRouteIndependentAsr: null,
        // 独立 ASR 已 fail-closed 的粘性标记：blocked 生命周期事件只发一次，
        // 而游戏 STT 网关持有麦克风时会跳过停麦，退出游戏的恢复路径必须据此
        // 拒绝把麦克风重新开到一条仍然关闭的路由上。
        voiceInputRouteBlocked: false,
        independentAsrActive: false,
        independentAsrProvider: '',
        externalAsrPreviewMessage: null, // 独立 ASR 实时转写预览的消息句柄（app-websocket.js 维护）
        pendingSettingsSyncPromise: null, // 设置同步 in-flight Promise（app-audio-capture.js 发布，ensureWebSocketOpen 等待）
        micVolumeAnimationId: null,
        silenceDetectionTimer: null,
        hasSoundDetected: false,
        isMicMuted: false,
        micLeaseOwner: 'none',
        voiceInputLifecycleState: 'off',
        gameRouteActive: false,
        gameRouteGameType: '',
        gameRouteLanlanName: '',
        gameRouteSessionId: '',
        gameRouteInstanceId: '',
        // Bounded, expiring route tombstones reject delayed STT-gate events
        // after multiple rapid route generations have already closed.
        // app-websocket prunes on record/check/open; page teardown releases
        // the array with the rest of this state object.
        gameRouteRecentlyEndedIdentities: [],
        gameRouteStateRevision: 0,
        gameVoiceSttGateActive: false,
        gameVoiceSttGameType: '',
        gameVoiceSttSessionId: '',
        gameVoiceSttRecognition: null,
        gameVoiceSttListening: false,
        gameVoiceSttStopping: false,
        gameVoiceSttRestartTimer: null,
        gameVoiceSttUnsupportedNotified: false,
        // Stable host-facing contract for mini-games. The game never derives
        // provider routing from free/paid labels; app-websocket publishes the
        // actual route selected by Core/independent ASR/browser fallback.
        gameVoiceTranscriptionMode: 'unavailable',
        gameVoiceTranscriptionProvider: '',
        gameVoiceTranscriptionReady: false,
        gameVoiceTranscriptionReason: 'route_inactive',
        proactiveChatWasStoppedByGameRoute: false,

        // --- 会话 / WebSocket ---
        socket: null,
        heartbeatInterval: null,
        autoReconnectTimeoutId: null,
        isRecording: false,
        voiceChatActive: false,
        voiceStartPending: false,
        isTextSessionActive: false,
        suppressAssistantStreamUntilNextSession: false,
        isSwitchingMode: false,
        sessionStartedResolver: null,
        sessionStartedRejecter: null,
        // 本次正在 await session_started 的启动请求模式（'audio' / 'text'）。
        // session_started 处理用它校验到达的 input_mode 是否与用户请求的一致：
        // 不一致（典型是 proactive/greeting 并发自起的 text 会话发来的 ack）时
        // 忽略，避免错误模式的 ack 收口用户的启动 promise / 翻转会话状态。
        _pendingSessionStartMode: null,
        // 本次 claim 的请求标识，随 start_session 发给后端、由 session_started
        // 原样带回。多窗口下 ack 会经 voice-lease fan-out 到达不是请求方的窗口
        // （抢麦的窗口会把 voice socket 换成自己的），标识就是「这条 ack 是不是
        // 在回应我」的唯一依据——没有它，一条为别人发出的 ack 会收口本窗口的
        // 启动 promise，而本窗口真正的 ack 到达时它已经放弃了。
        _pendingSessionStartRequestId: null,
        // 上一次 claim 的模式，释放后依然保留（_pendingSessionStartMode 会被清空）。
        // 用于判断"抢走槽位的那个启动是不是语音启动"——它可能已经 ack 完并释放，
        // 但仍然活着且正在驱动语音 UI，见 supersededByAudioStart。
        _lastSessionStartMode: null,
        voiceSessionStartEpoch: 0,
        assistantTurnId: null,
        assistantTurnStartedAt: 0,
        assistantPendingTurnServerId: null,
        assistantTurnAwaitingBubble: false,
        // 文本会话刚把 WS payload 发出去（text 和/或 screenshot），但 gemini_response
        // 还没回第一个 chunk 的那段空窗。用 ms 时间戳 + 15s 上限自我兜底，避免
        // 错过 clear 时永远卡 true。专门给 isAssistantTextResponseInFlight()
        // 用（_lastSubmittedRequestId 对纯截图请求会被故意清空，挡不住这段空窗）。
        pendingTextTurnSubmitAt: 0,
        assistantTurnSeq: 0,
        assistantTurnCompletedId: null,
        // 一轮干净收尾后（maybeFinalizeAssistantSpeech 成功），completedId 会被
        // clearAssistantTurnCompletion 清成 null，但 assistantTurnId 要等下条用户
        // 消息才清。没有这个 settled 标记的话，isAssistantTextResponseInFlight 的
        // turnMismatch（turnId !== completedId）在每条语音回复收尾后都恒为 true，
        // 切语音会干等满 15s。settledId 记下"这轮已收尾"，turn-start/cancel 时清。
        assistantTurnSettledId: null,
        assistantTurnCompletionSource: null,
        assistantSpeechActiveTurnId: null,
        assistantSpeechStartedTurnId: null,
        assistantSpeechPlaybackTurnId: null,
        assistantSpeechPlaybackStartAudioTime: 0,
        assistantSpeechPlaybackEndAudioTime: 0,
        // 后端声明"这一 speech 的音频流已关闭"之后记下的轮 id + 当时的 epoch。
        // 音频队列瞬时为空只能证明"此刻手里没有音频"，证明不了"后面不会再有"：
        // TTS 一阵一阵地到，阵间空档和真正的流结束在前端长得一模一样。所以收尾
        // 要等这个权威标记，或等 give-up 计时器到点。epoch 用来作废打断之后才
        // 迟到的信号（否则会去收尾一个已经被取消的轮）。
        assistantAudioStreamClosedTurnId: null,
        assistantAudioStreamClosedEpoch: -1,
        // speech_id → turnId。音频头只带 speech_id（后端 send_speech 不发 turn_id），
        // audio_done 也只能按 speech_id 对账，这里存下音频头到达时解析出的映射。
        // 随 close 标记一起清，所以条目数被限制在单轮之内。
        assistantAudioTurnBySpeechId: {},
        // 最近一次本地麦克风 RMS 超过语音阈值的时间戳（ms epoch）。
        // 由 app-audio-capture.js 里的 monitorInputVolume 持续写入；
        // app-proactive.js 在 voice 模式 tick 时用它判断"用户最近是否在发声"，
        // 与后端 _user_recent_activity_time 形成对称防线。
        userRecentSpeechTime: 0,

        // --- 屏幕共享 ---
        screenCaptureStream: null,
        screenCaptureStreamLastUsed: null,
        screenCaptureStreamIdleTimer: null,
        screenCaptureAutoPromptFailed: false,
        screenRecordingPermissionHintShown: false,
        selectedScreenSourceId: null,
        videoTrack: null,
        videoSenderInterval: null,

        // --- 主动搭话 ---
        proactiveChatEnabled: true,
        proactiveVisionEnabled: false,
        proactiveVisionChatEnabled: true,
        proactiveNewsChatEnabled: false,
        proactiveCommunityChatEnabled: false,
        proactiveVideoChatEnabled: true,
        proactivePersonalChatEnabled: false,
        proactiveMusicEnabled: true,
        proactiveMemeEnabled: true,
        proactiveMiniGameInviteEnabled: true,
        mergeMessagesEnabled: false,
        proactiveChatTimer: null,
        proactiveChatBackoffLevel: 0,
        // 屏幕专注态（gaming / focused_work，后端 propensity=restricted_screen_only）
        // 切到「固定间隔 + 后端抖动」调度：跳过 3-tier 退避，按 baseInterval
        // 等间隔触发，后端 /proactive_chat 入口注入 [0, 0.5×base] 的 sleep
        // 把实际间隔抹成 [base, 1.5×base] 均匀分布。由 /proactive_chat 响应里的
        // next_schedule_fixed_mode 字段控制开关；默认 false（即走常规退避）。
        proactiveFixedScheduleMode: false,
        _voiceProactiveNoResponseCount: 0,
        _voiceProactiveBackoffResetVersion: 0,
        _voiceSessionInitialTimer: null,
        isProactiveChatRunning: false,
        _proactiveSchedulerInitialized: false,
        _proactiveStartupDelayApplied: false,
        proactiveChatInterval: 15,
        proactiveVisionFrameTimer: null,
        proactiveVisionInterval: 10,
        _lastProactiveChatScreenTime: 0,

        // --- 角色切换 ---
        isSwitchingCatgirl: false,

        // --- UI / 杂项 ---
        focusModeEnabled: false,
        // 凝神（cognition focus）per-user 总开关，默认开；关掉后端进不了 focus 态。
        // 注意与上面的 focusModeEnabled（=麦克风静音/允许打断）是两回事。
        focusCognitionEnabled: true,
        avatarReactionBubbleEnabled: true,
        // 自然表达（slop reduction）总开关，默认开。promptOnly：仅改喂回模型的
        // 历史副本，用户看到的原文与持久化历史都不动（后端 utils/slop_filter.py）。
        slopFilterEnabled: true,
        renderQuality: DEFAULT_RENDER_QUALITY,
        targetFrameRate: 60,
        screenshotCounter: 0,
        statusToastTimeout: null,
        _statusToastPriority: 0,
        lastVoiceUserMessage: null,
        lastVoiceUserMessageTime: 0,

        // --- Agent ---
        agentMasterCheckbox: null,
        agentStateMachine: null,
    };

    window.appState = S;

    window.isNekoGoodbyeModeActive = function () {
        return !!(
            (window.live2dManager && window.live2dManager._goodbyeClicked)
            || (window.vrmManager && window.vrmManager._goodbyeClicked)
            || (window.mmdManager && window.mmdManager._goodbyeClicked)
        );
    };

    window.makeNekoSessionAbortError = function (reason) {
        var error = new Error(reason || 'Session aborted');
        error.sessionStartCancelled = true;
        error.voiceStartCancelled = true;
        return error;
    };

    // ---- voice-start slot ownership -------------------------------------
    //
    // S.sessionStartedResolver / Rejecter / _pendingSessionStartMode are ONE
    // shared slot, and concurrent starts genuinely exist: the mic button, the
    // composer's text send, the avatar-drop text entry and the automatic
    // reconnect restart can all be in flight together. Every flow used to
    // clear the slot unconditionally on its own way out, so whichever finished
    // first wiped whoever currently owned it -- the newer start then hung on a
    // promise nobody would ever settle, or had its timeout cancelled out from
    // under it.
    //
    // The owner token is the resolver function itself: it is already unique
    // per start and already in scope at every site that needs to check.
    // claim/release below are the only way a FLOW should touch the slot;
    // cancelPendingSessionStart stays deliberately unconditional because it is
    // the global "abandon whatever is pending" lever (goodbye, avatar drop,
    // character switch), where killing a foreign start is the intent.
    // An owner token answers "who holds the slot RIGHT NOW", and that is not
    // enough on its own: a start that claimed after us and has since finished
    // or been cancelled leaves the slot empty again -- byte for byte what our
    // own release looks like. Three separate review findings reduced to that
    // one blind spot. A claim sequence closes it, because it only ever moves
    // forward: "somebody claimed after me" survives their departure. The
    // WeakMap keeps it off the tokens themselves and out of GC's way.
    var startClaimSeq = 0;
    var startClaimSeqByOwner = new WeakMap();
    // The sequence of the last AUDIO claim, tracked separately because "was the
    // takeover a voice start" is not the same question as "what claimed last":
    // a text send can claim after a newer audio start that is still acquiring
    // its microphone, and the mode of the last claim then says 'text' while an
    // audio start is very much alive and holding the state the global unwind
    // destroys.
    var lastAudioClaimSeq = 0;
    // 每次 claim 一个新标识；按 owner 存，发送点只会读到自己那一个——被顶掉的
    // flow 即使还走到发送，也带的是它自己的标识，后端回的 ack 因此对不上当前
    // pending，不会误收口。窗口段随机，避免两个窗口的序号撞车。
    var startRequestIdSeq = 0;
    var startRequestIdWindowTag = Math.random().toString(36).slice(2, 10);
    var startRequestIdByOwner = new WeakMap();

    window.claimSessionStart = function (mode, resolve, reject) {
        // Whoever we are about to displace can no longer be settled by anything
        // else, so settle them HERE. Their acknowledgement is dropped by the
        // cross-mode guard in the session_started handler once our mode is the
        // pending one, and their 15s timeout is cancelled a few lines later by
        // the very flow that is claiming -- every claim setup clears the shared
        // window.sessionTimeoutId. A displaced start that nobody settles sits on
        // `await sessionStartPromise` forever, holding window.isMicStarting and
        // an active/disabled mic button straight through OUR session.
        //
        // A cancellation, not a failure: makeNekoSessionAbortError marks it so
        // the flows treat it as "abandoned", not "start failed".
        var displaced = S.sessionStartedRejecter;

        S.sessionStartedResolver = resolve;
        S.sessionStartedRejecter = reject;
        S._pendingSessionStartMode = mode;
        startClaimSeq += 1;
        startClaimSeqByOwner.set(resolve, startClaimSeq);
        startRequestIdSeq += 1;
        var requestId = startRequestIdWindowTag + '-' + startRequestIdSeq;
        startRequestIdByOwner.set(resolve, requestId);
        S._pendingSessionStartRequestId = requestId;
        if (mode === 'audio') lastAudioClaimSeq = startClaimSeq;
        // After the slot is ours, so anything the displaced flow does on its way
        // out already sees the new owner and stands down against it.
        if (displaced) {
            try {
                displaced(window.makeNekoSessionAbortError('Session start superseded by a newer start'));
            } catch (_) { }
        }
        // Sticky twin of _pendingSessionStartMode: which KIND of start claimed
        // last, still readable after it has released. supersededByAudioStart
        // needs it to decide whether the global voice-start unwind would land
        // on a live voice start, and by then the pending mode may be gone.
        S._lastSessionStartMode = mode;
        return resolve;
    };

    /** Release the slot only while ``owner`` still holds it. */
    window.releaseSessionStart = function (owner) {
        if (!owner || S.sessionStartedResolver !== owner) return false;
        S.sessionStartedResolver = null;
        S.sessionStartedRejecter = null;
        S._pendingSessionStartMode = null;
        S._pendingSessionStartRequestId = null;
        return true;
    };

    /**
     * The request id minted for ``owner``'s claim, for the send site to put on
     * the wire. Read it with your OWN owner token rather than off the shared
     * state: a flow displaced during its reconnect await would otherwise stamp
     * the newer start's id onto its own stale start_session.
     */
    window.sessionStartRequestId = function (owner) {
        return (owner && startRequestIdByOwner.get(owner)) || null;
    };

    /**
     * The current claim count, for a flow that must detect takeovers BEFORE it
     * has an owner token of its own -- the automatic restart spends 7.5s in a
     * timer before it claims, and a text session can be started and finished
     * whole inside that window. Snapshot this when the work is scheduled and
     * ask sessionStartsSince when it runs.
     */
    window.sessionStartClaimSeq = function () {
        return startClaimSeq;
    };

    /** True when any start has claimed since ``seq`` was taken. */
    window.sessionStartsSince = function (seq) {
        return startClaimSeq > seq;
    };

    /** True when an AUDIO start has claimed since ``seq`` was taken. */
    window.audioStartsSince = function (seq) {
        return lastAudioClaimSeq > seq;
    };

    /** True while ``owner`` is still the pending start. */
    window.sessionStartIsCurrent = function (owner) {
        return !!owner && S.sessionStartedResolver === owner;
    };

    /**
     * True when ANY start claimed after ``owner`` did -- whether it still holds
     * the slot, has completed, or was cancelled.
     *
     * Not "somebody else holds the slot now", which misses a completed takeover:
     * a text send can claim and be acknowledged inside an audio start's
     * getUserMedia await, releasing the slot before that start resumes, and the
     * stale start would then read an empty slot and go on to end the session and
     * rewrite the UI the text session is using. Not
     * `!sessionStartIsCurrent(owner)` either, which misses nothing but flags
     * everything: the normal success path releases the slot inside the ack
     * handler before settling the promise, so a start that simply succeeded also
     * resumes to an empty slot, and it must still clear the timeout it armed.
     * The claim sequence separates the two: it moved iff somebody else started.
     */
    window.sessionStartSuperseded = function (owner) {
        var seq = owner ? startClaimSeqByOwner.get(owner) : undefined;
        // An owner we never minted (or none at all): all we can say is whether
        // somebody is pending right now.
        if (seq === undefined) {
            return !!S.sessionStartedResolver && S.sessionStartedResolver !== owner;
        }
        return startClaimSeq > seq;
    };

    /**
     * True when the start that superseded ``owner`` is itself an AUDIO start.
     *
     * It matters because the superseded flow's unwind --
     * abortVoiceStartForBlockedRoute -- is GLOBAL: it bumps the mic generation
     * (invalidatePendingMicStart) and clears window.isMicStarting, which is
     * exactly the state a newer AUDIO start is relying on while it sits in
     * getUserMedia / addModule. Running it there makes that start abandon
     * capture and then fail its own ensureVoiceStartCurrent, leaving a session
     * the backend accepted with the microphone closed. A newer TEXT start
     * touches none of that state and leaves the voice-start UI stranded
     * instead, so there the unwind must still run.
     */
    window.supersededByAudioStart = function (owner) {
        var seq = owner ? startClaimSeqByOwner.get(owner) : undefined;
        // "Did an audio start claim after me", not "was the last claim audio".
        // With A superseded by an audio B that is still acquiring and then by a
        // text C, the last claim is C -- and unwinding on that verdict bumps the
        // mic generation out from under B, whose session the backend has already
        // accepted. Asking about audio claims after our own sequence keeps B
        // visible however many text starts follow it.
        if (seq === undefined) {
            // No token we minted: the sticky mode is all that is left. Callers
            // that must decide before they own anything pass their scheduling
            // snapshot to audioStartsSince instead.
            return window.sessionStartSuperseded(owner)
                && S._lastSessionStartMode === 'audio';
        }
        return lastAudioClaimSeq > seq;
    };

    /**
     * True while ``epoch`` is still the newest voice-start intent.
     *
     * Ownership cannot see an ABA: a newer start may claim the slot inside the
     * ack's 500ms deferred-resolution window and then be cancelled or complete,
     * leaving the slot back at EMPTY -- indistinguishable, to
     * sessionStartSuperseded, from "my own ack released it". The epoch can tell
     * them apart, because it only ever moves forward and only on a NEWER voice
     * intent: every mic-button press mints one, and cancelPendingSessionStart
     * -- the global abandon lever behind goodbye, avatar drop and character
     * switch -- bumps it. A flow that snapshots it when it claims can check
     * after its await whether the user has moved on.
     */
    window.voiceStartEpochIsCurrent = function (epoch) {
        return S.voiceSessionStartEpoch === epoch;
    };

    window.cancelPendingSessionStart = function (reason) {
        if (window.sessionTimeoutId) {
            clearTimeout(window.sessionTimeoutId);
            window.sessionTimeoutId = null;
        }
        S.voiceSessionStartEpoch += 1;
        S.voiceStartPending = false;
        window.isMicStarting = false;

        if (S.sessionStartedRejecter) {
            try {
                S.sessionStartedRejecter(window.makeNekoSessionAbortError(reason));
            } catch (_) { }
        }
        S.sessionStartedResolver = null;
        S.sessionStartedRejecter = null;
        S._pendingSessionStartMode = null;
        S._pendingSessionStartRequestId = null;
    };

    // ======================== 工具函数 ========================
    /** 分贝转线性增益 */
    function dbToLinear(db) {
        return Math.pow(10, db / 20);
    }
    /** 线性增益转分贝 */
    function linearToDb(linear) {
        return 20 * Math.log10(linear);
    }
    /** 画质 → 鼠标追踪性能等级映射 */
    function mapRenderQualityToFollowPerf(quality) {
        return quality === 'high' ? 'medium' : 'low';
    }
    /** 移动端检测 */
    function isMobile() {
        return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
    }
    /**
     * 带膝点的非线性滑块：轨道位置(0..1) → 数值。
     * knee 比例处映射到 base（标准锚点），右端到 max（增强区），左段与右段各自线性。
     */
    function kneeTrackToValue(pos, base, max, knee) {
        if (pos <= knee) return knee > 0 ? (pos / knee) * base : base;
        // knee >= 1 时无右段（增强区），整条轨道都是 [0, base]，膝点即终点
        return knee < 1 ? base + ((pos - knee) / (1 - knee)) * (max - base) : max;
    }
    /** kneeTrackToValue 的逆映射：数值 → 轨道位置(0..1)。 */
    function valueToKneeTrack(value, base, max, knee) {
        if (value <= base) return base > 0 ? (value / base) * knee : 0;
        // max <= base 时无增强区，超过 base 的值一律钉在轨道末端
        return max > base ? knee + ((value - base) / (max - base)) * (1 - knee) : 1;
    }

    window.appUtils = { dbToLinear, linearToDb, mapRenderQualityToFollowPerf, isMobile, kneeTrackToValue, valueToKneeTrack };

    // ======================== 向后兼容的全局双向绑定 ========================
    // 使用 defineProperty 使 window.xxx 始终和 S.xxx 同步
    const proactiveKeys = [
        'proactiveChatEnabled', 'proactiveVisionEnabled', 'proactiveVisionChatEnabled',
        'proactiveNewsChatEnabled', 'proactiveCommunityChatEnabled', 'proactiveVideoChatEnabled', 'proactivePersonalChatEnabled',
        'proactiveMusicEnabled', 'proactiveMemeEnabled', 'proactiveMiniGameInviteEnabled',
        'mergeMessagesEnabled', 'focusModeEnabled', 'focusCognitionEnabled',
        'proactiveChatInterval', 'proactiveVisionInterval', 'avatarReactionBubbleEnabled',
        'slopFilterEnabled',
        'renderQuality', 'targetFrameRate', 'isRecording',
    ];

    proactiveKeys.forEach(function (key) {
        // 先删除已有的简单赋值（如 window.proactiveChatEnabled = false）
        // 再用 getter/setter 桥接
        try { delete window[key]; } catch (_) { /* noop */ }
        Object.defineProperty(window, key, {
            get: function () { return S[key]; },
            set: function (v) { S[key] = v; },
            configurable: true,
            enumerable: true,
        });
    });

    // cursorFollowPerformanceLevel 由 renderQuality 派生
    Object.defineProperty(window, 'cursorFollowPerformanceLevel', {
        get: function () { return mapRenderQualityToFollowPerf(S.renderQuality); },
        set: function () { /* ignore — derived from renderQuality */ },
        configurable: true,
        enumerable: true,
    });

    // 音频全局同步辅助
    window.syncAudioGlobals = function () {
        window.audioPlayerContext = S.audioPlayerContext;
        window.globalAnalyser = S.globalAnalyser;
    };

    // 初始同步
    window.syncAudioGlobals();
})();
