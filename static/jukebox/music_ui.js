/**
 * Music UI Module
 * 职责：从 common-ui 分离出的所有音乐相关代码
 */
(function () {
    'use strict';

    // --- 集中配置中心 ---
    const MUSIC_CONFIG = {
        dom: {
            containerId: 'chat-container',
            insertBeforeId: 'text-input-area',
            barId: 'music-player-bar'
        },
        assets: {
            cssPath: '/static/libs/APlayer.min.css',
            jsPath: '/static/libs/APlayer.min.js',
            uiCssPath: '/static/css/music_ui.css',
            defaultCoverPath: '/static/assets/music/music-cover-placeholder.png'
        },
        themeColors: ['#667eea', '#764ba2', '#f093fb', '#f5576c', '#4facfe', '#00f2fe', '#a8edea', '#fed6e3'],
        primaryColor: '#667eea',
        secondaryColor: '#764ba2',
        // 主动分享音乐的兜底默认音量：刻意放得很低，避免突然冒出来的歌吓到人。
        // 用户手动调过的音量由 APlayer 自己持久化（aplayer-setting），会覆盖这个默认值。
        defaultVolume: 0.2,
        volumeStep: 0.05,
        // 自动销毁时长配置 (ms)
        timeouts: {
            ended: 21000,  // 自然播放结束
            idle: 24000,   // AI推荐未播放 (或被拦截)
            paused: 71000  // 用户点击暂停
        },
        // 标题超过「容器宽 × 该比例」时启用横向滚动（0.9~1 可调，便于微调观感）
        titleOverflowRatio: 1,
        // 域名白名单
        allowlist: [
            'i.scdn.co', 'p.scdn.co', 'a.scdn.co', 'i.imgur.com',
            'y.qq.com', 'u.y.qq.com', 'dl.stream.qqmusic.com', 'dl.stream.qqmusic.qq.com',
            'isure.stream.qqmusic.qq.com',
            'music.126.net', 'p1.music.126.net', 'p2.music.126.net', 'p3.music.126.net',
            'm7.music.126.net', 'm8.music.126.net', 'm9.music.126.net',
            'mmusic.spriteapp.cn', 'gg.spriteapp.cn',
            'freemusicarchive.org', 'musopen.org', 'bandcamp.com',
            'bcbits.com', 'soundcloud.com', 'sndcdn.com',
            'playback.media-streaming.soundcloud.cloud', 'api.soundcloud.com',
            'itunes.apple.com', 'audio-ssl.itunes.apple.com',
            'music.163.com',
            'hdslb.com', 'bilivideo.com'
        ]
    };
    // Only domains advertised by the backend may use the local music proxy.
    // Frontend-only plugin allowlist entries have not passed server-side SSRF checks.
    const backendProxyDomains = new Set(MUSIC_CONFIG.allowlist);
    // HTTP is allowed only for complete URLs explicitly advertised by a plugin.
    const pluginHttpUrls = new Set();
    const MAX_RECOMMENDED_TRACK_DURATION_SECONDS = 10 * 60;
    const MUSIC_MEDIA_LOAD_TIMEOUT_MS = 10000;

    const musicT = (key, fallback, params = {}) => {
        const fallbackText = String(fallback || key).replace(/\{\{(\w+)\}\}/g, (match, name) => (
            Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match
        ));
        if (typeof window.t === 'function') {
            try {
                const translated = window.t(key, Object.assign({}, params, { defaultValue: fallbackText }));
                if (typeof translated === 'string' && translated && translated !== key) return translated;
            } catch (_) { /* use the deterministic fallback below */ }
        }
        return fallbackText;
    };

    const applyMusicBarAccessibilityLabels = (musicBar) => {
        if (!musicBar) return;
        const playButton = musicBar.querySelector('.music-bar-play');
        const volumeButton = musicBar.querySelector('.music-bar-volume-btn');
        const closeButton = musicBar.querySelector('.music-bar-close');
        const playText = musicT('music.play', 'Play');
        const volumeText = musicT('music.volumeControl', 'Volume');
        const closeText = musicT('music.closePlayer', 'Close player');
        if (playButton) {
            playButton.setAttribute('title', playText);
            playButton.setAttribute('aria-label', playText);
        }
        if (volumeButton) {
            volumeButton.setAttribute('title', volumeText);
            volumeButton.setAttribute('aria-label', volumeText);
        }
        if (closeButton) {
            closeButton.setAttribute('title', closeText);
            closeButton.setAttribute('aria-label', closeText);
        }
    };

    const setMusicBarVisualState = (musicBar, state) => {
        if (!musicBar) return;
        musicBar.classList.remove('is-playing', 'is-paused', 'is-loading', 'is-error');
        musicBar.classList.add(`is-${state}`);
    };

    // --- CSS 注入（独立于 APlayer 库加载，follower 镜像 bar 也需要） ---
    const injectCSS = (path) => new Promise((res) => {
        if (!path) return res();
        if (document.querySelector(`link[href*="${path}"]`)) return res();
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = path;
        link.onload = () => { console.log('[Music UI] 样式加载成功:', path); res(); };
        link.onerror = () => { console.error('[Music UI] 样式加载失败，请检查路径:', path); res(); };
        document.head.appendChild(link);
    });

    let musicUiCssInjected = false;
    const ensureMusicUiCSS = () => {
        if (musicUiCssInjected) return;
        musicUiCssInjected = true;
        injectCSS(MUSIC_CONFIG.assets.uiCssPath);
    };

    let currentPlayingTrack = null;
    let currentMusicPlaybackId = null;
    let currentMusicOwnerStartedAt = 0;
    let localPlayer = null;
    let musicCardMessageId = null;
    let musicCardScopeKey = '';
    let musicCardState = '';
    let aplayerLoadPromise = null;
    let latestMusicRequestToken = 0;
    let currentMusicPlaybackContext = { source: '' };

    function setMusicPlaybackContext(options) {
        options = options || {};
        currentMusicPlaybackContext = {
            source: typeof options.source === 'string' ? options.source.slice(0, 16) : ''
        };
    }

    function getMusicLifecycleTimestamp() {
        if (
            typeof performance !== 'undefined'
            && Number.isFinite(performance.timeOrigin)
            && typeof performance.now === 'function'
        ) {
            return performance.timeOrigin + performance.now();
        }
        return Date.now();
    }

    function normalizeMusicEventTimestamp(event) {
        const eventTimestamp = Number(event && event.timeStamp);
        if (!Number.isFinite(eventTimestamp) || eventTimestamp <= 0) return null;
        // Modern browsers expose Event.timeStamp relative to performance.timeOrigin,
        // while older WebKit variants may already expose an epoch timestamp.
        if (eventTimestamp > 1e12) return eventTimestamp;
        if (
            typeof performance !== 'undefined'
            && Number.isFinite(performance.timeOrigin)
        ) {
            return performance.timeOrigin + eventTimestamp;
        }
        return null;
    }

    function createMusicPlaybackReportContext(playbackId, options, track, token) {
        options = options || {};
        track = track || {};
        return {
            playbackId: playbackId || '',
            token: token,
            lifecycleStartedAt: getMusicLifecycleTimestamp(),
            mediaReady: false,
            source: typeof options.source === 'string' ? options.source.slice(0, 16) : '',
            hasNextCandidate: options.hasNextCandidate === true,
            failureUiHandled: false,
            url: String(track.url || ''),
            track: {
                name: String(track.name || '').slice(0, 120),
                artist: String(track.artist || '').slice(0, 120)
            }
        };
    }

    function reportMusicPlaybackState(state, track, playbackContext, failureReason) {
        try {
            const appState = window.appState;
            const socket = appState && appState.socket;
            if (!socket || typeof socket.send !== 'function') return;
            if (socket.readyState !== 1 && typeof socket.readyState !== 'undefined') return;
            const context = playbackContext || createMusicPlaybackReportContext(
                getCurrentMusicPlaybackId(),
                currentMusicPlaybackContext,
                track,
                latestMusicRequestToken
            );
            const currentTrack = track || context.track || {};
            socket.send(JSON.stringify({
                action: 'music_playback_state',
                state: state,
                playback_id: context.playbackId,
                playback_window_id: MUSIC_COORD_SENDER_ID,
                playback_started_at: context.lifecycleStartedAt,
                source: context.source,
                reason: state === 'error'
                    ? String(failureReason || 'unknown').slice(0, 32)
                    : '',
                track: {
                    name: String(currentTrack.name || '').slice(0, 120),
                    artist: String(currentTrack.artist || '').slice(0, 120)
                }
            }));
            context.lastReportedState = state;
        } catch (_) { /* best-effort playback awareness */ }
    }

    function reportMusicPlaybackFailureIfNeeded(playbackContext, failureReason, deferFailureUi) {
        if (!playbackContext || playbackContext.lastReportedState === 'error') return;
        if (
            deferFailureUi
            && !['playing', 'paused'].includes(playbackContext.lastReportedState)
        ) return;
        reportMusicPlaybackState('error', null, playbackContext, failureReason);
    }

    function getOwnedMusicPlaybackReportContext(player, state) {
        const context = player && player._musicPlaybackReportContext;
        const audio = player && player.audio;
        if (!context || !audio) return null;
        if (context.token !== player._latestToken) return null;
        if (context.playbackId !== getCurrentMusicPlaybackId()) return null;
        const activeSource = audio.currentSrc || audio.src;
        if (
            context.url && activeSource
            && resolveMusicUrl(context.url) !== resolveMusicUrl(activeSource)
        ) return null;
        if (state === 'playing' && (audio.paused || audio.ended)) return null;
        if (state === 'paused' && (!audio.paused || audio.ended)) return null;
        if (state === 'ended' && !audio.ended) return null;
        return context;
    }

    // --- 竞态保护：dispatch 入口的"加载中"标记 ---
    // sendMusicMessage 的 URL 校验/库加载阶段对外暴露，避免并发 dispatch 在
    // 真正的 audio 还未启动时绕过 isMusicPlaying() 拦截。
    //
    // 用计数器而非 boolean：并发的 sendMusicMessage 调用各自 +1 / -1，
    // 谁先走早退分支都不会把尚在库加载中的兄弟调用误清为 idle。
    let musicDispatchPendingCount = 0;

    // --- 竞态保护：executePlay 串行化 ---
    // 两个并发 executePlay 在 await initializeAPlayer 期间会同时把
    // currentPlayingTrack / musicCardMessageId 覆盖一次，第一个实例还会
    // 残留一个未受控的 <audio>。用 Promise 链把它们排成单线。
    let executePlayChain = Promise.resolve();
    let pendingMusicMediaReadyCancel = null;

    // --- 跨窗口协调：当多个窗口（index.html + chat.html）同时开了主动搭话时，
    // 它们各自的播放器都会响应自己的 proactive_chat 响应。即使本地不在播，
    // 远程窗口可能正在播；用一个独立 BroadcastChannel 互相通报，
    // dispatchMusicPlay 在 source==='proactive' 时把远程视作"已占用"。 ---
    const MUSIC_COORD_SENDER_ID = (Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
    const REMOTE_MUSIC_TTL_MS = 30 * 1000; // 心跳超时
    const REMOTE_MUSIC_PENDING_TTL_MS = 12 * 1000;
    // 跨窗口音乐协调走桌面端主进程 IPC relay：compact 与 full 聊天窗处于隔离的
    // Electron partition（persist:neko-full-chat），BroadcastChannel/localStorage
    // 不跨 partition，无法保证「一次只有一个播放器 owner + 当前可见聊天窗镜像显示」。
    // 沿用 goodbye composer 同样的「partition 隔离 → 主进程 IPC」做法（N.E.K.O.-PC
    // 的 ipc-router 把事件 sender-aware 转发给其它窗口）。Web / 非 Electron 没有
    // IPC 时回落到 BroadcastChannel：同 session 多 tab 本就互通、无 partition 隔离。
    const MUSIC_BRIDGE_EVENT_NAME = 'neko:electron-music-bridge';
    const PEER_SURFACE_TTL_MS = 8 * 1000;
    const MUSIC_SURFACE_PULSE_MS = 2500;
    // sender_id -> expireAt
    const remoteMusicSenders = new Map();
    // sender_id -> { sender, mode, active, focused, visible, updatedAt, expireAt }
    // 对端上报的 surface 状态，用于在客户端复刻原服务端的 active_surface 仲裁
    // （谁该显示镜像播放器条）。
    const peerMusicSurfaces = new Map();
    let musicSurfacePulseTimer = null;
    let musicCoordChannel = null;

    // 桌面端主进程注入的音乐协调 IPC 桥（preload-common.js 的 setupMusicPlayerBridge）。
    // 存在即用 IPC（覆盖跨 partition 的 compact/full）；不存在则回落 BroadcastChannel。
    function getMusicIpcBridge() {
        const b = window.nekoElectronMusicBridge;
        return (b && typeof b.send === 'function') ? b : null;
    }
    try {
        // 有 IPC 桥时只走 IPC（覆盖跨 partition 的全部窗口），不再开 BroadcastChannel，
        // 否则同 partition 窗口（pet+compact）会同时收到 IPC 与 BC 两份、重复处理。
        if (typeof BroadcastChannel !== 'undefined' && !getMusicIpcBridge()) {
            musicCoordChannel = new BroadcastChannel('neko_music_coord');
            musicCoordChannel.onmessage = (event) => {
                const data = event && event.data;
                if (!data || typeof data !== 'object') return;
                const sid = data.sender;
                if (!sid || sid === MUSIC_COORD_SENDER_ID) return;
                if (data.type === 'music_started' || data.type === 'music_heartbeat') {
                    remoteMusicSenders.set(sid, Date.now() + REMOTE_MUSIC_TTL_MS);
                } else if (data.type === 'music_pending') {
                    remoteMusicSenders.set(sid, Date.now() + REMOTE_MUSIC_PENDING_TTL_MS);
                } else if (data.type === 'music_ended') {
                    if (sid === mirrorBarLeaderSender && !isCurrentMirrorPlayback(data)) return;
                    remoteMusicSenders.delete(sid);
                }
            };
            // 窗口关闭时通告一声 music_ended 并关闭 channel，避免对端等 30s TTL 才意识到我退出
            window.addEventListener('beforeunload', () => {
                try {
                    if (musicCoordChannel) {
                        musicCoordChannel.postMessage({
                            type: 'music_ended',
                            sender: MUSIC_COORD_SENDER_ID,
                            playbackId: getCurrentMusicPlaybackId(),
                            ts: Date.now()
                        });
                        musicCoordChannel.close();
                        musicCoordChannel = null;
                    }
                } catch (_) { /* ignore */ }
            });
        }
    } catch (e) {
        console.log('[Music UI] BroadcastChannel 不可用，跨窗口协调失效:', e);
    }

    const broadcastMusicCoord = (type) => {
        const playbackId = getCurrentMusicPlaybackId();
        if (musicCoordChannel) {
            try {
                musicCoordChannel.postMessage({ type, sender: MUSIC_COORD_SENDER_ID, playbackId, ts: Date.now() });
            } catch (_) { /* ignore */ }
        }
        postMusicPlayerBridgeEvent('coord', { coordType: type, playbackId });
    };

    // 心跳：当本地正在播时定期广播，防止其他窗口误以为对方已退出
    let musicHeartbeatTimer = null;
    const startMusicHeartbeat = () => {
        if (musicHeartbeatTimer) return;
        musicHeartbeatTimer = setInterval(() => {
            try {
                if (localPlayer && localPlayer.audio && !localPlayer.audio.paused) {
                    broadcastMusicCoord('music_heartbeat');
                } else {
                    stopMusicHeartbeat();
                }
            } catch (_) {
                stopMusicHeartbeat();
            }
        }, 10 * 1000);
    };
    const stopMusicHeartbeat = () => {
        if (musicHeartbeatTimer) {
            clearInterval(musicHeartbeatTimer);
            musicHeartbeatTimer = null;
        }
    };

    const isRemoteMusicActive = () => {
        const now = Date.now();
        if (remoteMusicSenders.size === 0) return false;
        // 顺手清理已过期的 sender
        for (const [sid, exp] of remoteMusicSenders) {
            if (now > exp) remoteMusicSenders.delete(sid);
        }
        return remoteMusicSenders.size > 0;
    };

    function createMusicPlaybackId(trackInfo, token) {
        const trackPart = trackInfo && trackInfo.url ? String(trackInfo.url).slice(0, 256) : 'unknown';
        return MUSIC_COORD_SENDER_ID + ':' + String(token || Date.now()) + ':' + trackPart;
    }

    function getCurrentMusicPlaybackId() {
        return currentMusicPlaybackId || '';
    }

    // --- 跨窗口 React 聊天镜像：把本窗口写入 reactChatWindowHost 的
    // append/update 动作同步广播给其他窗口，解决 PR #780 之后的问题 ——
    // proactive 聊天只在 leader（通常是 Pet/index.html）触发，后端下发的
    // 音乐卡片 / meme 气泡只会出现在 leader 的 React chat 里，chat.html
    // 作为 follower 不再发 /api/proactive_chat 也就收不到响应，它的 React
    // chat 里只能看到 WS 推的纯文字消息。通过这里的 mirror 把 leader 的
    // 卡片/气泡广播到 follower，让 chat.html 也能同步渲染。
    //
    // 不走 'neko_music_coord' 是为了职责分离：那个 channel 仅用于
    // "谁在播音乐/是否占用"的互斥协调，这里是 UI 层消息镜像，语义不同。
    const CHAT_MIRROR_CHANNEL_NAME = 'neko_chat_ui_mirror';
    let chatMirrorChannel = null;
    try {
        if (typeof BroadcastChannel !== 'undefined') {
            chatMirrorChannel = new BroadcastChannel(CHAT_MIRROR_CHANNEL_NAME);
            chatMirrorChannel.onmessage = (event) => {
                const data = event && event.data;
                if (!data || typeof data !== 'object') return;
                if (!data.sender || data.sender === MUSIC_COORD_SENDER_ID) return;
                const host = window.reactChatWindowHost;
                if (!host) return;
                try {
                    if (data.type === 'append' && typeof host.appendMessage === 'function' && data.message) {
                        host.appendMessage(data.message);
                    } else if (data.type === 'update' && typeof host.updateMessage === 'function' && data.messageId) {
                        host.updateMessage(data.messageId, data.patch || {});
                    }
                } catch (e) {
                    console.warn('[Music UI] 镜像 React chat 消息失败:', e);
                }
            };
            window.addEventListener('beforeunload', () => {
                try {
                    if (chatMirrorChannel) { chatMirrorChannel.close(); chatMirrorChannel = null; }
                } catch (_) { /* ignore */ }
            });
        }
    } catch (e) {
        console.log('[Music UI] chat mirror channel 不可用:', e);
    }

    const broadcastChatMirror = (payload) => {
        if (!chatMirrorChannel) return;
        try {
            chatMirrorChannel.postMessage(Object.assign({ sender: MUSIC_COORD_SENDER_ID, ts: Date.now() }, payload));
        } catch (_) { /* ignore */ }
    };

    // 对外暴露的"本地 append + 广播镜像"两合一 helper —— 供 app-proactive.js
    // 等下游在想让一条消息同时落到所有窗口的 React chat 时使用。
    // 下游不要直接调 reactChatWindowHost.appendMessage，否则就只在本窗口显示。
    const mirrorHostAppend = (host, message) => {
        if (!message) return;
        if (host && typeof host.appendMessage === 'function') {
            host.appendMessage(message);
        }
        broadcastChatMirror({ type: 'append', message: message });
    };
    const mirrorHostUpdate = (host, messageId, patch) => {
        if (!messageId) return;
        if (host && typeof host.updateMessage === 'function') {
            host.updateMessage(messageId, patch || {});
        }
        broadcastChatMirror({ type: 'update', messageId: messageId, patch: patch || {} });
    };
    window.__nekoMirrorChatAppend = mirrorHostAppend;
    window.__nekoMirrorChatUpdate = mirrorHostUpdate;

    // --- 跨窗口 player bar 镜像 ---
    // 继承 PR #780 "leader 独占 audio" 的设计：只有一个窗口持有 APlayer 实例
    // (localPlayer 非空即为 owner)，其他窗口通过 'neko_music_bar' 广播接收
    // leader 的 track/paused/currentTime/duration/volume，本地只渲染 DOM 不挂
    // <audio>，避免 #780 修过的"两首同响"竞态重现。
    //
    // follower 的 play/pause/volume/seek/close 通过同一 channel 发 ctrl 命令
    // 回 leader，由 leader 操作真正的 APlayer；这样 isMusicPlaying()、
    // is_playing_music 等 proactive 拦截输入始终指向 leader 真实状态，拦截语义
    // 不变。ctrl 'close' 还会触发 leader 的 destroyMusicPlayer，leader 随即
    // 广播 destroyed 让所有 follower 一起摘掉 bar。
    const MUSIC_BAR_CHANNEL_NAME = 'neko_music_bar';
    let musicBarChannel = null;
    let mirrorBarTrackSig = null; // follower 本地缓存 track 签名，用于判断是否切歌
    let mirrorBarDestroyTimer = null;
    // follower 记录当前镜像绑定的 leader sender id。所有 ctrl/destroyed 校验
    // 都要比对这个值，避免 leader 切换重叠时别的 owner 被误触发 pause/close。
    let mirrorBarLeaderSender = null;
    let mirrorBarLeaderSource = null;
    const processedBarCtrlIds = new Set();
    const processedBarCtrlOrder = [];

    function setMirrorBarLeader(sender, source) {
        const leaderChanged = sender !== mirrorBarLeaderSender;
        mirrorBarLeaderSender = sender || null;
        if (!sender) {
            mirrorBarLeaderSource = null;
            return;
        }
        if (source === 'broadcast' || leaderChanged || mirrorBarLeaderSource !== 'broadcast') {
            mirrorBarLeaderSource = source || null;
        }
    }

    function shouldSkipProcessedBarCtrl(ctrlId) {
        if (!ctrlId) return false;
        if (processedBarCtrlIds.has(ctrlId)) return true;
        processedBarCtrlIds.add(ctrlId);
        processedBarCtrlOrder.push(ctrlId);
        while (processedBarCtrlOrder.length > 120) {
            processedBarCtrlIds.delete(processedBarCtrlOrder.shift());
        }
        return false;
    }

    const isBarOwner = () => !!localPlayer;

    function getRemoteMusicStateTimestamp(state, eventTs) {
        const ts = Number(eventTs || (state && state.ts) || 0);
        return Number.isFinite(ts) ? ts : 0;
    }

    function acceptRemoteMusicOwnerState(sender, state, eventTs) {
        if (!isBarOwner()) return true;
        if (!sender || sender === MUSIC_COORD_SENDER_ID || !state || !state.track) return false;

        const remotePlaybackId = state.playbackId || '';
        if (remotePlaybackId && remotePlaybackId === getCurrentMusicPlaybackId()) return false;

        const remoteTs = getRemoteMusicStateTimestamp(state, eventTs);
        if (remoteTs && currentMusicOwnerStartedAt && remoteTs < currentMusicOwnerStartedAt) return false;

        // A newer remote owner wins; remove the local owner bar immediately so a mirror can replace it.
        destroyMusicPlayer(true, false, true);
        return true;
    }

    const broadcastBarState = (patch) => {
        const statePayload = Object.assign({}, patch || {});
        if (!statePayload.playbackId) statePayload.playbackId = getCurrentMusicPlaybackId();
        const message = Object.assign({
            type: 'state',
            sender: MUSIC_COORD_SENDER_ID,
            ts: Date.now()
        }, statePayload);
        if (musicBarChannel) {
            try {
                musicBarChannel.postMessage(message);
            } catch (_) { /* ignore */ }
        }
        postMusicPlayerBridgeEvent('bar_state', statePayload);
    };

    const broadcastBarDestroyed = (fullTeardown, playbackIdOverride) => {
        const playbackId = playbackIdOverride || getCurrentMusicPlaybackId();
        const message = {
            type: 'destroyed',
            sender: MUSIC_COORD_SENDER_ID,
            ts: Date.now(),
            playbackId: playbackId,
            fullTeardown: !!fullTeardown
        };
        if (musicBarChannel) {
            try {
                musicBarChannel.postMessage(message);
            } catch (_) { /* ignore */ }
        }
        postMusicPlayerBridgeEvent('bar_destroyed', { fullTeardown: !!fullTeardown, playbackId: playbackId });
    };

    const broadcastBarCtrl = (action, value) => {
        // 没绑到 leader（镜像 bar 没建或已 teardown）就不发 ctrl
        if (!mirrorBarLeaderSender) return;
        const ctrlId = MUSIC_COORD_SENDER_ID + ':' + Date.now().toString(36) + ':' + Math.random().toString(36).slice(2, 8);
        const message = {
            type: 'ctrl',
            sender: MUSIC_COORD_SENDER_ID,
            // target 指明给哪个 leader——owner 侧校验 target 才执行，避免
            // 非当前绑定的 leader 也一起 pause/seek/close
            target: mirrorBarLeaderSender,
            ts: Date.now(),
            ctrlId: ctrlId,
            action: action,
            value: value
        };
        if (musicBarChannel) {
            try {
                musicBarChannel.postMessage(message);
            } catch (_) { /* ignore */ }
        }
        postMusicPlayerBridgeEvent('bar_ctrl', {
            target: mirrorBarLeaderSender,
            ctrlId: ctrlId,
            action: action,
            value: value
        });
    };

    const computeCurrentBarState = () => {
        if (!localPlayer) return null;
        const audio = localPlayer.audio;
        return {
            track: currentPlayingTrack ? {
                name: currentPlayingTrack.name,
                artist: currentPlayingTrack.artist,
                cover: currentPlayingTrack.cover,
                url: currentPlayingTrack.url
            } : null,
            paused: audio ? !!audio.paused : true,
            ended: audio ? !!audio.ended : false,
            currentTime: audio ? (audio.currentTime || 0) : 0,
            duration: audio && isFinite(audio.duration) ? (audio.duration || 0) : 0,
            volume: (typeof localPlayer.volume === 'function') ? (localPlayer.volume() || 0) : 0,
            loadError: !!localPlayer._loadError,
            playbackId: getCurrentMusicPlaybackId()
        };
    };

    const emitBarState = () => {
        const state = computeCurrentBarState();
        if (!state) return;
        broadcastBarState(state);
    };

    // timeupdate 会以 ~4Hz 触发，限速到 500ms 一条，避免 IPC 噪声
    let lastBarTickTs = 0;
    const emitBarStateThrottled = (minIntervalMs) => {
        const now = Date.now();
        const gap = typeof minIntervalMs === 'number' ? minIntervalMs : 500;
        if (now - lastBarTickTs < gap) return;
        lastBarTickTs = now;
        emitBarState();
    };

    // track 尚未进 APlayer（executePlay 已写 currentPlayingTrack 但 localPlayer
    // 还在 await 库加载）阶段，也给 follower 一个先期占位状态，免得它先接到
    // ended 再接到新歌的 state 空窗。
    const emitBarInitialState = (trackInfo) => {
        if (!trackInfo) return;
        broadcastBarState({
            track: {
                name: trackInfo.name,
                artist: trackInfo.artist,
                cover: trackInfo.cover,
                url: trackInfo.url
            },
            paused: true,
            ended: false,
            currentTime: 0,
            duration: 0,
            volume: MUSIC_CONFIG.defaultVolume,
            loadError: false,
            playbackId: getCurrentMusicPlaybackId(),
            initial: true
        });
    };

    let musicMountObserver = null;
    let musicMountRelocationFrame = 0;
    let musicMountRelocationRetryTimer = 0;
    let musicMountRelocationRetryCount = 0;
    let musicMountGeometryFrame = 0;
    let pendingDetachedMusicBar = null;
    const MUSIC_MOUNT_RELOCATION_MAX_RETRIES = 6;

    function getCompactMusicMountTarget() {
        const compactMount = document.querySelector('[data-music-player-mount="compact-surface"]');
        return compactMount instanceof Element ? compactMount : null;
    }

    function getFullMusicMountTarget() {
        const mounts = document.querySelectorAll('#music-player-mount');
        for (const mount of mounts) {
            if (mount instanceof Element && mount.getAttribute('data-music-player-mount') !== 'compact-surface') {
                return mount;
            }
        }
        return null;
    }

    function getMountSurfaceMode(mount) {
        if (!(mount instanceof Element)) return '';
        const surfaceNode = mount.closest && mount.closest('[data-chat-surface-mode]');
        if (surfaceNode && surfaceNode.getAttribute) {
            const mode = surfaceNode.getAttribute('data-chat-surface-mode');
            if (mode === 'compact' || mode === 'full' || mode === 'minimized') return mode;
        }
        if (mount.getAttribute('data-music-player-mount') === 'compact-surface') return 'compact';
        return '';
    }

    function getRenderedChatMusicSurfaceMode() {
        const compactMount = getCompactMusicMountTarget();
        const fullMount = getFullMusicMountTarget();
        const compactMode = getMountSurfaceMode(compactMount);
        const fullMode = getMountSurfaceMode(fullMount);

        if (compactMode === 'minimized' || fullMode === 'minimized') return 'minimized';
        if (fullMode === 'full' && compactMode !== 'compact') return 'full';
        if (compactMode === 'compact' && fullMode !== 'full') return 'compact';
        if (fullMount && !compactMount && !fullMode) return 'full';
        if (compactMount && !fullMount && !compactMode) return 'compact';
        return '';
    }

    function getChatMusicSurfaceMode() {
        const renderedMode = getRenderedChatMusicSurfaceMode();
        if (renderedMode) return renderedMode;

        try {
            if (typeof window.getNekoChatMusicSurfaceState === 'function') {
                const state = window.getNekoChatMusicSurfaceState();
                if (state && state.mode) return String(state.mode);
            }
        } catch (_) { /* ignore */ }

        try {
            const host = window.reactChatWindowHost;
            if (host && typeof host.getChatSurfaceMode === 'function') {
                const mode = host.getChatSurfaceMode();
                if (mode) return String(mode);
            }
        } catch (_) { /* ignore */ }

        const shell = document.getElementById('react-chat-window-shell');
        if (shell && shell.getAttribute) {
            const mode = shell.getAttribute('data-chat-surface-mode');
            if (mode) return mode;
        }

        const body = document.body;
        if (body && body.getAttribute) {
            const declaredMode = body.getAttribute('data-initial-chat-surface-mode');
            if (declaredMode) return declaredMode;
        }

        try {
            const path = (window.location && window.location.pathname) || '';
            if (path === '/chat_full') return 'full';
            if (path === '/chat') return 'compact';
        } catch (_) { /* ignore */ }

        if (getCompactMusicMountTarget()) return 'compact';
        if (getFullMusicMountTarget()) return 'full';
        return '';
    }

    function isEmptyChatMusicMount(element) {
        return !!(element && element.id === 'music-player-mount' && !element.firstChild);
    }

    function isElementInHiddenTree(element) {
        let node = element;
        while (node && node instanceof Element) {
            if (node.hidden) return true;
            if (node.getAttribute('aria-hidden') === 'true') return true;
            if (node.getAttribute('data-compact-music-player-visibility') === 'closed') return true;
            try {
                const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
                const emptyMountHiddenByCss = node === element
                    && isEmptyChatMusicMount(node)
                    && (!node.style || node.style.display !== 'none');
                if (style && (
                    (style.display === 'none' && !emptyMountHiddenByCss)
                    || style.visibility === 'hidden'
                    || style.visibility === 'collapse'
                )) {
                    return true;
                }
            } catch (_) { /* ignore */ }
            node = node.parentElement;
        }
        return false;
    }

    function hasUsableLocalChatMusicSurface() {
        const mode = getChatMusicSurfaceMode();
        if (mode === 'minimized') return false;
        const mount = mode === 'compact'
            ? getCompactMusicMountTarget()
            : (mode === 'full' ? getFullMusicMountTarget() : (getFullMusicMountTarget() || getCompactMusicMountTarget()));
        if (!mount || isElementInHiddenTree(mount)) return false;

        const overlay = document.getElementById('react-chat-window-overlay');
        if (overlay && isElementInHiddenTree(overlay)) return false;
        return true;
    }

    function isLocalChatMusicSurfaceFocused() {
        if (!hasUsableLocalChatMusicSurface()) return false;
        try {
            if (document.visibilityState === 'hidden') return false;
        } catch (_) { /* ignore */ }
        try {
            return typeof document.hasFocus === 'function' ? document.hasFocus() : true;
        } catch (_) {
            return true;
        }
    }

    function isDedicatedChatMusicSurface() {
        try {
            const path = (window.location && window.location.pathname) || '';
            if (path === '/chat' || path === '/chat_full') return true;
        } catch (_) { /* ignore */ }
        const body = document.body;
        return !!(body && body.classList && body.classList.contains('electron-chat-window'));
    }

    function getMusicBridgeSurfaceState(reason) {
        const active = isLocalChatMusicSurfaceFocused();
        return {
            mode: getChatMusicSurfaceMode() || 'unknown',
            active: active,
            focused: active,
            visible: hasUsableLocalChatMusicSurface(),
            reason: reason || ''
        };
    }

    // --- 客户端 active surface 仲裁（替代原服务端 _choose_music_player_active_surface）---
    // 每个窗口周期性 pulse 自己的 surface 状态，其它窗口收到后存入 peerMusicSurfaces。
    // 判断「本窗口是否该显示镜像条」时，在 {本地 live surface} ∪ {对端 surface} 里
    // 取最强者：本地胜/平 → 显示；对端严格胜 → 让位。排序键纯由 surface 权重 + sender id
    // 决定（确定性，跨窗口一致），不掺本地时间戳，避免各窗口都以为自己最新而同时渲染。
    const MUSIC_SURFACE_MODE_WEIGHT = { full: 4, compact: 3, web: 2, pet: 1, unknown: 0, minimized: -1 };

    function updatePeerMusicSurface(sender, surface) {
        if (!sender || sender === MUSIC_COORD_SENDER_ID) return;
        const s = surface || {};
        const active = !!s.active;
        const focused = !!(s.focused || active);
        peerMusicSurfaces.set(sender, {
            sender: sender,
            mode: typeof s.mode === 'string' ? s.mode : 'unknown',
            active: active,
            focused: focused,
            visible: !!(s.visible || active || focused),
            updatedAt: Date.now(),
            expireAt: Date.now() + PEER_SURFACE_TTL_MS
        });
    }

    function purgePeerMusicSurfaces(now) {
        for (const [sid, s] of peerMusicSurfaces) {
            if ((s.expireAt || 0) <= now) peerMusicSurfaces.delete(sid);
        }
    }

    function musicSurfaceWeights(s) {
        return [
            s.active ? 2 : (s.focused ? 1 : 0),
            s.visible ? 1 : 0,
            MUSIC_SURFACE_MODE_WEIGHT[s.mode] || 0
        ];
    }

    // 返回 >0 表示 a 胜出。tiebreak 用 sender id 字典序，保证各窗口一致地选出同一个赢家。
    function compareMusicSurface(a, b) {
        const wa = musicSurfaceWeights(a), wb = musicSurfaceWeights(b);
        for (let i = 0; i < wa.length; i++) {
            if (wa[i] !== wb[i]) return wa[i] - wb[i];
        }
        if (a.sender === b.sender) return 0;
        return a.sender > b.sender ? 1 : -1;
    }

    function hasBridgeActiveChatMusicSurface() {
        const now = Date.now();
        purgePeerMusicSurfaces(now);
        const mine = getMusicBridgeSurfaceState('arbitrate');
        let winner = (mine.active || mine.focused || mine.visible)
            ? { sender: MUSIC_COORD_SENDER_ID, mode: mine.mode, active: mine.active, focused: mine.focused, visible: mine.visible }
            : null;
        for (const [sid, s] of peerMusicSurfaces) {
            if (!(s.active || s.focused || s.visible)) continue;
            if (!winner || compareMusicSurface(s, winner) > 0) {
                winner = { sender: sid, mode: s.mode, active: s.active, focused: s.focused, visible: s.visible };
            }
        }
        // 没有任何可用 surface（含本地）→ 无 active surface 信息，不拦截本地渲染。
        if (!winner) return false;
        // 赢家是对端 → 让对端显示，本窗口不渲染镜像条。
        return winner.sender !== MUSIC_COORD_SENDER_ID;
    }

    function shouldRenderMusicBarInThisSurface() {
        if (isLocalChatMusicSurfaceFocused()) return true;
        if (hasBridgeActiveChatMusicSurface()) return false;
        if (isDedicatedChatMusicSurface()) return hasUsableLocalChatMusicSurface();
        return hasUsableLocalChatMusicSurface();
    }

    function getPreferredMusicMountTarget(options = {}) {
        const renderHere = shouldRenderMusicBarInThisSurface();
        const allowInactiveOwner = !!options.allowInactiveOwner;
        if (!renderHere && !allowInactiveOwner) return { mountTarget: null, insertBeforeEl: null };

        const mode = getChatMusicSurfaceMode();
        let reactMount = null;
        if (mode === 'compact') reactMount = getCompactMusicMountTarget();
        else if (mode === 'full') reactMount = getFullMusicMountTarget();

        if (!reactMount && mode !== 'compact') reactMount = getFullMusicMountTarget();
        if (!reactMount && mode !== 'full') reactMount = getCompactMusicMountTarget();
        if (reactMount) return { mountTarget: reactMount, insertBeforeEl: null };

        const legacyMount = document.getElementById(MUSIC_CONFIG.dom.containerId);
        return {
            mountTarget: legacyMount,
            insertBeforeEl: legacyMount ? document.getElementById(MUSIC_CONFIG.dom.insertBeforeId) : null
        };
    }

    function requestCompactMusicGeometrySync() {
        if (musicMountGeometryFrame) return;
        const raf = window.requestAnimationFrame || ((callback) => window.setTimeout(callback, 16));
        musicMountGeometryFrame = raf(() => {
            musicMountGeometryFrame = 0;
            window.dispatchEvent(new CustomEvent('neko:compact-interaction-geometry-refresh'));
        });
    }

    function clearMusicMountRelocationRetry() {
        if (musicMountRelocationRetryTimer) {
            window.clearTimeout(musicMountRelocationRetryTimer);
            musicMountRelocationRetryTimer = 0;
        }
        musicMountRelocationRetryCount = 0;
    }

    function scheduleMusicBarRelocationRetry() {
        if (musicMountRelocationRetryTimer) return;
        if (musicMountRelocationRetryCount >= MUSIC_MOUNT_RELOCATION_MAX_RETRIES) return;
        musicMountRelocationRetryCount += 1;
        const delay = Math.min(320, 40 + musicMountRelocationRetryCount * 40);
        musicMountRelocationRetryTimer = window.setTimeout(() => {
            musicMountRelocationRetryTimer = 0;
            scheduleMusicBarRelocation();
        }, delay);
    }

    function prepareMusicBarHitRegion(musicBar) {
        if (!musicBar) return;
        musicBar.setAttribute('data-compact-hit-region', 'true');
        musicBar.setAttribute('data-compact-hit-region-id', 'music-player');
        musicBar.setAttribute('data-compact-hit-region-kind', 'music');
    }

    function mountMusicBar(musicBar) {
        if (!musicBar) return false;
        ensureMusicMountObserver();
        if (musicBar.dataset) delete musicBar.dataset.skipMountRelocation;
        prepareMusicBarHitRegion(musicBar);
        const isMirrorBar = !!(musicBar.dataset && musicBar.dataset.mirror === 'true');
        const renderHere = shouldRenderMusicBarInThisSurface();
        const target = getPreferredMusicMountTarget({ allowInactiveOwner: !isMirrorBar });
        if (!target || !target.mountTarget) {
            musicBar.hidden = true;
            requestCompactMusicGeometrySync();
            return false;
        }
        musicBar.hidden = !renderHere;
        if (musicBar.parentNode === target.mountTarget) {
            requestCompactMusicGeometrySync();
            return true;
        }
        if (target.insertBeforeEl && target.insertBeforeEl.parentNode === target.mountTarget) {
            target.mountTarget.insertBefore(musicBar, target.insertBeforeEl);
        } else {
            target.mountTarget.appendChild(musicBar);
        }
        requestCompactMusicGeometrySync();
        return true;
    }

    function removeMusicBarWithoutRelocation(musicBar) {
        if (!musicBar) return;
        if (musicBar.dataset) musicBar.dataset.skipMountRelocation = 'true';
        musicBar.remove();
        requestCompactMusicGeometrySync();
    }

    function findMusicBarInNode(node) {
        if (!(node instanceof Element)) return null;
        const musicBar = node.id === MUSIC_CONFIG.dom.barId
            ? node
            : (node.querySelector ? node.querySelector('#' + MUSIC_CONFIG.dom.barId) : null);
        if (musicBar && musicBar.dataset && musicBar.dataset.skipMountRelocation === 'true') return null;
        return musicBar;
    }

    function isMusicMountMutationNode(node) {
        if (!(node instanceof Element)) return false;
        if (node.id === 'music-player-mount' || node.getAttribute('data-music-player-mount') === 'compact-surface') return true;
        if (node.id === 'react-chat-window-shell' || node.id === 'react-chat-window-overlay') return true;
        return !!(node.querySelector && node.querySelector('#music-player-mount, [data-music-player-mount="compact-surface"], #react-chat-window-shell, #react-chat-window-overlay'));
    }

    function isMusicMountMutationTarget(node) {
        if (!(node instanceof Element)) return false;
        return node.id === 'music-player-mount'
            || node.id === 'react-chat-window-shell'
            || node.id === 'react-chat-window-overlay'
            || node.getAttribute('data-music-player-mount') === 'compact-surface';
    }

    function isCompactMusicGeometryMutationTarget(node) {
        if (!(node instanceof Element)) return false;
        const compactMusicMount = node.closest && node.closest('[data-music-player-mount="compact-surface"]');
        if (!compactMusicMount) return false;
        return node.classList.contains('music-bar-volume-container')
            || node.classList.contains('music-bar-volume-slider-wrapper');
    }

    function scheduleMusicBarRelocation(detachedMusicBar) {
        if (detachedMusicBar) {
            pendingDetachedMusicBar = detachedMusicBar;
            clearMusicMountRelocationRetry();
        }
        if (musicMountRelocationFrame) return;
        const raf = window.requestAnimationFrame || ((callback) => window.setTimeout(callback, 16));
        musicMountRelocationFrame = raf(() => {
            musicMountRelocationFrame = 0;
            const musicBar = pendingDetachedMusicBar || document.getElementById(MUSIC_CONFIG.dom.barId);
            if (musicBar) {
                const mounted = mountMusicBar(musicBar);
                if (mounted || musicBar.isConnected) {
                    pendingDetachedMusicBar = null;
                    clearMusicMountRelocationRetry();
                } else {
                    pendingDetachedMusicBar = musicBar;
                    scheduleMusicBarRelocationRetry();
                }
            } else if (!isBarOwner() && mirrorBarLastState) {
                if (renderMirrorBar(mirrorBarLastState)) {
                    clearMusicMountRelocationRetry();
                } else {
                    scheduleMusicBarRelocationRetry();
                }
            } else {
                pendingDetachedMusicBar = null;
                clearMusicMountRelocationRetry();
            }
        });
    }

    function ensureMusicMountObserver() {
        if (musicMountObserver || typeof MutationObserver === 'undefined' || !document.body) return;
        musicMountObserver = new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                if (mutation.type === 'attributes' && isMusicMountMutationTarget(mutation.target)) {
                    scheduleMusicBarRelocation();
                    return;
                }
                if (mutation.type === 'attributes' && isCompactMusicGeometryMutationTarget(mutation.target)) {
                    requestCompactMusicGeometrySync();
                    return;
                }
                for (const node of mutation.removedNodes) {
                    const removedBar = findMusicBarInNode(node);
                    if (removedBar) {
                        scheduleMusicBarRelocation(removedBar);
                        return;
                    }
                    if (isMusicMountMutationNode(node)) {
                        scheduleMusicBarRelocation();
                        return;
                    }
                }
                for (const node of mutation.addedNodes) {
                    if (isMusicMountMutationNode(node)) {
                        scheduleMusicBarRelocation();
                        return;
                    }
                }
            }
        });
        musicMountObserver.observe(document.body, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: [
                'class',
                'data-music-player-mount',
                'data-chat-surface-mode',
                'aria-hidden',
                'hidden',
                'style'
            ]
        });
    }

    // 把一条音乐协调事件经主进程 IPC relay 发给其它窗口。无 IPC 桥（Web）时是
    // no-op —— 那条路径由 BroadcastChannel 直接承载（见 broadcastMusicCoord /
    // broadcastBarState 等里的 musicCoordChannel / musicBarChannel 分支）。
    // ipcRenderer.send 是 fire-and-forget，beforeunload 里也能可靠送出，
    // 故不再需要原 HTTP 路径的 cachedOnly / CSRF 处理。
    function postMusicPlayerBridgeEvent(type, payload, options = {}) {
        const bridge = getMusicIpcBridge();
        if (!bridge) return;
        const message = {
            sender: MUSIC_COORD_SENDER_ID,
            type: type,
            payload: payload || {},
            surface: options.surface || getMusicBridgeSurfaceState(type),
            ts: Date.now()
        };
        try { bridge.send(message); } catch (_) { /* ignore */ }
    }

    const bindMirrorBarControls = (musicBar) => {
        const apBtn = musicBar.querySelector('.music-bar-play');
        const closeBtn = musicBar.querySelector('.music-bar-close');
        const volumeContainer = musicBar.querySelector('.music-bar-volume-container');
        const volumeBtn = musicBar.querySelector('.music-bar-volume-btn');
        const volumeSliderWrapper = musicBar.querySelector('.music-bar-volume-slider-wrapper');
        const volumeSlider = musicBar.querySelector('.music-bar-volume-slider');
        const volumeFill = musicBar.querySelector('.music-bar-volume-slider-fill');
        const volumeHandle = musicBar.querySelector('.music-bar-volume-slider-handle');
        const progressContainer = musicBar.querySelector('.music-bar-progress-container');
        const progressFill = musicBar.querySelector('.music-bar-progress-fill');
        const timeCurrent = musicBar.querySelector('.music-bar-time-current');

        // teardownMirrorBar 调用此数组里的 cleanup，保证 bar 被删掉时
        // 未结束的拖拽不会留下悬空的 window-level 监听（否则下一次 mouseup
        // 会针对已经不存在的旧会话发 seekPercent / volume ctrl）。
        const teardownCleanups = [];
        musicBar.__mirrorTeardownCleanups = teardownCleanups;

        if (apBtn) apBtn.onclick = (e) => { e.preventDefault(); broadcastBarCtrl('toggle'); };
        if (closeBtn) closeBtn.onclick = (e) => { e.preventDefault(); broadcastBarCtrl('close'); };

        if (volumeBtn) volumeBtn.onclick = (e) => {
            e.preventDefault(); e.stopPropagation();
            if (volumeContainer) {
                volumeContainer.classList.toggle('expanded');
                requestCompactMusicGeometrySync();
            }
        };

        if (volumeSliderWrapper && volumeSlider) {
            let vDragging = false;
            const applyVolumeUI = (per) => {
                if (volumeFill) volumeFill.style.height = (per * 100) + '%';
                if (volumeHandle) volumeHandle.style.bottom = (per * 100) + '%';
                if (volumeBtn) {
                    if (per === 0) volumeBtn.textContent = '🔇';
                    else if (per < 0.5) volumeBtn.textContent = '🔉';
                    else volumeBtn.textContent = '🔊';
                }
            };
            const adjustVolume = (clientY) => {
                const rect = volumeSlider.getBoundingClientRect();
                if (!rect.height) return;
                const y = rect.bottom - clientY;
                const per = Math.max(0, Math.min(y, rect.height)) / rect.height;
                // 本地立即预览 + ctrl 转给 leader
                applyVolumeUI(per);
                broadcastBarCtrl('volume', per);
            };
            const onMove = (e) => {
                if (!vDragging) return;
                const y = e.clientY !== undefined ? e.clientY : (e.touches && e.touches[0] ? e.touches[0].clientY : null);
                if (y !== null) adjustVolume(y);
            };
            const detachVolumeListeners = () => {
                window.removeEventListener('mousemove', onMove);
                window.removeEventListener('mouseup', onEnd);
                window.removeEventListener('touchmove', onMove);
                window.removeEventListener('touchend', onEnd);
                window.removeEventListener('touchcancel', onEnd);
            };
            const onEnd = () => {
                vDragging = false;
                detachVolumeListeners();
            };
            volumeSliderWrapper.onmousedown = (e) => {
                e.preventDefault(); e.stopPropagation();
                vDragging = true;
                adjustVolume(e.clientY);
                window.addEventListener('mousemove', onMove);
                window.addEventListener('mouseup', onEnd);
            };
            volumeSliderWrapper.ontouchstart = (e) => {
                e.preventDefault(); e.stopPropagation();
                vDragging = true;
                if (e.touches && e.touches[0]) adjustVolume(e.touches[0].clientY);
                window.addEventListener('touchmove', onMove);
                window.addEventListener('touchend', onEnd);
                window.addEventListener('touchcancel', onEnd);
            };
            teardownCleanups.push(() => { vDragging = false; detachVolumeListeners(); });
        }

        if (progressContainer) {
            let pDragging = false;
            let pAborted = false;
            let lastPer = 0;
            const moveProgress = (clientX) => {
                const rect = progressContainer.getBoundingClientRect();
                if (!rect.width) return;
                const x = Math.max(0, Math.min(clientX - rect.left, rect.width));
                lastPer = x / rect.width;
                if (progressFill) progressFill.style.width = (lastPer * 100) + '%';
                // current time 只能估算（follower 不持有 duration）；leader 广播的
                // state 会在 seek 成功后覆盖回来，这里不强行 formatTime 避免假信号
                if (timeCurrent && mirrorBarLastState && mirrorBarLastState.duration) {
                    timeCurrent.textContent = formatTime(lastPer * mirrorBarLastState.duration);
                }
            };
            const onMove = (e) => {
                if (!pDragging) return;
                const x = e.clientX !== undefined ? e.clientX : (e.touches && e.touches[0] ? e.touches[0].clientX : null);
                if (x !== null) moveProgress(x);
            };
            const detachProgressListeners = () => {
                window.removeEventListener('mousemove', onMove);
                window.removeEventListener('mouseup', onEnd);
                window.removeEventListener('touchmove', onMove);
                window.removeEventListener('touchend', onEnd);
                window.removeEventListener('touchcancel', onEnd);
            };
            const onEnd = () => {
                if (!pDragging) return;
                pDragging = false;
                detachProgressListeners();
                // bar 已经被 teardown 掉（或切到了新会话）—— 不要把这次拖拽的
                // 坐标当成对当前播放会话的 seek 发出去。
                if (pAborted) return;
                broadcastBarCtrl('seekPercent', lastPer);
            };
            progressContainer.onmousedown = (e) => {
                pDragging = true; moveProgress(e.clientX);
                window.addEventListener('mousemove', onMove);
                window.addEventListener('mouseup', onEnd);
                window.addEventListener('touchmove', onMove);
                window.addEventListener('touchend', onEnd);
            };
            progressContainer.ontouchstart = (e) => {
                pDragging = true;
                if (e.touches && e.touches[0]) moveProgress(e.touches[0].clientX);
                window.addEventListener('mousemove', onMove);
                window.addEventListener('mouseup', onEnd);
                window.addEventListener('touchmove', onMove);
                window.addEventListener('touchend', onEnd);
            };
            teardownCleanups.push(() => {
                pAborted = true;
                pDragging = false;
                detachProgressListeners();
            });
        }

        if (volumeContainer) {
            const closeOnOutside = (e) => {
                if (volumeContainer.classList.contains('expanded') && !volumeContainer.contains(e.target)) {
                    volumeContainer.classList.remove('expanded');
                    requestCompactMusicGeometrySync();
                }
            };
            document.addEventListener('mousedown', closeOnOutside);
            // document 级的监听不会自己清，标记到 DOM 上由 teardown 统一清
            musicBar.__mirrorOutsideClickHandler = closeOnOutside;
        }
    };

    let mirrorBarLastState = null; // 供 seek UI 计算 currentTime 显示

    function isCurrentMirrorPlayback(payload) {
        const currentPlaybackId = mirrorBarLastState && mirrorBarLastState.playbackId;
        if (!currentPlaybackId) return true;
        return !!(payload && payload.playbackId === currentPlaybackId);
    }

    const renderMirrorBar = (state) => {
        if (!state) return false;
        if (mirrorBarDestroyTimer) { clearTimeout(mirrorBarDestroyTimer); mirrorBarDestroyTimer = null; }
        mirrorBarLastState = state;

        const track = state.track || {};

        let musicBar = document.getElementById(MUSIC_CONFIG.dom.barId);
        const firstRender = !musicBar;

        // 已存在但属于本窗口的 owner bar（非 mirror）：说明 leader 是自己，不应覆盖
        if (musicBar && musicBar.dataset.mirror !== 'true') return false;

        if (firstRender) {
            ensureMusicUiCSS();
            const mountTarget = getPreferredMusicMountTarget().mountTarget;
            if (!mountTarget) return false;

            musicBar = document.createElement('div');
            musicBar.id = MUSIC_CONFIG.dom.barId;
            musicBar.className = 'music-player-bar';
            musicBar.dataset.mirror = 'true';
            if (!mountMusicBar(musicBar)) return false;

            const randomColor = MUSIC_CONFIG.themeColors[Math.floor(Math.random() * MUSIC_CONFIG.themeColors.length)];
            musicBar.style.setProperty('--dynamic-random-color', randomColor);
            musicBar.style.setProperty('--dynamic-primary-color', MUSIC_CONFIG.primaryColor);
            musicBar.style.setProperty('--dynamic-secondary-color', MUSIC_CONFIG.secondaryColor);

            musicBar.innerHTML = `
                <div class="music-bar-cover" aria-hidden="true">
                    <span class="music-bar-equalizer">
                        <span class="music-bar-equalizer-bar"></span>
                        <span class="music-bar-equalizer-bar"></span>
                        <span class="music-bar-equalizer-bar"></span>
                    </span>
                </div>
                <div class="music-bar-info">
                    <div class="music-bar-title-wrap">
                        <div class="music-bar-title-track">
                            <span class="music-bar-title-seg music-bar-title-seg-primary"></span><span class="music-bar-title-seg music-bar-title-seg-dup" aria-hidden="true"></span>
                        </div>
                    </div>
                    <div class="music-bar-progress-container">
                        <div class="music-bar-progress-fill"></div>
                    </div>
                    <div class="music-bar-time">
                        <span class="music-bar-time-current">00:00</span>
                        <span class="music-bar-time-total">00:00</span>
                    </div>
                    <div class="music-bar-artist"></div>
                </div>
                <button type="button" class="music-bar-play">▶</button>
                <div class="music-bar-volume-container">
                    <button type="button" class="music-bar-volume-btn">🔊</button>
                    <div class="music-bar-volume-slider-wrapper" data-compact-hit-region="true" data-compact-hit-region-id="music-player:volume" data-compact-hit-region-kind="music-volume">
                        <div class="music-bar-volume-slider">
                            <div class="music-bar-volume-slider-fill"></div>
                            <div class="music-bar-volume-slider-handle"></div>
                        </div>
                    </div>
                </div>
                <button type="button" class="music-bar-close">✕</button>
            `;
            applyMusicBarAccessibilityLabels(musicBar);
            ensureTitleMarqueeObserver(musicBar);
            bindMirrorBarControls(musicBar);
        } else {
            musicBar.classList.remove('fading-out');
            if (!mountMusicBar(musicBar)) return false;
        }

        // 切歌 / 首次：刷新标题与歌手
        const trackSig = (track.url || '') + '|' + (track.name || '') + '|' + (track.artist || '');
        if (firstRender || trackSig !== mirrorBarTrackSig) {
            setMusicBarTitle(musicBar, track.name || '');
            const artistEl = musicBar.querySelector('.music-bar-artist');
            if (artistEl) artistEl.textContent = track.artist || musicT('music.unknownArtist', 'Unknown Artist');
            mirrorBarTrackSig = trackSig;
        }

        // 进度 / 时间
        const progressFill = musicBar.querySelector('.music-bar-progress-fill');
        const timeCurrent = musicBar.querySelector('.music-bar-time-current');
        const timeTotal = musicBar.querySelector('.music-bar-time-total');
        const cur = state.currentTime || 0;
        const dur = state.duration || 0;
        if (dur > 0) {
            if (progressFill) progressFill.style.width = Math.max(0, Math.min(100, (cur / dur) * 100)) + '%';
            if (timeCurrent) timeCurrent.textContent = formatTime(cur);
            if (timeTotal) timeTotal.textContent = formatTime(dur);
        } else {
            if (progressFill) progressFill.style.width = '0%';
            if (timeCurrent) timeCurrent.textContent = '00:00';
            if (timeTotal) timeTotal.textContent = '00:00';
        }

        // 播放按钮图标
        const apBtn = musicBar.querySelector('.music-bar-play');
        if (apBtn) {
            const playing = !state.paused && !state.loadError;
            const icon = playing ? '⏸' : '▶';
            const tText = musicT(playing ? 'music.pause' : 'music.play', playing ? 'Pause' : 'Play');
            apBtn.textContent = icon;
            apBtn.setAttribute('title', tText);
            apBtn.setAttribute('aria-label', tText);
        }
        setMusicBarVisualState(
            musicBar,
            state.loadError ? 'error' : (state.initial ? 'loading' : (state.paused ? 'paused' : 'playing'))
        );

        // 音量 UI
        if (typeof state.volume === 'number') {
            const volumeFill = musicBar.querySelector('.music-bar-volume-slider-fill');
            const volumeHandle = musicBar.querySelector('.music-bar-volume-slider-handle');
            const volumeBtn = musicBar.querySelector('.music-bar-volume-btn');
            const per = state.volume * 100;
            if (volumeFill) volumeFill.style.height = per + '%';
            if (volumeHandle) volumeHandle.style.bottom = per + '%';
            if (volumeBtn) {
                if (state.volume === 0) volumeBtn.textContent = '🔇';
                else if (state.volume < 0.5) volumeBtn.textContent = '🔉';
                else volumeBtn.textContent = '🔊';
            }
        }
        return true;
    };

    const teardownMirrorBar = (fullTeardown) => {
        const musicBar = document.getElementById(MUSIC_CONFIG.dom.barId);
        if (!musicBar || musicBar.dataset.mirror !== 'true') return;
        if (mirrorBarDestroyTimer) { clearTimeout(mirrorBarDestroyTimer); mirrorBarDestroyTimer = null; }

        // 解绑 document 级 outside-click 监听
        if (musicBar.__mirrorOutsideClickHandler) {
            document.removeEventListener('mousedown', musicBar.__mirrorOutsideClickHandler);
            musicBar.__mirrorOutsideClickHandler = null;
        }

        // 清拖拽装的 window 级 move/end 监听（正在拖就直接打断，防止 mouseup
        // 到达时对已经不存在的旧会话发 seekPercent / volume ctrl）
        if (Array.isArray(musicBar.__mirrorTeardownCleanups)) {
            for (const fn of musicBar.__mirrorTeardownCleanups) {
                try { fn(); } catch (_) { /* ignore */ }
            }
            musicBar.__mirrorTeardownCleanups = null;
        }

        const removeNow = () => {
            if (musicBar.parentNode) removeMusicBarWithoutRelocation(musicBar);
            mirrorBarTrackSig = null;
            mirrorBarLastState = null;
            mirrorBarDestroyTimer = null;
        };
        if (fullTeardown) {
            musicBar.classList.add('fading-out');
            mirrorBarDestroyTimer = setTimeout(removeNow, 300);
        } else {
            removeNow();
        }
    };

    // leader 处理 follower 发来的控制命令。所有动作都只作用于 localPlayer，
    // 随后由 APlayer 事件回调自然触发一次 emitBarState() 把真实状态广播回来。
    const handleRemoteBarCtrl = (action, value) => {
        if (!localPlayer) return;
        try {
            if (typeof window.setMusicUserDriven === 'function' &&
                (action === 'toggle' || action === 'play' || action === 'pause' || action === 'close' || action === 'seekPercent')) {
                window.setMusicUserDriven();
            }
            switch (action) {
                case 'play':
                    if (localPlayer.audio && localPlayer.audio.ended) localPlayer.seek(0);
                    localPlayer.play();
                    break;
                case 'pause':
                    localPlayer.pause();
                    break;
                case 'toggle':
                    if (autoDestroyTimer) { clearTimeout(autoDestroyTimer); autoDestroyTimer = null; }
                    if (localPlayer._loadError) { destroyMusicPlayer(true, true, true); return; }
                    if (localPlayer.audio && localPlayer.audio.ended) localPlayer.seek(0);
                    localPlayer.toggle();
                    break;
                case 'volume':
                    if (typeof value === 'number') {
                        localPlayer.volume(Math.max(0, Math.min(1, value)));
                    }
                    break;
                case 'seekPercent':
                    if (localPlayer.audio && isFinite(localPlayer.audio.duration) && typeof value === 'number') {
                        localPlayer.seek(value * localPlayer.audio.duration);
                    }
                    break;
                case 'close': {
                    recordMusicCloseFeedback(playbackStartedAt);
                    playbackStartedAt = 0;
                    destroyMusicPlayer(true, true, true);
                    break;
                }
                default:
                    /* unknown action, ignore */
                    break;
            }
        } catch (e) {
            console.warn('[Music UI] 处理 bar 远程控制失败:', e);
        }
    };

    function applyMusicPlayerBridgeCoord(sender, coordType, payload) {
        if (!sender || sender === MUSIC_COORD_SENDER_ID) return;
        if (coordType === 'music_started' || coordType === 'music_heartbeat') {
            remoteMusicSenders.set(sender, Date.now() + REMOTE_MUSIC_TTL_MS);
        } else if (coordType === 'music_pending') {
            remoteMusicSenders.set(sender, Date.now() + REMOTE_MUSIC_PENDING_TTL_MS);
        } else if (coordType === 'music_ended') {
            if (sender === mirrorBarLeaderSender && !isCurrentMirrorPlayback(payload)) return;
            remoteMusicSenders.delete(sender);
        }
    }

    function applyMusicPlayerBridgeEvent(event) {
        if (!event || typeof event !== 'object') return false;
        const sender = event.sender;
        if (!sender || sender === MUSIC_COORD_SENDER_ID) return false;
        const payload = event.payload || {};
        try {
            if (event.type === 'coord') {
                if (payload.coordType === 'request_state') {
                    // 晚加入的窗口请求当前 owner 状态：我是 owner 就立即补发一帧 bar_state，
                    // 让它无需等下一次 timeupdate / 心跳就能镜像出播放器条。
                    if (isBarOwner()) emitBarState();
                    return true;
                }
                applyMusicPlayerBridgeCoord(sender, payload.coordType, payload);
                return true;
            } else if (event.type === 'surface_state') {
                // 对端上报的 surface 状态：存入 peerMusicSurfaces 供 active surface 仲裁。
                updatePeerMusicSurface(sender, payload);
                return true;
            } else if (event.type === 'bar_state') {
                if (!acceptRemoteMusicOwnerState(sender, payload, event.ts)) return false;
                setMirrorBarLeader(sender, 'bridge');
                const rendered = renderMirrorBar(payload);
                if (!rendered) scheduleMusicBarRelocation();
                return rendered;
            } else if (event.type === 'bar_destroyed') {
                if (isBarOwner()) return false;
                if (sender !== mirrorBarLeaderSender) return false;
                if (!isCurrentMirrorPlayback(payload)) return false;
                setMirrorBarLeader(null);
                teardownMirrorBar(!!payload.fullTeardown);
                return true;
            } else if (event.type === 'bar_ctrl') {
                if (!isBarOwner()) return false;
                if (payload.target !== MUSIC_COORD_SENDER_ID) return false;
                if (shouldSkipProcessedBarCtrl(payload.ctrlId)) return false;
                handleRemoteBarCtrl(payload.action, payload.value);
                return true;
            }
        } catch (e) {
            console.warn('[Music UI] music player bridge event failed:', e);
        }
        return false;
    }

    const publishMusicSurfaceState = (reason) => {
        postMusicPlayerBridgeEvent('surface_state', getMusicBridgeSurfaceState(reason));
        scheduleMusicBarRelocation();
    };

    function initializeMusicPlayerBridge() {
        const bridge = getMusicIpcBridge();
        if (!bridge) return; // Web / 非 Electron：BroadcastChannel 已承载，无需 IPC bridge

        // 主进程把其它窗口的协调事件经此 CustomEvent 投递进来（preload 的 ipcRenderer.on）。
        window.addEventListener(MUSIC_BRIDGE_EVENT_NAME, (e) => {
            const event = e && e.detail;
            applyMusicPlayerBridgeEvent(event);
            // surface / owner 变化后本窗口可能需要重新判断是否显示镜像条。
            if (event && (event.type === 'surface_state' || event.type === 'bar_state' || event.type === 'bar_destroyed')) {
                scheduleMusicBarRelocation();
            }
        });

        // 周期性 pulse 本窗口 surface 状态，供其它窗口做 active surface 仲裁。
        musicSurfacePulseTimer = window.setInterval(() => publishMusicSurfaceState('pulse'), MUSIC_SURFACE_PULSE_MS);
        window.addEventListener('focus', () => publishMusicSurfaceState('focus'));
        window.addEventListener('blur', () => publishMusicSurfaceState('blur'));
        window.addEventListener('react-chat-window:chat-surface-mode-change', () => publishMusicSurfaceState('surface-mode'));
        window.addEventListener('neko:chat-music-surface-change', () => publishMusicSurfaceState('surface-state'));
        document.addEventListener('visibilitychange', () => publishMusicSurfaceState('visibility'));

        // 初次 pulse 本窗口 surface + 主动请求当前 owner 状态：晚加入的窗口立即补镜像，
        // 不必等 owner 下一次 timeupdate / 心跳。
        window.setTimeout(() => {
            publishMusicSurfaceState('init');
            postMusicPlayerBridgeEvent('coord', { coordType: 'request_state' });
        }, 0);

        window.addEventListener('beforeunload', () => {
            // 退出前明确通告本窗口不再可用 + 若是 owner 则摘镜像，避免对端等 TTL。
            postMusicPlayerBridgeEvent('surface_state', Object.assign(
                getMusicBridgeSurfaceState('unload'),
                { active: false, focused: false, visible: false, reason: 'unload' }
            ));
            if (isBarOwner()) {
                const playbackId = getCurrentMusicPlaybackId();
                postMusicPlayerBridgeEvent('bar_destroyed', { fullTeardown: true, playbackId: playbackId });
                postMusicPlayerBridgeEvent('coord', { coordType: 'music_ended', playbackId: playbackId });
            }
            if (musicSurfacePulseTimer) {
                window.clearInterval(musicSurfacePulseTimer);
                musicSurfacePulseTimer = null;
            }
        });
    }

    initializeMusicPlayerBridge();

    try {
        // 同 coord channel：有 IPC 桥时不开 BroadcastChannel，避免与 IPC 双发。
        if (typeof BroadcastChannel !== 'undefined' && !getMusicIpcBridge()) {
            musicBarChannel = new BroadcastChannel(MUSIC_BAR_CHANNEL_NAME);
            musicBarChannel.onmessage = (event) => {
                const data = event && event.data;
                if (!data || typeof data !== 'object') return;
                if (!data.sender || data.sender === MUSIC_COORD_SENDER_ID) return;
                try {
                    if (data.type === 'state') {
                        // 如果本地仍是 owner，只接受比本地当前播放更新的远端 owner state，
                        // 防止旧 state 反杀新播放，也避免两个窗口同时保留 audio。
                        if (!acceptRemoteMusicOwnerState(data.sender, data, data.ts)) return;
                        // 绑定/切换到当前 leader —— 后续 ctrl 会带 target 指向它，
                        // destroyed 也只接受来自它的那一条，避免多 leader 交接时串窗
                        setMirrorBarLeader(data.sender, 'broadcast');
                        if (!renderMirrorBar(data)) scheduleMusicBarRelocation();
                    } else if (data.type === 'destroyed') {
                        if (isBarOwner()) return;
                        // 只尊重来自当前绑定 leader 的 destroyed；别的 owner 退出
                        // 不应该把我当前镜像的那条 bar 也一起摘掉
                        if (data.sender !== mirrorBarLeaderSender) return;
                        if (!isCurrentMirrorPlayback(data)) return;
                        setMirrorBarLeader(null);
                        teardownMirrorBar(!!data.fullTeardown);
                    } else if (data.type === 'ctrl') {
                        // owner 只响应明确 target 到自己的 ctrl，避免别的 leader
                        // 被 follower 的指令误触（leader 交接瞬间最容易发生）
                        if (!isBarOwner()) return;
                        if (data.target !== MUSIC_COORD_SENDER_ID) return;
                        if (shouldSkipProcessedBarCtrl(data.ctrlId)) return;
                        handleRemoteBarCtrl(data.action, data.value);
                    }
                } catch (e) {
                    console.warn('[Music UI] bar mirror 处理失败:', e);
                }
            };
            window.addEventListener('beforeunload', () => {
                try {
                    if (musicBarChannel) {
                        // owner 退出时顺手通告一声 destroyed，follower 不用等 TTL
                        if (isBarOwner()) broadcastBarDestroyed(true);
                        musicBarChannel.close();
                        musicBarChannel = null;
                    }
                } catch (_) { /* ignore */ }
            });
        }
    } catch (e) {
        console.log('[Music UI] music bar channel 不可用:', e);
    }

    // --- 更新 React 聊天窗口音乐卡片 ---
    const updateMusicCard = (state, track) => {
        if (!musicCardMessageId) return;
        musicCardState = state;

        const host = window.reactChatWindowHost;
        if (!host || typeof host.updateMessage !== 'function') return;

        let prefix = '❓';
        let text = musicT('music.unknownState', 'Unknown state');
        if (state === 'loading') { prefix = '⏳'; text = musicT('music.loading', 'Loading'); }
        else if (state === 'playing') { prefix = '🎵'; text = musicT('music.playing', 'Playing'); }
        else if (state === 'paused') { prefix = '⏸'; text = musicT('music.paused', 'Paused'); }
        else if (state === 'ended') { prefix = '✅'; text = musicT('music.ended', 'Ended'); }
        else if (state === 'error') { prefix = '❌'; text = musicT('music.playError', 'Playback failed'); }
        else { prefix = '❓'; text = musicT('music.unknownState', 'Unknown state'); }

        // 镜像到所有窗口：leader 本地 update + 广播给 follower
        mirrorHostUpdate(host, musicCardMessageId, {
            blocks: [{
                type: 'link',
                url: track?.url || '#',
                title: track?.name || musicT('music.unknownTrack', 'Unknown Track'),
                description: track?.artist || musicT('music.unknownArtist', 'Unknown Artist'),
                siteName: prefix + ' ' + text,
                thumbnailUrl: getMusicCoverUrl(track?.cover)
            }]
        });
    };

    // --- 状态追踪：用于 5 秒去重 与 进度条清理 ---
    let lastPlayedMusicUrl = null;
    let lastMusicPlayTime = 0;

    // --- 音乐秒关检测 & 自动冷却 ---
    const SKIP_CONFIG = {
        skipThresholdMs: 15000,              // < 15 秒关闭 = 视为"秒关"
        hardSkipThresholdMs: 3000,           // 起播后 ≤ 3 秒即关 = "秒叉"，单次即触发冷却
        consecutiveSkipsToTrigger: 2,        // 连续秒关 2 次触发冷却
        cooldownDurationMs: 20 * 60 * 1000   // 冷却 20 分钟
    };

    // --- 主动推荐频率限流 ---
    // 用户反馈"推荐太频繁"，加一层硬性最小间隔：任意一次 proactive 推荐
    // 成功派发后，接下来 RECOMMEND_COOLDOWN_MS 内不再放行新的 proactive 推荐。
    // 非 proactive 来源（用户主动点播、插件直推、[play_music:] 指令）不受影响。
    const RECOMMEND_COOLDOWN_MS = 18000;
    let lastProactiveRecommendAt = 0;

    const isMusicRecommendRateLimited = () => {
        if (lastProactiveRecommendAt <= 0) return false;
        return (Date.now() - lastProactiveRecommendAt) < RECOMMEND_COOLDOWN_MS;
    };
    const markProactiveMusicRecommended = () => {
        lastProactiveRecommendAt = Date.now();
    };
    let playbackStartedAt = 0;        // Date.now() at first play of current track; 0 = never started
    let consecutiveSkipCount = 0;
    let musicCooldownUntil = 0;

    // 从 localStorage 恢复冷却状态
    try {
        const stored = localStorage.getItem('music_cooldown_until');
        if (stored) {
            const val = parseInt(stored, 10);
            if (val > Date.now()) {
                musicCooldownUntil = val;
                console.log('[Music UI] 恢复冷却状态，截止', new Date(val).toLocaleTimeString());
            } else {
                localStorage.removeItem('music_cooldown_until');
            }
        }
    } catch (e) { /* localStorage 不可用 */ }

    function enterMusicCooldown() {
        musicCooldownUntil = Date.now() + SKIP_CONFIG.cooldownDurationMs;
        consecutiveSkipCount = 0;
        try { localStorage.setItem('music_cooldown_until', String(musicCooldownUntil)); } catch (e) {}
        console.log('[Music UI] 连续秒关触发冷却，音乐推荐暂停至', new Date(musicCooldownUntil).toLocaleTimeString());
    }

    function isInMusicCooldown() {
        if (musicCooldownUntil <= 0) return false;
        if (Date.now() >= musicCooldownUntil) {
            musicCooldownUntil = 0;
            try { localStorage.removeItem('music_cooldown_until'); } catch (e) {}
            return false;
        }
        return true;
    }

    function recordMusicSkip() {
        consecutiveSkipCount++;
        console.log('[Music UI] 秒关 #' + consecutiveSkipCount);
        if (consecutiveSkipCount >= SKIP_CONFIG.consecutiveSkipsToTrigger) {
            enterMusicCooldown();
        }
    }

    function resetSkipCounter() {
        if (consecutiveSkipCount > 0) {
            console.log('[Music UI] 用户正常收听，重置秒关计数');
        }
        consecutiveSkipCount = 0;
    }

    // 用户关闭播放器时结算「秒关」反馈。判定用「从首次起播到关闭」的墙钟时长：
    //   startedAt == 0（压根没起播过——autoplay 被拦 / 加载失败 / 关得太早）→ 这次分享没有
    //     效送达用户，不构成喜恶信号，既不冷却也不计秒关，直接返回
    //   墙钟按真实经过时间算，不受 seek / 进度条拖动污染，比按 currentTime 差值估算可靠
    //   （pause 期间也计入：用户主动暂停代表有意互动、非秒拒，久停后关按正常收听处理不冷却）
    //   ≤ hardSkipThresholdMs → 起播后一下就叉，最强拒绝信号，单次即进冷却
    //   < skipThresholdMs      → 秒关，累计 consecutiveSkipsToTrigger 次进冷却
    //   >= skipThresholdMs     → 正常收听，重置秒关计数
    function recordMusicCloseFeedback(startedAt) {
        if (!startedAt) return;
        const wallMs = Date.now() - startedAt;
        if (wallMs <= SKIP_CONFIG.hardSkipThresholdMs) {
            console.log('[Music UI] 秒叉（起播后 ≤' + (SKIP_CONFIG.hardSkipThresholdMs / 1000) + 's 关闭），立即冷却');
            enterMusicCooldown();
        } else if (wallMs < SKIP_CONFIG.skipThresholdMs) {
            recordMusicSkip();
        } else {
            resetSkipCounter();
        }
    }

    // 完整播放是用户对"音乐分享"通道最强的正向反馈：让后端把
    // _proactive_chat_history 里 channel=='music' 的最近条目通道清空，从而停止
    // 因为"刚刚分享过音乐"而对 music 通道继续做权重衰减惩罚。fire-and-forget。
    // 去抖按曲目维度：APlayer 同一首 audio 偶尔会重复 fire ended（音源/seek 抖动），
    // 但两首不同歌秒级连续 ended 是合法事件，不能被全局 timestamp 吞掉。
    let lastMusicPlayedThroughKey = '';
    let lastMusicPlayedThroughAt = 0;
    function notifyMusicPlayedThrough(track) {
        // 用 filter(Boolean) 把缺失字段挤掉再 join，避免 {url:'',name:'',artist:''}
        // 拼出"||"这种伪 key 让多首无元数据曲共享去抖钥匙
        const parts = track ? [track.url, track.name, track.artist].filter(Boolean) : [];
        const trackKey = parts.join('|');
        const now = Date.now();
        if (now - lastMusicPlayedThroughAt < 2000) {
            // 空 key 时退化到全局 2s 兜底；非空 key 仅在和上次相同时才吞
            if (!trackKey || trackKey === lastMusicPlayedThroughKey) return;
        }
        lastMusicPlayedThroughKey = trackKey;
        lastMusicPlayedThroughAt = now;
        const lanlanName = (window.lanlan_config && window.lanlan_config.lanlan_name) || '';
        // fire-and-forget：用 async IIFE 包一层，让 getMutationHeaders 能 await
        // 但不阻塞外层调用方（aplayer 'ended' 回调本身不关心后端是否成功）。
        (async () => {
            const playedHeaders = { 'Content-Type': 'application/json' };
            const sec = window.nekoLocalMutationSecurity;
            if (sec && typeof sec.getMutationHeaders === 'function') {
                try { Object.assign(playedHeaders, await sec.getMutationHeaders()); } catch (_) { }
            }
            try {
                await fetch('/api/proactive/music_played_through', {
                    method: 'POST',
                    headers: playedHeaders,
                    body: JSON.stringify({
                        lanlan_name: lanlanName,
                        track: track ? { name: track.name, artist: track.artist, url: track.url } : null
                    })
                });
            } catch (_) { /* 后端不可达不影响播放体验 */ }
        })();
    }

    // 全局监听管理
    let managedWindowListeners = [];
    const addManagedListener = (type, listener, options) => {
        window.addEventListener(type, listener, options);
        managedWindowListeners.push({ type, listener, options });
    };
    const clearManagedListeners = () => {
        managedWindowListeners.forEach(({ type, listener, options }) => {
            window.removeEventListener(type, listener, options);
        });
        managedWindowListeners = [];
    };

    // 全局拖拽清理引用
    let currentDragHandlers = null;
    let currentVolumeDragHandlers = null;

    // --- 2. 原始工具函数 ---
    const normalizeMusicUrlEscapes = (url) => {
        if (typeof url !== 'string') return url;
        let normalized = url;
        let previous = '';
        while (normalized !== previous) {
            previous = normalized;
            normalized = normalized
                .replace(/&amp;/g, '&')
                .replace(/&amp%3B/g, '&')
                .replace(/%26amp%3B/g, '&');
        }
        return normalized;
    };

    /**
     * 安全提取域名/IP
     */
    const extractHostname = (input) => {
        if (!input || typeof input !== 'string') return null;
        let target = input.trim();
        if (!target.startsWith('http://') && !target.startsWith('https://')) {
            target = 'https://' + target;
        }
        try {
            const url = new URL(target);
            return url.hostname;
        } catch (e) {
            return null;
        }
    };

    const isSafeUrl = (url) => {
        if (!url) return false;
        try {
            // 对内部代理路径直接放行（后端已做安全检查）
            if (url.startsWith('/api/')) return true;
            const parsed = new URL(url);
            if (parsed.protocol === 'http:') return pluginHttpUrls.has(parsed.href);
            if (parsed.protocol !== 'https:') return false;
            const hostname = parsed.hostname;
            return MUSIC_CONFIG.allowlist.some(d => hostname === d || hostname.endsWith('.' + d));
        } catch { return false; }
    };

    const normalizeMusicCoverUrl = (cover) => {
        if (!cover || typeof cover !== 'string') return '';
        const candidate = cover.startsWith('//') ? `https:${cover}` : cover;
        try {
            const parsed = new URL(candidate);
            const hostname = parsed.hostname.toLowerCase();
            if (parsed.protocol === 'http:' && (
                hostname === 'music.126.net' || hostname.endsWith('.music.126.net')
            )) {
                parsed.protocol = 'https:';
                return parsed.toString();
            }
        } catch (_) {}
        return candidate;
    };

    const getMusicCoverUrl = (cover) => {
        const normalizedCover = normalizeMusicCoverUrl(cover);
        return normalizedCover && isSafeUrl(normalizedCover)
            ? normalizedCover
            : MUSIC_CONFIG.assets.defaultCoverPath
    };

    const toBackendMusicProxyUrl = (url) => {
        if (!url || typeof url !== 'string' || url.startsWith('/api/')) return url;
        try {
            const parsed = new URL(url);
            if (parsed.protocol !== 'https:') return url;
            const hostname = parsed.hostname;
            const proxyAllowed = Array.from(backendProxyDomains)
                .some(domain => hostname === domain || hostname.endsWith('.' + domain));
            return proxyAllowed
                ? `/api/music/proxy?url=${encodeURIComponent(url)}`
                : url;
        } catch (_) {
            return url;
        }
    };

    const isUnsupportedMusicStream = (url) => {
        try {
            return new URL(url, window.location.origin).pathname.toLowerCase().endsWith('.m3u8');
        } catch (_) {
            return true;
        }
    };

    const resolveMusicUrl = (url) => {
        try { return new URL(url, window.location.origin).href; }
        catch (_) { return String(url || ''); }
    };

    const musicPlayResult = (ok, reason = '', canTryNextCandidate = false) => ({
        ok: ok === true,
        reason: reason,
        canTryNextCandidate: canTryNextCandidate === true
    });

    // Candidate-local failures may fall back; the backend supplies at most three candidates.
    const canTryNextMusicCandidate = (reason) => [
        'invalid_track',
        'unsafe_url',
        'unsupported_stream',
        'track_too_long',
        'load_timeout',
        'media_error'
    ].includes(reason);

    const shouldDeferCandidateFailureUi = (options, reason) => (
        options
        && options.hasNextCandidate === true
        && canTryNextMusicCandidate(reason)
    );

    const getMusicCardScopeKey = (playbackOptions) => {
        const options = playbackOptions || {};
        if (options.cardScopeId === undefined || options.cardScopeId === null || options.cardScopeId === '') {
            return '';
        }
        return String(options.source || 'music') + ':' + String(options.cardScopeId).slice(0, 160);
    };

    const finalizeScopedMusicCardFailure = (track, playbackOptions) => {
        const requestedScopeKey = getMusicCardScopeKey(playbackOptions);
        if (
            !musicCardMessageId
            || !requestedScopeKey
            || requestedScopeKey !== musicCardScopeKey
        ) return false;

        updateMusicCard('error', track);
        musicCardMessageId = null;
        musicCardScopeKey = '';
        musicCardState = '';
        return true;
    };

    const getCandidateMediaReadyTimeoutMs = (playbackOptions) => {
        const options = playbackOptions || {};
        if (options.hasNextCandidate !== true) return MUSIC_MEDIA_LOAD_TIMEOUT_MS;

        const deadlineAt = Number(options.fallbackDeadlineAt);
        const remainingMs = Number.isFinite(deadlineAt) && deadlineAt > 0
            ? Math.max(1, deadlineAt - Date.now())
            : MUSIC_MEDIA_LOAD_TIMEOUT_MS;
        const requestedTimeoutMs = Number(options.candidateTimeoutMs);
        const attemptTimeoutMs = Number.isFinite(requestedTimeoutMs) && requestedTimeoutMs > 0
            ? requestedTimeoutMs
            : MUSIC_MEDIA_LOAD_TIMEOUT_MS;
        return Math.min(MUSIC_MEDIA_LOAD_TIMEOUT_MS, remainingMs, attemptTimeoutMs);
    };

    const waitForMusicMediaReady = (
        player,
        token,
        expectedUrl,
        enforceRecommendationLimit,
        timeoutMs = MUSIC_MEDIA_LOAD_TIMEOUT_MS
    ) => new Promise((resolve) => {
        const audio = player && player.audio;
        if (!audio) {
            resolve({ ok: false, reason: 'missing_audio' });
            return;
        }

        let settled = false;
        let timeoutId = null;
        let cancelWait = null;
        const sourceLifecycleStartedAt = getMusicLifecycleTimestamp();
        const cleanup = () => {
            audio.removeEventListener('loadedmetadata', onMetadata);
            audio.removeEventListener('canplay', onCanPlay);
            audio.removeEventListener('error', onError);
            if (timeoutId) window.clearTimeout(timeoutId);
            if (pendingMusicMediaReadyCancel === cancelWait) {
                pendingMusicMediaReadyCancel = null;
            }
        };
        const finish = (ok, reason) => {
            if (settled) return;
            settled = true;
            cleanup();
            resolve({ ok: ok, reason: reason || '' });
        };
        cancelWait = () => finish(false, 'superseded');
        pendingMusicMediaReadyCancel = cancelWait;
        const isExpectedSource = () => {
            const activeUrl = audio.currentSrc || audio.src || '';
            return !expectedUrl || !activeUrl || resolveMusicUrl(activeUrl) === resolveMusicUrl(expectedUrl);
        };
        const validateDuration = (readyReason) => {
            if (token !== latestMusicRequestToken) {
                finish(false, 'superseded');
                return;
            }
            // A reused <audio> may emit a late event for the previous source.
            if (!isExpectedSource()) return;
            const duration = Number(audio.duration);
            if (enforceRecommendationLimit && (
                duration === Infinity
                || (Number.isFinite(duration) && duration >= MAX_RECOMMENDED_TRACK_DURATION_SECONDS)
            )) {
                try { audio.pause(); } catch (_) { /* best effort */ }
                finish(false, 'track_too_long');
                return;
            }
            if (duration === Infinity && !enforceRecommendationLimit) {
                finish(true, readyReason);
                return;
            }
            if (Number.isFinite(duration) && duration > 0) {
                finish(true, readyReason);
            }
        };
        function onMetadata() { validateDuration('loadedmetadata'); }
        function onCanPlay() {
            const duration = Number(audio.duration);
            if (duration === Infinity) validateDuration('canplay');
            else if (!Number.isFinite(duration) || duration <= 0) finish(true, 'canplay');
            else validateDuration('canplay');
        }
        function onError(event) {
            if (!audio.error) return;
            const eventTimestamp = normalizeMusicEventTimestamp(event);
            if (eventTimestamp !== null && eventTimestamp < sourceLifecycleStartedAt) return;
            if (isExpectedSource()) finish(false, 'media_error');
        }

        audio.addEventListener('loadedmetadata', onMetadata);
        audio.addEventListener('canplay', onCanPlay);
        audio.addEventListener('error', onError);
        const effectiveTimeoutMs = Math.max(
            1,
            Math.min(MUSIC_MEDIA_LOAD_TIMEOUT_MS, Number(timeoutMs) || MUSIC_MEDIA_LOAD_TIMEOUT_MS)
        );
        timeoutId = window.setTimeout(() => finish(false, 'load_timeout'), effectiveTimeoutMs);

        if (!audio.error && audio.readyState >= 1) {
            window.queueMicrotask(() => validateDuration('already_ready'));
        }
    });

    const getMusicPlayerInstance = () => localPlayer;

    const isPlayerInDOM = () => {
        const bar = document.getElementById(MUSIC_CONFIG.dom.barId);
        // 如果正在淡出，视为已经不在 DOM 中，允许后续逻辑重用/创建新条
        return !!(bar && !bar.classList.contains('fading-out'));
    };

    const isSameTrack = (info) => {
        return currentPlayingTrack &&
            currentPlayingTrack.name === info.name &&
            currentPlayingTrack.artist === info.artist &&
            currentPlayingTrack.url === info.url;
    };

    const showErrorToast = (msgKey, defaultMsg) => {
        if (typeof window.showStatusToast === 'function') {
            const errMsg = musicT(msgKey, defaultMsg);
            window.showStatusToast(errMsg, 3000);
        }
    };

    const showNowPlayingToast = (name) => {
        if (typeof window.showStatusToast === 'function') {
            const unknownTrack = musicT('music.unknownTrack', 'Unknown Track');
            const displayName = name || unknownTrack;
            const playMsg = musicT('music.nowPlaying', 'Now Playing: {{name}}', { name: displayName });

            window.showStatusToast(playMsg, 3000);
        }
    };

    let autoDestroyTimer = null;
    let domRemovalTimer = null;
    let titleMarqueeObserver = null;

    const disconnectTitleMarqueeObserver = () => {
        if (titleMarqueeObserver) {
            titleMarqueeObserver.disconnect();
            titleMarqueeObserver = null;
        }
    };

    const syncMusicBarTitleLayout = (musicBar) => {
        const wrap = musicBar && musicBar.querySelector('.music-bar-title-wrap');
        const track = wrap && wrap.querySelector('.music-bar-title-track');
        const segPrimary = wrap && wrap.querySelector('.music-bar-title-seg-primary');
        if (!wrap || !track || !segPrimary) return;

        wrap.classList.remove('is-marquee');
        track.style.removeProperty('--marquee-duration');

        const ratio = typeof MUSIC_CONFIG.titleOverflowRatio === 'number' ? MUSIC_CONFIG.titleOverflowRatio : 1;
        const maxW = Math.max(0, wrap.clientWidth * ratio);
        const textW = segPrimary.offsetWidth;

        if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
            return;
        }

        if (textW > maxW) {
            wrap.classList.add('is-marquee');
            requestAnimationFrame(() => {
                const bar = document.getElementById(MUSIC_CONFIG.dom.barId);
                if (!bar) return;
                const w = bar.querySelector('.music-bar-title-wrap');
                const t = w && w.querySelector('.music-bar-title-track');
                if (!w || !t || !w.classList.contains('is-marquee')) return;
                const loopPx = t.scrollWidth / 2;
                const duration = Math.min(50, Math.max(6, loopPx / 45));
                t.style.setProperty('--marquee-duration', duration + 's');
            });
        }
    };

    const setMusicBarTitle = (musicBar, text) => {
        const wrap = musicBar.querySelector('.music-bar-title-wrap');
        const segPrimary = musicBar.querySelector('.music-bar-title-seg-primary');
        const segDup = musicBar.querySelector('.music-bar-title-seg-dup');
        const display = text || musicT('music.unknownTrack', 'Unknown Track');
        if (segPrimary) segPrimary.textContent = display;
        if (segDup) segDup.textContent = display;
        if (wrap) {
            wrap.setAttribute('title', display);
            wrap.setAttribute('aria-label', display);
        }
        requestAnimationFrame(() => {
            requestAnimationFrame(() => syncMusicBarTitleLayout(musicBar));
        });
    };

    const ensureTitleMarqueeObserver = (musicBar) => {
        const wrap = musicBar.querySelector('.music-bar-title-wrap');
        if (!wrap || typeof ResizeObserver === 'undefined') return;
        disconnectTitleMarqueeObserver();
        titleMarqueeObserver = new ResizeObserver(() => {
            const bar = document.getElementById(MUSIC_CONFIG.dom.barId);
            if (bar) syncMusicBarTitleLayout(bar);
        });
        titleMarqueeObserver.observe(wrap);
    };

    const formatTime = (seconds) => {
        if (isNaN(seconds) || !isFinite(seconds)) return '00:00';
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    };


    const destroyMusicPlayer = (
        removeDOM = true,
        fullTeardown = false,
        updateToken = false,
        preserveMusicCard = false
    ) => {
        // 播放器销毁即结束当前曲目生命周期，清起播时间戳，避免残留到下一首
        playbackStartedAt = 0;
        const destroyedPlaybackId = getCurrentMusicPlaybackId();
        const terminalReportContext = localPlayer && localPlayer._musicPlaybackReportContext;
        if (
            fullTeardown
            && terminalReportContext
            && ['playing', 'paused'].includes(terminalReportContext.lastReportedState)
        ) {
            reportMusicPlaybackState('ended', null, terminalReportContext);
        }
        // 重要：销毁播放器意味着取消所有正在进行的异步加载令牌
        // 只有在 fullTeardown (手动关闭) 或明确要求时才更新 token
        if (updateToken || fullTeardown) {
            latestMusicRequestToken++;
        }

        // 清除可能的自动销毁定时器
        if (autoDestroyTimer) {
            clearTimeout(autoDestroyTimer);
            autoDestroyTimer = null;
        }

        // 重要：清除正在进行的 DOM 移除定时器，防止在切换歌曲时播放条被意外删除
        if (domRemovalTimer) {
            clearTimeout(domRemovalTimer);
            domRemovalTimer = null;
        }

        // Revoke event ownership before pause/destroy can emit callbacks.
        if (localPlayer) {
            localPlayer._musicPlaybackReportContext = null;
            localPlayer._destroying = true;
        }

        // 核心：优先执行本地暂停，避免声音残留
        if (localPlayer && typeof localPlayer.pause === 'function') {
            localPlayer.pause();
        }
        if (currentDragHandlers && typeof currentDragHandlers.cleanup === 'function') {
            currentDragHandlers.cleanup();
            currentDragHandlers = null;
        }
        if (currentVolumeDragHandlers && typeof currentVolumeDragHandlers.cleanup === 'function') {
            currentVolumeDragHandlers.cleanup();
            currentVolumeDragHandlers = null;
        }
        if (fullTeardown) {
            // 【核心修复】调整顺序：先调用外部销毁逻辑，再清理本地引用
            // 理由：APlayer/main.js 的 destroyAPlayer 依赖 window.aplayer 进行清理
            // 且此处理由 window.destroyAPlayer 统一完成实例销毁，不再本地重复销毁
            if (typeof window.destroyAPlayer === 'function') {
                window.destroyAPlayer();
            } else if (localPlayer && typeof localPlayer.destroy === 'function') {
                localPlayer.destroy();
            }
            localPlayer = null;
            window.aplayer = null;
            if (window.aplayerInjected) window.aplayerInjected.aplayer = null;
        } else {
            // 切歌模式下，手动销毁旧实例以防泄露
            if (localPlayer && typeof localPlayer.destroy === 'function') {
                try {
                    clearManagedListeners();
                    localPlayer.destroy();
                } catch (e) {
                    console.warn('[Music UI] Error during local player destroy:', e);
                }
            }
            localPlayer = null;
            window.aplayer = null;
            if (window.aplayerInjected) window.aplayerInjected.aplayer = null;
        }

        if (removeDOM) {
            disconnectTitleMarqueeObserver();
            const bar = document.getElementById(MUSIC_CONFIG.dom.barId);
            if (bar) {
                // 如果是手动关闭，执行动画
                if (fullTeardown) {
                    bar.classList.add('fading-out');
                    domRemovalTimer = setTimeout(() => {
                        removeMusicBarWithoutRelocation(bar);
                        domRemovalTimer = null;
                    }, 300);
                } else {
                    removeMusicBarWithoutRelocation(bar);
                }
            }
            clearManagedListeners();
        }
        // 完整销毁只结束非终态卡片；播放失败必须保留 error，不能被延迟销毁改写。
        if (
            fullTeardown
            && musicCardMessageId
            && !['error', 'ended'].includes(musicCardState)
        ) {
            updateMusicCard('ended', currentPlayingTrack);
        }
        currentPlayingTrack = null;
        if (!preserveMusicCard || fullTeardown) {
            musicCardMessageId = null;
            musicCardScopeKey = '';
            musicCardState = '';
        }

        // 跨窗口协调：通知其他窗口本地音乐已停
        stopMusicHeartbeat();
        broadcastMusicCoord('music_ended');

        // bar 镜像：让 follower 同步摘掉镜像 bar。fullTeardown 传下去
        // 是为了 follower 能走淡出动画而不是硬删。
        if (removeDOM) broadcastBarDestroyed(fullTeardown, destroyedPlaybackId);
        currentMusicPlaybackId = null;
        currentMusicOwnerStartedAt = 0;
    };

    // --- 查找并替换整个 loadAPlayerLibrary 函数 ---
    const loadAPlayerLibrary = () => {
        if (aplayerLoadPromise) return aplayerLoadPromise;

        aplayerLoadPromise = new Promise((resolve, reject) => {
            musicUiCssInjected = true;
            const cssPromises = [
                injectCSS(MUSIC_CONFIG.assets.cssPath),
                injectCSS(MUSIC_CONFIG.assets.uiCssPath)
            ];

            if (typeof window.APlayer !== 'undefined') {
                Promise.all(cssPromises).then(() => resolve());
                return;
            }

            // 同时并行加载：官方CSS、自定义CSS、APlayer脚本
            Promise.all([
                ...cssPromises,
                new Promise((resJS, rejJS) => {
                    const script = document.createElement('script');
                    script.src = MUSIC_CONFIG.assets.jsPath;
                    script.onload = () => (typeof window.APlayer !== 'undefined' ? resJS() : rejJS());
                    script.onerror = rejJS;
                    document.head.appendChild(script);
                })
            ]).then(() => {
                console.log('[Music UI] 所有资源（包括自定义CSS）已就绪');
                resolve();
            }).catch((err) => {
                aplayerLoadPromise = null;
                reject(err);
            });
        });
        return aplayerLoadPromise;
    };

    // --- 5. 播放器挂载逻辑 (支持原地更新与实例复用) ---
    // 核心逻辑：复用 APlayer 实例可以保留浏览器的“音频解锁”状态，极大提高自动播放成功率
    //
    // 【串行化】两次并发调用如果同时进入 needsInit 分支，会同时 await initializeAPlayer，
    // 都拿到自己的实例后写 currentPlayingTrack/musicCardMessageId，第一份卡片
    // 会被第二份盖掉，被覆盖的实例如果 destroy 不及时还会残留 <audio>。
    // 用 executePlayChain 把所有 executePlay 排成单线，保证内部 await 不会被抢跑。
    const executePlay = (trackInfo, currentToken, shouldAutoPlay = true, playbackOptions = {}) => {
        const run = () => executePlayCore(trackInfo, currentToken, shouldAutoPlay, playbackOptions);
        const next = executePlayChain.then(run, run); // 即使前一次 reject 也继续
        executePlayChain = next.catch(() => { /* 链路自愈，避免 rejection 阻断后续 */ });
        return next;
    };

    const executePlayCore = async (trackInfo, currentToken, shouldAutoPlay = true, playbackOptions = {}) => {
        if (currentToken !== latestMusicRequestToken) return musicPlayResult(false, 'superseded');

        // 清除可能的自动销毁与 DOM 移除定时器
        if (autoDestroyTimer) {
            clearTimeout(autoDestroyTimer);
            autoDestroyTimer = null;
        }
        if (domRemovalTimer) {
            clearTimeout(domRemovalTimer);
            domRemovalTimer = null;
        }

        // 本窗口从 follower 切成 owner：之前由别的 leader 推过来的镜像 bar
        // 上挂的是"按钮发 ctrl"的监听，不能复用。硬清一次让下面 executePlay
        // 新建一个绑本地 APlayer 的 bar，避免旧的 outside-click / drag 监听泄露。
        const existingBar = document.getElementById(MUSIC_CONFIG.dom.barId);
        if (existingBar && existingBar.dataset.mirror === 'true') {
            if (existingBar.__mirrorOutsideClickHandler) {
                document.removeEventListener('mousedown', existingBar.__mirrorOutsideClickHandler);
                existingBar.__mirrorOutsideClickHandler = null;
            }
            if (Array.isArray(existingBar.__mirrorTeardownCleanups)) {
                for (const fn of existingBar.__mirrorTeardownCleanups) {
                    try { fn(); } catch (_) { /* ignore */ }
                }
                existingBar.__mirrorTeardownCleanups = null;
            }
            removeMusicBarWithoutRelocation(existingBar);
            mirrorBarTrackSig = null;
            mirrorBarLastState = null;
            // 自己升成 owner 后就不再持有"绑定到某个 leader"的状态
            setMirrorBarLeader(null);
        }

        const displayCoverUrl = getMusicCoverUrl(trackInfo.cover);
        let musicBar = document.getElementById(MUSIC_CONFIG.dom.barId);
        let isFirstRender = !musicBar;

        // --- 1. DOM 基础架构 ---
        if (isFirstRender) {
            // 优先使用紧凑历史目标，其次 React composer 挂载点，最后回退旧 chat-container。
            const mountTarget = getPreferredMusicMountTarget({ allowInactiveOwner: true }).mountTarget;
            if (!mountTarget) return musicPlayResult(false, 'player_error');

            musicBar = document.createElement('div');
            musicBar.id = MUSIC_CONFIG.dom.barId;
            musicBar.className = 'music-player-bar';
            mountMusicBar(musicBar);

            const randomColor = MUSIC_CONFIG.themeColors[Math.floor(Math.random() * MUSIC_CONFIG.themeColors.length)];
            musicBar.style.setProperty('--dynamic-random-color', randomColor);
            musicBar.style.setProperty('--dynamic-primary-color', MUSIC_CONFIG.primaryColor);
            musicBar.style.setProperty('--dynamic-secondary-color', MUSIC_CONFIG.secondaryColor);

            musicBar.innerHTML = `
                <div class="music-bar-cover" aria-hidden="true">
                    <span class="music-bar-equalizer">
                        <span class="music-bar-equalizer-bar"></span>
                        <span class="music-bar-equalizer-bar"></span>
                        <span class="music-bar-equalizer-bar"></span>
                    </span>
                </div>
                <div class="music-bar-info">
                    <div class="music-bar-title-wrap">
                        <div class="music-bar-title-track">
                            <span class="music-bar-title-seg music-bar-title-seg-primary"></span><span class="music-bar-title-seg music-bar-title-seg-dup" aria-hidden="true"></span>
                        </div>
                    </div>
                    <div class="music-bar-progress-container">
                        <div class="music-bar-progress-fill"></div>
                    </div>
                    <div class="music-bar-time">
                        <span class="music-bar-time-current">00:00</span>
                        <span class="music-bar-time-total">00:00</span>
                    </div>
                    <div class="music-bar-artist"></div>
                </div>
                <button type="button" class="music-bar-play">▶</button>
                <div class="music-bar-volume-container">
                    <button type="button" class="music-bar-volume-btn">🔊</button>
                    <div class="music-bar-volume-slider-wrapper" data-compact-hit-region="true" data-compact-hit-region-id="music-player:volume" data-compact-hit-region-kind="music-volume">
                        <div class="music-bar-volume-slider">
                            <div class="music-bar-volume-slider-fill"></div>
                            <div class="music-bar-volume-slider-handle"></div>
                        </div>
                    </div>
                </div>
                <button type="button" class="music-bar-close">✕</button>
                <div class="aplayer-internal-container" style="display: none;"></div>
            `;
            applyMusicBarAccessibilityLabels(musicBar);
            ensureTitleMarqueeObserver(musicBar);
        } else {
            musicBar.classList.remove('fading-out');
            mountMusicBar(musicBar);
        }

        // 切歌前，先把上一首卡片标记为"已结束"。必须在 currentPlayingTrack
        // 被覆盖之前用旧值更新，否则旧卡片会被改写成新曲目信息。
        const previousTrackForCard = currentPlayingTrack;
        const previousCardId = musicCardMessageId;
        const requestedCardScopeKey = getMusicCardScopeKey(playbackOptions);
        const reuseScopedMusicCard = Boolean(
            previousCardId
            && requestedCardScopeKey
            && requestedCardScopeKey === musicCardScopeKey
        );

        // --- 2. 原地更新 UI 文本与音乐状态图标 (始终执行) ---
        const playbackIdForRequest = createMusicPlaybackId(trackInfo, currentToken);
        currentMusicPlaybackId = playbackIdForRequest;
        setMusicPlaybackContext(playbackOptions);
        const playbackReportContext = createMusicPlaybackReportContext(
            playbackIdForRequest,
            playbackOptions,
            trackInfo,
            currentToken
        );
        currentMusicOwnerStartedAt = Date.now();
        currentPlayingTrack = trackInfo;
        // 广播一次占位 state —— APlayer 还在初始化/切曲，但 follower 现在
        // 就能把 bar 刷新到新 track，避免旧歌信息停留或 bar 空白。
        emitBarInitialState(trackInfo);
        setMusicBarTitle(musicBar, trackInfo.name || '');
        musicBar.querySelector('.music-bar-artist').textContent = trackInfo.artist || musicT('music.unknownArtist', 'Unknown Artist');
        setMusicBarVisualState(musicBar, 'loading');

        const progressFill = musicBar.querySelector('.music-bar-progress-fill');
        const timeCurrent = musicBar.querySelector('.music-bar-time-current');
        const timeTotal = musicBar.querySelector('.music-bar-time-total');
        if (progressFill) progressFill.style.width = '0%';
        if (timeCurrent) timeCurrent.textContent = '00:00';

        // --- 2b. 向 React 聊天窗口推送音乐卡片消息 ---
        {
            const host = window.reactChatWindowHost;
            if (host && typeof host.appendMessage === 'function') {
                // 切歌时，先把上一首的卡片标记为"已结束"，避免覆盖 musicCardMessageId
                // 之后旧卡片永远停在"播放中"。注意要用旧 id + 旧 track。
                if (previousCardId && !reuseScopedMusicCard && !['ended', 'error'].includes(musicCardState)) {
                    try {
                        // 镜像到所有窗口：follower 也要把上一首标成已播完
                        mirrorHostUpdate(host, previousCardId, {
                            blocks: [{
                                type: 'link',
                                url: (previousTrackForCard && previousTrackForCard.url) || '#',
                                title: (previousTrackForCard && previousTrackForCard.name) || musicT('music.unknownTrack', 'Unknown Track'),
                                description: (previousTrackForCard && previousTrackForCard.artist) || musicT('music.unknownArtist', 'Unknown Artist'),
                                siteName: '✅ ' + musicT('music.ended', 'Ended'),
                                thumbnailUrl: getMusicCoverUrl(previousTrackForCard && previousTrackForCard.cover)
                            }]
                        });
                    } catch (_) { /* ignore */ }
                }
                if (reuseScopedMusicCard) {
                    updateMusicCard('loading', trackInfo);
                    musicCardScopeKey = requestedCardScopeKey;
                } else {
                    let assistantName = '';
                    if (window.lanlan_config && window.lanlan_config.lanlan_name) assistantName = window.lanlan_config.lanlan_name;
                    else if (window._currentCatgirl) assistantName = window._currentCatgirl;
                    else if (window.currentCatgirl) assistantName = window.currentCatgirl;
                    assistantName = assistantName || 'Neko';
                    let avatarUrl = '';
                    if (window.appChatAvatar && typeof window.appChatAvatar.getCurrentAvatarDataUrl === 'function') {
                        avatarUrl = window.appChatAvatar.getCurrentAvatarDataUrl() || '';
                    }
                    const now = new Date();
                    const timeStr = now.getHours().toString().padStart(2, '0') + ':' + now.getMinutes().toString().padStart(2, '0');
                    const msgId = 'music-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
                    musicCardMessageId = msgId;
                    musicCardScopeKey = requestedCardScopeKey;
                    musicCardState = 'loading';
                    // 镜像到所有窗口：leader 本地 append + 广播给 follower，
                    // 保证 chat.html 里也能出现音乐卡片（见本文件 chat mirror 段的注释）
                    mirrorHostAppend(host, {
                        id: msgId,
                        role: 'assistant',
                        author: assistantName,
                        time: timeStr,
                        createdAt: Date.now(),
                        avatarLabel: assistantName.trim().slice(0, 1).toUpperCase(),
                        avatarUrl: avatarUrl || undefined,
                        blocks: [{
                            type: 'link',
                            url: trackInfo.url || '#',
                            title: trackInfo.name || musicT('music.unknownTrack', 'Unknown Track'),
                            description: trackInfo.artist || musicT('music.unknownArtist', 'Unknown Artist'),
                            siteName: '⏳ ' + musicT('music.loading', 'Loading'),
                            thumbnailUrl: displayCoverUrl
                        }],
                        status: 'sent'
                    });
                }
            }
        }
        if (timeTotal) timeTotal.textContent = '00:00';

        // --- 3. APlayer 实例管理 (复用或创建) ---
        try {
            const apBtn = musicBar.querySelector('.music-bar-play');
            const updatePlayBtnState = (isPlaying) => {
                const icon = isPlaying ? '⏸' : '▶';
                const text = isPlaying ? 'Pause' : 'Play';
                const tText = musicT(isPlaying ? 'music.pause' : 'music.play', text);
                apBtn.textContent = icon;
                apBtn.setAttribute('title', tText);
                apBtn.setAttribute('aria-label', tText);
                setMusicBarVisualState(musicBar, isPlaying ? 'playing' : 'paused');
            };

            let needsInit = isFirstRender || !localPlayer;
            let autoplayBlocked = false;

            if (needsInit) {
                const container = musicBar.querySelector('.aplayer-internal-container');
                const playerConfig = {
                    container: container,
                    theme: MUSIC_CONFIG.primaryColor,
                    loop: 'none',
                    preload: shouldAutoPlay ? 'auto' : 'metadata',
                    autoplay: shouldAutoPlay,
                    mutex: true, volume: MUSIC_CONFIG.defaultVolume,
                    listFolded: true, order: 'normal',
                    audio: [{ name: trackInfo.name, artist: trackInfo.artist, url: trackInfo.url, cover: displayCoverUrl }]
                };

                let aplayerInstance = null;
                if (typeof window.initializeAPlayer === 'function')
                    aplayerInstance = await window.initializeAPlayer(playerConfig);
                else
                    aplayerInstance = new window.APlayer(playerConfig);

                if (!aplayerInstance) throw new Error("APlayer init failed");
                if (currentToken !== latestMusicRequestToken) {
                    if (aplayerInstance.destroy) aplayerInstance.destroy();
                    // 回滚：前面 emitBarInitialState 已经让 follower 建起占位
                    // bar，但现在请求被更新的 token 取代，我们不会再发权威
                    // state，得主动广播 destroyed 把占位 bar 摘掉，不然 follower
                    // 会卡在一条假 bar 直到被下一次 state 盖掉。
                    broadcastBarDestroyed(false, playbackIdForRequest);
                    return musicPlayResult(false, 'superseded');
                }

                localPlayer = aplayerInstance;
                localPlayer._latestToken = currentToken;
                localPlayer._musicPlaybackReportContext = playbackReportContext;
                window.aplayer = localPlayer;
                if (!window.aplayerInjected) window.aplayerInjected = {};
                window.aplayerInjected.aplayer = localPlayer;

                // --- 绑定核心事件 (仅在初始化时绑定一次) ---
                // 【核心修复】使用闭包固定当前的播放器实例
                const boundPlayer = localPlayer;

                boundPlayer.on('play', () => {
                    const reportContext = getOwnedMusicPlaybackReportContext(boundPlayer, 'playing');
                    if (!reportContext) return;
                    reportContext.mediaReady = true;
                    if (autoDestroyTimer) { clearTimeout(autoDestroyTimer); autoDestroyTimer = null; }
                    updatePlayBtnState(true);
                    autoplayBlocked = false;
                    boundPlayer._loadError = false;
                    if (!playbackStartedAt) playbackStartedAt = Date.now();
                    updateMusicCard('playing', currentPlayingTrack);
                    reportMusicPlaybackState('playing', null, reportContext);
                    // 跨窗口协调：本地真正开始放歌后通知其他窗口
                    broadcastMusicCoord('music_started');
                    startMusicHeartbeat();
                    emitBarState();
                });
                boundPlayer.on('pause', () => {
                    const playbackState = boundPlayer.audio && boundPlayer.audio.ended ? 'ended' : 'paused';
                    const reportContext = getOwnedMusicPlaybackReportContext(boundPlayer, playbackState);
                    if (!reportContext) return;
                    if (!reportContext.mediaReady && reportContext.hasNextCandidate) return;
                    updatePlayBtnState(false);
                    const tokenAtEvent = boundPlayer._latestToken;
                    if (autoDestroyTimer) clearTimeout(autoDestroyTimer);
                    autoDestroyTimer = setTimeout(() => {
                        if (localPlayer === boundPlayer && boundPlayer._latestToken === tokenAtEvent) {
                            destroyMusicPlayer(true, true, true);
                        }
                    }, MUSIC_CONFIG.timeouts.paused);
                    updateMusicCard(playbackState, currentPlayingTrack);
                    reportMusicPlaybackState(
                        playbackState,
                        null,
                        reportContext
                    );
                    emitBarState();
                });
                boundPlayer.on('ended', () => {
                    const reportContext = getOwnedMusicPlaybackReportContext(boundPlayer, 'ended');
                    if (!reportContext) return;
                    updatePlayBtnState(false);
                    resetSkipCounter();
                    notifyMusicPlayedThrough(currentPlayingTrack);
                    playbackStartedAt = 0;
                    const tokenAtEvent = boundPlayer._latestToken;
                    if (autoDestroyTimer) clearTimeout(autoDestroyTimer);
                    autoDestroyTimer = setTimeout(() => {
                        if (localPlayer === boundPlayer && boundPlayer._latestToken === tokenAtEvent) {
                            destroyMusicPlayer(true, true, true);
                        }
                    }, MUSIC_CONFIG.timeouts.ended);
                    updateMusicCard('ended', currentPlayingTrack);
                    reportMusicPlaybackState('ended', null, reportContext);
                    emitBarState();
                });
                boundPlayer.on('error', (err) => {
                    if (boundPlayer._destroying) return;
                    if (!boundPlayer.audio || !boundPlayer.audio.error) {
                        console.log('[Music UI] Ignoring stale media error without an active MediaError');
                        return;
                    }
                    const failedSource = err && err.target && (err.target.currentSrc || err.target.src);
                    const activeSource = boundPlayer.audio && (boundPlayer.audio.currentSrc || boundPlayer.audio.src);
                    if (failedSource && activeSource && resolveMusicUrl(failedSource) !== resolveMusicUrl(activeSource)) {
                        console.log('[Music UI] Ignoring stale media error from the previous source:', failedSource);
                        return;
                    }
                    const reportContext = getOwnedMusicPlaybackReportContext(boundPlayer, 'error');
                    if (!reportContext) return;
                    const eventTimestamp = normalizeMusicEventTimestamp(err);
                    if (
                        eventTimestamp !== null
                        && eventTimestamp < reportContext.lifecycleStartedAt
                    ) {
                        console.log('[Music UI] Ignoring queued media error from the previous lifecycle');
                        return;
                    }
                    if (eventTimestamp === null && reportContext.mediaReady !== true) {
                        console.log('[Music UI] Ignoring unowned media error before readiness');
                        return;
                    }

                    const tokenAtEvent = reportContext.token;

                    setTimeout(() => {
                        if (
                            getOwnedMusicPlaybackReportContext(boundPlayer, 'error') !== reportContext
                        ) return;
                        if (autoplayBlocked) return;
                        if (boundPlayer._destroying) return;

                        console.error('[Music UI] APlayer error:', err);
                        playbackStartedAt = 0;
                        boundPlayer._loadError = true;

                        // 仍处于首轮媒体加载、并且后面确有候选时，当前错误只驱动
                        // 内部回退。候选一旦就绪，后续流错误仍必须正常反馈给用户。
                        const deferFailureUi = (
                            !reportContext.mediaReady
                            && shouldDeferCandidateFailureUi(reportContext, 'media_error')
                        );
                        reportMusicPlaybackFailureIfNeeded(
                            reportContext,
                            'media_error',
                            deferFailureUi
                        );
                        if (deferFailureUi) return;
                        if (reportContext.failureUiHandled) return;
                        reportContext.failureUiHandled = true;

                        let errorDetail = musicT('music.playError', 'Playback failed');
                        if (err && err.message) errorDetail = err.message;

                        showErrorToast('music.playError', errorDetail);
                        updatePlayBtnState(false);
                        setMusicBarVisualState(musicBar, 'error');
                        if (autoDestroyTimer) clearTimeout(autoDestroyTimer);
                        autoDestroyTimer = setTimeout(() => {
                            if (localPlayer === boundPlayer && boundPlayer._latestToken === tokenAtEvent) {
                                destroyMusicPlayer(true, true, true);
                            }
                        }, 3000);

                        updateMusicCard('error', currentPlayingTrack);
                        emitBarState();
                    }, 200);
                });

                // 进度条与播放按钮点击 (使用直接赋值防止重复挂载)
                musicBar.querySelector('.music-bar-close').onclick = (e) => {
                    e.preventDefault();
                    recordMusicCloseFeedback(playbackStartedAt);
                    playbackStartedAt = 0;
                    destroyMusicPlayer(true, true, true);
                };
                apBtn.onclick = (e) => {
                    e.preventDefault();
                    if (autoDestroyTimer) clearTimeout(autoDestroyTimer);
                    if (typeof window.setMusicUserDriven === 'function') window.setMusicUserDriven();

                    if (boundPlayer._loadError) {
                        destroyMusicPlayer(true, true, true);
                        return;
                    }

                    if (boundPlayer.audio.ended) boundPlayer.seek(0);
                    boundPlayer.toggle();
                };

                // --- 音量控制逻辑 ---
                const volumeContainer = musicBar.querySelector('.music-bar-volume-container');
                const volumeBtn = musicBar.querySelector('.music-bar-volume-btn');
                const volumeSliderWrapper = musicBar.querySelector('.music-bar-volume-slider-wrapper');
                const volumeSlider = musicBar.querySelector('.music-bar-volume-slider');
                const volumeFill = musicBar.querySelector('.music-bar-volume-slider-fill');
                const volumeHandle = musicBar.querySelector('.music-bar-volume-slider-handle');

                const updateVolumeUI = (vol) => {
                    const percent = vol * 100;
                    volumeFill.style.height = percent + '%';
                    volumeHandle.style.bottom = percent + '%';

                    if (vol === 0) volumeBtn.textContent = '🔇';
                    else if (vol < 0.5) volumeBtn.textContent = '🔉';
                    else volumeBtn.textContent = '🔊';

                    const volText = musicT('music.volume', 'Volume: ') + Math.round(percent) + '%';
                    volumeBtn.setAttribute('title', volText);
                    volumeBtn.setAttribute('aria-label', volText);
                };

                // 初始化音量 UI
                updateVolumeUI(boundPlayer.volume());

                volumeBtn.onclick = (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    volumeContainer.classList.toggle('expanded');
                    requestCompactMusicGeometrySync();
                };

                let isDraggingVolume = false;
                const adjustVolume = (e) => {
                    const rect = volumeSlider.getBoundingClientRect();

                    let clientY;
                    if (e.clientY !== undefined) {
                        clientY = e.clientY;
                    } else if (e.touches && e.touches.length > 0) {
                        clientY = e.touches[0].clientY;
                    } else {
                        boundPlayer.volume(MUSIC_CONFIG.defaultVolume);
                        updateVolumeUI(MUSIC_CONFIG.defaultVolume);
                        return;
                    }

                    let y = rect.bottom - clientY;
                    let per = Math.max(0, Math.min(y, rect.height)) / rect.height;
                    boundPlayer.volume(per);
                    updateVolumeUI(per);
                };

                currentVolumeDragHandlers = {
                    cleanup: () => {
                        window.removeEventListener('mousemove', adjustVolume);
                        window.removeEventListener('mouseup', stopVolumeDrag);
                        window.removeEventListener('touchmove', adjustVolume);
                        window.removeEventListener('touchend', stopVolumeDrag);
                        window.removeEventListener('touchcancel', stopVolumeDrag);
                        isDraggingVolume = false;
                    }
                };

                const stopVolumeDrag = (e) => {
                    if (!isDraggingVolume) return;
                    // 拖拽结束时，直接调用清理工具
                    if (currentVolumeDragHandlers) currentVolumeDragHandlers.cleanup();
                };

                volumeSliderWrapper.onmousedown = (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    isDraggingVolume = true;
                    adjustVolume(e);
                    // 直接使用原生绑定
                    window.addEventListener('mousemove', adjustVolume);
                    window.addEventListener('mouseup', stopVolumeDrag);
                };

                volumeSliderWrapper.ontouchstart = (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    isDraggingVolume = true;
                    adjustVolume(e);
                    window.addEventListener('touchmove', adjustVolume);
                    window.addEventListener('touchend', stopVolumeDrag);
                    window.addEventListener('touchcancel', stopVolumeDrag);
                };

                // 点击外部收起音量
                const closeVolumeOnOutsideClick = (e) => {
                    if (volumeContainer.classList.contains('expanded') && !volumeContainer.contains(e.target)) {
                        volumeContainer.classList.remove('expanded');
                        requestCompactMusicGeometrySync();
                    }
                };
                addManagedListener('mousedown', closeVolumeOnOutsideClick);

                // 同步 APlayer 的音量变化
                boundPlayer.on('volumechange', () => {
                    updateVolumeUI(boundPlayer.volume());
                    emitBarState();
                });

                // 进度更新与拖拽 (保持原有逻辑)
                let isDragging = false;
                const progressContainer = musicBar.querySelector('.music-bar-progress-container');
                localPlayer.on('timeupdate', () => {
                    if (!localPlayer || !localPlayer.audio || isDragging) return;
                    const cur = localPlayer.audio.currentTime, dur = localPlayer.audio.duration;
                    if (dur > 0) {
                        if (progressFill) progressFill.style.width = (cur / dur * 100) + '%';
                        if (timeCurrent) timeCurrent.textContent = formatTime(cur);
                        if (timeTotal) timeTotal.textContent = formatTime(dur);
                    }
                    // timeupdate 一秒 ~4 次，限速到 500ms 一条状态镜像
                    emitBarStateThrottled(500);
                });

                const handleProgressMove = (e) => {
                    if (!isDragging) return;
                    const rect = progressContainer.getBoundingClientRect();
                    if (!rect.width) return;

                    // 修复细节：判断 0 的写法，防止 clientX 为 0 时误触 fallback
                    const clientX = e.clientX !== undefined ? e.clientX : (e.touches && e.touches[0] ? e.touches[0].clientX : 0);
                    let x = clientX - rect.left;

                    x = Math.max(0, Math.min(x, rect.width));
                    const per = x / rect.width;
                    if (progressFill) progressFill.style.width = (per * 100) + '%';
                    if (timeCurrent && localPlayer.audio.duration) timeCurrent.textContent = formatTime(per * localPlayer.audio.duration);
                };
                const stopDrag = (e) => {
                    if (!isDragging) return;
                    isDragging = false;
                    // 【核心修复】必须先移除全局监听，然后再执行可能的 early return (CodeRabbit 建议)
                    window.removeEventListener('mousemove', handleProgressMove);
                    window.removeEventListener('mouseup', stopDrag);
                    window.removeEventListener('touchmove', handleProgressMove);
                    window.removeEventListener('touchend', stopDrag);

                    const rect = progressContainer.getBoundingClientRect();
                    if (!rect.width) return;

                    const clientX = e.clientX !== undefined ? e.clientX : (e.changedTouches && e.changedTouches[0] ? e.changedTouches[0].clientX : 0);
                    let x = clientX - rect.left;

                    const per = Math.max(0, Math.min(x, rect.width)) / rect.width;
                    if (boundPlayer.audio.duration) boundPlayer.seek(per * boundPlayer.audio.duration);
                };

                // 记录全局引用以便销毁时清理
                currentDragHandlers = {
                    cleanup: () => {
                        window.removeEventListener('mousemove', handleProgressMove);
                        window.removeEventListener('mouseup', stopDrag);
                        window.removeEventListener('touchmove', handleProgressMove);
                        window.removeEventListener('touchend', stopDrag);
                        window.removeEventListener('touchcancel', stopDrag);
                        isDragging = false;
                    }
                };

                // 【核心修复】使用直接赋值绑定，防止 DOM 复用时监听器叠加 (CodeRabbit 建议)
                progressContainer.onmousedown = (e) => {
                    isDragging = true; handleProgressMove(e);
                    window.addEventListener('mousemove', handleProgressMove); window.addEventListener('mouseup', stopDrag);
                    window.addEventListener('touchmove', handleProgressMove); window.addEventListener('touchend', stopDrag);
                };
                progressContainer.ontouchstart = (e) => {
                    isDragging = true; handleProgressMove(e);
                    window.addEventListener('mousemove', handleProgressMove); window.addEventListener('mouseup', stopDrag);
                    window.addEventListener('touchmove', handleProgressMove); window.addEventListener('touchend', stopDrag);
                    window.addEventListener('touchcancel', stopDrag);
                };

                // 自动播放拦截器：精确区分“被拦截”与“加载失败”
                if (localPlayer.audio && typeof localPlayer.audio.play === 'function') {
                    const originalPlay = localPlayer.audio.play;
                    // 使用闭包捕获当前的播放器引用
                    const boundPlayerForProxy = localPlayer;
                    localPlayer.audio.play = function () {
                        // 捕获触发播放时的 token
                        const tokenAtPlay = latestMusicRequestToken;
                        const pp = originalPlay.call(this);
                        if (pp && pp.catch) {
                            pp.catch(err => {
                                // 逻辑漏洞修复：如果 play 失败的回调执行时，用户已经切换了下一首歌，则不应再为旧歌曲设置销毁定时器
                                if (tokenAtPlay !== latestMusicRequestToken) {
                                    console.log('[Music UI] Observed rejected play promise from obsolete token, ignoring.');
                                    return;
                                }

                                if (err.name === 'NotAllowedError') {
                                    autoplayBlocked = true;
                                    updatePlayBtnState(false);
                                    updateMusicCard('paused', currentPlayingTrack);
                                    showErrorToast('music.autoplayBlocked', 'Browser blocked autoplay. Click the play button to continue.');

                                    // 自动播放被拦截视为“未播放”，保持 24 秒销毁计时
                                    if (autoDestroyTimer) clearTimeout(autoDestroyTimer);
                                    autoDestroyTimer = setTimeout(() => {
                                        if (tokenAtPlay === latestMusicRequestToken) {
                                            destroyMusicPlayer(true, true, true);
                                        }
                                    }, MUSIC_CONFIG.timeouts.idle);

                                    // 交互式代理：一旦被拦截，监听全局下一次点击并尝试自动播放
                                    setupAutoplayProxy(tokenAtPlay, boundPlayerForProxy);
                                }
                            });
                        }
                        return pp;
                    };
                }

                function setupAutoplayProxy(tokenAtProxy, bPlayer) {
                    const startOnInteraction = () => {
                        // 【核心修复】增加 token 校验，且交互后移除监听
                        // 如果在点击之前，用户已经切换到了新的请求，或者播放器已销毁，则不执行旧的播放操作
                        if (tokenAtProxy === latestMusicRequestToken && bPlayer && bPlayer.audio && bPlayer.audio.paused) {
                            console.log('[Music UI] 检测到用户交互，正在尝试通过代理触发延迟播放');
                            bPlayer.play();
                        }
                        // 使用一旦触发即移除的特性，手动解绑所有潜在的代理监听
                        window.removeEventListener('mousedown', startOnInteraction);
                        window.removeEventListener('touchstart', startOnInteraction);
                    };
                    window.addEventListener('mousedown', startOnInteraction, { once: true });
                    window.addEventListener('touchstart', startOnInteraction, { once: true });
                    // 这些 once 监听器也会被管理，虽然它们会自动移除
                    managedWindowListeners.push({ type: 'mousedown', listener: startOnInteraction, options: { once: true } });
                    managedWindowListeners.push({ type: 'touchstart', listener: startOnInteraction, options: { once: true } });
                }
            } else {
                // --- 复用模式下的切歌逻辑 ---
                // Reset skip-tracking so the previous track's timing doesn't carry over
                playbackStartedAt = 0;
                localPlayer._loadError = false;
                if (localPlayer.list) {
                    localPlayer.list.clear();
                    localPlayer.list.add([{ name: trackInfo.name, artist: trackInfo.artist, url: trackInfo.url, cover: displayCoverUrl }]);
                    localPlayer.list.switch(0);
                }
                updatePlayBtnState(false);
                setMusicBarVisualState(musicBar, 'loading');
            }

            // 【核心修复】同步更新实例的最新 Token，确保复用模式下事件回调中的 Token 校验依然有效
            localPlayer._latestToken = currentToken;
            localPlayer._musicPlaybackReportContext = playbackReportContext;
            localPlayer._loadError = false;
            const mediaReadyPromise = waitForMusicMediaReady(
                localPlayer,
                currentToken,
                trackInfo.url,
                playbackOptions.source === 'proactive',
                getCandidateMediaReadyTimeoutMs(playbackOptions)
            );

            // 执行播放
            if (shouldAutoPlay) {
                setTimeout(() => {
                    // 【核心修复】延迟播放校验 Token，防止旧请求误触发新曲播放 (CodeRabbit 建议)
                    if (currentToken === latestMusicRequestToken && localPlayer && typeof localPlayer.play === 'function') {
                        localPlayer.play();
                    }
                }, 100);
            } else {
                // AI 推荐但是未点击自动播放，启动 24 秒销毁计时
                if (autoDestroyTimer) clearTimeout(autoDestroyTimer);
                autoDestroyTimer = setTimeout(() => destroyMusicPlayer(true, true, true), MUSIC_CONFIG.timeouts.idle);
            }

            const mediaResult = await mediaReadyPromise;
            if (currentToken !== latestMusicRequestToken) return musicPlayResult(false, 'superseded');
            if (!mediaResult.ok) {
                localPlayer._loadError = true;
                const deferFailureUi = shouldDeferCandidateFailureUi(playbackOptions, mediaResult.reason);
                reportMusicPlaybackFailureIfNeeded(
                    playbackReportContext,
                    mediaResult.reason,
                    deferFailureUi
                );
                if (!playbackReportContext.failureUiHandled && !deferFailureUi) {
                    playbackReportContext.failureUiHandled = true;
                    if (mediaResult.reason === 'track_too_long') {
                        showErrorToast('music.trackTooLong', 'This track is too long for music recommendations');
                    } else if (mediaResult.reason === 'load_timeout') {
                        showErrorToast('music.loadTimeout', 'Music loading timed out');
                    }
                    setMusicBarVisualState(musicBar, 'error');
                    updateMusicCard('error', currentPlayingTrack);
                    emitBarState();
                }
                return musicPlayResult(
                    false,
                    mediaResult.reason,
                    canTryNextMusicCandidate(mediaResult.reason)
                );
            }
            playbackReportContext.mediaReady = true;
            if (!shouldAutoPlay) {
                setMusicBarVisualState(musicBar, 'paused');
                updateMusicCard('paused', currentPlayingTrack);
            }
            return musicPlayResult(true, mediaResult.reason);
        } catch (err) {
            if (currentToken !== latestMusicRequestToken) return musicPlayResult(false, 'superseded');
            console.error('[Music UI] 播放器处理异常:', err);
            reportMusicPlaybackFailureIfNeeded(playbackReportContext, 'player_error', false);
            updateMusicCard('error', currentPlayingTrack);
            destroyMusicPlayer(true, false, true);
            showErrorToast('music.playError', 'Music playback failed to load');
            return musicPlayResult(false, 'player_error');
        }
    };

    // --- 6. 暴露全局接口 ---
    /**
     * 向播放器发送播放请求 [Async Ready]
     * 如果 URL 暂时不在白名单中，会等待最多 500ms 以响应并行的插件注册
     */
    window.sendMusicMessageDetailed = async function (trackInfo, shouldAutoPlay = true, playbackOptions = {}) {
        if (!trackInfo) return musicPlayResult(false, 'invalid_track', true);

        // 进入 dispatch 流水线就立即 +1 —— 让并发的 dispatchMusicPlay
        // 能在 isMusicPlaying() 还未变成 true 的"加载中"窗口里也识别到占用。
        // 用本地 pendingReleased 防止重复释放。
        musicDispatchPendingCount += 1;
        let pendingReleased = false;
        const releasePending = () => {
            if (pendingReleased) return;
            pendingReleased = true;
            musicDispatchPendingCount = Math.max(0, musicDispatchPendingCount - 1);
        };
        broadcastMusicCoord('music_pending');

        // Keep playback and plugin allowlist registration on the same URL form.
        if (trackInfo.url && typeof trackInfo.url === 'string') {
            trackInfo.url = normalizeMusicUrlEscapes(trackInfo.url);
        }

        const currentToken = ++latestMusicRequestToken;
        if (pendingMusicMediaReadyCancel) pendingMusicMediaReadyCancel();

        // 竞态保护：如果 URL 不在白名单，原地等待 500ms 看看是否会有插件注册进来
        if (trackInfo.url && !isSafeUrl(trackInfo.url)) {
            console.log('[Music UI] URL 暂未加入白名单，等待加白信号...', trackInfo.url);
            try {
                await new Promise((resolve) => {
                    const timeout = setTimeout(() => {
                        window.removeEventListener('music-allowlist-updated', onUpdate);
                        resolve();
                    }, 500);

                    function onUpdate() {
                        if (isSafeUrl(trackInfo.url)) {
                            console.log('[Music UI] 收到加白信号，URL 已加白名单。');
                            clearTimeout(timeout);
                            window.removeEventListener('music-allowlist-updated', onUpdate);
                            resolve();
                        }
                    }
                    window.addEventListener('music-allowlist-updated', onUpdate);
                });
            } catch (e) {
                console.warn('[Music UI] 竞态等待异常:', e);
            }
        }

        if (currentToken !== latestMusicRequestToken) {
            releasePending();
            return musicPlayResult(false, 'superseded');
        }

        if (trackInfo.url && isUnsupportedMusicStream(trackInfo.url)) {
            console.warn('[Music UI] 不支持直接播放 HLS 音频流:', trackInfo.url);
            if (!shouldDeferCandidateFailureUi(playbackOptions, 'unsupported_stream')) {
                showErrorToast('music.playError', 'This audio stream is not supported');
                finalizeScopedMusicCardFailure(trackInfo, playbackOptions);
            }
            releasePending();
            return musicPlayResult(false, 'unsupported_stream', true);
        }

        if (!trackInfo.url || !isSafeUrl(trackInfo.url)) {
            console.warn('[Music UI] 音频 URL 未通过安全校验:', trackInfo.url);
            if (!shouldDeferCandidateFailureUi(playbackOptions, 'unsafe_url') && window.showStatusToast) {
                var domain = extractHostname(trackInfo.url) || musicT('music.unknownSource', 'Unknown source');
                var msg = musicT('music.unsafeSource', 'Blocked unsafe audio source: {{domain}}', { domain: domain });
                window.showStatusToast(msg, 5000);
            }
            if (!shouldDeferCandidateFailureUi(playbackOptions, 'unsafe_url')) {
                finalizeScopedMusicCardFailure(trackInfo, playbackOptions);
            }
            releasePending();
            return musicPlayResult(false, 'unsafe_url', true);
        }

        const originalUrl = trackInfo.url;
        trackInfo.url = toBackendMusicProxyUrl(originalUrl);
        if (trackInfo.url !== originalUrl) {
            console.log('[Music UI] 音频 URL 已转为本地代理:', originalUrl, '->', trackInfo.url);
        }

        const now = Date.now();
        // 5秒去重逻辑
        if (lastPlayedMusicUrl === trackInfo.url && (now - lastMusicPlayTime) < 5000 && isPlayerInDOM()) {
            const duplicatePlayer = getMusicPlayerInstance();
            const duplicateAudio = duplicatePlayer && duplicatePlayer.audio;
            if (
                duplicatePlayer
                && !duplicatePlayer._loadError
                && duplicateAudio
                && !duplicateAudio.paused
                && !duplicateAudio.ended
                && duplicateAudio.readyState >= 2
            ) {
                setMusicPlaybackContext(playbackOptions);
                const duplicateReportContext = createMusicPlaybackReportContext(
                    getCurrentMusicPlaybackId(),
                    playbackOptions,
                    trackInfo,
                    latestMusicRequestToken
                );
                duplicateReportContext.mediaReady = true;
                duplicatePlayer._latestToken = latestMusicRequestToken;
                duplicatePlayer._musicPlaybackReportContext = duplicateReportContext;
                reportMusicPlaybackState('playing', null, duplicateReportContext);
                console.log('[Music UI] 5秒内相同音乐且已在播放中，跳过播发请求:', trackInfo.name);
                releasePending();
                return musicPlayResult(true, 'duplicate');
            }
        }

        if (isSameTrack(trackInfo) && !isPlayerInDOM()) {
            currentPlayingTrack = null;
        }

        lastPlayedMusicUrl = trackInfo.url;
        lastMusicPlayTime = now;

        // 特殊优化：如果是一模一样的歌曲且播放器已存在，直接播放而不是重载整个库
        if (isSameTrack(trackInfo) && isPlayerInDOM()) {
            const player = getMusicPlayerInstance();
            if (!player) {
                destroyMusicPlayer(true, false, false);
            } else if (player._loadError) {
                destroyMusicPlayer(true, false, false);
            } else if (!player.audio || player.audio.readyState < 2) {
                // A superseded request can leave the same source in the DOM
                // while it is still loading. Rebuild it so this request owns
                // a fresh readiness result and can fall back on failure.
                destroyMusicPlayer(true, false, false);
            } else {
                setMusicPlaybackContext(playbackOptions);
                const playbackReportContext = createMusicPlaybackReportContext(
                    getCurrentMusicPlaybackId(),
                    playbackOptions,
                    trackInfo,
                    latestMusicRequestToken
                );
                playbackReportContext.mediaReady = true;
                player._latestToken = latestMusicRequestToken;
                player._musicPlaybackReportContext = playbackReportContext;
                if (shouldAutoPlay && player && player.audio && player.audio.paused) {
                    if (typeof window.setMusicUserDriven === 'function')
                        window.setMusicUserDriven();
                    player.play();
                    showNowPlayingToast(trackInfo.name);
                } else if (player && player.audio && !player.audio.paused) {
                    reportMusicPlaybackState('playing', null, playbackReportContext);
                }
                releasePending();
                return musicPlayResult(true, 'already_playing');
            }
        }

        // A single <audio> cannot identify which load produced an error when
        // successive lifecycles use the same URL. If no fast path above
        // accepted the existing media, isolate the retry on a new element.
        const currentAudioForRequest = localPlayer && localPlayer.audio;
        const currentAudioUrl = currentAudioForRequest
            && (currentAudioForRequest.currentSrc || currentAudioForRequest.src);
        if (
            currentAudioUrl
            && resolveMusicUrl(currentAudioUrl) === resolveMusicUrl(trackInfo.url)
        ) {
            destroyMusicPlayer(true, false, false);
        }

        try {
            await loadAPlayerLibrary();
            const result = await executePlay(trackInfo, currentToken, shouldAutoPlay, playbackOptions);
            if (!result.ok && currentToken === latestMusicRequestToken) {
                destroyMusicPlayer(
                    true,
                    false,
                    true,
                    shouldDeferCandidateFailureUi(playbackOptions, result.reason)
                );
            }
            if (result.ok && shouldAutoPlay) showNowPlayingToast(trackInfo.name);
            return result;
        } catch (err) {
            // 库加载失败同样需要校验 token，防止关闭后弹出报错
            if (currentToken === latestMusicRequestToken) {
                console.error('[Music UI] 库加载失败:', err);
                showErrorToast('music.loadError', 'Music player failed to load');
            } else {
                console.log('[Music UI] 库加载失败，但请求已取消，忽略报错');
            }
            return musicPlayResult(
                false,
                currentToken === latestMusicRequestToken ? 'player_error' : 'superseded'
            );
        } finally {
            // 每次调用独立释放：不用 token 判断，本次引用计数 -1 就好。
            releasePending();
        }
    };

    window.sendMusicMessage = async function (trackInfo, shouldAutoPlay = true, playbackOptions = {}) {
        const result = await window.sendMusicMessageDetailed(trackInfo, shouldAutoPlay, playbackOptions);
        return result.ok === true;
    };
    // 全局解锁函数
    const unlockAudio = () => {
        console.log('[Audio] 检测到交互，尝试激活音频环境...');

        // 1. 解锁 Web Audio API
        if (window.lanlanAudioContext && window.lanlanAudioContext.state === 'suspended') {
            window.lanlanAudioContext.resume();
        }

        // 2. 解锁 APlayer 实例 (如果有的话)
        const player = window.aplayer || (window.aplayerInjected && window.aplayerInjected.aplayer);
        if (player && player.audio && player.audio.paused) {
            // 如果当前有排队中的音乐，尝试播放
            const playPromise = player.play();
            if (playPromise !== undefined && typeof playPromise.catch === 'function') {
                playPromise.catch(() => { });
            }
        }

        // 移除监听器，只需触发一次
        document.removeEventListener('click', unlockAudio);
        document.removeEventListener('keydown', unlockAudio);
    };

    // 监听任何点击或按键
    document.addEventListener('click', unlockAudio, { once: true });
    document.addEventListener('keydown', unlockAudio, { once: true });

    const isMusicPlaying = () => {
        try {
            return !!(localPlayer && localPlayer.audio && !localPlayer.audio.paused && isPlayerInDOM());
        } catch (e) {
            console.error('[Music UI] Error checking if music is playing:', e);
            return false;
        }
    };

    const isMusicOccupied = () => {
        try {
            const localAudio = localPlayer && localPlayer.audio;
            const localOccupied = !!(
                localAudio && !localAudio.ended && !localPlayer._loadError && isPlayerInDOM()
            );
            const remoteOccupied = isRemoteMusicActive();
            if (
                mirrorBarLeaderSender
                && !remoteMusicSenders.has(mirrorBarLeaderSender)
            ) {
                teardownMirrorBar(false);
                setMirrorBarLeader(null);
            }
            const mirrorOccupied = !!(
                mirrorBarLastState && mirrorBarLastState.track
                && !mirrorBarLastState.ended && !mirrorBarLastState.loadError
            );
            return musicDispatchPendingCount > 0 || localOccupied || mirrorOccupied || remoteOccupied;
        } catch (e) {
            console.error('[Music UI] Error checking if music is occupied:', e);
            return false;
        }
    };

    const getMusicCurrentTrack = () => {
        try {
            return currentPlayingTrack || null;
        } catch (e) {
            console.error('[Music UI] Error getting current track:', e);
            return null;
        }
    };

    // --- 自动从后端同步音乐源域名到白名单 ---
    const syncDomainsFromBackend = async () => {
        try {
            const response = await fetch('/api/music/domains');
            if (response.ok) {
                const data = await response.json();
                if (data.success && data.domains) {
                    data.domains.forEach(domain => backendProxyDomains.add(domain));
                    const newDomains = data.domains.filter(d => !MUSIC_CONFIG.allowlist.includes(d));
                    if (newDomains.length > 0) {
                        MUSIC_CONFIG.allowlist.push(...newDomains);
                        console.log('[Music UI] 已同步后端域名到白名单', newDomains);
                        window.dispatchEvent(new CustomEvent('music-allowlist-updated'));
                    }
                }
            }
        } catch (e) {
            console.warn('[Music UI] 从后端同步域名失败:', e);
        }
    };

    const MusicPluginAPI = {
        getAllowlist: () => [...MUSIC_CONFIG.allowlist],
        getHttpUrls: () => [...pluginHttpUrls],
        addAllowlist: (input, httpUrlInput = []) => {
            const inputs = Array.isArray(input) ? input : [input];
            const newDomains = inputs
                .map(extractHostname)
                .filter(d => d && !MUSIC_CONFIG.allowlist.includes(d));
            const explicitHttpInputs = Array.isArray(httpUrlInput) ? httpUrlInput : [httpUrlInput];
            const httpInputs = inputs.concat(explicitHttpInputs);
            const newHttpUrls = httpInputs
                .map(value => {
                    try {
                        const parsed = new URL(normalizeMusicUrlEscapes(value));
                        return parsed.protocol === 'http:' ? parsed.href : null;
                    } catch (_) { return null; }
                })
                .filter(value => value && !pluginHttpUrls.has(value));

            if (newDomains.length > 0) {
                MUSIC_CONFIG.allowlist.push(...newDomains);
                console.log('[Music UI] Allowlist updated:', newDomains);
            }
            if (newHttpUrls.length > 0) {
                newHttpUrls.forEach(value => pluginHttpUrls.add(value));
                console.log('[Music UI] HTTP URL allowlist updated:', newHttpUrls);
            }
            if (newDomains.length > 0 || newHttpUrls.length > 0) {
                window.dispatchEvent(new CustomEvent('music-allowlist-updated'));
            }
        }
    };

    // --- 暴露接口 ---
    window.destroyMusicPlayer = destroyMusicPlayer;
    window.getMusicPlayerInstance = getMusicPlayerInstance;
    window.isMusicPlaying = isMusicPlaying;
    window.isMusicOccupied = isMusicOccupied;
    window.isMusicCooldown = isInMusicCooldown;
    window.getMusicCurrentTrack = getMusicCurrentTrack;
    window.MusicPluginAPI = MusicPluginAPI;
    window.cancelPendingMusicMediaReady = () => {
        latestMusicRequestToken++;
        if (pendingMusicMediaReadyCancel) pendingMusicMediaReadyCancel();
    };
    // 竞态拦截辅助：dispatch 流水线中（URL 校验/库加载/init）的占位标记
    window.isMusicPending = () => musicDispatchPendingCount > 0;
    // 跨窗口协调：其他窗口正在播歌（基于 BroadcastChannel 通报）
    window.isRemoteMusicActive = isRemoteMusicActive;
    // 推荐频率限流：最近是否刚派发过 proactive 推荐
    window.isMusicRecommendRateLimited = isMusicRecommendRateLimited;
    window.markProactiveMusicRecommended = markProactiveMusicRecommended;
    window.finalizeMusicCandidateCardFailure = finalizeScopedMusicCardFailure;

    // 派发就绪事件，通知提前加载的插件可以开始注册域名了
    window.dispatchEvent(new CustomEvent('music-ui-ready'));
    console.log('[Music UI] 接口已暴露，就绪信号已发送');

    async function syncDomainsAfterStorageBarrier() {
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

        syncDomainsFromBackend();
    }

    // 自动从后端同步音乐源域名到白名单
    syncDomainsAfterStorageBarrier();

})();
