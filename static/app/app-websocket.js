/**
 * app-websocket.js -- WebSocket connection, heartbeat, reconnect & message dispatch
 * Extracted from app.js lines 434-1617.
 *
 * Depends on:
 *   window.appState   (S) -- shared mutable state
 *   window.appConst   (C) -- frozen constants
 *   window.appAudioPlayback  -- audio playback helpers
 *   window.appChat           -- chat rendering helpers
 *   window.appScreen         -- screen sharing helpers
 *   window.appUi             -- UI helpers (toasts, buttons)
 */
(function () {
    'use strict';

    const mod = {};
    const S = window.appState;
    const C = window.appConst;
    const USER_ACTIVITY_CANCEL_GRACE_MS = 700;
    const GREETING_CHECK_RETRY_BASE_MS = 800;
    const GREETING_CHECK_RETRY_MAX_MS = 5000;
    const STARTUP_GREETING_RELEASE_FALLBACK_MS = 65000;
    const STARTUP_GREETING_RELEASE_EVENT = 'neko:startup-greeting-release';
    // Every cat-form return keeps the same minimum dwell time. Cat Mind
    // activity never bypasses this return-conversation gate.
    const CAT_GREETING_SILENT_BELOW_SECONDS = 180;
    const NEW_USER_ICEBREAKER_STORAGE_KEY = 'neko.new_user_icebreaker.v1';
    const NEW_USER_ICEBREAKER_BLOCKING_WINDOW_MS = 2 * 60 * 60 * 1000;
    const MUSIC_PLAY_URL_FOLLOWER_GRACE_MS = 500;
    const MUSIC_PLAY_URL_SECONDARY_CONFIRM_MS = 100;
    const MUSIC_PLAY_URL_CLAIM_TTL_MS = 5000;
    const MUSIC_PLAY_URL_CLAIM_CLEANUP_MS = 60000;
    const MUSIC_PLAY_URL_COORD_CHANNEL_NAME = 'neko_music_play_url_coord';
    const MUSIC_PLAY_URL_COORD_STORAGE_KEY = 'neko_music_play_url_coord';
    const CAPTURE_BRIDGE_REANNOUNCE_INTERVAL_MS = 250;
    const CAPTURE_BRIDGE_REANNOUNCE_MAX_ATTEMPTS = 40;
    const CAPTURE_BRIDGE_REGION_IMAGE_MAX_CHARS = 9 * 1024 * 1024;
    const GAME_ROUTE_ENDED_IDENTITY_LIMIT = 8;
    const GAME_ROUTE_ENDED_IDENTITY_TTL_MS = 2 * 60 * 1000;
    let _pendingUserActivityCancelTimer = 0;
    let _pendingUserActivityCancelTurnId = null;
    let _gameRouteReconciliationGeneration = 0;
    let _lanlanNameWaitAttempts = 0;
    let _lanlanNameWaitLastLogAt = 0;
    let _coreApiCapabilityRefreshPromise = null;
    let _coreApiCapabilityRequestGeneration = 0;
    let _musicPlayUrlCoordChannel = null;
    let _musicPlayUrlCoordChannelReady = false;
    let _musicPlayUrlClaims = Object.create(null);
    let _musicPlayUrlClaimCleanupTimer = 0;
    let _musicPlayUrlCoordBeforeUnloadBound = false;
    let _musicPlayUrlBroadcastUnavailableWarned = false;
    let _jukeboxControlQueue = Promise.resolve();
    // 「顶替」世代。就地取消只够停住「已经在跑」的那条；还在队列里等着的那条尚未
    // 取到任何取消世代，轮到它时会把此刻的世代当成最新的，于是在用户最后那条指令
    // 之后又响起来——而 play 要等运行时初始化、预检、动画加载，这一响可能是好几秒。
    // 播放类指令到达时记下当时的世代，轮到执行时对不上就整条跳过。
    //
    // 谁能顶替，按「绝对 / 相对」分：
    //   stop 与 play 是绝对的——一个要静音，一个点名要这首，排在它们前面还没开跑
    //   的播放指令一律作废；
    //   next / previous 是相对当前曲目算的，把排在它前面那条 play 吞掉的话，它算
    //   的就是旧位置了，所以它们只被顶替、不顶替别人。
    // set_volume / set_mode 两头都不沾，永远不会被顺手丢掉。
    //
    // 放在这里而不是 Jukebox 里：转发出去的指令在拥有者窗口用的是另一套计数器，
    // 作废判定必须留在发件的这一侧。
    let _jukeboxSupersedeGeneration = 0;
    let _latestAsrControlIdentity = null;
    let _seenAsrIncidentIds = Object.create(null);
    let _seenAsrIncidentOrder = [];
    const MAX_SEEN_ASR_INCIDENTS = 64;
    const MUSIC_PLAY_URL_SENDER_ID = (Date.now().toString(36) + Math.random().toString(36).slice(2, 10));

    function gameRouteStateRevision() {
        var revision = Number(S.gameRouteStateRevision);
        return Number.isFinite(revision) ? (revision >>> 0) : 0;
    }

    function advanceGameRouteStateRevision() {
        S.gameRouteStateRevision = (gameRouteStateRevision() + 1) >>> 0;
        return S.gameRouteStateRevision;
    }

    function pruneRecentlyEndedGameRouteIdentities() {
        var cutoff = Date.now() - GAME_ROUTE_ENDED_IDENTITY_TTL_MS;
        var identities = Array.isArray(S.gameRouteRecentlyEndedIdentities)
            ? S.gameRouteRecentlyEndedIdentities
            : [];
        identities = identities.filter(function (identity) {
            return identity && identity.sessionId && Number(identity.endedAt || 0) > cutoff;
        });
        if (identities.length > GAME_ROUTE_ENDED_IDENTITY_LIMIT) {
            identities = identities.slice(-GAME_ROUTE_ENDED_IDENTITY_LIMIT);
        }
        S.gameRouteRecentlyEndedIdentities = identities;
        return identities;
    }

    function rememberEndedGameRouteIdentity(gameType, sessionId, routeInstanceId) {
        var normalized = {
            gameType: String(gameType || ''),
            sessionId: String(sessionId || ''),
            routeInstanceId: String(routeInstanceId || ''),
            endedAt: Date.now()
        };
        if (!normalized.sessionId) return;
        var identities = pruneRecentlyEndedGameRouteIdentities().filter(function (identity) {
            return identity.gameType !== normalized.gameType
                || identity.sessionId !== normalized.sessionId
                || identity.routeInstanceId !== normalized.routeInstanceId;
        });
        identities.push(normalized);
        if (identities.length > GAME_ROUTE_ENDED_IDENTITY_LIMIT) {
            identities.splice(0, identities.length - GAME_ROUTE_ENDED_IDENTITY_LIMIT);
        }
        S.gameRouteRecentlyEndedIdentities = identities;
    }

    function isRecentlyEndedGameRouteIdentity(gameType, sessionId, routeInstanceId) {
        if (!sessionId) return false;
        var incomingGameType = String(gameType || '');
        var incomingSessionId = String(sessionId || '');
        var incomingRouteInstanceId = String(routeInstanceId || '');
        return pruneRecentlyEndedGameRouteIdentities().some(function (endedIdentity) {
            if (incomingSessionId !== String(endedIdentity.sessionId || '')) return false;
            if (incomingGameType && endedIdentity.gameType
                    && incomingGameType !== String(endedIdentity.gameType)) return false;
            if (!incomingRouteInstanceId) return true;
            return !!endedIdentity.routeInstanceId
                && incomingRouteInstanceId === String(endedIdentity.routeInstanceId);
        });
    }

    // ---- DOM element shortcuts (resolved lazily / once) ----
    function $id(id) { return document.getElementById(id); }
    function micButton()          { return $id('micButton'); }
    function muteButton()         { return $id('muteButton'); }
    function screenButton()       { return $id('screenButton'); }
    function stopButton()         { return $id('stopButton'); }
    function resetSessionButton() { return $id('resetSessionButton'); }
    function returnSessionButton(){ return $id('returnSessionButton'); }
    function textInputBox()       { return $id('textInputBox'); }
    function textSendButton()     { return $id('textSendButton'); }
    function screenshotButton()   { return $id('screenshotButton'); }
    function chatContainer()      { return $id('chatContainer'); }

    function resolveDesktopCaptureProvider() {
        return typeof window.getDesktopCaptureProvider === 'function'
            ? window.getDesktopCaptureProvider()
            : null;
    }

    function announceCaptureBridgeStatus(socket) {
        if (!socket || socket !== S.socket || socket.readyState !== WebSocket.OPEN) {
            return true;
        }
        try {
            var dc = resolveDesktopCaptureProvider();
            var available = !!(dc && (
                (dc.getSources && dc.captureSourceAsDataUrl)
                || dc.captureDesktopRegionAsDataUrl
            ));
            socket.send(JSON.stringify({
                action: 'capture_bridge_status',
                available: available,
                capabilities: {
                    getSources: !!(dc && dc.getSources),
                    captureSourceAsDataUrl: !!(dc && dc.captureSourceAsDataUrl),
                    captureSourceWithoutNeko: !!(dc && dc.captureSourceWithoutNeko),
                    captureDesktopRegionAsDataUrl: !!(dc && dc.captureDesktopRegionAsDataUrl)
                }
            }));
            return available;
        } catch (_) {
            return false;
        }
    }

    function getCaptureBridgeCropTranslations() {
        var keys = [
            'chat.cropTabScreenshot', 'chat.cropTabHideNeko', 'chat.cropTabCancel',
            'chat.cropClearSelectionTitle', 'chat.cropConfirmTitle'
        ];
        var translations = {};
        if (typeof window.t !== 'function') return translations;
        keys.forEach(function (key) {
            try {
                var value = window.t(key);
                if (typeof value === 'string' && value && value !== key) {
                    translations[key] = value;
                }
            } catch (_) { /* use crop overlay fallback */ }
        });
        return translations;
    }

    function loadCaptureBridgeImage(dataUrl) {
        return new Promise(function (resolve, reject) {
            var image = new Image();
            image.onload = function () { resolve(image); };
            image.onerror = function () { reject(new Error('invalid_capture_image')); };
            image.src = dataUrl;
        });
    }

    async function boundCaptureBridgeRegionImage(dataUrl) {
        if (typeof dataUrl !== 'string' || dataUrl.indexOf('data:image/') !== 0) return null;
        if (dataUrl.length <= CAPTURE_BRIDGE_REGION_IMAGE_MAX_CHARS) return dataUrl;

        var image;
        try {
            image = await loadCaptureBridgeImage(dataUrl);
        } catch (_) {
            return null;
        }
        var scales = [1, 0.85, 0.7, 0.55];
        var qualities = [0.92, 0.85, 0.75];
        for (var scaleIndex = 0; scaleIndex < scales.length; scaleIndex++) {
            var canvas = document.createElement('canvas');
            canvas.width = Math.max(1, Math.round(image.naturalWidth * scales[scaleIndex]));
            canvas.height = Math.max(1, Math.round(image.naturalHeight * scales[scaleIndex]));
            var context = canvas.getContext('2d');
            if (!context) return null;
            context.drawImage(image, 0, 0, canvas.width, canvas.height);
            for (var qualityIndex = 0; qualityIndex < qualities.length; qualityIndex++) {
                var candidate = canvas.toDataURL('image/jpeg', qualities[qualityIndex]);
                if (candidate.length <= CAPTURE_BRIDGE_REGION_IMAGE_MAX_CHARS) {
                    return candidate;
                }
                await Promise.resolve();
            }
        }
        return null;
    }

    function reannounceCaptureBridgeWhenReady(socket, attempt) {
        if (attempt >= CAPTURE_BRIDGE_REANNOUNCE_MAX_ATTEMPTS) return;
        setTimeout(function () {
            if (!socket || socket !== S.socket || socket.readyState !== WebSocket.OPEN) return;
            if (!announceCaptureBridgeStatus(socket)) {
                reannounceCaptureBridgeWhenReady(socket, attempt + 1);
            }
        }, CAPTURE_BRIDGE_REANNOUNCE_INTERVAL_MS);
    }

    function isGoodbyeUiSuppressed() {
        try {
            if (typeof window.isNekoGoodbyeResourceSuspendingOrSuspended === 'function' &&
                window.isNekoGoodbyeResourceSuspendingOrSuspended()) {
                return true;
            }
            if (typeof window.isNekoGoodbyeModeActive === 'function' && window.isNekoGoodbyeModeActive()) {
                return true;
            }
        } catch (_) { }
        return false;
    }

    async function releaseVoiceCaptureResources() {
        if (S.stream && typeof S.stream.getTracks === 'function') {
            S.stream.getTracks().forEach(function (track) {
                if (track && typeof track.stop === 'function') {
                    try {
                        track.stop();
                    } catch (error) {
                        console.warn('[App] mic track cleanup failed:', error);
                    }
                }
            });
        }
        S.stream = null;

        [S.workletNode, S.micGainNode, S.inputAnalyser].forEach(function (node) {
            if (node && typeof node.disconnect === 'function') {
                try {
                    node.disconnect();
                } catch (_) { }
            }
        });
        S.workletNode = null;
        S.micGainNode = null;
        S.inputAnalyser = null;

        if (S.audioContext) {
            var audioContext = S.audioContext;
            S.audioContext = null;
            if (audioContext.state !== 'closed' && typeof audioContext.close === 'function') {
                try {
                    await audioContext.close();
                } catch (error) {
                    console.warn('[App] audioContext cleanup failed:', error);
                }
            }
        }

        S.isRecording = false;
        window.isRecording = false;
    }

    async function resetVoiceUiAfterAutoClose(options) {
        var keepSwitchingMode = !!(options && options.keepSwitchingMode);

        if (S._voiceSessionInitialTimer) {
            clearTimeout(S._voiceSessionInitialTimer);
            S._voiceSessionInitialTimer = null;
        }

        if (typeof window.stopMicCapture === 'function') {
            try {
                // Stop recording WITHOUT notifying first: the backend is already
                // tearing this session down, and a pause_session from a
                // superseded recorder socket would be read as a character
                // switch and get that socket closed (see the
                // session_ended_by_server teardown below). stopMicCapture's own
                // bare stopRecording() then hits its !S.isRecording early
                // return, so no server message goes out.
                if (typeof window.stopRecording === 'function') {
                    window.stopRecording({ notifyServer: false });
                }
                await window.stopMicCapture();
            } catch (error) {
                console.warn('[App] auto_close_mic cleanup failed:', error);
            }
        }
        await releaseVoiceCaptureResources();

        if (typeof window.hideVoicePreparingToast === 'function') window.hideVoicePreparingToast();
        if (typeof window.stopSilenceDetection === 'function') window.stopSilenceDetection();
        if (typeof window.stopGameVoiceSttGate === 'function') window.stopGameVoiceSttGate({ restoreOrdinaryMic: false });
        if (typeof window.updateMicVolumeStatusNow === 'function') window.updateMicVolumeStatusNow(false);

        S.isTextSessionActive = false;
        S.voiceChatActive = false;
        S.voiceStartPending = false;
        S.isRecording = false;
        if (!keepSwitchingMode) {
            S.isSwitchingMode = false;
        }
        window.isRecording = false;
        window.isMicStarting = false;
        window.currentGeminiMessage = null;
        S.lastVoiceUserMessage = null;
        S.lastVoiceUserMessageTime = 0;

        var mb = micButton();
        if (mb) {
            mb.classList.remove('active');
            mb.classList.remove('recording');
            mb.disabled = false;
        }
        var sb = screenButton();
        if (sb) {
            sb.classList.remove('active');
            sb.disabled = true;
        }
        var mu = muteButton(); if (mu) mu.disabled = true;
        var st = stopButton(); if (st) st.disabled = true;
        var rs = resetSessionButton(); if (rs) rs.disabled = false;
        var rt = returnSessionButton(); if (rt) rt.disabled = true;
        var ts = textSendButton(); if (ts) ts.disabled = false;
        var ti = textInputBox(); if (ti) ti.disabled = false;
        var ss = screenshotButton(); if (ss) ss.disabled = false;

        var textInputArea = document.getElementById('text-input-area');
        if (textInputArea) textInputArea.classList.remove('hidden');
        if (typeof window.syncVoiceChatComposerHidden === 'function') window.syncVoiceChatComposerHidden(false);
        if (typeof window.syncFloatingMicButtonState === 'function') window.syncFloatingMicButtonState(false);
        if (typeof window.syncFloatingScreenButtonState === 'function') window.syncFloatingScreenButtonState(false);
    }

    function resolveAutoCloseMicToastMessage(response) {
        var reasonCode = response && response.reason_code;
        if (reasonCode === 'free_api_silence_timeout' && typeof window.t === 'function') {
            return window.t('app.freeApiAutoCloseNotice', {
                defaultValue: '免费 API 长时间未检测到语音，已自动关闭语音会话'
            });
        }
        return (typeof window.t === 'function' && window.t('app.autoMuteTimeout'))
            || (response && response.message)
            || '长时间无语音输入，已自动关闭麦克风';
    }

    function showAutoCloseMicToast(response) {
        if (typeof window.showStatusToast !== 'function') return;
        var now = Date.now();
        if (S._lastAutoCloseMicToastAt && now - S._lastAutoCloseMicToastAt < 1500) return;
        S._lastAutoCloseMicToastAt = now;
        window.showStatusToast(
            resolveAutoCloseMicToastMessage(response),
            7000,
            { priority: 80 }
        );
    }

    function handleMusicPlayUrlCoordMessage(data) {
        if (!data || typeof data !== 'object') return;
        if (data.sender === MUSIC_PLAY_URL_SENDER_ID) return;
        if (data.type === 'music_play_url_claim' && data.key && data.sender && data.token) {
            _musicPlayUrlClaims[data.key] = {
                sender: data.sender,
                token: data.token,
                expires: Date.now() + MUSIC_PLAY_URL_CLAIM_TTL_MS
            };
        } else if (
            data.type === 'music_play_url_claim_release'
            && data.key
            && data.sender
            && data.token
        ) {
            var claim = getValidMusicPlayUrlClaim(data.key);
            if (claim && claim.sender === data.sender && claim.token === data.token) {
                delete _musicPlayUrlClaims[data.key];
            }
        }
    }

    function startMusicPlayUrlClaimCleanup() {
        if (_musicPlayUrlClaimCleanupTimer) return;
        _musicPlayUrlClaimCleanupTimer = setInterval(pruneMusicPlayUrlClaims, MUSIC_PLAY_URL_CLAIM_CLEANUP_MS);
    }

    function bindMusicPlayUrlCoordCleanup() {
        if (_musicPlayUrlCoordBeforeUnloadBound) return;
        _musicPlayUrlCoordBeforeUnloadBound = true;
        window.addEventListener('beforeunload', function () {
            if (_musicPlayUrlClaimCleanupTimer) {
                clearInterval(_musicPlayUrlClaimCleanupTimer);
                _musicPlayUrlClaimCleanupTimer = 0;
            }
            releaseOwnedMusicPlayUrlClaims();
            try {
                if (_musicPlayUrlCoordChannel && typeof _musicPlayUrlCoordChannel.close === 'function') {
                    _musicPlayUrlCoordChannel.close();
                    _musicPlayUrlCoordChannel = null;
                }
            } catch (error) {
                console.warn('[Music] music_play_url 协调通道关闭失败:', error, {
                    channelId: MUSIC_PLAY_URL_COORD_CHANNEL_NAME,
                    sender: MUSIC_PLAY_URL_SENDER_ID
                });
            }
        });
    }

    function createMusicPlayUrlStorageCoord() {
        if (typeof window.addEventListener !== 'function' || typeof localStorage === 'undefined') {
            throw new Error('localStorage coordination unavailable');
        }
        var storageListener = function (event) {
            if (!event || event.key !== MUSIC_PLAY_URL_COORD_STORAGE_KEY || !event.newValue) return;
            try {
                handleMusicPlayUrlCoordMessage(JSON.parse(event.newValue));
            } catch (error) {
                console.warn('[Music] music_play_url localStorage 协调消息解析失败:', error, {
                    channelId: MUSIC_PLAY_URL_COORD_STORAGE_KEY,
                    sender: MUSIC_PLAY_URL_SENDER_ID
                });
            }
        };
        window.addEventListener('storage', storageListener);
        return {
            _nekoCoordType: 'localStorage',
            _nekoCoordId: MUSIC_PLAY_URL_COORD_STORAGE_KEY,
            postMessage: function (payload) {
                var serialized = JSON.stringify(Object.assign({
                    storageNonce: Date.now().toString(36) + Math.random().toString(36).slice(2, 8)
                }, payload || {}));
                localStorage.setItem(MUSIC_PLAY_URL_COORD_STORAGE_KEY, serialized);
                setTimeout(function () {
                    try {
                        if (localStorage.getItem(MUSIC_PLAY_URL_COORD_STORAGE_KEY) === serialized) {
                            localStorage.removeItem(MUSIC_PLAY_URL_COORD_STORAGE_KEY);
                        }
                    } catch (_) { /* 忽略 */ }
                }, 0);
            },
            close: function () {
                window.removeEventListener('storage', storageListener);
            }
        };
    }

    function activateMusicPlayUrlCoordChannel(channel) {
        _musicPlayUrlCoordChannel = channel;
        _musicPlayUrlCoordChannelReady = true;
        bindMusicPlayUrlCoordCleanup();
        startMusicPlayUrlClaimCleanup();
        return _musicPlayUrlCoordChannel;
    }

    function getMusicPlayUrlCoordChannel() {
        if (_musicPlayUrlCoordChannelReady) {
            return _musicPlayUrlCoordChannel;
        }
        try {
            if (typeof BroadcastChannel !== 'undefined') {
                var channel = new BroadcastChannel(MUSIC_PLAY_URL_COORD_CHANNEL_NAME);
                channel._nekoCoordType = 'BroadcastChannel';
                channel._nekoCoordId = MUSIC_PLAY_URL_COORD_CHANNEL_NAME;
                channel.onmessage = function (event) {
                    handleMusicPlayUrlCoordMessage(event && event.data);
                };
                return activateMusicPlayUrlCoordChannel(channel);
            }
            if (!_musicPlayUrlBroadcastUnavailableWarned) {
                _musicPlayUrlBroadcastUnavailableWarned = true;
                console.warn('[Music] music_play_url BroadcastChannel 不可用，使用 localStorage 后备通道', {
                    channelId: MUSIC_PLAY_URL_COORD_CHANNEL_NAME,
                    sender: MUSIC_PLAY_URL_SENDER_ID
                });
            }
        } catch (error) {
            console.warn('[Music] music_play_url BroadcastChannel 初始化失败，使用 localStorage 后备通道:', error, {
                channelId: MUSIC_PLAY_URL_COORD_CHANNEL_NAME,
                sender: MUSIC_PLAY_URL_SENDER_ID
            });
        }

        try {
            return activateMusicPlayUrlCoordChannel(createMusicPlayUrlStorageCoord());
        } catch (error) {
            console.warn('[Music] music_play_url localStorage 后备通道初始化失败:', error, {
                channelId: MUSIC_PLAY_URL_COORD_STORAGE_KEY,
                sender: MUSIC_PLAY_URL_SENDER_ID
            });
            _musicPlayUrlCoordChannel = null;
            return null;
        }
    }

    function pruneMusicPlayUrlClaims() {
        var now = Date.now();
        Object.keys(_musicPlayUrlClaims).forEach(function (key) {
            var claim = _musicPlayUrlClaims[key];
            var expires = claim && typeof claim === 'object' ? claim.expires : claim;
            if (!claim || !expires || expires <= now) {
                delete _musicPlayUrlClaims[key];
            }
        });
    }

    function getMusicPlayUrlClaimKey(response) {
        if (!response || !response.url) return '';
        return JSON.stringify([
            String(response.url || '').trim(),
            String(response.name || '').trim(),
            String(response.artist || '').trim()
        ]);
    }

    function hasMusicPlayUrlClaim(key) {
        if (!key) return false;
        return !!getValidMusicPlayUrlClaim(key);
    }

    function getValidMusicPlayUrlClaim(key) {
        if (!key) return null;
        pruneMusicPlayUrlClaims();
        var claim = _musicPlayUrlClaims[key];
        if (!claim || typeof claim !== 'object' || !claim.sender || !claim.token || !claim.expires) {
            if (claim) delete _musicPlayUrlClaims[key];
            return null;
        }
        if (claim.expires <= Date.now()) {
            delete _musicPlayUrlClaims[key];
            return null;
        }
        return claim;
    }

    function claimMusicPlayUrl(key) {
        if (!key) return '';
        pruneMusicPlayUrlClaims();
        var token = MUSIC_PLAY_URL_SENDER_ID + ':' + Date.now().toString(36)
            + ':' + Math.random().toString(36).slice(2, 10);
        _musicPlayUrlClaims[key] = {
            sender: MUSIC_PLAY_URL_SENDER_ID,
            token: token,
            expires: Date.now() + MUSIC_PLAY_URL_CLAIM_TTL_MS
        };
        var channel = getMusicPlayUrlCoordChannel();
        if (!channel) return token;
        var timestamp = Date.now();
        try {
            channel.postMessage({
                type: 'music_play_url_claim',
                key: key,
                sender: MUSIC_PLAY_URL_SENDER_ID,
                token: token,
                ts: timestamp
            });
        } catch (error) {
            console.warn('[Music] music_play_url claim 广播失败:', error, {
                key: key,
                sender: MUSIC_PLAY_URL_SENDER_ID,
                timestamp: timestamp,
                channelId: channel._nekoCoordId || MUSIC_PLAY_URL_COORD_CHANNEL_NAME,
                channelType: channel._nekoCoordType || 'unknown'
            });
        }
        return token;
    }

    function releaseMusicPlayUrlClaim(key, token) {
        var claim = getValidMusicPlayUrlClaim(key);
        if (
            !claim
            || claim.sender !== MUSIC_PLAY_URL_SENDER_ID
            || !token
            || claim.token !== token
        ) return;
        delete _musicPlayUrlClaims[key];
        var channel = _musicPlayUrlCoordChannel;
        if (!channel || typeof channel.postMessage !== 'function') return;
        var timestamp = Date.now();
        try {
            channel.postMessage({
                type: 'music_play_url_claim_release',
                key: key,
                sender: MUSIC_PLAY_URL_SENDER_ID,
                token: token,
                ts: timestamp
            });
        } catch (error) {
            console.warn('[Music] music_play_url claim 释放广播失败:', error, {
                key: key,
                sender: MUSIC_PLAY_URL_SENDER_ID,
                timestamp: timestamp,
                channelId: channel._nekoCoordId || MUSIC_PLAY_URL_COORD_CHANNEL_NAME,
                channelType: channel._nekoCoordType || 'unknown'
            });
        }
    }

    function releaseOwnedMusicPlayUrlClaims() {
        var keys = Object.keys(_musicPlayUrlClaims).filter(function (key) {
            var claim = getValidMusicPlayUrlClaim(key);
            return claim && claim.sender === MUSIC_PLAY_URL_SENDER_ID;
        });
        keys.forEach(function (key) {
            var claim = getValidMusicPlayUrlClaim(key);
            if (claim) releaseMusicPlayUrlClaim(key, claim.token);
        });
    }

    function isStandaloneChatPageForMusic() {
        var pathname = (window.location && window.location.pathname) || '';
        return pathname === '/chat' || pathname === '/chat/';
    }

    function hasLocalMusicOwnerOrPending() {
        try {
            if (typeof window.getMusicPlayerInstance === 'function' && window.getMusicPlayerInstance()) {
                return true;
            }
        } catch (_) {}
        try {
            if (typeof window.isMusicPlaying === 'function' && window.isMusicPlaying()) {
                return true;
            }
        } catch (_) {}
        try {
            if (typeof window.isMusicPending === 'function' && window.isMusicPending()) {
                return true;
            }
        } catch (_) {}
        return false;
    }

    function hasRemoteMusicLeaderHint() {
        try {
            if (typeof window.isRemoteMusicActive === 'function' && window.isRemoteMusicActive()) {
                return true;
            }
        } catch (_) {}
        try {
            var musicBar = document.getElementById('music-player-bar');
            if (musicBar && musicBar.dataset && musicBar.dataset.mirror === 'true') {
                return true;
            }
        } catch (_) {}
        return false;
    }

    function getMusicPlayUrlFollowerGraceMs() {
        var configured = NaN;
        try {
            if (window.NEKO_MUSIC_PLAY_URL_FOLLOWER_GRACE_MS !== undefined) {
                configured = Number(window.NEKO_MUSIC_PLAY_URL_FOLLOWER_GRACE_MS);
            } else if (typeof localStorage !== 'undefined') {
                configured = Number(localStorage.getItem('neko_music_play_url_follower_grace_ms'));
            }
        } catch (_) {
            configured = NaN;
        }
        if (Number.isFinite(configured) && configured >= 100 && configured <= 3000) {
            return configured;
        }
        return MUSIC_PLAY_URL_FOLLOWER_GRACE_MS;
    }

    function shouldSkipMusicPlayUrlForOtherWindow(key) {
        return !hasLocalMusicOwnerOrPending() && (hasRemoteMusicLeaderHint() || hasMusicPlayUrlClaim(key));
    }

    async function dispatchMusicPlayUrlResponse(response, reason) {
        if (!response || !response.url || typeof window.dispatchMusicPlay !== 'function') {
            return false;
        }
        var key = getMusicPlayUrlClaimKey(response);
        var track = {
            name: response.name || 'Plugin Music',
            artist: response.artist || 'External',
            url: response.url,
            cover: response.cover || undefined
        };
        var dispatchResult;
        try {
            dispatchResult = await window.dispatchMusicPlay(track, {
                source: 'music_play_url',
                reason: reason || 'websocket'
            });
        } catch (error) {
            console.warn('[Music] music_play_url 播放派发失败，未发布跨窗口 claim:', error, {
                key: key,
                url: response.url,
                reason: reason || 'websocket'
            });
            return false;
        }
        if (dispatchResult === true) {
            claimMusicPlayUrl(key);
            console.log('[Music] Received direct play command from backend:', response.url);
            return true;
        }
        if (dispatchResult === 'queued') {
            console.log('[Music] music_play_url 播放派发仍在等待接口就绪，暂不发布跨窗口 claim:', {
                key: key,
                url: response.url,
                reason: reason || 'websocket'
            });
            return false;
        }
        console.warn('[Music] music_play_url 播放派发被拒绝，未发布跨窗口 claim:', {
            key: key,
            url: response.url,
            reason: reason || 'websocket',
            result: dispatchResult
        });
        return false;
    }

    function handleMusicPlayUrlResponse(response) {
        if (!response || !response.url || typeof window.dispatchMusicPlay !== 'function') {
            return;
        }

        var key = getMusicPlayUrlClaimKey(response);
        getMusicPlayUrlCoordChannel();

        // chat.html 是独立聊天窗口时默认作为从窗口，给主窗口一个很短的
        // 抢占窗口；若主窗口不存在或没有接管播放，再由 chat.html 兜底。
        if (isStandaloneChatPageForMusic() && !hasLocalMusicOwnerOrPending()) {
            setTimeout(function () {
                if (shouldSkipMusicPlayUrlForOtherWindow(key)) {
                    console.log('[Music] 跳过 music_play_url：其他窗口已接管播放');
                    return;
                }
                setTimeout(function () {
                    if (shouldSkipMusicPlayUrlForOtherWindow(key)) {
                        console.log('[Music] 跳过 music_play_url：其他窗口已接管播放');
                        return;
                    }
                    dispatchMusicPlayUrlResponse(response, 'chat-fallback');
                }, MUSIC_PLAY_URL_SECONDARY_CONFIRM_MS);
            }, getMusicPlayUrlFollowerGraceMs());
            return;
        }

        if (shouldSkipMusicPlayUrlForOtherWindow(key)) {
            console.log('[Music] 跳过 music_play_url：其他窗口已接管播放');
            return;
        }

        dispatchMusicPlayUrlResponse(response, 'websocket');
    }

    // 多窗口分发形态下同一条 WS 消息会被 RAW_MESSAGE 转发给多个窗口，chat 窗口和
    // pet 窗口都会走到这里。谁来执行必须唯一，判据都能在本窗口内直接判定：
    //   1. 独立点唱机窗口开着 -> 它才持有可见播放器，转发给它
    //   2. 多窗口下的 chat 窗口 -> 让位给主窗口（它和点唱机窗口同 partition，
    //      能听见拥有者；chat 处于 persist:neko-full-chat，听不见）
    //   3. 其余（网页端单窗口、pet 窗口）-> 本地执行
    function isSecondaryJukeboxControlSurface() {
        return window.__NEKO_MULTI_WINDOW__ === true
            && /^\/chat(?:_full)?(?:\/|$)/.test(window.location.pathname || '');
    }

    function dispatchJukeboxControl(payload, isNotSuperseded) {
        // 归属必须在真正要执行的这一刻算，不能用入队之前的快照：排队期间独立
        // 点唱机窗口可能刚打开（那就该转发，否则会在隐藏窗口里另起一条音轨），
        // 也可能刚关闭（那就该本地执行，否则白等一次转发超时）。
        var loader = window.__nekoJukeboxLoader;
        var ownerAlive = !!(loader && typeof loader.hasControlOwner === 'function' && loader.hasControlOwner());
        if (ownerAlive) return loader.forwardControl(payload);
        if (!window.Jukebox || typeof window.Jukebox.executeControl !== 'function') {
            // 分片正在加载：bootstrap.js 一落地就把带 executeControl 的惰性门面
            // 换成了空对象，而 executeControl 定义在第五个分片里。这中间到达的
            // 指令不该被丢掉（冷缓存/慢盘下这个窗口有几百毫秒到数秒），交给
            // loader 等分片加载完再执行。
            if (loader && typeof loader.load === 'function') {
                return Promise.resolve(loader.load()).then(function (jukebox) {
                    // 分片加载是几百毫秒到数秒的窗口，入队之前算好的两件事到这里
                    // 都可能已经过期，都要重算。
                    //
                    // 顶替：这段时间里来的 stop / play 推进了顶替世代，但它那次
                    // 就地取消对本条毫无作用 —— 分片加载期间 window.Jukebox 是
                    // bootstrap 换上的空对象，cancelActivePlayback 还不存在，那次
                    // 调用是静默空操作。runCommand 入口那次判定也早过去了。
                    if (typeof isNotSuperseded === 'function' && !isNotSuperseded()) {
                        console.log('[Jukebox] 跳过点歌台控制：分片加载期间已被后来的指令作废');
                        return null;
                    }
                    // 归属：独立点唱机窗口可能刚打开并宣告归属，否则这条指令会在
                    // 本窗口另起一条隐藏音轨。
                    if (typeof loader.hasControlOwner === 'function' && loader.hasControlOwner()) {
                        return loader.forwardControl(payload);
                    }
                    if (!jukebox || typeof jukebox.executeControl !== 'function') {
                        console.log('[Jukebox] 跳过点歌台控制：分片加载后仍无控制入口');
                        return null;
                    }
                    return jukebox.executeControl(payload);
                });
            }
            console.log('[Jukebox] 跳过点歌台控制：当前窗口没有点歌台控制入口');
            return Promise.resolve(null);
        }
        return window.Jukebox.executeControl(payload);
    }

    function handleJukeboxControlResponse(response) {
        if (!response) return;

        var command = response.command && typeof response.command === 'object' ? response.command : response;
        var payload = {
            action: command.action,
            query: command.query || '',
            value: command.value,
            mode: command.mode,
            headless: true
        };

        // 让位判据与有没有拥有者无关：同一条消息会被 RAW_MESSAGE 转给多个角色
        // 窗口，它们都能看见同一个拥有者，于是会各转发一次、拥有者执行两遍 ——
        // 一条 adjust_volume 被叠加，play 和切歌互相抢。只有主控制窗口能继续。
        if (isSecondaryJukeboxControlSurface()) {
            console.log('[Jukebox] 跳过点歌台控制：多窗口下由主窗口执行');
            return;
        }

        var arrivalSupersedeGeneration = null;
        // 判据只写一处，两个检查点共用：入队等待期间，以及分片加载的等待期间。
        var isNotSuperseded = function () {
            return arrivalSupersedeGeneration === null
                || arrivalSupersedeGeneration === _jukeboxSupersedeGeneration;
        };
        var runCommand = function () {
            // 排队期间被顶替了：整条跳过，别等轮到自己才把声音放出来。
            if (!isNotSuperseded()) {
                console.log('[Jukebox] 跳过点歌台控制：排队期间已被后来的指令作废');
                return Promise.resolve();
            }
            return Promise.resolve(dispatchJukeboxControl(payload, isNotSuperseded)).then(function (result) {
                console.log('[Jukebox] 点歌台控制完成:', result);
            }).catch(function (error) {
                console.warn('[Jukebox] 点歌台控制失败:', error);
            });
        };

        // 取消动作排在它要取消的那个操作后面毫无意义：一条卡在慢动画加载里的
        // play 会把后面所有指令一起堵死，而声音早就出来了。这不止 stop —— 后来的
        // play / next / previous 同样是替换意图，用户已经不要那条在途的了。
        //
        // 但直接插队执行会把同一拍到达的两条颠倒（入队走的是微任务，插队是同步的），
        // 结果变成「新的先跑、旧的后跑」。所以只把「作废在途播放」这一步就地做掉——
        // 声音立刻停，卡住的那条也会在下一个世代检查处解开——指令本身仍然按顺序
        // 排队执行，次序不变。
        var normalizedControlAction = String(payload.action || '').trim().toLowerCase();
        if (normalizedControlAction === 'stop' || normalizedControlAction === 'play') {
            _jukeboxSupersedeGeneration += 1;
        }
        if (normalizedControlAction === 'play'
            || normalizedControlAction === 'next'
            || normalizedControlAction === 'previous') {
            // 自增在前、取号在后，所以顶替者自己记的是新世代，不会被自己顶掉。
            arrivalSupersedeGeneration = _jukeboxSupersedeGeneration;
        }
        var preemptingActions = ['stop', 'play', 'next', 'previous'];
        if (preemptingActions.indexOf(normalizedControlAction) >= 0) {
            // 取消也得按归属走：在途的那条 play 可能正跑在独立点唱机窗口里，
            // 本窗口的 cancelActivePlayback 够不着它，队列里的 stop 就只能干等
            // 一次转发超时。
            //
            // 两个可能的播放方都要取消，不能二选一（归属允许在指令排队期间变化），
            // 而且必须各自包 try —— 共用一个的话前一个抛异常会把后一个整个跳过，
            // 那正是这段代码要堵的洞。
            var cancelLoader = window.__nekoJukeboxLoader;
            var cancelOwnerAlive = !!(cancelLoader
                && typeof cancelLoader.hasControlOwner === 'function'
                && cancelLoader.hasControlOwner());
            if (cancelOwnerAlive && typeof cancelLoader.cancelOnOwner === 'function') {
                try {
                    cancelLoader.cancelOnOwner(normalizedControlAction);
                } catch (error) {
                    console.warn('[Jukebox] 作废拥有者在途播放失败:', error);
                }
            }
            if (window.Jukebox && typeof window.Jukebox.cancelActivePlayback === 'function') {
                try {
                    // 相对导航不静音：目标可能不存在，那时这条指令是空操作，
                    // 不该把正在放的歌停掉。判据与顶替那套一致。
                    window.Jukebox.cancelActivePlayback({
                        silenceAudio: normalizedControlAction !== 'next'
                            && normalizedControlAction !== 'previous'
                    });
                } catch (error) {
                    console.warn('[Jukebox] 作废本地在途播放失败:', error);
                }
            }
        }
        _jukeboxControlQueue = _jukeboxControlQueue.then(runCommand, runCommand);
    }

    function readNewUserIcebreakerStore() {
        try {
            if (typeof localStorage === 'undefined') return null;
            var raw = localStorage.getItem(NEW_USER_ICEBREAKER_STORAGE_KEY);
            if (!raw) return null;
            var parsed = JSON.parse(raw);
            return parsed && typeof parsed === 'object' ? parsed : null;
        } catch (_) {
            return null;
        }
    }

    function hasCompletedNewUserIcebreaker() {
        var store = readNewUserIcebreakerStore();
        var days = store && typeof store.days === 'object' ? store.days : null;
        var finalDay = days && days['7'];
        return !!(finalDay && finalDay.completed === true);
    }

    function isRecentNewUserIcebreakerEntry(entry) {
        if (!entry || typeof entry !== 'object') return false;
        var timestamps = [
            Number(entry.triggeredAt || 0),
            Number(entry.updatedAt || 0),
            Number(entry.completedAt || 0),
            Number(entry.endedAt || 0)
        ].filter(function (value) {
            return Number.isFinite(value) && value > 0;
        });
        if (!timestamps.length) return false;
        var latest = Math.max.apply(Math, timestamps);
        return Date.now() - latest <= NEW_USER_ICEBREAKER_BLOCKING_WINDOW_MS;
    }

    function isNewUserIcebreakerEntryBlocking(entry) {
        return !!(entry && entry.completed !== true && isRecentNewUserIcebreakerEntry(entry));
    }

    function isNewUserIcebreakerStorePeriodActive() {
        var store = readNewUserIcebreakerStore();
        var days = store && typeof store.days === 'object' ? store.days : null;
        if (!days) return false;
        if (hasCompletedNewUserIcebreaker()) return false;
        for (var day = 1; day <= 7; day += 1) {
            var entry = days[String(day)];
            if (isNewUserIcebreakerEntryBlocking(entry)) {
                return true;
            }
        }
        return false;
    }

    function isNewUserIcebreakerActiveForGreeting() {
        if (window.newUserIcebreaker && typeof window.newUserIcebreaker.getActiveSession === 'function') {
            try {
                if (window.newUserIcebreaker.getActiveSession()) return true;
            } catch (_) {}
        }
        try {
            var state = window.NekoNewUserIcebreakerState;
            if (state && typeof state.isPeriodActive === 'function') {
                if (state.isPeriodActive()) return true;
            }
        } catch (_) {}
        return isNewUserIcebreakerStorePeriodActive();
    }

    function isNewUserIcebreakerPeriodActive() {
        return isNewUserIcebreakerActiveForGreeting();
    }

    function isNewUserIcebreakerBlockingGreeting(reason) {
        return isNewUserIcebreakerActiveForGreeting();
    }

    function normalizeAssistantTurnId(turnId) {
        if (turnId === undefined || turnId === null || turnId === '') {
            return null;
        }
        return String(turnId);
    }

    function resolveAssistantRequestId(requestId, responseMeta) {
        var meta = responseMeta && typeof responseMeta === 'object' ? responseMeta : {};
        return normalizeAssistantTurnId(
            requestId
            || meta.request_id
            || meta.requestId
            || meta.interaction_id
            || meta.interactionId
        );
    }

    function allocateAssistantTurnId(serverTurnId) {
        var normalized = normalizeAssistantTurnId(serverTurnId);
        if (normalized) {
            return normalized;
        }
        S.assistantTurnSeq = (S.assistantTurnSeq || 0) + 1;
        return 'local-' + S.assistantTurnSeq;
    }

    function emitAssistantLifecycleEvent(eventName, detail) {
        window.dispatchEvent(new CustomEvent(eventName, {
            detail: Object.assign({
                timestamp: Date.now()
            }, detail || {})
        }));
    }

    function getRenderableAssistantChunkText(text) {
        return String(text || '')
            .replace(/\[play_music:[^\]]*(\]|$)/g, '')
            .trim();
    }

    function getAssistantDisplayName() {
        var name = '';
        try {
            name = (window.__NEKO_TUTORIAL_ASSISTANT_NAME_OVERRIDE__
                || (window.lanlan_config && window.lanlan_config.lanlan_name)
                || window._currentCatgirl
                || window.currentCatgirl
                || '');
        } catch (_) {}
        return String(name || '').trim() || 'Neko';
    }

    function appendAssistantStatusMessage(text) {
        var cleanText = getRenderableAssistantChunkText(text);
        if (!cleanText) return false;

        var timeStr = (typeof window.getCurrentTimeString === 'function')
            ? window.getCurrentTimeString()
            : new Date().toLocaleTimeString('en-US', {
                hour12: false,
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            });
        var assistantName = getAssistantDisplayName();
        var messageId = 'assistant-status-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
        var appendedToReact = false;

        if (window.reactChatWindowHost && typeof window.reactChatWindowHost.appendMessage === 'function') {
            try {
                var avatarUrl = '';
                if (window.appChatAvatar && typeof window.appChatAvatar.getCurrentAvatarDataUrl === 'function') {
                    avatarUrl = window.appChatAvatar.getCurrentAvatarDataUrl() || '';
                }
                window.reactChatWindowHost.appendMessage({
                    id: messageId,
                    role: 'assistant',
                    author: assistantName,
                    time: timeStr,
                    createdAt: Date.now(),
                    avatarLabel: assistantName ? String(assistantName).slice(0, 1).toUpperCase() : undefined,
                    avatarUrl: avatarUrl || undefined,
                    blocks: [{ type: 'text', text: cleanText }],
                    status: 'failed'
                });
                appendedToReact = true;
            } catch (reactAppendError) {
                console.warn('[WS] failed to append assistant status to React chat:', reactAppendError);
            }
        }

        if (appendedToReact) {
            window.currentTurnGeminiBubbles = [{
                dataset: { reactChatMessageId: messageId },
                parentNode: null,
                isConnected: true,
                textContent: '[' + timeStr + '] \u{1F380} ' + cleanText,
                nodeType: 1
            }];
            return true;
        }

        var messageDiv = document.createElement('div');
        messageDiv.classList.add('message', 'gemini');
        messageDiv.textContent = '[' + timeStr + '] \u{1F380} ' + cleanText;
        var cc = chatContainer();
        if (!cc) return false;
        cc.appendChild(messageDiv);
        window.currentTurnGeminiBubbles = [messageDiv];
        cc.scrollTop = cc.scrollHeight;
        return true;
    }

    // preview.asrTurnId 记录“屏幕上这个预览气泡属于哪一轮独立 ASR”。后端的
    // final 经 transcript dispatcher 的独立 worker 回调，可能排在下一轮
    // partial 之后才到达；带上轮次 id，过期轮次的清除信号才能被识别成 no-op。
    // 后端未带 id（旧后端 / 轮次尚未 prepare）时保持空串 = 老的无条件移除语义。
    function upsertExternalAsrPreview(text) {
        var host = window.reactChatWindowHost;
        if (!host || typeof host.appendMessage !== 'function' ||
            typeof host.updateMessage !== 'function') {
            return null;
        }
        var cleanText = String(text || '');
        var preview = S.externalAsrPreviewMessage;
        var existingId = preview && preview.dataset
            ? preview.dataset.reactChatMessageId
            : '';
        if (existingId) {
            host.updateMessage(existingId, {
                blocks: [{ type: 'text', text: cleanText }],
                status: 'streaming'
            });
            preview.textContent = cleanText;
            return preview;
        }

        var messageId = 'external-asr-preview-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
        host.appendMessage({
            id: messageId,
            role: 'user',
            author: '',
            time: (typeof window.getCurrentTimeString === 'function')
                ? window.getCurrentTimeString()
                : '',
            createdAt: Date.now(),
            blocks: [{ type: 'text', text: cleanText }],
            status: 'streaming'
        });
        return {
            dataset: { reactChatMessageId: messageId },
            parentNode: null,
            isConnected: true,
            textContent: cleanText,
            asrTurnId: '',
            nodeType: 1
        };
    }

    function removeExternalAsrPreview() {
        var preview = S.externalAsrPreviewMessage;
        if (!preview) return;
        var messageId = preview.dataset && preview.dataset.reactChatMessageId;
        var host = window.reactChatWindowHost;
        if (messageId && host && typeof host.removeMessage === 'function') {
            host.removeMessage(messageId);
        }
        if (preview.parentNode && typeof preview.parentNode.removeChild === 'function') {
            preview.parentNode.removeChild(preview);
        }
        if (S.lastVoiceUserMessage === preview) {
            S.lastVoiceUserMessage = null;
            S.lastVoiceUserMessageTime = 0;
        }
        S.externalAsrPreviewMessage = null;
    }
    window.removeExternalAsrPreview = removeExternalAsrPreview;

    var GAME_VOICE_TRANSCRIPTION_MODES = [
        'backend_pending',
        'native_core',
        'independent_asr',
        'browser_fallback',
        'unavailable'
    ];

    /**
     * Publish the actual host-owned transcription route to mini-game bridges.
     *
     * This is capability-based on purpose. Games must not infer their voice
     * behavior from a free/paid label, and they never receive microphone PCM.
     */
    function setGameVoiceTranscriptionState(next) {
        next = next && typeof next === 'object' ? next : {};
        var mode = String(next.transcription_mode || next.mode || 'unavailable');
        if (GAME_VOICE_TRANSCRIPTION_MODES.indexOf(mode) === -1) {
            mode = 'unavailable';
        }
        var provider = String(next.provider || '');
        var ready = next.ready === true;
        var reason = String(next.reason || '');
        var changed = S.gameVoiceTranscriptionMode !== mode
            || S.gameVoiceTranscriptionProvider !== provider
            || S.gameVoiceTranscriptionReady !== ready
            || S.gameVoiceTranscriptionReason !== reason;
        S.gameVoiceTranscriptionMode = mode;
        S.gameVoiceTranscriptionProvider = provider;
        S.gameVoiceTranscriptionReady = ready;
        S.gameVoiceTranscriptionReason = reason;
        if (changed) {
            window.dispatchEvent(new CustomEvent(
                'neko-game-voice-transcription-state-change',
                {
                    detail: {
                        capture_owner: 'host',
                        transcription_mode: mode,
                        provider: provider,
                        ready: ready,
                        reason: reason
                    }
                }
            ));
        }
        return {
            capture_owner: 'host',
            transcription_mode: mode,
            provider: provider,
            ready: ready,
            reason: reason
        };
    }
    mod.setGameVoiceTranscriptionState = setGameVoiceTranscriptionState;

    function setBlockedGameVoiceTranscriptionState() {
        setGameVoiceTranscriptionState({
            transcription_mode: 'unavailable',
            provider: S.independentAsrProvider || S.coreApiProvider || '',
            ready: false,
            reason: 'route_blocked'
        });
    }

    function parseAsrControlIdentity(details) {
        if (!details || typeof details !== 'object') return null;
        var sessionEpoch = details.session_epoch;
        var transportGeneration = details.transport_generation;
        var lifecycleRevision = details.lifecycle_revision;
        if (!Number.isInteger(sessionEpoch) || sessionEpoch < 0
            || !Number.isInteger(transportGeneration) || transportGeneration < 0
            || !Number.isInteger(lifecycleRevision) || lifecycleRevision < 0) {
            return null;
        }
        return {
            sessionEpoch: sessionEpoch,
            transportGeneration: transportGeneration,
            lifecycleRevision: lifecycleRevision
        };
    }

    function acceptAsrControlIdentity(details) {
        var incoming = parseAsrControlIdentity(details);
        if (!incoming) return false;
        var current = _latestAsrControlIdentity;
        var newer = !current
            || incoming.sessionEpoch > current.sessionEpoch
            || (incoming.sessionEpoch === current.sessionEpoch
                && incoming.transportGeneration > current.transportGeneration)
            || (incoming.sessionEpoch === current.sessionEpoch
                && incoming.transportGeneration === current.transportGeneration
                && incoming.lifecycleRevision > current.lifecycleRevision);
        if (!newer) return false;
        _latestAsrControlIdentity = incoming;
        return true;
    }

    function normalizeAsrReasonCode(value) {
        if (typeof value !== 'string') return '';
        var normalized = value.trim();
        return /^ASR_[A-Z0-9_]{1,60}$/.test(normalized) ? normalized : '';
    }

    function normalizeAsrIncidentId(value) {
        if (typeof value !== 'string') return '';
        var normalized = value.trim();
        if (!normalized || normalized.length > 128) return '';
        return /^(?:asr-failure-|asr-deny-)[A-Za-z0-9_-]+$/.test(normalized)
            ? normalized
            : '';
    }

    function formatAsrFailureMessage(baseMessage, reasonCode) {
        var normalizedReason = normalizeAsrReasonCode(reasonCode);
        return normalizedReason
            ? baseMessage + ' [' + normalizedReason + ']'
            : baseMessage;
    }

    function showAsrIncidentToast(incidentId, message, durationMs) {
        var normalizedIncident = normalizeAsrIncidentId(incidentId);
        if (normalizedIncident && _seenAsrIncidentIds[normalizedIncident]) {
            return false;
        }
        if (normalizedIncident) {
            _seenAsrIncidentIds[normalizedIncident] = true;
            _seenAsrIncidentOrder.push(normalizedIncident);
            if (_seenAsrIncidentOrder.length > MAX_SEEN_ASR_INCIDENTS) {
                delete _seenAsrIncidentIds[_seenAsrIncidentOrder.shift()];
            }
        }
        if (typeof window.showStatusToast !== 'function') return false;
        window.showStatusToast(message, durationMs);
        return true;
    }

    // Fail-closed voice-route teardown, shared by the two ways a route dies:
    // a runtime failure (ASR_LIFECYCLE_STATE blocked) and a STARTUP failure
    // (terminal ASR_INDEPENDENT_* codes). Startup failures can never emit
    // BLOCKED -- IndependentAsrRuntime.start cannot reach the only emitter --
    // so before this was shared they showed a toast and left the hardware
    // microphone running for the whole session.
    function tearDownBlockedVoiceRoute() {

    removeExternalAsrPreview();
    S.independentAsrActive = false;
    // Set the sticky bit before publishing. The host bridge reacts
    // synchronously to the event and otherwise normalizes an active-but-
    // unavailable route back to backend_pending for one misleading frame.
    S.voiceInputRouteBlocked = true;
    setBlockedGameVoiceTranscriptionState();
    // Sticky: the teardown below is skipped while
    // the game STT gate owns the hardware, and
    // BLOCKED is never re-sent, so the game-exit
    // resume path would otherwise reopen the mic
    // onto a route that is still fail-closed.
    // The route is now fail-closed. _handle_core_asr_failure
    // (main_logic/core/asr_runtime.py) pins the microphone
    // route to "blocked" and nothing re-arms it inside this
    // session -- only a new start_session, or a hot swap that
    // also changes core_api_type. canUploadOrdinaryMicFrame()
    // consults the mic lease and mute/focus only, never the
    // lifecycle state, so without this the browser keeps the
    // hardware microphone (and its OS indicator) open and
    // keeps uploading PCM that the backend decodes, denoises
    // and VADs before dropping it -- while this very toast
    // says voice input has stopped. An audio session never
    // gets VOICE_INPUT_BLOCKED_TEXT_SESSION either, so this
    // event is the only signal that exists.
    //
    // stopMicCapture rather than bare stopRecording: it is
    // the only path that restores the whole non-recording UI
    // (mic/mute/screen buttons, floating button state, the
    // text input area, the volume readout). The user is not
    // otherwise stranded -- the 闭麦 button is bound to
    // stopMicCapture -- but leaving that UI claiming a live
    // voice session is the same lie as the open mic.
    //
    // Guarded twice: only the capturing window acts, and
    // never while the game STT gate holds the microphone,
    // where the ordinary uplink is already released and a
    // teardown would kill working game voice.
    //
    // Delivery contract, corrected: an earlier version of
    // this comment claimed a per-window broadcast that does
    // not exist. send_status targets the CURRENT socket,
    // and sync_message_queue feeds the monitor process over a
    // separate port that no app window connects to. Mic
    // control-plane codes are therefore additionally pushed to
    // the socket holding the voice lease (notify.py
    // _send_to_voice_owner), which is how a recorder
    // superseded by a newer chat window receives this at all.
    if (S.isRecording === true
        && S.gameVoiceSttGateActive !== true) {
        console.log('[App] independent ASR blocked; stopping the microphone');
        if (typeof window.stopMicCapture === 'function') {
            Promise.resolve(window.stopMicCapture()).catch(function (micTeardownErr) {
                console.warn('[App] blocked-ASR microphone teardown failed:', micTeardownErr);
            });
        } else if (typeof window.stopRecording === 'function') {
            window.stopRecording();
        }
    }
    }


    function websocketTraceEnabled() {
        return window.NEKO_DEBUG_BUBBLE_LIFECYCLE === true;
    }

    function logAssistantLifecycle(label, extra) {
        if (!websocketTraceEnabled()) {
            return;
        }
        console.log('[WSTrace]', label, Object.assign({
            assistantTurnId: S.assistantTurnId,
            pendingTurnServerId: S.assistantPendingTurnServerId,
            assistantTurnAwaitingBubble: S.assistantTurnAwaitingBubble,
            assistantTurnCompletedId: S.assistantTurnCompletedId,
            assistantSpeechActiveTurnId: S.assistantSpeechActiveTurnId,
            currentPlayingSpeechId: S.currentPlayingSpeechId,
            pendingAudioMetaQueue: S.pendingAudioChunkMetaQueue.length,
            incomingAudioBlobQueue: S.incomingAudioBlobQueue.length
        }, extra || {}));
    }

    function clearPendingAssistantTurnStart() {
        S.assistantPendingTurnServerId = null;
        S.assistantTurnAwaitingBubble = false;
        // 同时清掉 submit-to-first-chunk 空窗 marker。本函数被所有 turn-end /
        // response_discarded / socket_close / user_activity_cancel 路径调用，
        // 等于把 marker 接进了完整的 turn 生命周期收尾。
        S.pendingTextTurnSubmitAt = 0;
    }

    function clearPendingRollbackForRequest(requestId) {
        if (window.reactChatWindowHost && typeof window.reactChatWindowHost.clearPendingRollbackDraft === 'function') {
            window.reactChatWindowHost.clearPendingRollbackDraft(requestId);
        }
        if (requestId && window._lastSubmittedRequestId === requestId) {
            window._lastSubmittedText = '';
            window._lastSubmittedRequestId = '';
        }
    }

    function isNewUserIcebreakerMirrorTurnEnd(response) {
        var meta = response && response.meta;
        if (!meta || typeof meta !== 'object') return false;
        if (meta.source === 'new_user_icebreaker' || meta.kind === 'new_user_icebreaker') {
            return true;
        }
        var event = meta.event;
        return !!(event && typeof event === 'object' && event.source === 'new_user_icebreaker');
    }

    // turn-end / turn end agent_callback 两条路径共用的 realistic/structured
    // buffer 收尾：标 bubble 为 sent、设 _geminiTurnEndSealed 让 adapter 在
    // 后续 chunk 来时新建气泡而非追加到封口气泡（封口气泡的 React
    // StreamingText 在 status sent→streaming 切换时重 mount，追加文字会视觉
    // 丢失，详见 adapter 里的 _geminiTurnEndSealed 注释）、清 pending music、
    // structured 流 drop 掉残余 buffer（自己有 renderer），realistic 流把
    // 残余 trim 后 enqueue。
    // 之前两边各写一份导致这次 PR 修 agent_callback `return` 时才发现行为不
    // 一致；抽成共享 helper 防止下次又单边演进。
    function flushRealisticBufferOnTurnEnd() {
        var endingTurnId = resolveAssistantLifecycleTurnId();
        if (endingTurnId) {
            emitAssistantLifecycleEvent('neko-assistant-turn-ending', {
                turnId: endingTurnId,
                source: 'turn_end_flush'
            });
        }
        if (typeof window.setReactMessageStatus === 'function' && window.currentGeminiMessage) {
            window.setReactMessageStatus(window.currentGeminiMessage, 'assistant', 'sent');
        }
        window._geminiTurnEndSealed = true;
        window._pendingMusicCommand = '';
        if (window._structuredGeminiStreaming) {
            window._realisticGeminiBuffer = '';
            window._structuredGeminiStreaming = false;
            return;
        }
        var rest = typeof window._realisticGeminiBuffer === 'string'
            ? window._realisticGeminiBuffer.replace(/\[play_music:[^\]]*(\]|$)/g, '')
            : '';
        rest = rest.replace(/\[play_music:[^\]]*(\]|$)/g, '');
        window._realisticGeminiBuffer = '';
        var trimmed = rest.replace(/^\s+/, '').replace(/\s+$/, '');
        if (trimmed) {
            window._realisticGeminiQueue = window._realisticGeminiQueue || [];
            window._realisticGeminiQueue.push({
                text: trimmed,
                turnId: endingTurnId || null
            });
            if (typeof window.processRealisticQueue === 'function') {
                window.processRealisticQueue(window._realisticGeminiVersion || 0);
            }
        }
    }

    function clearPendingUserActivityCancel() {
        if (_pendingUserActivityCancelTimer) {
            clearTimeout(_pendingUserActivityCancelTimer);
            _pendingUserActivityCancelTimer = 0;
        }
        _pendingUserActivityCancelTurnId = null;
    }

    function hasBufferedAssistantAudioForTurn(turnId) {
        var normalizedTurnId = normalizeAssistantTurnId(turnId);
        if (!normalizedTurnId) {
            return false;
        }

        if (S.scheduledSources.some(function (source) {
            return normalizeAssistantTurnId(source && source._nekoAssistantTurnId) === normalizedTurnId;
        })) {
            return true;
        }

        if (S.audioBufferQueue.some(function (item) {
            return normalizeAssistantTurnId(item && item.turnId) === normalizedTurnId;
        })) {
            return true;
        }

        return S.incomingAudioBlobQueue.some(function (item) {
            return item &&
                !item.shouldSkip &&
                item.epoch === S.incomingAudioEpoch &&
                normalizeAssistantTurnId(item.turnId) === normalizedTurnId;
        });
    }

    function hasPendingAssistantAudioHeaderForTurn(turnId) {
        var normalizedTurnId = normalizeAssistantTurnId(turnId);
        if (!normalizedTurnId) {
            return false;
        }

        return S.pendingAudioChunkMetaQueue.some(function (item) {
            return item &&
                !item.shouldSkip &&
                item.epoch === S.incomingAudioEpoch &&
                normalizeAssistantTurnId(item.turnId) === normalizedTurnId;
        });
    }

    function resolveAssistantLifecycleTurnId(turnId) {
        return normalizeAssistantTurnId(
            turnId ||
            S.assistantTurnId ||
            S.assistantPendingTurnServerId ||
            S.assistantTurnCompletedId ||
            S.assistantSpeechActiveTurnId
        );
    }

    // 一轮 AI 文本说完后的统一收尾：音乐指令（可选）+ 情感分析 + 字幕翻译。
    // 'turn end'（用户发起）与 'turn end agent_callback'（主动消息 / 热切换回调）
    // 两条路径共用，避免 emotion / 字幕逻辑再像以前那样在两个分支间悄悄走样
    // ——旧版 agent_callback 分支漏掉了 emotion 分析，导致主动消息时头像表情僵住，
    // 且这类用户在 telemetry 上表现为「有 galgame_options 调用却从无 emotion 调用」。
    // 唯一保留的分支差异：
    //   - music commands：proactive 轮默认关闭（主动消息自动放歌过于侵入）；
    //   - proactive 调度：仅 'turn end' 分支 reschedule，agent_callback 不排，
    //     防止 proactive 自己触发下一条 proactive。
    // emotion 本身是只读的（仅向头像推一条表情，不触发对话 / 记忆 / 再投递），
    // 没有自触发风险，因此 proactive 轮也应当照常触发。
    function finalizeAssistantTurn(assistantTurnId, options) {
        options = options || {};
        var enableMusic = options.enableMusic !== false;

        var bufferedFullText = typeof window._geminiTurnFullText === 'string'
            ? window._geminiTurnFullText
            : '';
        var fallbackFromBubble = (window.currentGeminiMessage &&
            window.currentGeminiMessage.nodeType === Node.ELEMENT_NODE &&
            window.currentGeminiMessage.isConnected &&
            typeof window.currentGeminiMessage.textContent === 'string')
            ? window.currentGeminiMessage.textContent.replace(/^\[\d{2}:\d{2}:\d{2}\] \u{1F380} /, '')
            : '';

        var fullText = (bufferedFullText && bufferedFullText.trim()) ? bufferedFullText : fallbackFromBubble;

        // Trigger music bubble generation
        if (enableMusic && typeof window.processMusicCommands === 'function' && fullText) {
            window.processMusicCommands(fullText);
        }

        // Strip music commands before emotion analysis / subtitle translation
        fullText = fullText.replace(/\[play_music:[^\]]*(\]|$)/g, '').trim();

        if (!fullText || !fullText.trim()) {
            return;
        }

        // Emotion analysis (5s timeout)
        setTimeout(async function () {
            try {
                var emotionPromise = (typeof window.analyzeEmotion === 'function')
                    ? window.analyzeEmotion(fullText)
                    : Promise.resolve(null);
                var timeoutPromise = new Promise(function (_, reject2) {
                    setTimeout(function () { reject2(new Error('情感分析超时')); }, 5000);
                });
                var emotionResult = await Promise.race([emotionPromise, timeoutPromise]);
                if (emotionResult && emotionResult.emotion) {
                    console.log(window.t('console.emotionAnalysisComplete'), emotionResult);
                    if (typeof window.applyEmotion === 'function') window.applyEmotion(emotionResult.emotion);
                    if (assistantTurnId) {
                        emitAssistantLifecycleEvent('neko-assistant-emotion-ready', {
                            turnId: assistantTurnId,
                            emotion: emotionResult.emotion,
                            source: 'emotion_analysis'
                        });
                    }
                }
            } catch (emotionError) {
                if (emotionError.message === '情感分析超时') {
                    console.warn(window.t('console.emotionAnalysisTimeout'));
                } else {
                    console.warn(window.t('console.emotionAnalysisFailed'), emotionError);
                }
            }
        }, 100);

        // Frontend subtitle finalization: subtitle.js 内部根据开关决定是否
        // 真正发请求；不需要的语言会保留流式累积的原文，不会清空字幕。
        // 结构化 turn 收尾为 [markdown] 占位，跳过翻译链路。
        if (window._turnIsStructured) {
            if (typeof window.finalizeSubtitleAsStructured === 'function') {
                try { window.finalizeSubtitleAsStructured(); } catch (_) {}
            }
            return;
        }
        (async function () {
            try {
                if (typeof window.translateAndShowSubtitle === 'function') {
                    await window.translateAndShowSubtitle(fullText);
                }
            } catch (transError) {
                console.error(window.t('console.translationProcessFailed'), {
                    error: transError.message,
                    stack: transError.stack,
                    fullText: fullText.substring(0, 50) + '...'
                });
                if (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1') {
                    console.warn(window.t('console.translationUnavailable'));
                }
            }
        })();
    }

    function ensureAssistantTurnStarted(source, serverTurnId, responseMeta, requestId) {
        if (S.assistantTurnId) {
            window._nekoAssistantTurnId = S.assistantTurnId;
            clearPendingAssistantTurnStart();
            logAssistantLifecycle('ensureAssistantTurnStarted:reuse_existing', {
                source: source || 'visible_gemini_bubble',
                serverTurnId: normalizeAssistantTurnId(serverTurnId)
            });
            return S.assistantTurnId;
        }
        if (!S.assistantTurnAwaitingBubble && serverTurnId === undefined) {
            logAssistantLifecycle('ensureAssistantTurnStarted:skip', {
                source: source || 'visible_gemini_bubble'
            });
            return null;
        }

        S.assistantTurnId = allocateAssistantTurnId(
            serverTurnId === undefined ? S.assistantPendingTurnServerId : serverTurnId
        );
        window._nekoAssistantTurnId = S.assistantTurnId;
        S.assistantTurnStartedAt = Date.now();
        clearPendingAssistantTurnStart();
        emitAssistantLifecycleEvent('neko-assistant-turn-start', {
            turnId: S.assistantTurnId,
            requestId: resolveAssistantRequestId(requestId, responseMeta),
            source: source || 'visible_gemini_bubble',
            meta: responseMeta
        });
        logAssistantLifecycle('ensureAssistantTurnStarted:emitted', {
            source: source || 'visible_gemini_bubble',
            serverTurnId: normalizeAssistantTurnId(serverTurnId),
            turnId: S.assistantTurnId
        });
        return S.assistantTurnId;
    }

    function emitAssistantSpeechCancel(source) {
        var currentTurnId = resolveAssistantLifecycleTurnId();
        S.assistantSpeechActiveTurnId = null;
        logAssistantLifecycle('emitAssistantSpeechCancel', {
            source: source,
            turnId: currentTurnId
        });
        if (currentTurnId) {
            emitAssistantLifecycleEvent('neko-assistant-speech-cancel', {
                turnId: currentTurnId,
                source: source
            });
        } else {
            emitAssistantLifecycleEvent('neko-assistant-speech-cancel', {
                source: source
            });
        }
    }

    function applyUserActivityCancel(interruptedSpeechId, source) {
        clearPendingUserActivityCancel();
        emitAssistantSpeechCancel(source || 'user_activity');
        S.assistantTurnId = null;
        window._nekoAssistantTurnId = null;
        clearPendingAssistantTurnStart();
        S.interruptedSpeechId = interruptedSpeechId || null;
        S.pendingDecoderReset = true;
        S.skipNextAudioBlob = false;
        S.incomingAudioEpoch += 1;
        S.incomingAudioBlobQueue = [];
        S.pendingAudioChunkMetaQueue = [];

        if (typeof window.clearAudioQueueWithoutDecoderReset === 'function') {
            window.clearAudioQueueWithoutDecoderReset();
        }
    }

    function shouldDelayUserActivityCancel(turnId) {
        var normalizedTurnId = normalizeAssistantTurnId(turnId);
        if (!normalizedTurnId) {
            return false;
        }

        if (normalizeAssistantTurnId(S.assistantSpeechActiveTurnId) === normalizedTurnId) {
            return false;
        }

        if (hasBufferedAssistantAudioForTurn(normalizedTurnId)) {
            return false;
        }

        if (hasPendingAssistantAudioHeaderForTurn(normalizedTurnId)) {
            return true;
        }

        return normalizeAssistantTurnId(S.assistantTurnCompletedId) === normalizedTurnId;
    }

    function scheduleUserActivityCancel(turnId, interruptedSpeechId) {
        clearPendingUserActivityCancel();

        var normalizedTurnId = normalizeAssistantTurnId(turnId);
        if (!normalizedTurnId) {
            applyUserActivityCancel(interruptedSpeechId, 'user_activity');
            return;
        }

        _pendingUserActivityCancelTurnId = normalizedTurnId;
        logAssistantLifecycle('scheduleUserActivityCancel:scheduled', {
            turnId: normalizedTurnId,
            delayMs: USER_ACTIVITY_CANCEL_GRACE_MS
        });
        _pendingUserActivityCancelTimer = window.setTimeout(function () {
            var pendingTurnId = _pendingUserActivityCancelTurnId;
            _pendingUserActivityCancelTimer = 0;
            _pendingUserActivityCancelTurnId = null;

            if (!pendingTurnId || pendingTurnId !== normalizedTurnId) {
                logAssistantLifecycle('scheduleUserActivityCancel:skip_turn_mismatch', {
                    turnId: normalizedTurnId
                });
                return;
            }

            if (normalizeAssistantTurnId(S.assistantSpeechActiveTurnId) === pendingTurnId ||
                hasBufferedAssistantAudioForTurn(pendingTurnId)) {
                logAssistantLifecycle('scheduleUserActivityCancel:skip_audio_resumed', {
                    turnId: pendingTurnId
                });
                return;
            }

            applyUserActivityCancel(interruptedSpeechId, 'user_activity_delayed');
        }, USER_ACTIVITY_CANCEL_GRACE_MS);
    }

    function clearAssistantLifecycleOnDisconnect(source) {
        clearPendingUserActivityCancel();
        emitAssistantSpeechCancel(source || 'socket_close');
        try {
            window.dispatchEvent(new CustomEvent('neko:websocket-disconnected', {
                detail: { source: source || 'socket_close' }
            }));
        } catch (_) {}
        S.assistantSpeechActiveTurnId = null;
        S.assistantTurnId = null;
        window._nekoAssistantTurnId = null;
        S.assistantTurnCompletedId = null;
        S.assistantTurnSettledId = null;
        S.assistantTurnCompletionSource = null;
        clearPendingAssistantTurnStart();
        S.currentPlayingSpeechId = null;
        S.currentPlayingSpeechCorrelationId = '';
        S.interruptedSpeechId = null;
        S.pendingDecoderReset = false;
        S.skipNextAudioBlob = false;
        S.incomingAudioEpoch += 1;
        S.incomingAudioBlobQueue = [];
        S.pendingAudioChunkMetaQueue = [];
        logAssistantLifecycle('clearAssistantLifecycleOnDisconnect', {
            source: source || 'socket_close'
        });
    }

    function stopAssistantTextOutputOnSessionEnd(source) {
        S.suppressAssistantStreamUntilNextSession = true;
        window._realisticGeminiVersion = (window._realisticGeminiVersion || 0) + 1;
        window._realisticGeminiQueue = [];
        window._realisticGeminiBuffer = '';
        window._geminiTurnFullText = '';
        window._pendingMusicCommand = '';
        window._structuredGeminiStreaming = false;
        window._isProcessingRealisticQueue = false;
        window._realisticProcessingOwner = null;
        window._geminiTurnEndSealed = true;

        var currentBubbles = Array.isArray(window.currentTurnGeminiBubbles)
            ? window.currentTurnGeminiBubbles.slice()
            : [];
        if (currentBubbles.length === 0 && window.currentGeminiMessage) {
            currentBubbles = [window.currentGeminiMessage];
        }
        var currentBubbleIds = [];
        currentBubbles.forEach(function (bubble) {
            if (bubble && bubble.dataset && bubble.dataset.reactChatMessageId) {
                currentBubbleIds.push(bubble.dataset.reactChatMessageId);
            }
            if (typeof window.setReactMessageStatus === 'function') {
                try {
                    window.setReactMessageStatus(bubble, 'assistant', 'sent');
                } catch (_) {}
            }
        });
        if (currentBubbleIds.length > 0 && typeof window._clearPendingHostMessagesByIds === 'function') {
            window._clearPendingHostMessagesByIds(currentBubbleIds);
        }

        window.currentGeminiMessage = null;
        window.currentTurnGeminiBubbles = [];
        window.realisticGeminiCurrentTurnId = null;
        logAssistantLifecycle('stopAssistantTextOutputOnSessionEnd', {
            source: source || 'session_end'
        });
    }

    window.addEventListener('neko-assistant-turn-start', clearPendingUserActivityCancel);
    window.addEventListener('neko-assistant-speech-start', clearPendingUserActivityCancel);
    window.addEventListener('neko-assistant-speech-cancel', clearPendingUserActivityCancel);

    // ========================  Convenience helpers  ========================

    /** Check whether the WebSocket is open */
    mod.isOpen = function () {
        return S.socket && S.socket.readyState === WebSocket.OPEN;
    };

    // ========================  ensureWebSocketOpen  ========================

    // 区分"字段未注入"和"字段注入为空串"：未注入返回 null（继续等待 page config 注入），
    // 注入为空串返回 ''（合法的"当前没有角色"，应直接尝试 connect 而不是无谓等待 5s 超时）。
    function getWebSocketLanlanName() {
        var cfg = window.lanlan_config;
        if (!cfg || typeof cfg !== 'object') return null;
        if (!Object.prototype.hasOwnProperty.call(cfg, 'lanlan_name')) return null;
        var v = cfg.lanlan_name;
        return v == null ? '' : String(v);
    }

    function getLiveRendererLanguage() {
        try {
            if (window.i18next && window.i18next.language) return window.i18next.language;
            if (window.i18n && window.i18n.language) return window.i18n.language;
            return localStorage.getItem('i18nextLng') || navigator.language || 'en';
        } catch (_) {
            return 'en';
        }
    }

    function getConversationLanguageForCurrentCharacter() {
        try {
            // Once the server preference has been hydrated, it is authoritative for
            // this session even when a previous localStorage write failed.
            if (S.conversationLanguageHydrated === true) {
                if (S.conversationLanguage) return S.conversationLanguage;
                return getLiveRendererLanguage();
            }
            if (typeof window.getConversationLanguagePreference === 'function') {
                return window.getConversationLanguagePreference(getWebSocketLanlanName() || '');
            }
            if (S.conversationLanguage) return S.conversationLanguage;
            return getLiveRendererLanguage();
        } catch (_) {
            return S.conversationLanguage || 'en';
        }
    }

    function getExplicitConversationLanguageForCurrentCharacter() {
        try {
            if (S.conversationLanguageHydrated === true) {
                return S.conversationLanguageExplicit || '';
            }
            if (typeof window.getExplicitConversationLanguagePreference === 'function') {
                return window.getExplicitConversationLanguagePreference(
                    getWebSocketLanlanName() || ''
                ) || '';
            }
        } catch (_) { /* omit unavailable explicit preference */ }
        return '';
    }

    function hydrateConversationLanguage(characterName) {
        var hydrationId = (Number(S._conversationLanguageHydrationId) || 0) + 1;
        S._conversationLanguageHydrationId = hydrationId;
        S.conversationLanguageHydrated = false;
        var fallback = getConversationLanguageForCurrentCharacter();
        if (!characterName) {
            S.conversationLanguage = '';
            S.conversationLanguageExplicit = '';
            S.conversationLanguageHydrated = true;
            return Promise.resolve(fallback);
        }

        var explicitAtStart = '';
        var preferenceRevisionAtStart = null;
        try {
            if (typeof window.getExplicitConversationLanguagePreference === 'function') {
                explicitAtStart = window.getExplicitConversationLanguagePreference(characterName) || '';
            }
        } catch (_) { /* continue with server hydration */ }
        try {
            if (typeof window.getConversationLanguagePreferenceRevision === 'function') {
                preferenceRevisionAtStart = window.getConversationLanguagePreferenceRevision(characterName);
            }
        } catch (_) { /* retain the value-only fence below */ }

        var request = fetch('/api/characters/character/' + encodeURIComponent(characterName) + '/language-preference', {
            cache: 'no-store'
        }).then(function (response) {
            if (!response.ok) throw new Error('HTTP ' + response.status);
            return response.json();
        }).then(function (payload) {
            if (!payload || payload.success !== true) throw new Error('invalid language preference response');
            var explicitLanguage = typeof payload.language === 'string'
                ? payload.language.trim()
                : '';
            return {
                language: explicitLanguage,
                explicitLanguage: explicitLanguage
            };
        }).catch(function (error) {
            console.warn('[ConversationLanguage] preference hydration failed, using UI fallback:', error);
            return { language: fallback, explicitLanguage: '', requestFailed: true };
        });

        function applyHydratedConversationLanguage(hydrated) {
            var resolved = hydrated || {};
            var degraded = !!(resolved.requestFailed || resolved.timedOut);
            // A successful empty response proves only that there is no durable
            // character preference. Its backend effective_language can lag a
            // URL/localStorage hot switch in this renderer, so resolve the live
            // renderer locale at application time instead of replaying that
            // process-level fallback over a newer UI update.
            var language = resolved.explicitLanguage || (
                degraded
                    ? (resolved.language || fallback)
                    : getLiveRendererLanguage()
            );
            if (S._conversationLanguageHydrationId !== hydrationId) return language;
            var currentExplicit = '';
            var currentExplicitResolved = false;
            var preferenceRevisionChanged = false;
            var localPreferenceOwnsResult = false;
            try {
                if (typeof window.getExplicitConversationLanguagePreference === 'function') {
                    currentExplicit = window.getExplicitConversationLanguagePreference(characterName) || '';
                    currentExplicitResolved = true;
                }
            } catch (_) { /* a newer revision still fences the stale response */ }
            try {
                if (
                    preferenceRevisionAtStart !== null
                    && typeof window.getConversationLanguagePreferenceRevision === 'function'
                ) {
                    preferenceRevisionChanged = (
                        window.getConversationLanguagePreferenceRevision(characterName)
                        !== preferenceRevisionAtStart
                    );
                }
            } catch (_) { /* retain the value-only fence below */ }
            // A revision change covers same-page dispatch:false writes and clears,
            // including those that win before the timeout. Never let a degraded
            // result mark that newer state untrusted or let a late response replace it.
            if (preferenceRevisionChanged) {
                language = currentExplicitResolved && currentExplicit
                    ? currentExplicit
                    : getLiveRendererLanguage();
                resolved = {
                    language: language,
                    explicitLanguage: currentExplicitResolved ? currentExplicit : ''
                };
                degraded = false;
                localPreferenceOwnsResult = true;
            } else if (!degraded && currentExplicit && currentExplicit !== explicitAtStart) {
                // Shared storage can expose a newer explicit value before this
                // document receives its storage event. An unchanged startup cache
                // must not override a fresh authoritative empty response.
                resolved = {
                    language: currentExplicit,
                    explicitLanguage: currentExplicit
                };
                language = currentExplicit;
            }
            var effectiveConversationLanguage = resolved.explicitLanguage || '';
            if (degraded) {
                try {
                    if (typeof window.getCachedConversationLanguagePreference === 'function') {
                        effectiveConversationLanguage = window.getCachedConversationLanguagePreference(
                            characterName
                        ) || '';
                    }
                } catch (_) { effectiveConversationLanguage = ''; }
                try {
                    if (typeof window.markConversationLanguagePreferenceUntrusted === 'function') {
                        window.markConversationLanguagePreferenceUntrusted(characterName);
                    }
                } catch (_) { /* keep hydration fail-soft */ }
            }
            // Fresh hydration stores the durable character preference. A degraded
            // local-cache value may keep this page's templates stable, but it is
            // not server evidence and must remain render-only.
            S.conversationLanguage = effectiveConversationLanguage;
            S.conversationLanguageExplicit = resolved.explicitLanguage || '';
            S.conversationLanguageHydrated = true;
            // Only mirror an explicit character preference into the local cache.
            // effective_language is a UI/global fallback and must remain dynamic.
            if (
                !localPreferenceOwnsResult
                && resolved.explicitLanguage
                && typeof window.setConversationLanguagePreference === 'function'
            ) {
                window.setConversationLanguagePreference(
                    resolved.explicitLanguage,
                    characterName,
                    { dispatch: false, source: 'server' }
                );
            } else if (
                !localPreferenceOwnsResult
                &&
                !resolved.requestFailed
                && !resolved.timedOut
                && typeof window.clearConversationLanguagePreference === 'function'
            ) {
                window.clearConversationLanguagePreference(characterName, {
                    dispatch: false,
                    source: 'server'
                });
            }
            if (!localPreferenceOwnsResult && resolved.explicitLanguage) {
                _syncLanguageToBackend(resolved.explicitLanguage);
            } else if (
                !localPreferenceOwnsResult
                && !resolved.requestFailed
                && !resolved.timedOut
            ) {
                _syncClearedLanguageToBackend(language, characterName);
            }
            _sendGreetingCheckIfReady();
            return language;
        }

        return Promise.race([
            request,
            new Promise(function (resolve) {
                setTimeout(function () {
                    resolve({ language: fallback, explicitLanguage: '', timedOut: true });
                }, 2500);
            })
        ]).then(function (hydrated) {
            var language = applyHydratedConversationLanguage(hydrated);
            if (hydrated && hydrated.timedOut) {
                // The timeout keeps startup responsive, but it must not discard a
                // valid server preference that arrives later for the same character.
                void request.then(function (lateHydrated) {
                    if (lateHydrated && !lateHydrated.requestFailed) {
                        applyHydratedConversationLanguage(lateHydrated);
                    }
                });
            }
            return language;
        });
    }

    // Upper bound for the settings-sync gate below: a hung POST must never
    // block session starts or socket-dependent flows for longer than this.
    var SETTINGS_SYNC_GATE_TIMEOUT_MS = 3000;

    function waitForConversationLanguageHydration() {
        var pending = S._conversationLanguageHydration;
        if (!pending || typeof pending.then !== 'function') return Promise.resolve();
        return pending.catch(function () { /* hydration is fail-soft */ });
    }

    /**
     * Refresh the current Core's independent-ASR capability from the same
     * configuration endpoint used by the API settings UI. The capability is
     * deliberately tri-state: only an explicit false may disable the effective
     * UI. It never rewrites the user's preference or handshake, and a failed or
     * legacy response remains unknown.
     *
     * Concurrent callers in one window share the in-flight refresh. `force`
     * only bypasses completed cache data; the generation fence remains a
     * defensive guard around request publication.
     */
    function publishCoreApiCapability(provider, capability) {
        var previousProvider = S.coreApiProvider || '';
        var previousCapability = S.coreApiSupportsIndependentAsr;
        S.coreApiProvider = typeof provider === 'string' ? provider : '';
        S.coreApiSupportsIndependentAsr =
            typeof capability === 'boolean' ? capability : null;
        if (
            previousProvider !== S.coreApiProvider
            || previousCapability !== S.coreApiSupportsIndependentAsr
        ) {
            try {
                window.dispatchEvent(new CustomEvent(
                    'neko:core-api-capability-changed',
                    {
                        detail: {
                            provider: S.coreApiProvider,
                            supportsIndependentAsr:
                                S.coreApiSupportsIndependentAsr
                        }
                    }
                ));
            } catch (_) { /* optional UI notification */ }
        }
        return {
            provider: S.coreApiProvider,
            supportsIndependentAsr: S.coreApiSupportsIndependentAsr
        };
    }

    function refreshCoreApiCapability(options) {
        options = options || {};
        // `force` bypasses completed cache data, not a request that is already
        // in flight. All callers in this window share the same fresh result.
        if (_coreApiCapabilityRefreshPromise) {
            return _coreApiCapabilityRefreshPromise;
        }
        if (
            options.force !== true
            && typeof S.coreApiSupportsIndependentAsr === 'boolean'
        ) {
            return Promise.resolve({
                provider: S.coreApiProvider || '',
                supportsIndependentAsr: S.coreApiSupportsIndependentAsr
            });
        }
        if (typeof window.fetch !== 'function') {
            return Promise.resolve({
                provider: S.coreApiProvider || '',
                supportsIndependentAsr: S.coreApiSupportsIndependentAsr
            });
        }

        var requestGeneration = ++_coreApiCapabilityRequestGeneration;
        var refreshPromise = window.fetch('/api/config/core_api', {
            cache: 'no-store',
            headers: { Accept: 'application/json' }
        }).then(function (response) {
            if (!response || response.ok === false) {
                throw new Error('Core API capability request failed');
            }
            return response.json();
        }).then(function (data) {
            if (requestGeneration !== _coreApiCapabilityRequestGeneration) {
                return {
                    provider: S.coreApiProvider || '',
                    supportsIndependentAsr: S.coreApiSupportsIndependentAsr
                };
            }
            data = data && typeof data === 'object' ? data : {};
            if (data.success === false) {
                throw new Error('Core API capability response was unsuccessful');
            }
            return publishCoreApiCapability(
                typeof data.effectiveCoreApi === 'string'
                    ? data.effectiveCoreApi
                    : data.coreApi,
                data.supportsIndependentAsr
            );
        }).catch(function (error) {
            console.warn('[Core API] Failed to refresh ASR capability:', error);
            if (requestGeneration === _coreApiCapabilityRequestGeneration) {
                return publishCoreApiCapability('', null);
            }
            return {
                provider: S.coreApiProvider || '',
                supportsIndependentAsr: S.coreApiSupportsIndependentAsr
            };
        }).finally(function () {
            if (_coreApiCapabilityRefreshPromise === refreshPromise) {
                _coreApiCapabilityRefreshPromise = null;
            }
        });
        _coreApiCapabilityRefreshPromise = refreshPromise;
        return refreshPromise;
    }

    // Prime the capability once for both Web and Electron windows. API Core
    // changes normally reload these pages; the microphone popup also forces a
    // refresh so a window that missed that reload cannot keep a stale view.
    refreshCoreApiCapability();

    /**
     * Wait for the WebSocket to reach OPEN state.
     *   - Already OPEN  -> resolves immediately
     *   - CONNECTING     -> waits via addEventListener('open')
     *   - CLOSED/CLOSING -> cancels queued auto-reconnect, calls connectWebSocket(), waits
     * @param {number} timeoutMs  timeout in ms (default 5000)
     * @returns {Promise<void>}
     */
    function ensureWebSocketOpen(timeoutMs = 5000) {
        // Settings-sync gate: toggling independent ASR publishes its in-flight
        // settings POST as S.pendingSettingsSyncPromise (app-audio-capture.js).
        // Every start_session send awaits ensureWebSocketOpen() first, and the
        // backend reads the SERVER-persisted independentAsrEnabled value at
        // session start (asr_runtime.py _start_independent_asr_if_enabled), so
        // waiting here closes the race where the first voice session after the
        // toggle silently used the previous route. The wait is bounded and the
        // gated promise never rejects (syncSettingsToServer swallows errors).
        var pendingSync = S.pendingSettingsSyncPromise;
        if (pendingSync && typeof pendingSync.then === 'function') {
            return Promise.race([
                pendingSync.catch(function () { /* gate must never reject */ }),
                new Promise(function (resolve) { setTimeout(resolve, SETTINGS_SYNC_GATE_TIMEOUT_MS); })
            ]).then(function () {
                return ensureWebSocketOpenNow(timeoutMs);
            }).then(waitForConversationLanguageHydration);
        }
        return ensureWebSocketOpenNow(timeoutMs).then(waitForConversationLanguageHydration);
    }

    function ensureWebSocketOpenNow(timeoutMs) {
        return new Promise(function (resolve, reject) {
            if (S.socket && S.socket.readyState === WebSocket.OPEN) {
                return resolve();
            }

            var settled = false;
            var timer = null;
            var lanlanWaitTimer = null;
            var socketPollTimer = null;

            var clearAutoReconnectTimer = function () {
                if (S.autoReconnectTimeoutId) {
                    clearTimeout(S.autoReconnectTimeoutId);
                    S.autoReconnectTimeoutId = null;
                }
            };

            var settle = function (fn, arg) {
                if (settled) return;
                settled = true;
                if (timer) { clearTimeout(timer); timer = null; }
                if (lanlanWaitTimer) { clearTimeout(lanlanWaitTimer); lanlanWaitTimer = null; }
                if (socketPollTimer) { clearTimeout(socketPollTimer); socketPollTimer = null; }
                clearAutoReconnectTimer();
                // 这次 ensureWebSocketOpen 已结束，归零退避状态，避免下次调用继承旧 attempts
                // 让退避一上来又是 ~2s 的 stale 节奏。
                _lanlanNameWaitAttempts = 0;
                _lanlanNameWaitLastLogAt = 0;
                fn(arg);
            };

            // Timeout
            timer = setTimeout(function () {
                settle(reject, new Error(window.t ? window.t('app.websocketNotConnectedError') : 'WebSocket未连接'));
            }, timeoutMs);

            // Attach listener to current or future socket
            var attachOpenListener = function (ws) {
                if (!ws || settled) return;
                if (ws.readyState === WebSocket.OPEN) {
                    settle(resolve); return;
                }
                if (ws.readyState === WebSocket.CONNECTING) {
                    ws.addEventListener('open', function () { settle(resolve); }, { once: true });
                    ws.addEventListener('error', function () { /* wait for new socket */ }, { once: true });
                    return;
                }
                // CLOSING / CLOSED -- fall through to polling
            };

            if (S.socket && S.socket.readyState === WebSocket.CONNECTING) {
                attachOpenListener(S.socket);
            } else if (S.isSwitchingCatgirl) {
                // 切换期间 handleCatgirlSwitch 独家负责新建 socket（close → sleep → connect）。
                // 如果这里也发起 connectWebSocket，会和 handleCatgirlSwitch 的 connect 双重重连：
                // 前一个新 socket 被后一个覆盖变成孤儿，polling 被迫重绑，5s 超时即报
                // "WebSocket not connected"。改为仅靠下面的 polling 等新 socket 就位。
            } else {
                // socket does not exist or CLOSED/CLOSING -> rebuild
                clearAutoReconnectTimer();
                var connectWhenLanlanNameReady = function () {
                    if (settled) return;
                    if (getWebSocketLanlanName() !== null) {
                        connectWebSocket();
                        return;
                    }
                    _lanlanNameWaitAttempts += 1;
                    var waitNow = Date.now();
                    if (!_lanlanNameWaitLastLogAt || waitNow - _lanlanNameWaitLastLogAt >= 5000) {
                        console.warn('[WebSocket] lanlan_name not ready, waiting for page config');
                        _lanlanNameWaitLastLogAt = waitNow;
                    }
                    lanlanWaitTimer = setTimeout(function () {
                        lanlanWaitTimer = null;
                        connectWhenLanlanNameReady();
                    }, Math.min(3000, 500 + Math.min(_lanlanNameWaitAttempts, 6) * 250));
                };
                connectWhenLanlanNameReady();
            }

            // Polling fallback: track socket reference; re-attach when replaced
            var lastAttachedWs = null;
            var scheduleSocketPoll = function (delay) {
                if (settled) return;
                socketPollTimer = setTimeout(function () {
                    socketPollTimer = null;
                    waitForNewSocket();
                }, delay);
            };
            var waitForNewSocket = function () {
                if (settled) return;
                if (S.socket) {
                    if (S.socket !== lastAttachedWs) {
                        lastAttachedWs = S.socket;
                        attachOpenListener(S.socket);
                    }
                    if (!settled) {
                        scheduleSocketPoll(S.socket.readyState === WebSocket.CONNECTING ? 200 : 50);
                    }
                } else {
                    scheduleSocketPoll(50);
                }
            };
            scheduleSocketPoll(10);
        });
    }
    mod.ensureWebSocketOpen = ensureWebSocketOpen;
    mod.refreshCoreApiCapability = refreshCoreApiCapability;

    // ========================  connectWebSocket  ========================

    // Stamp the frontend's authoritative independent-ASR toggle onto every
    // outgoing start_session payload. The bounded settings-sync gate in
    // ensureWebSocketOpen() is best-effort only: when the settings POST fails
    // or is still in flight past the bound, the backend would read a stale
    // persisted independentAsrEnabled at session start. Carrying the toggle in
    // the start_session handshake lets the backend override that read for this
    // session (websocket_router -> asr_runtime.set_independent_asr_handshake).
    // Wrapping send() at socket creation is the single seam that covers every
    // start_session send site, including the ones in app-buttons.js.
    //
    // Hydration gate, two parts, BOTH required (Codex P2). S.settingsHydrated
    // means "some authoritative settings event happened" — but it also flips on
    // any unrelated user preference change (settings popup, subtitle toggle,
    // chat-window translate toggle), which says nothing about the ASR value.
    // S.independentAsrAuthoritative is the per-key half: set only by a merged
    // server GET, an explicit independent-ASR toggle, or a cross-window ASR
    // flip. Before both hold — fresh browser profile, an early start_session
    // racing the still-pending GET, or a permanently failing GET plus one
    // unrelated setting change — S.independentAsrEnabled is just the boot
    // default false, and stamping it would override the
    // backend's persisted true. Omit the field instead: websocket_router
    // forwards an absent field as None and set_independent_asr_handshake
    // falls back to the persisted setting (pinned by
    // test_start_session_handshake_missing_falls_back_to_persisted). If the
    // GET fails permanently the field simply stays omitted and the backend's
    // persisted value keeps governing — the correct fallback.
    //
    // Core capability is intentionally NOT folded into this payload. Another
    // window may switch to a capable Core while this window still has a stale
    // false capability cache. The handshake always carries the authoritative
    // user preference; the backend applies the current Core capability as the
    // final routing guard.
    function attachStartSessionHandshake(ws) {
        var rawSend = ws.send.bind(ws);
        ws.send = function (data) {
            if (typeof data === 'string' && data.indexOf('start_session') !== -1) {
                try {
                    var msg = JSON.parse(data);
                    var handshakeStamped = false;
                    if (msg && msg.action === 'start_session' && S.settingsHydrated === true && S.independentAsrAuthoritative === true) {
                        msg.independent_asr_enabled = S.independentAsrEnabled === true;
                        handshakeStamped = true;
                    }
                    if (msg && msg.action === 'start_session' && S.settingsHydrated === true && S.voiceInputResourceOptimizationAuthoritative === true) {
                        msg.voice_input_resource_optimization_enabled = S.voiceInputResourceOptimizationEnabled !== false;
                        handshakeStamped = true;
                    }
                    if (msg && msg.action === 'start_session') {
                        var explicitLanguage = typeof getExplicitConversationLanguageForCurrentCharacter === 'function'
                            ? getExplicitConversationLanguageForCurrentCharacter()
                            : '';
                        var renderLanguage = typeof getConversationLanguageForCurrentCharacter === 'function'
                            ? getConversationLanguageForCurrentCharacter()
                            : '';
                        if (explicitLanguage) {
                            msg.language = explicitLanguage;
                            handshakeStamped = true;
                        }
                        if (renderLanguage) {
                            msg.render_language = renderLanguage;
                            handshakeStamped = true;
                        }
                    }
                    if (handshakeStamped) {
                        data = JSON.stringify(msg);
                    }
                } catch (e) {
                    // Non-JSON text frames pass through untouched.
                }
            }
            return rawSend(data);
        };
    }

    function connectWebSocket() {
        var currentLanlanName = getWebSocketLanlanName();
        // 进入 connectWebSocket 即意味着"当前已经在主动重连"，排队中的 auto-reconnect 不再需要。
        // 切换档案时 Chat 窗口曾出现这样的 stale 序列：handleCatgirlSwitch 刚 connect 的新代理被
        // 旧 WS 生命周期的 CLOSED IPC 误触发 close，onclose 排了一个 3s auto-reconnect；紧接着
        // READY IPC 让代理变 OPEN 恢复正常，但 3s 到期后这个 stale 定时器又跑一次 connectWebSocket，
        // 产出一个永远停在 CONNECTING 的僵尸代理，直接复现 "Start failed: WebSocket not connected"。
        if (S.autoReconnectTimeoutId) {
            clearTimeout(S.autoReconnectTimeoutId);
            S.autoReconnectTimeoutId = null;
        }
        // 仅在字段未注入（null）时退避等待；空串是合法"当前没有角色"，按下面正常 encode 走。
        if (currentLanlanName === null) {
            _lanlanNameWaitAttempts += 1;
            var waitNow = Date.now();
            if (!_lanlanNameWaitLastLogAt || waitNow - _lanlanNameWaitLastLogAt >= 5000) {
                console.warn('[WebSocket] lanlan_name not injected yet, waiting for page config');
                _lanlanNameWaitLastLogAt = waitNow;
            }
            S.autoReconnectTimeoutId = setTimeout(
                connectWebSocket,
                Math.min(3000, 500 + Math.min(_lanlanNameWaitAttempts, 6) * 250)
            );
            return;
        }
        _lanlanNameWaitAttempts = 0;
        _lanlanNameWaitLastLogAt = 0;
        var protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
        // 对 lanlan_name 做 percent-encode：WebSocket.url 会把非 ASCII 字符（中文角色名）
        // 编成 %XX，下面幂等守卫用 S.socket.url === wsUrl 比对，两侧编码口径必须一致，
        // 否则中文名时守卫永远失败、造不出真正的幂等。
        var wsUrl = protocol + '://' + window.location.host + '/ws/' + encodeURIComponent(currentLanlanName);

        // 幂等兜底：如果当前 socket 已经 OPEN 且指向同一个 URL，说明有 stale 路径
        // （比如 Chat 窗口里被误触发 onclose 排队的 auto-reconnect）到了这一步。
        // 此时再 new WebSocket 等同于主动造一个僵尸 socket：旧的 OPEN 失去引用、
        // 新的在 CONNECTING 里干等（Chat 代理不会再收 READY）。直接跳过即可。
        if (S.socket && S.socket.readyState === WebSocket.OPEN && S.socket.url === wsUrl) {
            return;
        }
        // A queued proactive/plugin dispatch belongs to the connection and
        // character that created it. Invalidate both asynchronous playback
        // stages before replacing that scope.
        if (typeof window.cancelPendingMusicMediaReady === 'function') {
            window.cancelPendingMusicMediaReady();
        }
        if (typeof window.cancelQueuedMusicDispatch === 'function') {
            window.cancelQueuedMusicDispatch();
        }

        S._conversationLanguageHydration = hydrateConversationLanguage(currentLanlanName);
        // 新连接重置模型就绪标志，等待模型重新加载
        S._modelReady = false;

        console.log(window.t('console.websocketConnecting'), currentLanlanName, window.t('console.websocketUrl'), wsUrl);
        S.socket = new WebSocket(wsUrl);
        attachStartSessionHandshake(S.socket);
        var _thisSocket = S.socket; // 闭包捕获，供 onclose 判断是否已被替换

        // ---- onopen ----
        S.socket.onopen = function () {
            if (S.socket !== _thisSocket) return;
            _latestAsrControlIdentity = null;
            _seenAsrIncidentIds = Object.create(null);
            _seenAsrIncidentOrder = [];
            console.log(window.t('console.websocketConnected'));

            if (S._conversationLanguageClearPending) {
                var pendingLanguageClear = S._conversationLanguageClearPending;
                if (pendingLanguageClear.characterName === getWebSocketLanlanName()) {
                    _syncClearedLanguageToBackend(
                        getConversationLanguageForCurrentCharacter(),
                        pendingLanguageClear.characterName
                    );
                } else {
                    S._conversationLanguageClearPending = null;
                }
            }

            window.dispatchEvent(new CustomEvent('voice-input-socket-open', {
                detail: { socket: _thisSocket }
            }));

            // Warm up Agent snapshot once websocket is ready.
            Promise.all([
                fetch('/api/agent/health').then(function (r) { return r.ok; }).catch(function () { return false; }),
                fetch('/api/agent/flags').then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; }),
                fetch('/api/agent/state').then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; })
            ]).then(function (results) {
                var healthOk = results[0];
                var flagsResp = results[1];
                var stateResp = results[2];

                if (flagsResp && flagsResp.success) {
                    window._agentStatusSnapshot = {
                        server_online: !!healthOk,
                        analyzer_enabled: !!flagsResp.analyzer_enabled,
                        flags: flagsResp.agent_flags || {},
                        agent_api_gate: flagsResp.agent_api_gate || {},
                        capabilities: (window._agentStatusSnapshot && window._agentStatusSnapshot.capabilities) || {},
                        updated_at: new Date().toISOString()
                    };
                    if (window.agentStateMachine && typeof window.agentStateMachine.updateCache === 'function') {
                        var warmFlags = flagsResp.agent_flags || {};
                        warmFlags.agent_enabled = !!flagsResp.analyzer_enabled;
                        window.agentStateMachine.updateCache(!!healthOk, warmFlags);
                    }
                }
                // Restore active tasks from state snapshot (covers page refresh / reconnect)
                if (stateResp && stateResp.success && stateResp.snapshot) {
                    var curName = (window.lanlan_config && window.lanlan_config.lanlan_name) || '';
                    var activeTasks = stateResp.snapshot.active_tasks || [];
                    var filteredTasks = curName
                        ? activeTasks.filter(function (t) { return !t.lanlan_name || t.lanlan_name === curName; })
                        : activeTasks;
                    window._agentTaskMap = new Map();
                    filteredTasks.forEach(function (t) { if (t && t.id) window._agentTaskMap.set(t.id, t); });
                    var tasks = Array.from(window._agentTaskMap.values());
                    var hasRunning = tasks.some(function (t) { return t.status === 'running' || t.status === 'queued'; });
                    if (tasks.length > 0 && window.AgentHUD && typeof window.AgentHUD.updateAgentTaskHUD === 'function') {
                        window.AgentHUD.showAgentTaskHUD();
                        window.AgentHUD.updateAgentTaskHUD({
                            success: true, tasks: tasks,
                            running_count: tasks.filter(function (t) { return t.status === 'running'; }).length,
                            queued_count: tasks.filter(function (t) { return t.status === 'queued'; }).length,
                        });
                        if (hasRunning && !window._agentTaskTimeUpdateInterval && !isGoodbyeUiSuppressed()) {
                            window._agentTaskTimeUpdateInterval = setInterval(function () {
                                if (typeof window.updateTaskRunningTimes === 'function') window.updateTaskRunningTimes();
                            }, 1000);
                        }
                    } else if (typeof window.checkAndToggleTaskHUD === 'function') {
                        window.checkAndToggleTaskHUD();
                    } else if (window.AgentHUD && typeof window.AgentHUD.hideAgentTaskHUD === 'function') {
                        window.AgentHUD.hideAgentTaskHUD();
                    }
                }
            }).catch(function () { });

            // Capture bridge: tell the backend whether this renderer can
            // service window-level captures through the active desktop host.
            // The backend uses this to fail /api/capture/health fast when
            // no desktop renderer is available (e.g. running in a plain
            // browser tab), which matters for the galgame OCR fallback path
            // on Linux pure-Wayland where MSS / PyAutoGUI can't see other
            // windows.
            // Note: intentionally broadcast for all renderers; non-desktop
            // environments send available=false and the backend ignores them.
            if (!announceCaptureBridgeStatus(_thisSocket)) {
                // Tauri injects its bridge after navigation. Re-announce for a
                // bounded window so a late bridge does not require reconnecting.
                reannounceCaptureBridgeWhenReady(_thisSocket, 0);
            }

            // Start heartbeat
            if (S.heartbeatInterval) {
                clearInterval(S.heartbeatInterval);
            }
            S.heartbeatInterval = setInterval(function () {
                if (S.socket && S.socket.readyState === WebSocket.OPEN) {
                    S.socket.send(JSON.stringify({ action: 'ping' }));
                }
            }, C.HEARTBEAT_INTERVAL);
            console.log(window.t('console.heartbeatStarted'));

            // ── 首次连接 / 切换角色：标记 greeting 意图，若模型已就绪则立即发送 ──
            var goodbyeActiveOnOpen = false;
            var goodbyeSyncOnOpen = null;
            try {
                var pendingGoodbyeState = window.__nekoGoodbyeSilentState;
                if (pendingGoodbyeState && pendingGoodbyeState.pending === true) {
                    goodbyeSyncOnOpen = {
                        active: !!pendingGoodbyeState.active,
                        reason: pendingGoodbyeState.reason || (pendingGoodbyeState.active ? 'goodbye' : 'return')
                    };
                }
                goodbyeActiveOnOpen = (typeof window.isNekoGoodbyeModeActive === 'function')
                    ? window.isNekoGoodbyeModeActive()
                    : !!((window.live2dManager && window.live2dManager._goodbyeClicked)
                        || (window.vrmManager && window.vrmManager._goodbyeClicked)
                        || (window.mmdManager && window.mmdManager._goodbyeClicked));
                if (!goodbyeSyncOnOpen && goodbyeActiveOnOpen) {
                    goodbyeSyncOnOpen = {
                        active: true,
                        reason: 'ws-open-goodbye'
                    };
                }
                if (!goodbyeSyncOnOpen && pendingGoodbyeState && pendingGoodbyeState.active === true) {
                    goodbyeSyncOnOpen = {
                        active: true,
                        reason: 'ws-open-goodbye-from-sync'
                    };
                }
                if (goodbyeSyncOnOpen && _thisSocket && _thisSocket.readyState === WebSocket.OPEN) {
                    _thisSocket.send(JSON.stringify({
                        action: 'goodbye_state',
                        active: !!goodbyeSyncOnOpen.active,
                        reason: goodbyeSyncOnOpen.reason
                    }));
                    window.__nekoGoodbyeSilentState = {
                        active: !!goodbyeSyncOnOpen.active,
                        reason: goodbyeSyncOnOpen.reason,
                        pending: false,
                        updatedAt: Date.now()
                    };
                }
            } catch (_) {
                goodbyeActiveOnOpen = false;
            }
            _resetGreetingCheckRetry(true);
            if (goodbyeActiveOnOpen || (goodbyeSyncOnOpen && goodbyeSyncOnOpen.active)) {
                S._greetingCheckPending = false;
                S._greetingCheckIsSwitch = false;
                S._greetingCheckReason = '';
                S._pendingGreetingSwitch = false;
            } else {
                var isGreetingSwitchOnOpen = !!S._pendingGreetingSwitch;
                var greetingReasonOnOpen = S._greetingCheckReason || (isGreetingSwitchOnOpen ? 'character-switch' : 'ws-open');
                _markGreetingCheckPending(isGreetingSwitchOnOpen, greetingReasonOnOpen);
                S._pendingGreetingSwitch = false;
                if (isGreetingSwitchOnOpen || S._startupGreetingReleaseGateUsed) {
                    _sendGreetingCheckIfReady();
                } else {
                    S._startupGreetingReleaseGateUsed = true;
                    sendStartupGreetingReleaseRequest('ws-open');
                }
            }

            // ── game-window-state 重连兜底（codex P2）──
            // game_window_state_change 是 edge-triggered WS 事件——只在 activate
            // / finalize 那一瞬推。WS 在 game 期间断开 + 期间 close 事件丢失 →
            // _gameWindowActive 卡在 true，UI 永远停在收缩态。onopen 同时覆盖
            // 首次连接和重连，主动查 /api/game/route/active 拿当前权威状态，
            // dispatch 对应 CustomEvent 让既有 listener 走正常 minimize / restore
            // 路径。idempotent：active=true + 已 minimize → _gameMinimizeForGame
            // 早返回；active=false + 无 snap → _gameRestoreAfterGame 早返回。
            (function syncGameWindowStateOnWsConnect() {
                var lan = '';
                try {
                    if (window.appState && typeof window.appState.lanlan_name === 'string') {
                        lan = window.appState.lanlan_name;
                    }
                    if (!lan && window.lanlan_config && typeof window.lanlan_config.lanlan_name === 'string') {
                        lan = window.lanlan_config.lanlan_name;
                    }
                } catch (_) {}
                if (!lan) return; // greeting 流水线还没解析角色 → 跳过本次，下次 onopen 再来
                _gameRouteReconciliationGeneration = (
                    _gameRouteReconciliationGeneration + 1
                ) >>> 0;
                var reconciliationGeneration = _gameRouteReconciliationGeneration;
                var routeRevisionAtRequest = gameRouteStateRevision();
                fetch('/api/game/route/active?lanlan_name=' + encodeURIComponent(lan))
                    .then(function (resp) { return resp && resp.ok ? resp.json() : null; })
                    .then(function (data) {
                        if (!data) return;
                        if (
                            reconciliationGeneration !== _gameRouteReconciliationGeneration
                            || gameRouteStateRevision() !== routeRevisionAtRequest
                        ) {
                            console.log('[GameWindow] 忽略晚到的重连路由快照');
                            return;
                        }
                        var action = data.active ? 'opened' : 'closed';
                        try {
                            window.dispatchEvent(new CustomEvent('neko-game-window-state-change', {
                                detail: {
                                    action: action,
                                    lanlanName: data.lanlan_name || lan,
                                    gameType: data.game_type || '',
                                    sessionId: data.session_id || '',
                                    routeInstanceId: data.sdk_route_instance_id || ''
                                }
                            }));
                        } catch (_) {}
                        // Repair appState from the authoritative snapshot too.
                        // The dispatch above is only converted into appState by
                        // app-game-voice-control.js, which ONLY index.html loads;
                        // chat.html has no listener that writes S.gameRoute*, so
                        // without this its route state stays wrong in both
                        // directions after a reconnect or reload. Runs after the
                        // dispatch so index.html keeps today's ordering and this
                        // is an idempotent rewrite there.
                        try {
                            if (action === 'opened') {
                                S.gameRouteActive = true;
                                S.gameRouteGameType = data.game_type || '';
                                S.gameRouteLanlanName = data.lanlan_name || lan;
                                S.gameRouteSessionId = data.session_id || '';
                                S.gameRouteInstanceId = data.sdk_route_instance_id || '';
                                if (typeof window.stopProactiveChatSchedule === 'function') {
                                    S.proactiveChatWasStoppedByGameRoute = !!S.proactiveChatEnabled;
                                    window.stopProactiveChatSchedule();
                                }
                            } else {
                                var reconciledWasActive = !!S.gameRouteActive;
                                // This branch exists to compensate for a MISSED
                                // `closed` event, so there is usually no tombstone
                                // for the route being cleared and a late
                                // GAME_VOICE_STT_GATE_ACTIVE would re-activate it.
                                // Record the identity the SERVER says it finalized,
                                // never the page's own: this read can disagree with
                                // the socket, and tombstoning a live route rejects
                                // its real gate for the rest of the round.
                                advanceGameRouteStateRevision();
                                var reconciledEnded = data.ended_route || null;
                                if (reconciledEnded && reconciledEnded.session_id) {
                                    rememberEndedGameRouteIdentity(
                                        reconciledEnded.game_type || '',
                                        reconciledEnded.session_id,
                                        reconciledEnded.sdk_route_instance_id || ''
                                    );
                                }
                                S.gameRouteActive = false;
                                S.gameRouteGameType = '';
                                S.gameRouteLanlanName = '';
                                S.gameRouteSessionId = '';
                                S.gameRouteInstanceId = '';
                                if ((reconciledWasActive || S.proactiveChatWasStoppedByGameRoute)
                                        && S.proactiveChatEnabled
                                        && typeof window.scheduleProactiveChat === 'function') {
                                    window.scheduleProactiveChat();
                                }
                                S.proactiveChatWasStoppedByGameRoute = false;
                            }
                        } catch (_) {}
                    })
                    .catch(function () {});
            })();
        };

        // ---- onmessage ----
        S.socket.onmessage = function (event) {
            if (S.socket !== _thisSocket) {
                console.log('[WS] stale onmessage skipped (socket already replaced)');
                return;
            }

            // Binary audio data
            if (event.data instanceof Blob) {
                if (window.DEBUG_AUDIO) {
                    console.log(window.t('console.audioBinaryReceived'), event.data.size, window.t('console.audioBinaryBytes'));
                }
                if (typeof window.enqueueIncomingAudioBlob === 'function') {
                    window.enqueueIncomingAudioBlob(event.data);
                }
                return;
            }

            try {
                var response = JSON.parse(event.data);
                if (response.type === 'catgirl_switched') {
                    console.log(window.t('console.catgirlSwitchedReceived'), response);
                }

                if (response.type === 'chat_blocks') {
                    // NOT gated on suppressAssistantStreamUntilNextSession.
                    // That latch stops a finished session's ASSISTANT stream
                    // from continuing to write into chat. A chat_blocks frame
                    // is a system post that claims no assistant identity and
                    // opens no turn, and `ai_behavior="blind"` is explicitly
                    // allowed to render with no model session at all — so
                    // gating it on model-session state discarded plugin
                    // notifications that never depended on a session, and they
                    // were gone for good if the user never started another
                    // one (Codex).
                    if (typeof window.appendReactChatBlocks === 'function') {
                        window.appendReactChatBlocks(response);
                    }
                    return;
                }

                // -------- gemini_response --------
                if (response.type === 'gemini_response') {
                    if (S.suppressAssistantStreamUntilNextSession) {
                        console.log('[App] discard assistant chunk after session ended by server');
                        return;
                    }
                    var isNewMessage = response.isNewMessage || false;
                    // Ordinary responses historically expose lifecycle metadata as
                    // `meta`, while mirror responses (including game dialogue) use
                    // `metadata`. Preserve the legacy field when present, but let
                    // mirror turns carry their session identity into turn-start.
                    var assistantResponseMeta = response.meta !== undefined
                        ? response.meta
                        : response.metadata;
                    var hasStructuredResponseBlocks = Array.isArray(response.blocks)
                        && response.blocks.length > 0;
                    if (response.metadata && response.metadata.game_route) {
                        var gameMeta = response.metadata.game_route;
                        var gameEvent = gameMeta.event || {};
                        console.log(`[GameMirror] 主聊天栏收到游戏台词 | game=${gameMeta.game_type || '-'} session=${gameMeta.session_id || '-'} kind=${gameEvent.kind || '-'} round=${gameEvent.round || '-'} source=${response.metadata.source || '-'}`);
                    }
                    // adapter 用 startNewSegment 抽象统一把每段独立 utterance 处理
                    // （path A: isNewMessage=true 多 response item；path B: turn_end
                    // 后的 late continuation, sealed && !isNewMessage）。lifecycle
                    // 这边也对偶：两条路径都重置 assistantTurn lifecycle 并 emit
                    // 新的 neko-assistant-turn-start，让 avatar-reaction-bubble /
                    // subtitle / audio-playback 等 listeners 都拿到独立通知。
                    //
                    // path B 尤其关键：avatar-reaction-bubble 的 handleTurnEnd 在
                    // text-only 段会 schedule fallback hide 定时器，没新 turn-start
                    // 取消的话 seg2 typing 期间表情气泡会被隐掉。
                    var sealedContinuation = !isNewMessage && !!window._geminiTurnEndSealed;
                    if (isNewMessage) {
                        // voice chat 中，AI 新消息到来时若上一条人类消息为纯空白则替换为 ...
                        // 仅 isNewMessage 走这条 voice-msg fix，sealed continuation
                        // 是同 dialog turn 延续，无新用户语音消息要修。
                        if (S.lastVoiceUserMessage && S.lastVoiceUserMessage.isConnected &&
                            !S.lastVoiceUserMessage.textContent.trim()) {
                            S.lastVoiceUserMessage.textContent = '...';
                        }
                        S.lastVoiceUserMessage = null;
                        S.lastVoiceUserMessageTime = 0;
                    }
                    if (isNewMessage || sealedContinuation) {
                        S.assistantTurnId = null;
                        window._nekoAssistantTurnId = null;
                        S.assistantPendingTurnServerId = normalizeAssistantTurnId(response.turn_id);
                        S.assistantTurnAwaitingBubble = true;
                    }
                    if (!S.assistantTurnId
                            && S.assistantTurnAwaitingBubble
                            && (getRenderableAssistantChunkText(response.text)
                                || hasStructuredResponseBlocks)) {
                        ensureAssistantTurnStarted(
                            'gemini_response_first_chunk',
                            response.turn_id,
                            assistantResponseMeta,
                            response.request_id
                        );
                    }
                    var createdVisibleBubble = false;
                    if (typeof window.appendMessage === 'function') {
                        if (hasStructuredResponseBlocks) {
                            createdVisibleBubble = window.appendMessage(
                                response.text,
                                'gemini',
                                isNewMessage,
                                { blocks: response.blocks }
                            ) === true;
                        } else {
                            createdVisibleBubble = window.appendMessage(response.text, 'gemini', isNewMessage) === true;
                        }
                    }
                    if (createdVisibleBubble && response.request_id) {
                        if (window.reactChatWindowHost && typeof window.reactChatWindowHost.clearPendingRollbackDraft === 'function') {
                            window.reactChatWindowHost.clearPendingRollbackDraft(response.request_id);
                        }
                        if (window._lastSubmittedRequestId === response.request_id) {
                            window._lastSubmittedText = '';
                            window._lastSubmittedRequestId = '';
                        }
                    }
                    if (!S.assistantTurnId && S.assistantTurnAwaitingBubble && createdVisibleBubble) {
                        ensureAssistantTurnStarted(
                            'gemini_response_visible_bubble',
                            response.turn_id,
                            assistantResponseMeta,
                            response.request_id
                        );
                    }
                    if (response.turn_id) {
                        window.realisticGeminiCurrentTurnId = response.turn_id;
                        // 如果有暂存的主动搭话附件，立即展示
                        if (window.appProactive && typeof window.appProactive._flushProactiveAttachments === 'function') {
                            window.appProactive._flushProactiveAttachments(response.turn_id);
                        }
                    }

                // -------- response_discarded --------
                } else if (response.type === 'response_discarded') {
                    clearPendingUserActivityCancel();
                    window.invalidatePendingMusicSearch();
                    if (S.suppressAssistantStreamUntilNextSession) {
                        logAssistantLifecycle('response_discarded_suppressed_after_session_end', {
                            reason: response.reason,
                            willRetry: !!response.will_retry
                        });
                        return;
                    }
                    if (!response.will_retry) {
                        try {
                            window.dispatchEvent(new CustomEvent('neko:assistant-response-cancelled', {
                                detail: {
                                    reason: response.reason || 'response-discarded',
                                    requestId: resolveAssistantRequestId(response.request_id, response.meta)
                                }
                            }));
                        } catch (_) {}
                    }
                    emitAssistantSpeechCancel('response_discarded');
                    S.assistantTurnId = null;
                    window._nekoAssistantTurnId = null;
                    clearPendingAssistantTurnStart();
                    // will_retry 时后端会再发一次 LLM 请求，对外仍然是"这一轮还在跑"——
                    // 但上面的 clearPendingAssistantTurnStart 已经把 awaitingBubble /
                    // pendingTextTurnSubmitAt 都清零了。重新写一次时间戳，让
                    // isAssistantTextResponseInFlight() 在 retry 的下一个 first-chunk
                    // 到来前保持 true，否则切语音那条等待循环会过早 resolve 然后
                    // end_session 把 retry 的 LLM 流又掐掉。
                    if (response.will_retry) {
                        S.pendingTextTurnSubmitAt = Date.now();
                    }
                    var attempt = response.attempt || 0;
                    var maxAttempts = response.max_attempts || 0;
                    console.log('[Discard] AI回复被丢弃 reason=' + response.reason + ' attempt=' + attempt + '/' + maxAttempts + ' retry=' + response.will_retry);

                    window._realisticGeminiQueue = [];
                    window._realisticGeminiBuffer = '';
                    window._pendingMusicCommand = '';
                    window._realisticGeminiVersion = (window._realisticGeminiVersion || 0) + 1;
                    // 重置并发锁，确保正在 sleep 的 processRealisticQueue 循环
                    // 醒来后通过 version 检查退出，且不会阻塞下一轮启动
                    window._isProcessingRealisticQueue = false;
                    window._realisticProcessingOwner = null;

                    // 同时清理 host 未就绪期间缓存的待发消息（防止 discard 的消息在 host ready 后被重放）
                    var hadTrackedBubbles = window.currentTurnGeminiBubbles && window.currentTurnGeminiBubbles.length > 0;
                    if (hadTrackedBubbles) {
                        var _discardIds = [];
                        window.currentTurnGeminiBubbles.forEach(function (bubble) {
                            if (bubble && bubble.dataset && bubble.dataset.reactChatMessageId) {
                                _discardIds.push(bubble.dataset.reactChatMessageId);
                            }
                        });
                        if (_discardIds.length > 0 && typeof window._clearPendingHostMessagesByIds === 'function') {
                            window._clearPendingHostMessagesByIds(_discardIds);
                        }
                        var _discardHost = window.reactChatWindowHost;
                        window.currentTurnGeminiBubbles.forEach(function (bubble) {
                            // Remove paired React mirror message
                            if (_discardHost && typeof _discardHost.removeMessage === 'function' &&
                                bubble && bubble.dataset && bubble.dataset.reactChatMessageId) {
                                _discardHost.removeMessage(bubble.dataset.reactChatMessageId);
                            }
                            if (bubble && bubble.parentNode) {
                                bubble.parentNode.removeChild(bubble);
                            }
                        });
                        window.currentTurnGeminiBubbles = [];
                    }
                    window.currentGeminiMessage = null;

                    if (window.currentTurnGeminiAttachments && window.currentTurnGeminiAttachments.length > 0) {
                        window.currentTurnGeminiAttachments.forEach(function (attachment) {
                            if (attachment && attachment.parentNode) {
                                attachment.parentNode.removeChild(attachment);
                            }
                        });
                        window.currentTurnGeminiAttachments = [];
                    }
                    window.realisticGeminiCurrentTurnId = null;

                    // Fallback: clear trailing gemini bubbles not tracked
                    var cc = chatContainer();
                    if (!hadTrackedBubbles &&
                        cc && cc.children && cc.children.length > 0) {
                        var _fallbackHost = window.reactChatWindowHost;
                        var toRemove = [];
                        for (var i = cc.children.length - 1; i >= 0; i--) {
                            var el = cc.children[i];
                            if (el.classList && el.classList.contains('message') && el.classList.contains('gemini')) {
                                toRemove.push(el);
                            } else {
                                break;
                            }
                        }
                        toRemove.forEach(function (el) {
                            if (_fallbackHost && typeof _fallbackHost.removeMessage === 'function' &&
                                el && el.dataset && el.dataset.reactChatMessageId) {
                                _fallbackHost.removeMessage(el.dataset.reactChatMessageId);
                            }
                            if (el && el.parentNode) el.parentNode.removeChild(el);
                        });
                    }

                    window._geminiTurnFullText = '';
                    window._pendingMusicCommand = '';
                    // discard 后清掉 turn_end seal flag，避免残留导致下一个 chunk
                    // 被误判为 sealedContinuation 触发不该触发的 lifecycle reset。
                    window._geminiTurnEndSealed = false;

                    // 推进 epoch 并清空入站音频队列，防止在途 TTS blob 被消费播放
                    S.incomingAudioEpoch += 1;
                    S.incomingAudioBlobQueue = [];
                    S.pendingAudioChunkMetaQueue = [];

                    (async function () {
                        if (typeof window.clearAudioQueue === 'function') await window.clearAudioQueue();
                    })();

                    // Check the discard code:
                    //   RESPONSE_TOO_LONG          — reroll exhausted with no recoverable
                    //                                sentence-end. UI rolls back the user's
                    //                                input so they can retry.
                    //   RESPONSE_LENGTH_TRUNCATED  — reroll exhausted but text was salvaged
                    //                                by truncating to the last sentence-end;
                    //                                the truncated text arrives via the
                    //                                normal gemini_response stream, so we
                    //                                must NOT rollback the input here.
                    var _isResponseTooLong = false;
                    var _isLengthTruncated = false;
                    if (!response.will_retry && response.message) {
                        try {
                            var _pdm = typeof response.message === 'string' ? JSON.parse(response.message) : response.message;
                            if (_pdm && _pdm.code === 'RESPONSE_TOO_LONG') _isResponseTooLong = true;
                            else if (_pdm && _pdm.code === 'RESPONSE_LENGTH_TRUNCATED') _isLengthTruncated = true;
                        } catch (_) { /* ignore */ }
                    }

                    if (_isResponseTooLong) {
                        // Suppress toast — backend sends cute text via gemini_response
                        // Only rollback user input here
                        if (window.reactChatWindowHost && typeof window.reactChatWindowHost.rollbackLastDraft === 'function') {
                            window.reactChatWindowHost.rollbackLastDraft(response.request_id);
                        }
                        var legacyInput = document.getElementById('textInputBox');
                        if (legacyInput && !legacyInput.value &&
                            response.request_id && window._lastSubmittedRequestId === response.request_id &&
                            window._lastSubmittedText) {
                            legacyInput.value = window._lastSubmittedText;
                            window._lastSubmittedText = '';
                            window._lastSubmittedRequestId = '';
                        }
                    } else if (_isLengthTruncated) {
                        // Suppress toast / error bubble. Keep the user's input cleared
                        // (truncated answer is a valid completion, no retry needed).
                        if (window.reactChatWindowHost && typeof window.reactChatWindowHost.clearPendingRollbackDraft === 'function') {
                            window.reactChatWindowHost.clearPendingRollbackDraft(response.request_id);
                        }
                        if (response.request_id && window._lastSubmittedRequestId === response.request_id) {
                            window._lastSubmittedText = '';
                            window._lastSubmittedRequestId = '';
                        }
                    } else {
                        if (!response.will_retry) {
                            if (window.reactChatWindowHost && typeof window.reactChatWindowHost.clearPendingRollbackDraft === 'function') {
                                window.reactChatWindowHost.clearPendingRollbackDraft(response.request_id);
                            }
                            if (response.request_id && window._lastSubmittedRequestId === response.request_id) {
                                window._lastSubmittedText = '';
                                window._lastSubmittedRequestId = '';
                            }
                        }
                        var retryMsg = window.t ? window.t('console.aiRetrying') : '猫娘链接出现异常，校准中…';
                        var failMsg = window.t ? window.t('console.aiFailed') : '猫娘链接出现异常';
                        if (typeof window.showStatusToast === 'function') {
                            window.showStatusToast(response.will_retry ? retryMsg : failMsg, 2500);
                        }

                        if (!response.will_retry && response.message) {
                            var translatedDiscardMsg = window.translateStatusMessage ? window.translateStatusMessage(response.message) : response.message;
                            appendAssistantStatusMessage(translatedDiscardMsg);
                        } else {
                            var cc3 = chatContainer();
                            if (cc3) cc3.scrollTop = cc3.scrollHeight;
                        }
                    }

                // -------- user_transcript_preview (independent ASR only) --------
                } else if (response.type === 'user_transcript_preview') {
                    var externalPreviewText = String(response.text || '');
                    var externalPreviewTurnId = String(response.asr_turn_id || '');
                    if (externalPreviewText === '') {
                        // 空 text 是后端的 preview-clear 信号（asr_runtime.py
                        // _send_core_asr_preview_clear）：空 final 结束的轮次不会注入
                        // user_transcript，唯有显式清除才能撤掉流式预览气泡；真实
                        // partial 后端保证非空，不会误触发。
                        // Codex P2：final 走 transcript dispatcher 的独立 worker，
                        // 可能排在下一轮 partial 之后才回调，于是上一轮的清除会抹掉
                        // 新一轮的气泡。按 asr_turn_id 配对：只有清除信号指向的轮次
                        // 仍是屏幕上这个气泡时才移除；过期的清除直接忽略。任一侧缺
                        // id（旧后端 / 轮次未 prepare）时退回无条件移除的老行为。
                        var displayedPreview = S.externalAsrPreviewMessage;
                        var displayedPreviewTurnId = displayedPreview
                            ? String(displayedPreview.asrTurnId || '')
                            : '';
                        if (!externalPreviewTurnId || !displayedPreviewTurnId ||
                            externalPreviewTurnId === displayedPreviewTurnId) {
                            removeExternalAsrPreview();
                        }
                    } else {
                        S.externalAsrPreviewMessage = upsertExternalAsrPreview(externalPreviewText);
                        if (S.externalAsrPreviewMessage) {
                            S.externalAsrPreviewMessage.asrTurnId = externalPreviewTurnId;
                        }
                    }

                // -------- user_transcript --------
                } else if (response.type === 'user_transcript') {
                    // user_transcript 不带轮次身份（由 main_logic/core/turn.py 发出），
                    // 这里无法配对，只能保持无条件移除；迟到 final 误删新一轮气泡的情况
                    // 由后端在 user_transcript 之后补发该轮 preview 复原
                    // （asr_runtime.py _restore_core_asr_preview_after_final）。
                    removeExternalAsrPreview();
                    var normalizedVoiceTranscript = String(response.text || '').trim();
                    if (normalizedVoiceTranscript) {
                        window.dispatchEvent(new CustomEvent('neko:user-voice-content-received', {
                            detail: {
                                requestId: resolveAssistantRequestId(response.request_id, response.meta),
                                text: normalizedVoiceTranscript,
                                source: String(response.source || 'voice'),
                                gameType: String(response.game_type || ''),
                                sessionId: String(response.session_id || ''),
                                routeInstanceId: String(response.sdk_route_instance_id || '')
                            }
                        }));
                    }
                    // 语音转写也属于用户首次输入；这里只标记，成就仍等 AI 首次可见回复时触发
                    if (window.appChat && typeof window.appChat.isFirstUserInput === 'function' && window.appChat.isFirstUserInput()) {
                        window.appChat.markFirstUserInput();
                        console.log(window.t('console.userFirstInputDetected'));
                    }

                    // 收到 transcription，清除 session 初始 5 秒计时器
                    if (S._voiceSessionInitialTimer) {
                        clearTimeout(S._voiceSessionInitialTimer);
                        S._voiceSessionInitialTimer = null;
                    }
                    // 真用户语音到达 → 等同于一次"用户输入"：清退避级别 +
                    // 复位语音模式无回复计数。否则连续被 preempt / 长时间没
                    // 回应都不会复位 _voiceProactiveNoResponseCount，10 轮后
                    // 主动搭话会被永久关闭，即使用户其实一直在讲话。
                    // 跨窗口通过 BroadcastChannel 广播，让 leader 同步。
                    if (typeof window.resetProactiveChatBackoff === 'function') {
                        window.resetProactiveChatBackoff();
                    }
                    var now = Date.now();
                    var shouldMerge = S.isRecording &&
                        S.lastVoiceUserMessage &&
                        S.lastVoiceUserMessage.isConnected &&
                        (now - S.lastVoiceUserMessageTime) < C.VOICE_TRANSCRIPT_MERGE_WINDOW;

                    if (shouldMerge) {
                        S.lastVoiceUserMessage.textContent += response.text;
                        S.lastVoiceUserMessageTime = now;
                    } else {
                        if (typeof window.appendMessage === 'function') {
                            window.appendMessage(response.text, 'user', true);
                        }
                        if (S.isRecording) {
                            var cc4 = chatContainer();
                            if (cc4) {
                                var userMessages = cc4.querySelectorAll('.message.user');
                                if (userMessages.length > 0) {
                                    S.lastVoiceUserMessage = userMessages[userMessages.length - 1];
                                    S.lastVoiceUserMessageTime = now;
                                }
                            }
                        }
                    }

                // -------- user_activity --------
                } else if (response.type === 'user_activity') {
                    var userActivityTurnId = resolveAssistantLifecycleTurnId();
                    if (shouldDelayUserActivityCancel(userActivityTurnId)) {
                        logAssistantLifecycle('user_activity:delay_cancel', {
                            turnId: userActivityTurnId,
                            interruptedSpeechId: response.interrupted_speech_id || null
                        });
                        scheduleUserActivityCancel(userActivityTurnId, response.interrupted_speech_id || null);
                    } else {
                        logAssistantLifecycle('user_activity:immediate_cancel', {
                            turnId: userActivityTurnId,
                            interruptedSpeechId: response.interrupted_speech_id || null
                        });
                        applyUserActivityCancel(response.interrupted_speech_id || null, 'user_activity');
                    }

                // -------- audio_chunk --------
                } else if (response.type === 'audio_chunk') {
                    if (window.DEBUG_AUDIO) {
                        console.log(window.t('console.audioChunkHeaderReceived'), response);
                    }
                    if (!S.assistantTurnId && S.assistantTurnAwaitingBubble) {
                        ensureAssistantTurnStarted(
                            'audio_chunk_header_fallback',
                            response.turn_id,
                            response.meta,
                            response.request_id
                        );
                    }
                    var speechId = response.speech_id;
                    var shouldSkip = false;
                    var playbackGain = Number(response.playback_gain);
                    if (!Number.isFinite(playbackGain)) playbackGain = 1;
                    playbackGain = Math.max(0, Math.min(2, playbackGain));

                    if (speechId && S.interruptedSpeechId && speechId === S.interruptedSpeechId) {
                        if (window.DEBUG_AUDIO) {
                            console.log(window.t('console.discardInterruptedAudio'), speechId);
                        }
                        shouldSkip = true;
                    } else if (speechId && speechId !== S.currentPlayingSpeechId) {
                        if (S.pendingDecoderReset) {
                            console.log(window.t('console.newConversationResetDecoder'), speechId);
                            S.decoderResetPromise = (async function () {
                                if (typeof window.resetOggOpusDecoder === 'function') {
                                    await window.resetOggOpusDecoder();
                                }
                                S.pendingDecoderReset = false;
                            })();
                        } else {
                            S.pendingDecoderReset = false;
                        }
                        S.currentPlayingSpeechId = speechId;
                        S.currentPlayingSpeechCorrelationId = String(
                            response.sdk_speech_correlation_id || ''
                        );
                        S.interruptedSpeechId = null;
                    } else if (speechId && response.sdk_speech_correlation_id) {
                        S.currentPlayingSpeechCorrelationId = String(
                            response.sdk_speech_correlation_id
                        );
                    }

                    S.pendingAudioChunkMetaQueue.push({
                        speechId: speechId || S.currentPlayingSpeechId || null,
                        turnId: resolveAssistantLifecycleTurnId(response.turn_id),
                        shouldSkip: shouldSkip,
                        playbackGain: playbackGain,
                        epoch: S.incomingAudioEpoch,
                        receivedAt: Date.now()
                    });
                    logAssistantLifecycle('ws:audio_chunk_header', {
                        speechId: speechId || S.currentPlayingSpeechId || null,
                        turnId: resolveAssistantLifecycleTurnId(response.turn_id),
                        shouldSkip: shouldSkip,
                        playbackGain: playbackGain,
                        epoch: S.incomingAudioEpoch
                    });
                    if (window.appAudioPlayback &&
                        typeof window.appAudioPlayback.schedulePendingAudioMetaStallCheck === 'function') {
                        window.appAudioPlayback.schedulePendingAudioMetaStallCheck();
                    }
                    // 记下 speech_id 属于哪一轮：随后的 audio_done 只带 speech_id，
                    // 音频通道上从来没有服务端权威的 turn_id。
                    if (!shouldSkip && window.appAudioPlayback &&
                        typeof window.appAudioPlayback.rememberAssistantAudioSpeechTurn === 'function') {
                        window.appAudioPlayback.rememberAssistantAudioSpeechTurn(
                            speechId || S.currentPlayingSpeechId || null,
                            resolveAssistantLifecycleTurnId(response.turn_id)
                        );
                    }
                    S.skipNextAudioBlob = false;

                // -------- audio_done（本 speech 的音频流已关闭，权威结束信号）--------
                // 后端 TTS worker / realtime provider 看得到"这一轮不会再有音频"，
                // 前端看不到（阵间空档和真结束同构）。收到即可放行收尾；漏发时
                // 由 app-audio-playback 的 give-up 计时器兜底。
                } else if (response.type === 'audio_done') {
                    logAssistantLifecycle('ws:audio_done', {
                        speechId: response.speech_id || null
                    });
                    if (window.appAudioPlayback &&
                        typeof window.appAudioPlayback.noteAssistantAudioStreamClosed === 'function') {
                        window.appAudioPlayback.noteAssistantAudioStreamClosed(response.speech_id);
                    }

                // -------- game_route_speech_cancel --------
                // Route teardown may happen after the backend has finished
                // producing audio while the browser still has decoded or
                // scheduled chunks. The correlation id is request-unique: a
                // delayed cancel cannot clear newer ordinary/game speech.
                } else if (response.type === 'game_route_speech_cancel') {
                    var cancelledCorrelationId = String(
                        response.sdk_speech_correlation_id || ''
                    );
                    if (
                        cancelledCorrelationId
                        && cancelledCorrelationId === S.currentPlayingSpeechCorrelationId
                    ) {
                        logAssistantLifecycle('ws:game_route_speech_cancel', {
                            speechId: S.currentPlayingSpeechId || null,
                            correlationId: cancelledCorrelationId
                        });
                        applyUserActivityCancel(
                            S.currentPlayingSpeechId || null,
                            'game_route_end'
                        );
                    }

                // -------- cozy_audio --------
                } else if (response.type === 'cozy_audio') {
                    console.log(window.t('console.newAudioHeaderReceived'));
                    var isNewMsg = response.isNewMessage || false;
                    if (isNewMsg) {
                        (async function () {
                            if (typeof window.clearAudioQueue === 'function') await window.clearAudioQueue();
                        })();
                    }
                    if (response.format === 'base64') {
                        if (typeof window.handleBase64Audio === 'function') {
                            window.handleBase64Audio(response.audioData, isNewMsg);
                        }
                    }

                // -------- screen_share_error --------
                } else if (response.type === 'screen_share_error') {
                    var translatedMsg = window.translateStatusMessage ? window.translateStatusMessage(response.message) : response.message;
                    if (typeof window.showStatusToast === 'function') window.showStatusToast(translatedMsg, 4000);

                    if (typeof window.stopScreening === 'function') window.stopScreening();

                    if (S.screenCaptureStream) {
                        S.screenCaptureStream.getTracks().forEach(function (track) { track.stop(); });
                        S.screenCaptureStream = null;
                    }

                    if (S.isRecording) {
                        var mb = micButton(); if (mb) mb.disabled = true;
                        var mu = muteButton(); if (mu) mu.disabled = false;
                        var sb = screenButton(); if (sb) sb.disabled = false;
                        var st = stopButton(); if (st) st.disabled = true;
                        var rs = resetSessionButton(); if (rs) rs.disabled = false;
                    } else if (S.isTextSessionActive) {
                        var ss = screenshotButton(); if (ss) ss.disabled = false;
                    }

                // -------- catgirl_switched --------
                } else if (response.type === 'catgirl_switched') {
                    var newCatgirl = response.new_catgirl;
                    var oldCatgirl = response.old_catgirl;
                    console.log(window.t('console.catgirlSwitchNotification'), oldCatgirl, window.t('console.catgirlSwitchTo'), newCatgirl);
                    console.log(window.t('console.currentFrontendCatgirl'), window.lanlan_config.lanlan_name);
                    if (typeof window.handleCatgirlSwitch === 'function') {
                        window.handleCatgirlSwitch(newCatgirl, oldCatgirl);
                    }

                // -------- focus_state (凝神 indicator) --------
                // Backend mirrors Focus enter/exit (LLMSessionManager
                // ._on_focus_transition). Re-dispatch as a CustomEvent the React
                // chat window listens for to toggle its subtle 思考微光 glow.
                // Inert by default — only emitted when FOCUS_MODE_ENABLED.
                } else if (response.type === 'focus_state') {
                    window.dispatchEvent(new CustomEvent('neko-focus-state', {
                        detail: { active: !!response.active },
                    }));

                // -------- focus_charge (凝神 edge-glow level) --------
                // Continuous Focus charge (0..1) + wall-clock stamp. The React
                // window scales its edge glow from this and extrapolates the
                // time decay locally between pushes for a smooth fade.
                } else if (response.type === 'focus_charge') {
                    window.dispatchEvent(new CustomEvent('neko-focus-charge', {
                        detail: { charge: Number(response.charge) || 0, atMs: Number(response.at_ms) || 0 },
                    }));

                // -------- focus_thinking (凝神 model-thinking pulse) --------
                // True while a Focus turn runs thinking-on but hasn't emitted
                // visible content yet; cleared once it speaks or the turn ends.
                // The React chat window shows a thinking-dots bubble at the tail
                // of the history while active. Inert unless Focus is engaged.
                } else if (response.type === 'focus_thinking') {
                    window.dispatchEvent(new CustomEvent('neko-focus-thinking', {
                        detail: { active: !!response.active },
                    }));

                // -------- topic_hint（深话题预告气泡，仅前端展示，不入上下文）--------
                } else if (response.type === 'topic_hint') {
                    if (typeof window.appendReactTopicHint === 'function') {
                        try {
                            window.appendReactTopicHint(response.author, response.turn_id);
                        } catch (topicHintErr) {
                            console.warn('[topic_hint] append failed', topicHintErr);
                        }
                    }

                // -------- cancel_topic_hint（开场白生成失败时撤回孤儿预告气泡）--------
                } else if (response.type === 'cancel_topic_hint') {
                    if (typeof window.removeReactTopicHint === 'function') {
                        try {
                            window.removeReactTopicHint(response.turn_id);
                        } catch (cancelHintErr) {
                            console.warn('[cancel_topic_hint] remove failed', cancelHintErr);
                        }
                    }

                // -------- status --------
                } else if (response.type === 'status') {
                    var statusCode = null;
                    var statusPayload = null;
                    var statusDetails = null;
                    try {
                        statusPayload = JSON.parse(response.message);
                        if (statusPayload && statusPayload.code) statusCode = statusPayload.code;
                        if (statusPayload && statusPayload.details && typeof statusPayload.details === 'object') {
                            statusDetails = statusPayload.details;
                        }
                    } catch (_) { }

                    var statusReasonCode = normalizeAsrReasonCode(
                        statusDetails && statusDetails.reason_code
                    );
                    var statusIncidentId = normalizeAsrIncidentId(
                        statusDetails && statusDetails.incident_id
                    );
                    var isAsrStatus = typeof statusCode === 'string'
                        && statusCode.indexOf('ASR_') === 0;
                    var isAsrControlStatus = statusCode === 'ASR_LIFECYCLE_STATE'
                        || statusCode === 'ASR_SPEAKER_EVIDENCE_UNAVAILABLE'
                        || statusCode === 'ASR_AUDIO_PREPROCESSING_FAILED'
                        || statusCode === 'ASR_DENY_CLEANUP_FAILED'
                        || (statusCode && statusCode.indexOf('ASR_INDEPENDENT_') === 0)
                        || (isAsrStatus && !!statusIncidentId);
                    if (isAsrControlStatus && !acceptAsrControlIdentity(statusDetails)) {
                        return;
                    }

                    if (statusCode === 'ASR_LIFECYCLE_STATE') {
                        var lifecycleState = (statusDetails && statusDetails.state) || '';
                        var lifecycleReasonCode = statusReasonCode;
                        var lifecycleIncidentId = statusIncidentId;
                        var lifecycleProvider = (statusDetails && statusDetails.provider) || '';
                        var allowedLifecycleStates = [
                            'off', 'local_listen', 'prewarming', 'active',
                            'draining', 'warm_idle', 'deep_sleep', 'backoff',
                            'blocked', 'suspended'
                        ];
                        if (allowedLifecycleStates.indexOf(lifecycleState) !== -1) {
                            S.voiceInputLifecycleState = lifecycleState;
                            document.documentElement.setAttribute(
                                'data-voice-input-state',
                                lifecycleState
                            );
                            window.dispatchEvent(new CustomEvent(
                                'voice-input-lifecycle-changed',
                                { detail: statusDetails }
                            ));
                            // Hard-failure cleanup. The runtime's
                            // _handle_independent_asr_error (main_logic/asr_client/
                            // runtime.py) is the only BLOCKED emitter, and it always
                            // broadcasts lifecycle BLOCKED before the fatal status
                            // code. That code set is open-ended and mostly NOT
                            // ASR_INDEPENDENT_-prefixed (ASR_ENDPOINTING_FAILED,
                            // ASR_BLOCKED_ENDPOINTING, ASR_AUDIO_ORDERING_FAILED,
                            // ASR_PROVIDER_FINAL_TIMEOUT, provider-specific codes...),
                            // so derive the independent-ASR failure teardown from
                            // BLOCKED instead of enumerating fatal codes. Start-path
                            // failures (ASR_INDEPENDENT_PROVIDER_UNAVAILABLE /
                            // ASR_INDEPENDENT_FAILED before READY) never emit
                            // BLOCKED and keep their per-code toasts below; a
                            // prefixed fatal code after BLOCKED re-shows the same
                            // fallback text, which the toast renders as one message.
                            if (lifecycleState === 'blocked') {
                                tearDownBlockedVoiceRoute();
                                var blockedMessage;
                                if (lifecycleReasonCode === 'ASR_DENY_CLEANUP_FAILED') {
                                    blockedMessage = window.t
                                        ? window.t('microphone.independentAsrCleanupFailed')
                                        : 'Voice session cleanup failed. Please restart the microphone.';
                                } else if (lifecycleReasonCode === 'ASR_INDEPENDENT_PROVIDER_UNAVAILABLE') {
                                    blockedMessage = window.t
                                        ? window.t('microphone.independentAsrProviderUnavailable', { providerKey: lifecycleProvider || 'unknown' })
                                        : ((lifecycleProvider || 'ASR') + ' is temporarily unavailable. Voice input has stopped for this session. It did not switch to another speech recognition service. Please start a new voice session later.');
                                } else if (lifecycleReasonCode === 'ASR_INDEPENDENT_FAILED') {
                                    blockedMessage = window.t
                                        ? window.t('microphone.independentAsrFallback')
                                        : 'Independent ASR unavailable. Voice input has stopped for this session. Check the independent ASR configuration, then start a new voice session.';
                                } else {
                                    blockedMessage = window.t
                                        ? window.t('microphone.independentAsrRuntimeFailed')
                                        : 'Independent ASR stopped because of a runtime error. Please restart the microphone.';
                                }
                                showAsrIncidentToast(
                                    lifecycleIncidentId,
                                    formatAsrFailureMessage(blockedMessage, lifecycleReasonCode),
                                    5000
                                );
                            }
                        }
                        return;
                    }

                    if (statusCode === 'VOICE_INPUT_LEASE_RESYNC_REQUIRED') {
                        // 仅采集中的窗口重发 lease 快照；非采集窗口忽略，避免多窗口互相覆盖
                        if (S.isRecording === true
                            && window.appAudioCapture
                            && typeof window.appAudioCapture.sendVoiceInputControlState === 'function') {
                            window.appAudioCapture.sendVoiceInputControlState(true);
                        }
                        return;
                    }

                    if (statusCode === 'ASR_AUDIO_PREPROCESSING_FAILED') {
                        // Same class of failure as BLOCKED and the terminal
                        // ASR_INDEPENDENT_* codes: the backend pins the route
                        // to blocked for the rest of the session. But this code
                        // rides neither channel — _fail_voice_input_pipeline
                        // never reaches _handle_independent_asr_error (the only
                        // BLOCKED emitter) and the code carries no
                        // ASR_INDEPENDENT_ prefix — so without this branch it
                        // is the one status that says "route dead" while the
                        // microphone keeps running.
                        tearDownBlockedVoiceRoute();
                        showAsrIncidentToast(
                            statusIncidentId,
                            formatAsrFailureMessage(
                                window.t ? window.t('microphone.audioPreprocessingFailed') : 'Microphone audio processing failed. Voice input has stopped for this session. Please start a new voice session.',
                                statusReasonCode
                            ),
                            5000
                        );
                        return;
                    }

                    if (statusCode === 'ASR_DENY_CLEANUP_FAILED') {
                        tearDownBlockedVoiceRoute();
                        showAsrIncidentToast(
                            statusIncidentId,
                            formatAsrFailureMessage(
                                window.t ? window.t('microphone.independentAsrCleanupFailed') : 'Voice session cleanup failed. Please restart the microphone.',
                                statusReasonCode
                            ),
                            5000
                        );
                        return;
                    }

                    if (statusCode && statusCode.indexOf('ASR_INDEPENDENT_') === 0) {
                        var asrProvider = (statusDetails && statusDetails.provider) || '';
                        S.independentAsrProvider = asrProvider;
                        if (statusCode === 'ASR_INDEPENDENT_READY') {
                            S.independentAsrActive = true;
                            S.voiceInputRouteBlocked = false;
                            if (S.gameRouteActive === true) {
                                setGameVoiceTranscriptionState({
                                    transcription_mode: 'independent_asr',
                                    provider: asrProvider,
                                    ready: true,
                                    reason: 'asr_ready'
                                });
                            }
                            if (typeof window.showStatusToast === 'function') {
                                window.showStatusToast(
                                    window.t ? window.t('microphone.independentAsrActive', { providerKey: asrProvider || 'unknown' }) : ('Independent ASR active: ' + asrProvider),
                                    3000
                                );
                            }
                            return;
                        }
                        if (statusCode === 'ASR_INDEPENDENT_DISABLED') {
                            removeExternalAsrPreview();
                            S.independentAsrActive = false;
                            // Healthy native route: nothing is fail-closed.
                            S.voiceInputRouteBlocked = false;
                            if (S.gameRouteActive === true) {
                                setGameVoiceTranscriptionState({
                                    transcription_mode: 'native_core',
                                    provider: asrProvider || S.coreApiProvider || '',
                                    ready: true,
                                    reason: 'native_ready'
                                });
                            }
                            return;
                        }
                        if (statusCode === 'ASR_INDEPENDENT_INJECTION_FAILED') {
                            return;
                        }
                        if (statusCode !== 'ASR_INDEPENDENT_PROVIDER_UNAVAILABLE'
                            && statusCode !== 'ASR_INDEPENDENT_FAILED'
                            && statusCode !== 'ASR_INDEPENDENT_STREAM_FAILED') {
                            console.warn('[App] ignored unknown independent ASR status:', statusCode);
                            return;
                        }
                        // Terminal startup failure. Same fail-closed state as a
                        // runtime BLOCKED, but no lifecycle event is ever emitted
                        // for it, so run the same teardown here. The per-code
                        // toasts below already say the right thing.
                        tearDownBlockedVoiceRoute();
                        if (statusCode === 'ASR_INDEPENDENT_PROVIDER_UNAVAILABLE') {
                            showAsrIncidentToast(
                                statusIncidentId,
                                formatAsrFailureMessage(
                                    window.t
                                        ? window.t('microphone.independentAsrProviderUnavailable', { providerKey: asrProvider || 'unknown' })
                                        : ((asrProvider || 'ASR') + ' is temporarily unavailable. Voice input has stopped for this session. It did not switch to another speech recognition service. Please start a new voice session later.'),
                                    statusReasonCode
                                ),
                                5000
                            );
                            return;
                        }
                        showAsrIncidentToast(
                            statusIncidentId,
                            formatAsrFailureMessage(
                                statusCode === 'ASR_INDEPENDENT_FAILED'
                                    ? (window.t ? window.t('microphone.independentAsrFallback') : 'Independent ASR unavailable. Voice input has stopped for this session. Check the independent ASR configuration, then start a new voice session.')
                                    : (window.t ? window.t('microphone.independentAsrRuntimeFailed') : 'Independent ASR stopped because of a runtime error. Please restart the microphone.'),
                                statusReasonCode
                            ),
                            5000
                        );
                        return;
                    }

                    // Evidence degradation leaves the ASR route and microphone
                    // active. Consume it before the terminal incident fallback,
                    // after the same session and revision fence as other ASR statuses.
                    if (statusCode === 'ASR_SPEAKER_EVIDENCE_UNAVAILABLE') {
                        showAsrIncidentToast(
                            statusIncidentId,
                            formatAsrFailureMessage(
                                window.t
                                    ? window.t('microphone.speakerEvidenceUnavailable')
                                    : 'Speaker verification is temporarily unavailable. Speech recognition continues under the existing policy.',
                                statusReasonCode
                            ),
                            5000
                        );
                        return;
                    }

                    // Runtime failures normally arrive after a BLOCKED lifecycle
                    // notification. Keep the terminal status independently useful
                    // when that earlier delivery is lost, without weakening the
                    // ASR identity fence: only a validated incident reaches here.
                    if (isAsrStatus && statusIncidentId) {
                        tearDownBlockedVoiceRoute();
                        showAsrIncidentToast(
                            statusIncidentId,
                            formatAsrFailureMessage(
                                window.t
                                    ? window.t('microphone.independentAsrRuntimeFailed')
                                    : 'Independent ASR stopped because of a runtime error. Please restart the microphone.',
                                statusReasonCode
                            ),
                            5000
                        );
                        return;
                    }

                    if (statusCode === 'TTS_CONNECTION_FAILED') {
                        emitAssistantLifecycleEvent('neko-assistant-speech-unavailable', {
                            turnId: resolveAssistantLifecycleTurnId(response.turn_id),
                            code: statusCode,
                            details: statusDetails || null,
                            source: 'tts_status'
                        });
                    }

                    if (statusCode === 'GAME_ROUTE_ENDED') {
                        var shouldResumeAudio = !!(statusDetails && statusDetails.should_resume_external_on_exit);
                        var realtimeRestore = statusDetails && statusDetails.realtime_restore;
                        var wasRecording = !!S.isRecording;
                        // Stale-event guard: a delayed GAME_ROUTE_ENDED for a previous
                        // session can arrive AFTER /route/start has finalized that one
                        // and activated a new session_id. Without this check the handler
                        // would unconditionally clear S.gameRoute* state and tear down
                        // the freshly-activated STT gate. We keep an empty current
                        // session_id permissive (legacy fallback) so events that
                        // genuinely lack a session_id still process.
                        var endedSessionId = (statusDetails && statusDetails.session_id) || '';
                        var currentSessionId = S.gameRouteSessionId || '';
                        var endedRouteInstanceId = (statusDetails && statusDetails.sdk_route_instance_id) || '';
                        var currentRouteInstanceId = S.gameRouteInstanceId || '';
                        if (endedSessionId && currentSessionId && endedSessionId !== currentSessionId) {
                            console.log(`[GameVoiceSTT] 忽略过期的 GAME_ROUTE_ENDED | ended_session=${endedSessionId} current_session=${currentSessionId}`);
                            // Ignoring the event for OUR state is right; forgetting the
                            // identity is not. GAME_ROUTE_ENDED is only ever emitted from
                            // route finalize, so the identity in this payload is provably
                            // a dead route -- and without a tombstone a late STT gate for
                            // it can strand S.gameRouteActive = true after the current
                            // route also ends, which suppresses proactive chat and
                            // auto-goodbye until a full open/close cycle or a reload.
                            // The payload's OWN identity only: `|| current...` would
                            // tombstone the live route.
                            rememberEndedGameRouteIdentity(
                                (statusDetails && statusDetails.game_type) || '',
                                endedSessionId,
                                endedRouteInstanceId
                            );
                            return;
                        }
                        if (
                            (endedRouteInstanceId || currentRouteInstanceId)
                            && endedRouteInstanceId !== currentRouteInstanceId
                        ) {
                            console.log(`[GameVoiceSTT] 忽略过期的 GAME_ROUTE_ENDED | ended_route=${endedRouteInstanceId} current_route=${currentRouteInstanceId}`);
                            if (endedSessionId) {
                                rememberEndedGameRouteIdentity(
                                    (statusDetails && statusDetails.game_type) || '',
                                    endedSessionId,
                                    endedRouteInstanceId
                                );
                            }
                            return;
                        }
                        advanceGameRouteStateRevision();
                        rememberEndedGameRouteIdentity(
                            (statusDetails && statusDetails.game_type) || S.gameRouteGameType || '',
                            endedSessionId || currentSessionId,
                            endedRouteInstanceId || currentRouteInstanceId
                        );
                        S.gameRouteActive = false;
                        S.gameRouteGameType = '';
                        S.gameRouteLanlanName = '';
                        S.gameRouteSessionId = '';
                        S.gameRouteInstanceId = '';
                        setGameVoiceTranscriptionState({
                            transcription_mode: 'unavailable',
                            provider: '',
                            ready: false,
                            reason: 'route_inactive'
                        });
                        console.log(`[GameVoiceSTT] 游戏语音路由已结束 | resume=${shouldResumeAudio} recording=${wasRecording} realtime_restore=${realtimeRestore && realtimeRestore.ok === false ? realtimeRestore.reason : 'ok'}`);
                        if (realtimeRestore && realtimeRestore.attempted && realtimeRestore.ok === false) {
                            console.warn('[GameVoiceSTT] 游戏退出后 Realtime 恢复未确认:', realtimeRestore.reason || 'unknown');
                        }
                        if (typeof window.stopGameVoiceSttGate === 'function') {
                            window.stopGameVoiceSttGate({ restoreOrdinaryMic: false });
                        } else {
                            S.gameVoiceSttGateActive = false;
                            S.gameVoiceSttGameType = '';
                            S.gameVoiceSttSessionId = '';
                        }
                        if (shouldResumeAudio && wasRecording && !S.isMicMuted
                            && S.voiceInputRouteBlocked !== true) {
                            var micPipelineAlive = !!(S.stream && S.audioContext && S.workletNode);
                            if (!micPipelineAlive && typeof window.startMicCapture === 'function') {
                                Promise.resolve(window.startMicCapture()).catch(function (error) {
                                    console.warn('[GameVoiceSTT] 游戏退出后恢复普通语音采集失败:', error);
                                });
                            }
                        }
                        if (S.proactiveChatWasStoppedByGameRoute && S.proactiveChatEnabled && typeof window.scheduleProactiveChat === 'function') {
                            window.scheduleProactiveChat();
                        }
                        S.proactiveChatWasStoppedByGameRoute = false;
                        return;
                    }

                    if (statusCode === 'GAME_VOICE_STT_GATE_ACTIVE') {
                        var incomingSttGameType = (statusDetails && statusDetails.game_type) || '';
                        var incomingSttSessionId = (statusDetails && statusDetails.session_id) || '';
                        var incomingSttRouteInstanceId = (
                            statusDetails && statusDetails.sdk_route_instance_id
                        ) || '';
                        var currentSttGameType = S.gameRouteGameType || '';
                        var currentSttSessionId = S.gameRouteSessionId || '';
                        var currentSttRouteInstanceId = S.gameRouteInstanceId || '';
                        var staleSttGate = (
                            S.gameRouteActive === true && (
                                (incomingSttGameType && currentSttGameType
                                    && incomingSttGameType !== currentSttGameType)
                                || (incomingSttSessionId && currentSttSessionId
                                    && incomingSttSessionId !== currentSttSessionId)
                                || ((incomingSttRouteInstanceId || currentSttRouteInstanceId)
                                    && incomingSttRouteInstanceId !== currentSttRouteInstanceId)
                            )
                        ) || (
                            S.gameRouteActive !== true
                            && isRecentlyEndedGameRouteIdentity(
                                incomingSttGameType,
                                incomingSttSessionId,
                                incomingSttRouteInstanceId
                            )
                        );
                        if (staleSttGate) {
                            console.warn('[GameVoiceSTT] 忽略迟到的语音门禁状态:', statusDetails);
                            return;
                        }
                        var sttProvider = (statusDetails && statusDetails.stt_provider) || 'browser';
                        var transcriptionMode = (statusDetails && statusDetails.transcription_mode) || (
                            sttProvider === 'realtime' ? 'backend_pending' : 'browser_fallback'
                        );
                        if (GAME_VOICE_TRANSCRIPTION_MODES.indexOf(transcriptionMode) === -1) {
                            transcriptionMode = 'unavailable';
                        }
                        var transcriptionProvider = (statusDetails && statusDetails.provider) || '';
                        var transcriptionReady = !!(statusDetails && statusDetails.ready === true);
                        advanceGameRouteStateRevision();
                        S.gameRouteActive = true;
                        S.gameRouteGameType = incomingSttGameType;
                        S.gameRouteLanlanName = (statusDetails && statusDetails.lanlan_name) || '';
                        S.gameRouteSessionId = incomingSttSessionId;
                        S.gameRouteInstanceId = incomingSttRouteInstanceId || S.gameRouteInstanceId || '';
                        S.gameVoiceSttGameType = incomingSttGameType;
                        S.gameVoiceSttSessionId = incomingSttSessionId;
                        // The route-resolution status may have arrived before
                        // this game-takeover edge. Preserve that authoritative
                        // verdict rather than regressing to backend_pending.
                        if (transcriptionMode === 'backend_pending') {
                            if (S.independentAsrActive === true) {
                                transcriptionMode = 'independent_asr';
                                transcriptionProvider = S.independentAsrProvider || transcriptionProvider;
                                transcriptionReady = true;
                            } else if (
                                S.independentAsrProvider
                                && S.voiceInputRouteBlocked !== true
                            ) {
                                transcriptionMode = 'native_core';
                                transcriptionProvider = S.independentAsrProvider || S.coreApiProvider || transcriptionProvider;
                                transcriptionReady = true;
                            }
                        }
                        setGameVoiceTranscriptionState({
                            transcription_mode: transcriptionMode,
                            provider: transcriptionProvider,
                            ready: transcriptionReady,
                            reason: transcriptionReady ? 'route_ready' : 'route_resolving'
                        });
                        console.log(`[GameVoiceSTT] 游戏语音接管已激活 | game=${S.gameVoiceSttGameType} mode=${transcriptionMode} provider=${transcriptionProvider || sttProvider} ready=${transcriptionReady} recording=${!!S.isRecording} muted=${!!S.isMicMuted}`);
                        if (S._voiceSessionInitialTimer) {
                            clearTimeout(S._voiceSessionInitialTimer);
                            S._voiceSessionInitialTimer = null;
                        }
                        if (typeof window.stopProactiveChatSchedule === 'function') {
                            S.proactiveChatWasStoppedByGameRoute = !!S.proactiveChatEnabled;
                            window.stopProactiveChatSchedule();
                        }
                        if (['backend_pending', 'native_core', 'independent_asr'].indexOf(transcriptionMode) !== -1) {
                            if (typeof window.stopGameVoiceSttGate === 'function') {
                                window.stopGameVoiceSttGate();
                            } else {
                                S.gameVoiceSttGateActive = false;
                            }
                            console.log(`[GameVoiceSTT] 使用宿主后台转写 | mode=${transcriptionMode} provider=${transcriptionProvider || 'resolving'}；继续发送普通麦克风音频，普通回复由后端丢弃`);
                            return;
                        }
                        S.gameVoiceSttGateActive = true;
                        if (typeof window.startGameVoiceSttGate === 'function') {
                            window.startGameVoiceSttGate();
                        } else {
                            console.warn('[GameVoiceSTT] startGameVoiceSttGate unavailable');
                        }
                        return;
                    }

                    if (statusCode === 'GAME_ROUTE_MEDIA_SKIPPED') {
                        return;
                    }

                    var isGoodbyeActive = (window.live2dManager && window.live2dManager._goodbyeClicked) || (window.vrmManager && window.vrmManager._goodbyeClicked) || (window.mmdManager && window.mmdManager._goodbyeClicked);
                    if (statusCode === 'CHARACTER_LEFT') {
                        window.dispatchEvent(new CustomEvent('neko:character-left', { detail: response }));
                    }
                    if ((S.isSwitchingMode || isGoodbyeActive || S._suppressCharacterLeft) && (statusCode === 'CHARACTER_LEFT' || response.message.includes('已离开'))) {
                        S._suppressCharacterLeft = false;
                        console.log(window.t('console.modeSwitchingIgnoreLeft'));
                        return;
                    }

                    var criticalErrorCodes = ['SESSION_START_CRITICAL', 'MEMORY_SERVER_CRASHED', 'API_KEY_REJECTED', 'API_RATE_LIMIT_SESSION', 'ERROR_1007_ARREARS', 'AGENT_QUOTA_EXCEEDED', 'RESPONSE_TIMEOUT', 'CONNECTION_TIMEOUT'];
                    var isCriticalError = statusCode && criticalErrorCodes.indexOf(statusCode) !== -1;
                    if (isCriticalError) {
                        console.log(window.t('console.seriousErrorHidePreparing'));
                        if (typeof window.hideVoicePreparingToast === 'function') window.hideVoicePreparingToast();
                    }

                    var translatedMessage = window.translateStatusMessage ? window.translateStatusMessage(response.message) : response.message;

                    // TTS 水印提示需要更长显示时间和更高优先级，避免被后续消息覆盖
                    var stickyInfoCodes = ['TTS_WATERMARK_DETECTED'];
                    var isStickyInfo = statusCode && stickyInfoCodes.indexOf(statusCode) !== -1;
                    var highPriorityInfoCodes = ['FREE_API_AUTO_CLOSE_VOICE'];
                    var isHighPriorityInfo = statusCode && highPriorityInfoCodes.indexOf(statusCode) !== -1;
                    if (isHighPriorityInfo) {
                        S._lastAutoCloseMicToastAt = Date.now();
                    }

                    if (typeof window.showStatusToast === 'function') window.showStatusToast(
                        translatedMessage,
                        isStickyInfo ? 8000 : (isHighPriorityInfo ? 7000 : 4000),
                        { important: isCriticalError, priority: isStickyInfo ? 50 : (isHighPriorityInfo ? 80 : undefined) }
                    );

                    if (statusCode === 'CHARACTER_DISCONNECTED') {
                        if (S.isRecording === false && !S.isTextSessionActive) {
                            if (typeof window.showStatusToast === 'function') {
                                window.showStatusToast(window.t ? window.t('app.catgirlResting', { name: window.lanlan_config.lanlan_name }) : (window.lanlan_config.lanlan_name + '正在打盹...'), 5000);
                            }
                        } else if (S.isTextSessionActive) {
                            if (typeof window.showStatusToast === 'function') {
                                window.showStatusToast(window.t ? window.t('app.textChatting') : '正在文本聊天中...', 5000);
                            }
                        } else {
                            // Recording mode: stop and auto-restart
                            if (typeof window.stopRecording === 'function') window.stopRecording();
                            if (typeof window.syncFloatingMicButtonState === 'function') window.syncFloatingMicButtonState(false);
                            if (typeof window.syncFloatingScreenButtonState === 'function') window.syncFloatingScreenButtonState(false);

                            if (S.socket && S.socket.readyState === WebSocket.OPEN) {
                                S.socket.send(JSON.stringify({ action: 'end_session' }));
                            }
                            if (typeof window.hideLive2d === 'function') window.hideLive2d();

                            var _mb = micButton(); if (_mb) _mb.disabled = true;
                            var _mu = muteButton(); if (_mu) _mu.disabled = true;
                            var _sb = screenButton(); if (_sb) _sb.disabled = true;
                            var _st = stopButton(); if (_st) _st.disabled = true;
                            var _rs = resetSessionButton(); if (_rs) _rs.disabled = true;
                            var _rt = returnSessionButton(); if (_rt) _rt.disabled = true;

                            // Snapshot the voice-start intent this restart is acting on --
                            // HERE, where the restart is decided, not inside the callback
                            // 7.5s later. A goodbye or avatar drop during the delay goes
                            // through cancelPendingSessionStart and bumps the epoch, and a
                            // snapshot taken afterwards would read that cancellation as its
                            // own starting point and restart anyway (codex P2). Nothing
                            // between here and the callback moves the epoch on its own: the
                            // three cancelPendingSessionStart callers are all user actions.
                            //
                            // Unlike the mic-button flow this path has no
                            // ensureVoiceStartCurrent, and ownership alone cannot see an
                            // ABA -- see voiceStartEpochIsCurrent.
                            var restartVoiceEpoch = S.voiceSessionStartEpoch;
                            // ...and the claim count with it. Until the callback claims, it
                            // has no owner token to compare against, and a whole text
                            // session can be started AND finished inside the 7.5s: by the
                            // time we look, its resolver is gone, and text never moves the
                            // epoch. Only the claim count still remembers it (codex P2).
                            var restartClaimSeq = window.sessionStartClaimSeq();

                            setTimeout(async function () {
                                var restartStartOwner = null;

                                // Every point where this flow resumes from an await asks the
                                // same question, and asking only part of it is how round
                                // after round of this bug survived: ownership cannot see a
                                // cancel-and-clear (the slot is back to empty), and the
                                // epoch cannot see a TEXT takeover (text starts never mint
                                // one). Returns true when this restart must stand down.
                                //
                                // Before the claim there is no owner token to compare
                                // against, so the snapshot taken at scheduling time stands
                                // in: it catches a text session that both started and
                                // finished during the delay, which leaves nothing else
                                // behind. After the claim the owner's own sequence carries
                                // the same fact.
                                function restartMustStandDown() {
                                    var owned = !!restartStartOwner;
                                    var takenOver = owned
                                        ? window.sessionStartSuperseded(restartStartOwner)
                                        : window.sessionStartsSince(restartClaimSeq);
                                    if (takenOver
                                            || (S._pendingSessionStartMode
                                                && S._pendingSessionStartMode !== 'audio')) {
                                        // Cancellation outranks the takeover: goodbye, avatar
                                        // drop and character switch are the later intent and
                                        // have already unwound and re-dressed the UI, so
                                        // unwinding again would re-enable the mic button and
                                        // unhide the composer on top of theirs (codex P2).
                                        // The claim sequence cannot see it -- a cancellation
                                        // clears the slot without claiming -- so it is asked
                                        // here, ahead of the unwind, rather than at the end.
                                        if (!window.voiceStartEpochIsCurrent(restartVoiceEpoch)) {
                                            return true;
                                        }
                                        // The unwind is global -- it bumps the mic
                                        // generation and clears window.isMicStarting -- so
                                        // running it while the newer AUDIO start (a mic
                                        // press inside the ack window) is still acquiring
                                        // media makes that start abandon capture and fail
                                        // its own ensureVoiceStartCurrent, leaving a
                                        // backend-accepted session with the mic closed
                                        // (greptile P1). That start is already driving this
                                        // UI; a text start is not, so there it still runs.
                                        var byAudio = owned
                                            ? window.supersededByAudioStart(restartStartOwner)
                                            : window.audioStartsSince(restartClaimSeq);
                                        if (!byAudio) {
                                            // Committed capture must be stopped BEFORE the
                                            // unwind: the unwind clears S.isRecording without
                                            // touching the stream, and the text
                                            // session_started teardown is gated on that very
                                            // flag, so the hardware microphone would stay
                                            // live (codex P1). notifyServer:false -- the
                                            // newer start owns the socket.
                                            if (S.isRecording === true
                                                    && typeof window.stopRecording === 'function') {
                                                window.stopRecording({ notifyServer: false });
                                            }
                                            if (typeof window.abortVoiceStartForBlockedRoute === 'function') {
                                                window.abortVoiceStartForBlockedRoute();
                                            }
                                        }
                                        return true;
                                    }
                                    // Quietly: the cancel lever has already unwound the UI,
                                    // and a newer mic start is driving it.
                                    return !window.voiceStartEpochIsCurrent(restartVoiceEpoch);
                                }

                                try {
                                    // BEFORE claiming anything. The first check used to sit
                                    // after claim + start_session + ack, so a goodbye, an
                                    // avatar drop or a mic press during the 7.5s delay was
                                    // answered by taking the slot from whoever it belonged
                                    // to, asking the backend for a session, and only then
                                    // walking away -- without ending it (codex P2).
                                    if (restartMustStandDown()) return;

                                    var sessionStartPromise = new Promise(function (resolve, reject) {
                                        // Owner token for every release in this
                                        // flow; see claimSessionStart in
                                        // app-state.js.
                                        restartStartOwner = window.claimSessionStart('audio', resolve, reject);
                                        // Re-arm the fail-closed latch on user
                                        // intent, strictly before start_session
                                        // goes out and therefore before any
                                        // route verdict for it can arrive.
                                        S.voiceInputRouteBlocked = false;
                                        if (window.sessionTimeoutId) {
                                            clearTimeout(window.sessionTimeoutId);
                                            window.sessionTimeoutId = null;
                                        }
                                    });
                                    // Consume the rejection up front. claimSessionStart settles the start it
                                    // displaces, and that can land while this flow is still inside
                                    // ensureWebSocketOpen -- before it reaches the await, and possibly before a
                                    // stand-down returns without ever awaiting at all. Without a handler on the
                                    // promise itself a routine takeover surfaces as an unhandledrejection and
                                    // the health diagnostics log it as a runtime error. `await` below still sees
                                    // the rejection: this attaches a handler, it does not swallow one.
                                    sessionStartPromise.catch(function () { });

                                    // The pre-claim check does not cover the reconnect below:
                                    // a text send or a mic press inside it takes the slot,
                                    // and sending anyway hands the backend a stale audio
                                    // start_session, overwrites the shared timeout handle
                                    // and leaves this flow awaiting a promise whose resolver
                                    // is no longer installed -- its own owner-gated timeout
                                    // returns early, so nothing settles it and no later
                                    // stand-down runs. Hence the check between the two lines
                                    // that follow (codex P2).
                                    await ensureWebSocketOpen();
                                    if (restartMustStandDown()) return;
                                    S.socket.send(JSON.stringify({
                                        action: 'start_session',
                                        input_type: 'audio',
                                        request_id: window.sessionStartRequestId(restartStartOwner)
                                    }));

                                    window.sessionTimeoutId = setTimeout(function () {
                                        // Only for the start this timer was armed for.
                                        // A displaced start is settled by claimSessionStart:
                                        // the flow that displaces us clears the shared
                                        // window.sessionTimeoutId in its own claim setup, so
                                        // this callback would not run to do it anyway.
                                        if (!window.sessionStartIsCurrent(restartStartOwner)) return;
                                        if (S.sessionStartedRejecter) {
                                            var rejecter = S.sessionStartedRejecter;
                                            window.releaseSessionStart(restartStartOwner);
                                            window.sessionTimeoutId = null;

                                            if (S.socket && S.socket.readyState === WebSocket.OPEN) {
                                                S.socket.send(JSON.stringify({ action: 'end_session' }));
                                                console.log(window.t('console.autoRestartTimeoutEndSession'));
                                            }
                                            var timeoutMsg = (window.t && window.t('app.sessionTimeout')) || '\u542F\u52A8\u8D85\u65F6\uFF0C\u670D\u52A1\u5668\u53EF\u80FD\u7E41\u5FD9\uFF0C\u8BF7\u7A0D\u540E\u624B\u52A8\u91CD\u8BD5';
                                            rejecter(new Error(timeoutMsg));
                                        }
                                    }, 15000);

                                    await sessionStartPromise;

                                    // Same takeover check as the mic-button flow
                                    // (app-buttons.js): on mobile the composer stays
                                    // visible during an audio session, so the user can
                                    // send text inside the ack's 500ms settle window.
                                    // The ack timer then leaves _pendingSessionStartMode
                                    // owned by that newer text start and settles this
                                    // promise anyway, and none of the guards below can
                                    // see it -- the text ack changes neither
                                    // voiceSessionStartEpoch nor isMicStarting, and never
                                    // sets voiceInputRouteBlocked. This automatic restart
                                    // would otherwise reclaim a lease onto the text
                                    // session's blocked route (Codex P2).
                                    //
                                    // Ownership first, mode second, for the same reason as
                                    // app-buttons.js: a newer AUDIO start (a mic press
                                    // inside the ack window) passes `mode !== 'audio'`, and
                                    // this restart would then open the microphone on top of
                                    // it. Neither test subsumes the other -- the disconnect
                                    // cleanup nulls the resolver but leaves the mode set --
                                    // and neither sees a cancel-and-clear, which is why
                                    // restartMustStandDown also asks the epoch.
                                    if (restartMustStandDown()) return;

                                    if (typeof window.showCurrentModel === 'function') await window.showCurrentModel();

                                    // The SAME full question again, not just the epoch: this
                                    // await is wide open, the disconnect path never disabled
                                    // the mobile composer, and a text send inside it claims
                                    // the slot without minting a voice epoch. An epoch-only
                                    // recheck passes and this stale restart then opens the
                                    // microphone on top of the text session and reports
                                    // "restart complete" (codex P2).
                                    if (restartMustStandDown()) return;

                                    if (S.voiceInputRouteBlocked === true) {
                                        // The rebuilt session came back fail-closed (independent
                                        // ASR was enabled and failed to start). Its status ALWAYS
                                        // precedes this ack — lifecycle.py runs
                                        // _start_independent_asr_if_enabled before
                                        // send_session_started — so this latch is THIS session's
                                        // own verdict, and startMicCapture would refuse silently.
                                        // Reporting "restart complete", lighting the floating mic
                                        // and leaving the button row disabled would claim a live
                                        // voice call with no microphone and no way back: the
                                        // restart disabled mute/screen/stop/reset/return above and
                                        // nothing here re-enables them. Fixed at the caller rather
                                        // than inside startMicCapture, because the toast and the
                                        // button row are outside it — moving the unwind there
                                        // would not fix this.
                                        if (typeof window.abortVoiceStartForBlockedRoute === 'function') {
                                            window.abortVoiceStartForBlockedRoute();
                                        }
                                        var _muB = muteButton(); if (_muB) _muB.disabled = true;
                                        var _sbB = screenButton(); if (_sbB) _sbB.disabled = true;
                                        var _stB = stopButton(); if (_stB) _stB.disabled = true;
                                        var _rsB = resetSessionButton(); if (_rsB) _rsB.disabled = false;
                                        var _rtB = returnSessionButton(); if (_rtB) _rtB.disabled = false;
                                        if (typeof window.syncFloatingMicButtonState === 'function') {
                                            window.syncFloatingMicButtonState(false);
                                        }
                                        // Let the ASR failure toast stand as the explanation.
                                        return;
                                    }
                                    var microphoneStarted = false;
                                    if (typeof window.startMicCapture === 'function') {
                                        microphoneStarted = await window.startMicCapture();
                                    }
                                    if (microphoneStarted !== true) {
                                        // startMicCapture uses false for a benign
                                        // ownership cancellation. The backend has
                                        // already accepted this restart, though, so
                                        // route the cancellation through the common
                                        // unwind below instead of reporting success
                                        // with no committed microphone pipeline.
                                        var microphoneStartCancelled = new Error('Microphone start cancelled');
                                        microphoneStartCancelled.microphoneStartCancelled = true;
                                        throw microphoneStartCancelled;
                                    }
                                    if (S.screenCaptureStream != null) {
                                        if (typeof window.startScreenSharing === 'function') await window.startScreenSharing();
                                    }

                                    // The capture awaits are the last wide-open window, and
                                    // the dual of the mic-button flow's post-getUserMedia
                                    // check: a text takeover invalidates an in-flight mic
                                    // start, but that cancellation path RETURNS rather than
                                    // throws, so without this the restart lights the
                                    // floating controls and reports "restart complete" over
                                    // the text session (codex P2).
                                    if (restartMustStandDown()) return;

                                    if (window.live2dManager && window.live2dManager._floatingButtons) {
                                        if (typeof window.syncFloatingMicButtonState === 'function') window.syncFloatingMicButtonState(true);
                                        if (S.screenCaptureStream != null) {
                                            if (typeof window.syncFloatingScreenButtonState === 'function') window.syncFloatingScreenButtonState(true);
                                        }
                                    }

                                    if (typeof window.showStatusToast === 'function') {
                                        window.showStatusToast(window.t ? window.t('app.restartComplete', { name: window.lanlan_config.lanlan_name }) : ('重启完成，' + window.lanlan_config.lanlan_name + '回来了！'), 4000);
                                    }
                                } catch (error) {
                                    var isMicrophoneStartCancelled = !!(
                                        error && error.microphoneStartCancelled
                                    );
                                    if (!isMicrophoneStartCancelled) {
                                        console.error(window.t('console.restartError'), error);
                                    }

                                    // Only tear down THIS restart's slot.
                                    if (window.sessionStartIsCurrent(restartStartOwner)) {
                                        if (window.sessionTimeoutId) {
                                            clearTimeout(window.sessionTimeoutId);
                                            window.sessionTimeoutId = null;
                                        }
                                        window.releaseSessionStart(restartStartOwner);
                                    }

                                    // A takeover during any await above -- including one
                                    // that caused this very error, and including
                                    // ensureWebSocketOpen rejecting before the claim -- means
                                    // everything below lands on somebody else: end_session
                                    // would kill their session, and the failure toast plus
                                    // the global recording/UI teardown would rewrite the
                                    // state they are driving (codex P2). Gating the slot
                                    // release alone left all of that unguarded.
                                    if (restartMustStandDown()) return;

                                    if (S.socket && S.socket.readyState === WebSocket.OPEN) {
                                        S.socket.send(JSON.stringify({ action: 'end_session' }));
                                        console.log(window.t('console.autoRestartFailedEndSession'));
                                    }

                                    if (typeof window.hideVoicePreparingToast === 'function') window.hideVoicePreparingToast();
                                    if (!isMicrophoneStartCancelled
                                            && typeof window.showStatusToast === 'function') {
                                        window.showStatusToast(window.t ? window.t('app.restartFailed', { error: error.message }) : ('重启失败: ' + error.message), 5000);
                                    }

                                    var mb2 = micButton();
                                    if (mb2) { mb2.classList.remove('recording'); mb2.classList.remove('active'); }
                                    var sb2 = screenButton();
                                    if (sb2) sb2.classList.remove('active');

                                    S.isRecording = false;
                                    S.voiceChatActive = false;
                                    S.voiceStartPending = false;
                                    window.isRecording = false;
                                    // 必须在 syncVoiceChatComposerHidden(false) 之前清掉，
                                    // 否则 shouldKeepVoiceComposerHidden() 还会按"启动中"判定要求隐藏，
                                    // 重启失败的输入栏会被新守卫再次压回去。
                                    window.isMicStarting = false;

                                    if (typeof window.syncFloatingMicButtonState === 'function') window.syncFloatingMicButtonState(false);
                                    if (typeof window.syncFloatingScreenButtonState === 'function') window.syncFloatingScreenButtonState(false);

                                    var mb3 = micButton(); if (mb3) mb3.disabled = false;
                                    var ts2 = textSendButton(); if (ts2) ts2.disabled = false;
                                    var ti2 = textInputBox(); if (ti2) ti2.disabled = false;
                                    var ss2 = screenshotButton(); if (ss2) ss2.disabled = false;
                                    var rs2 = resetSessionButton(); if (rs2) rs2.disabled = false;

                                    var mu2 = muteButton(); if (mu2) mu2.disabled = true;
                                    var sb3 = screenButton(); if (sb3) sb3.disabled = true;
                                    var st2 = stopButton(); if (st2) st2.disabled = true;

                                    var tia = document.getElementById('text-input-area');
                                    if (tia) tia.classList.remove('hidden');
                                    if (typeof window.syncVoiceChatComposerHidden === 'function') window.syncVoiceChatComposerHidden(false);
                                }
                            }, 7500);
                        }
                    }

                // -------- expression --------
                } else if (response.type === 'expression') {
                    var lanlan = window.LanLan1;
                    var registry = lanlan && lanlan.registered_expressions;
                    var fn = registry && registry[response.message];
                    if (typeof fn === 'function') {
                        fn();
                    } else {
                        console.warn(window.t('console.unknownExpressionCommand'), response.message);
                    }

                // -------- agent_status_update --------
                } else if (response.type === 'agent_status_update') {
                    var snapshot = response.snapshot || {};
                    var snapshotMeta = { sourceCharacter: response.lanlan_name || '' };
                    if (typeof window.isAgentStatusSnapshotCurrent === 'function'
                        && !window.isAgentStatusSnapshotCurrent(snapshotMeta)) {
                        return;
                    }
                    window._agentStatusSnapshot = snapshot;
                    var serverOnline = snapshot.server_online !== false;
                    var flags = snapshot.flags || {};
                    if (!('agent_enabled' in flags) && snapshot.analyzer_enabled !== undefined) {
                        flags.agent_enabled = !!snapshot.analyzer_enabled;
                    }
                    if (window.agentStateMachine && typeof window.agentStateMachine.updateCache === 'function') {
                        window.agentStateMachine.updateCache(serverOnline, flags);
                    }
                    if (typeof window.applyAgentStatusSnapshotToUI === 'function') {
                        window.applyAgentStatusSnapshotToUI(snapshot, snapshotMeta);
                    }
                    try {
                        var masterOn = !!flags.agent_enabled;
                        var anyChildOn = !!(flags.computer_use_enabled || flags.browser_use_enabled || flags.user_plugin_enabled || flags.openclaw_enabled);
                        if (masterOn && anyChildOn && typeof window.startAgentTaskPolling === 'function' && !isGoodbyeUiSuppressed()) {
                            window.startAgentTaskPolling();
                        }
                        var curName2 = (window.lanlan_config && window.lanlan_config.lanlan_name) || '';
                        var snapshotTasks = snapshot.active_tasks || [];
                        var filteredSnapshotTasks = curName2
                            ? snapshotTasks.filter(function (t) { return !t.lanlan_name || t.lanlan_name === curName2; })
                            : snapshotTasks;
                        if (!window._agentTaskMap) window._agentTaskMap = new Map();
                        var now2 = Date.now();
                        var LINGER_MS = 10000;
                        var newMap = new Map();
                        filteredSnapshotTasks.forEach(function (t) {
                            if (t && t.id) newMap.set(t.id, t);
                        });
                        window._agentTaskMap.forEach(function (t, id) {
                            if (!newMap.has(id)) {
                                if (curName2 && t.lanlan_name && t.lanlan_name !== curName2) return;
                                var isTerminal = t.status === 'completed' || t.status === 'failed' || t.status === 'cancelled';
                                if (isTerminal && t.terminal_at && (now2 - t.terminal_at < LINGER_MS)) {
                                    newMap.set(id, t);
                                }
                            }
                        });
                        window._agentTaskMap = newMap;
                        var tasks2 = Array.from(window._agentTaskMap.values());
                        if (tasks2.length > 0) {
                            if (window.AgentHUD && typeof window.AgentHUD.updateAgentTaskHUD === 'function') {
                                window.AgentHUD.updateAgentTaskHUD({
                                    success: true,
                                    tasks: tasks2,
                                    total_count: tasks2.length,
                                    running_count: tasks2.filter(function (t) { return t.status === 'running'; }).length,
                                    queued_count: tasks2.filter(function (t) { return t.status === 'queued'; }).length,
                                    completed_count: tasks2.filter(function (t) { return t.status === 'completed'; }).length,
                                    failed_count: tasks2.filter(function (t) { return t.status === 'failed'; }).length,
                                    timestamp: new Date().toISOString()
                                });
                            }
                        } else if (typeof window.checkAndToggleTaskHUD === 'function') {
                            window.checkAndToggleTaskHUD();
                        } else if (window.AgentHUD && typeof window.AgentHUD.hideAgentTaskHUD === 'function') {
                            window.AgentHUD.hideAgentTaskHUD();
                        }
                    } catch (_e) { /* ignore */ }

                // -------- avatar_interaction_ack --------
                } else if (response.type === 'avatar_interaction_ack') {
                    emitAssistantLifecycleEvent('neko-avatar-interaction-ack', {
                        interactionId: response.interaction_id || '',
                        accepted: response.accepted === true,
                        reason: response.reason || '',
                        turnId: response.turn_id || ''
                    });

                // -------- agent_notification --------
                } else if (response.type === 'agent_notification') {
                    var notifMsg = typeof response.text === 'string' ? response.text : '';
                    if (notifMsg) {
                        if (typeof window.setFloatingAgentStatus === 'function') window.setFloatingAgentStatus(notifMsg, response.status || 'completed');
                        if (typeof window.maybeShowContentFilterModal === 'function') window.maybeShowContentFilterModal(notifMsg);
                        if (response.error_message && typeof window.maybeShowContentFilterModal === 'function') {
                            window.maybeShowContentFilterModal(response.error_message);
                        }
                    }

                // -------- agent_task_update --------
                } else if (response.type === 'agent_task_update') {
                    try {
                        if (!window._agentTaskMap) window._agentTaskMap = new Map();
                        if (!window._agentTaskRemoveTimers) window._agentTaskRemoveTimers = new Map();
                        var task = response.task || {};
                        if (task.id) {
                            var existing = window._agentTaskMap.get(task.id);
                            var merged = existing ? Object.assign({}, existing, task) : task;
                            if (existing && existing.params && typeof task.params === 'undefined') {
                                merged.params = existing.params;
                            }
                            if (['completed', 'failed', 'cancelled'].indexOf(task.status) !== -1) {
                                if (!existing || ['completed', 'failed', 'cancelled'].indexOf(existing.status) === -1) {
                                    merged.terminal_at = Date.now();
                                }
                            }
                            window._agentTaskMap.set(task.id, merged);
                            if (['completed', 'failed', 'cancelled'].indexOf(task.status) !== -1) {
                                if (window._agentTaskRemoveTimers.has(task.id)) clearTimeout(window._agentTaskRemoveTimers.get(task.id));
                                window._agentTaskRemoveTimers.set(task.id, setTimeout(function () {
                                    var current = window._agentTaskMap.get(task.id);
                                    if (current && ['completed', 'failed', 'cancelled'].indexOf(current.status) !== -1) {
                                        window._agentTaskMap.delete(task.id);
                                    }
                                    window._agentTaskRemoveTimers.delete(task.id);
                                    var remaining = Array.from(window._agentTaskMap.values());
                                    if (window.AgentHUD && typeof window.AgentHUD.updateAgentTaskHUD === 'function') {
                                        window.AgentHUD.updateAgentTaskHUD({
                                            success: true, tasks: remaining,
                                            total_count: remaining.length,
                                            running_count: remaining.filter(function (t) { return t.status === 'running'; }).length,
                                            queued_count: remaining.filter(function (t) { return t.status === 'queued'; }).length,
                                            completed_count: remaining.filter(function (t) { return t.status === 'completed'; }).length,
                                            failed_count: remaining.filter(function (t) { return t.status === 'failed'; }).length,
                                            timestamp: new Date().toISOString()
                                        });
                                    }
                                }, 10000));
                            } else if (window._agentTaskRemoveTimers.has(task.id)) {
                                clearTimeout(window._agentTaskRemoveTimers.get(task.id));
                                window._agentTaskRemoveTimers.delete(task.id);
                            }
                        }
                        var tasks3 = Array.from(window._agentTaskMap.values());
                        var hasRunning2 = tasks3.some(function (t) { return t.status === 'running' || t.status === 'queued'; });
                        if (tasks3.length > 0 && window.AgentHUD) {
                            if (typeof window.AgentHUD.showAgentTaskHUD === 'function') {
                                window.AgentHUD.showAgentTaskHUD();
                            }
                            if (hasRunning2 && !window._agentTaskTimeUpdateInterval && !isGoodbyeUiSuppressed()) {
                                window._agentTaskTimeUpdateInterval = setInterval(function () {
                                    if (typeof window.updateTaskRunningTimes === 'function') window.updateTaskRunningTimes();
                                }, 1000);
                            }
                        }
                        if (window.AgentHUD && typeof window.AgentHUD.updateAgentTaskHUD === 'function') {
                            window.AgentHUD.updateAgentTaskHUD({
                                success: true,
                                tasks: tasks3,
                                total_count: tasks3.length,
                                running_count: tasks3.filter(function (t) { return t.status === 'running'; }).length,
                                queued_count: tasks3.filter(function (t) { return t.status === 'queued'; }).length,
                                completed_count: tasks3.filter(function (t) { return t.status === 'completed'; }).length,
                                failed_count: tasks3.filter(function (t) { return t.status === 'failed'; }).length,
                                timestamp: new Date().toISOString()
                            });
                        }
                        if (task && task.status === 'failed') {
                            var errMsg = task.error || task.reason || '';
                            if (errMsg) {
                                if (typeof window.maybeShowContentFilterModal === 'function') window.maybeShowContentFilterModal(errMsg);
                            }
                        }
                    } catch (e) {
                        console.warn('[App] 处理 agent_task_update 失败:', e);
                    }

                // -------- capture_bridge_region_request (interactive desktop selection) --------
                } else if (response.type === 'capture_bridge_region_request') {
                    (async function () {
                        var requestId = response.request_id || '';
                        var responseSocket = _thisSocket;
                        var sendRegionResp = function (payload) {
                            if (!responseSocket || responseSocket.readyState !== WebSocket.OPEN) return;
                            payload.action = 'capture_bridge_region_response';
                            payload.request_id = requestId;
                            responseSocket.send(JSON.stringify(payload));
                        };
                        try {
                            var dc = resolveDesktopCaptureProvider();
                            if (!dc || typeof dc.captureDesktopRegionAsDataUrl !== 'function') {
                                sendRegionResp({ success: false, error: 'unavailable' });
                                return;
                            }
                            var regionResult = await dc.captureDesktopRegionAsDataUrl({
                                selectionOnly: response.selection_only === true,
                                copyToClipboard: response.copy_to_clipboard !== false,
                                sessionTimeoutMs: response.session_timeout_ms,
                                allowPin: false,
                                returnDataUrl: true,
                                includeOriginalDataUrl: false,
                                translations: getCaptureBridgeCropTranslations()
                            });
                            if (!regionResult || regionResult.canceled || regionResult.cancelled) {
                                sendRegionResp({ success: false, canceled: true });
                                return;
                            }
                            if (regionResult.success === false) {
                                sendRegionResp({
                                    success: false,
                                    error: regionResult.error || regionResult.code || 'capture_failed'
                                });
                                return;
                            }
                            var regionDataUrl = typeof regionResult === 'string'
                                ? regionResult
                                : regionResult.dataUrl;
                            regionDataUrl = await boundCaptureBridgeRegionImage(regionDataUrl);
                            if (!regionDataUrl) {
                                sendRegionResp({ success: false, error: 'image_too_large' });
                                return;
                            }
                            sendRegionResp({ success: true, image: regionDataUrl });
                        } catch (regionError) {
                            var regionCode = regionError && (regionError.code || regionError.message);
                            sendRegionResp({ success: false, error: regionCode || 'internal_error' });
                        }
                    })();

                // -------- capture_bridge_request (galgame OCR window capture) --------
                } else if (response.type === 'capture_bridge_request') {
                    (async function () {
                        var requestId = response.request_id || '';
                        var responseSocket = _thisSocket;
                        var sendResp = function (payload) {
                            if (!responseSocket || responseSocket.readyState !== WebSocket.OPEN) return;
                            payload.action = 'capture_bridge_response';
                            payload.request_id = requestId;
                            responseSocket.send(JSON.stringify(payload));
                        };
                        var sourcePidMatches = function (source, pidValue) {
                            if (!source || !pidValue) return false;
                            var expected = String(pidValue);
                            var directPid = source.pid || source.processId || source.ownerPid;
                            if (directPid !== undefined && directPid !== null && String(directPid) === expected) {
                                return true;
                            }
                            return false;
                        };
                        var sourceIdMatchesTarget = function (source, targetValue) {
                            if (!source || !targetValue) return false;
                            var expected = String(targetValue);
                            var sourceId = String(source.id || '');
                            if (sourceId === expected) return true;
                            var tokens = sourceId.split(/[^0-9A-Za-z]+/);
                            for (var idx = 0; idx < tokens.length; idx++) {
                                if (tokens[idx] === expected) return true;
                            }
                            return false;
                        };
                        var normalizeCaptureBridgeImage = function (result) {
                            if (typeof result === 'string') return result || null;
                            if (!result || typeof result !== 'object') return null;
                            if (result.success === false) return null;
                            return (typeof result.dataUrl === 'string' && result.dataUrl) ? result.dataUrl : null;
                        };
                        try {
                            var dc = resolveDesktopCaptureProvider();
                            if (!dc || !dc.getSources) {
                                sendResp({ success: false, error: 'unavailable' });
                                return;
                            }
                            var targetId = typeof response.target_id === 'string'
                                ? response.target_id.trim() : '';
                            if (targetId === '0' || targetId === '<target_id>') {
                                targetId = '';
                            }
                            var pid = typeof response.pid === 'number' ? response.pid : 0;
                            var title = typeof response.title === 'string' ? response.title : '';
                            var pidStr = pid > 0 ? String(pid) : '';
                            var lowerTitle = title.toLowerCase();
                            var sources = [];
                            try {
                                sources = await dc.getSources({
                                    types: ['window'],
                                    thumbnailSize: { width: 80, height: 45 }
                                });
                            } catch (gsErr) {
                                sendResp({ success: false, error: 'get_sources_failed' });
                                return;
                            }
                            if (!sources || !sources.length) {
                                sendResp({ success: false, error: 'source_not_found' });
                                return;
                            }
                            // Match priority: target_id exact/source-token > exact pid/token > title substring.
                            // Never blindly pick the first window.
                            var matched = null;
                            if (targetId) {
                                for (var i = 0; i < sources.length; i++) {
                                    if (sourceIdMatchesTarget(sources[i], targetId)) {
                                        matched = sources[i];
                                        break;
                                    }
                                }
                            }
                            if (!matched && pidStr) {
                                for (var j = 0; j < sources.length; j++) {
                                    if (sourcePidMatches(sources[j], pidStr)) {
                                        matched = sources[j];
                                        break;
                                    }
                                }
                            }
                            if (!matched && lowerTitle) {
                                for (var k = 0; k < sources.length; k++) {
                                    var name = (sources[k].name || '').toLowerCase();
                                    if (name && name.indexOf(lowerTitle) !== -1) {
                                        matched = sources[k];
                                        break;
                                    }
                                }
                            }
                            if (!matched) {
                                sendResp({ success: false, error: 'source_not_found' });
                                return;
                            }
                            var dataUrl = null;
                            var captureResult = null;
                            if (typeof dc.captureSourceWithoutNeko === 'function') {
                                try {
                                    captureResult = await window.captureDesktopSourceWithTimeout(
                                        dc,
                                        'captureSourceWithoutNeko',
                                        matched.id
                                    );
                                    dataUrl = normalizeCaptureBridgeImage(captureResult);
                                } catch (_woNekoErr) {
                                    dataUrl = null;
                                }
                            }
                            if (!dataUrl && typeof dc.captureSourceAsDataUrl === 'function') {
                                try {
                                    captureResult = await window.captureDesktopSourceWithTimeout(
                                        dc,
                                        'captureSourceAsDataUrl',
                                        matched.id
                                    );
                                    dataUrl = normalizeCaptureBridgeImage(captureResult);
                                } catch (_dataUrlErr) {
                                    dataUrl = null;
                                }
                            }
                            if (!dataUrl) {
                                sendResp({ success: false, error: 'capture_failed' });
                                return;
                            }
                            sendResp({
                                success: true,
                                image: dataUrl,
                                source_id: matched.id || ''
                            });
                        } catch (capErr) {
                            try { sendResp({ success: false, error: 'internal_error' }); } catch (_) {}
                        }
                    })();

                // -------- request_screenshot (existing path, unrelated to capture bridge) --------
                } else if (response.type === 'request_screenshot') {
                    (async function () {
                        try {
                            var dataUrl = null;
                            // captureType 由截图函数在抓帧那一刻定好，这里不再二次推断：
                            // 抓帧是异步的，等到这一步 S 里的流 / 源可能已经换人。
                            var shotCaptureType = null;
                            var hasPairedCaptureType = false;
                            if (window.appProactive
                                && typeof window.appProactive.captureProactiveChatScreenshotWithSource === 'function') {
                                var shot = await window.appProactive.captureProactiveChatScreenshotWithSource();
                                dataUrl = shot && shot.dataUrl ? shot.dataUrl : null;
                                shotCaptureType = shot ? (shot.captureType || null) : null;
                                hasPairedCaptureType = true;
                            } else if (typeof window.captureProactiveChatScreenshot === 'function') {
                                dataUrl = await window.captureProactiveChatScreenshot();
                            }
                            if (dataUrl && S.socket && S.socket.readyState === WebSocket.OPEN) {
                                var respMsg = { action: 'screenshot_response', data: dataUrl };
                                // null = 窗口截图或无法确定 → 不叠加
                                var captureType = null;
                                if (hasPairedCaptureType) {
                                    captureType = shotCaptureType;
                                } else {
                                    // 旧契约兜底：拿不到带来源的截图函数时只能就地推断。
                                    if (typeof window.detectScreenshotCaptureType === 'function') {
                                        captureType = window.detectScreenshotCaptureType(
                                            S.screenCaptureStream, S.selectedScreenSourceId
                                        );
                                    }
                                    // 这一步必须是独立判断而不是 else：detect 存在但判不出
                                    // （无流无源的后端整屏兜底）时也要提升成 'screen'。写成
                                    // else if 会让它只在 detect 缺席时生效，后端把「不带坐标」
                                    // 当明确的否定信号，于是整屏图变成永久不叠。
                                    if (captureType === null
                                        && !S.screenCaptureStream && !S.selectedScreenSourceId) {
                                        captureType = 'screen';
                                    }
                                }
                                var avatarPos = typeof window.getAvatarScreenPosition === 'function'
                                    ? window.getAvatarScreenPosition(captureType) : null;
                                if (avatarPos) {
                                    respMsg.avatar_position = avatarPos;
                                }
                                S.socket.send(JSON.stringify(respMsg));
                            }
                        } catch (e2) {
                            console.warn('[App] request_screenshot capture failed:', e2);
                        }
                    })();

                // -------- system turn end (agent_callback — no proactive chat) --------
                } else if (response.type === 'system' && response.data === 'turn end agent_callback') {
                    if (S.suppressAssistantStreamUntilNextSession) {
                        console.log('[App] discard assistant turn_end after session ended by server');
                        clearPendingRollbackForRequest(response.request_id);
                        clearPendingAssistantTurnStart();
                        return;
                    }
                    clearPendingRollbackForRequest(response.request_id);
                    console.log('[WS] turn end (agent_callback) - skipping proactive chat schedule');
                    logAssistantLifecycle('ws:turn_end_agent_callback:received');
                    try {
                        flushRealisticBufferOnTurnEnd();
                    } catch (e3) {
                        console.warn('[WS] turn end agent_callback flush failed:', e3);
                    }
                    if (!S.assistantTurnId && S.assistantTurnAwaitingBubble) {
                        ensureAssistantTurnStarted(
                            'turn_end_agent_callback_fallback',
                            undefined,
                            response.meta,
                            response.request_id
                        );
                    }
                    var agentCallbackTurnId = resolveAssistantLifecycleTurnId();
                    if (agentCallbackTurnId) {
                        logAssistantLifecycle('ws:turn_end_agent_callback:emit', {
                            turnId: agentCallbackTurnId
                        });
                        emitAssistantLifecycleEvent('neko-assistant-turn-end', {
                            turnId: agentCallbackTurnId,
                            requestId: resolveAssistantRequestId(response.request_id, response.meta),
                            source: 'turn_end_agent_callback',
                            meta: response.meta
                        });
                    } else {
                        logAssistantLifecycle('ws:turn_end_agent_callback:clear_pending');
                    }
                    clearPendingAssistantTurnStart();

                    // 主动消息 / 热切换回调也产生了 AI 文本（来自 send_lanlan_response），
                    // 与正常 'turn end' 走同一套收尾（emotion + 字幕）。music 关闭——
                    // 主动消息不自动放歌；也不在此调 scheduleProactiveChat（见上方
                    // "skipping proactive chat schedule"），防 proactive 自触发。
                    finalizeAssistantTurn(agentCallbackTurnId, { enableMusic: false });

                // -------- system turn end --------
                } else if (response.type === 'system' && response.data === 'turn end') {
                    if (S.suppressAssistantStreamUntilNextSession) {
                        console.log('[App] discard assistant turn_end after session ended by server');
                        clearPendingRollbackForRequest(response.request_id);
                        clearPendingAssistantTurnStart();
                        return;
                    }
                    clearPendingRollbackForRequest(response.request_id);
                    console.log(window.t('console.turnEndReceived'));
                    logAssistantLifecycle('ws:turn_end:received');
                    // Flush remaining buffer
                    try {
                        flushRealisticBufferOnTurnEnd();
                    } catch (e3) {
                        console.warn(window.t('console.turnEndFlushFailed'), e3);
                    }
                    if (!S.assistantTurnId && S.assistantTurnAwaitingBubble) {
                        ensureAssistantTurnStarted(
                            'turn_end_fallback',
                            undefined,
                            response.meta,
                            response.request_id
                        );
                    }
                    var assistantTurnId = resolveAssistantLifecycleTurnId();
                    if (assistantTurnId) {
                        logAssistantLifecycle('ws:turn_end:emit', {
                            turnId: assistantTurnId
                        });
                        emitAssistantLifecycleEvent('neko-assistant-turn-end', {
                            turnId: assistantTurnId,
                            requestId: resolveAssistantRequestId(response.request_id, response.meta),
                            source: 'turn_end',
                            meta: response.meta
                        });
                    } else {
                        logAssistantLifecycle('ws:turn_end:clear_pending');
                    }
                    clearPendingAssistantTurnStart();

                    // Emotion analysis & subtitle on turn completion —— 与
                    // agent_callback 路径共用 finalizeAssistantTurn；正常轮启用 music。
                    //
                    // 破冰 mirror TTS 的 turn_end 只表示语音播报链路结束；破冰文案
                    // 已在 icebreaker runtime 用 subtitleBridge 精确 finalize。这里
                    // 若再走普通聊天 finalizeAssistantTurn，会用 Gemini buffer /
                    // 当前聊天气泡的旧文本二次翻译，覆盖破冰字幕。
                    if (!isNewUserIcebreakerMirrorTurnEnd(response)) {
                        finalizeAssistantTurn(assistantTurnId);
                    }

                    // AI turn_end 后只 reschedule，不 reset backoff。
                    // 理由：turn_end 无法区分"用户发话引发的 turn"和"proactive 自己引发的 turn"，
                    // 如果一律 reset 会让 proactive 自己的 turn 把退避清零 → 指数退避形同虚设。
                    // 用户真的说话时会由 sendTextPayload / 录音开关等路径单独 reset，
                    // 不依赖 turn_end。语音模式本来就不退避，只是"从 turn end 开始算下一个间隔"。
                    var hasChatMode = (typeof window.hasAnyChatModeEnabled === 'function') ? window.hasAnyChatModeEnabled() : false;
                    if (S.proactiveChatEnabled && hasChatMode) {
                        if (typeof window.scheduleProactiveChat === 'function') {
                            window.scheduleProactiveChat();
                        }
                    }

                // -------- session_preparing --------
                } else if (response.type === 'session_preparing') {
                    console.log(window.t('console.sessionPreparingReceived'), response.input_mode);
                    if (response.input_mode !== 'text') {
                        if (typeof window.isNekoGoodbyeModeActive === 'function'
                                && window.isNekoGoodbyeModeActive()) {
                            if (typeof window.hideVoicePreparingToast === 'function') window.hideVoicePreparingToast();
                            return;
                        }
                        var preparingMessage = window.t ? window.t('app.voiceSystemPreparing') : '语音系统准备中，请稍候...';
                        if (typeof window.showVoicePreparingToast === 'function') window.showVoicePreparingToast(preparingMessage);
                    }

                // -------- session_started --------
                } else if (response.type === 'session_started') {
                    if (response.input_mode !== 'text'
                            && typeof window.isNekoGoodbyeModeActive === 'function'
                            && window.isNekoGoodbyeModeActive()) {
                        console.log('[App] ignore stale audio session_started while goodbye is active');
                        if (typeof window.stopScreening === 'function') window.stopScreening();
                        if (typeof window.cancelPendingSessionStart === 'function') {
                            window.cancelPendingSessionStart('Voice start cancelled by goodbye');
                        } else {
                            S.voiceStartPending = false;
                            window.isMicStarting = false;
                            S.sessionStartedResolver = null;
                            S.sessionStartedRejecter = null;
                        }
                        S.isTextSessionActive = false;
                        S.voiceChatActive = false;
                        if (window.sessionTimeoutId) {
                            clearTimeout(window.sessionTimeoutId);
                            window.sessionTimeoutId = null;
                        }
                        if (typeof window.hideVoicePreparingToast === 'function') window.hideVoicePreparingToast();
                        if (S.socket && S.socket.readyState === WebSocket.OPEN) {
                            S._suppressCharacterLeft = true;
                            S.socket.send(JSON.stringify({ action: 'end_session' }));
                        }
                        return;
                    }
                    // 跨模式 ack 守卫：用户点的麦/文本启动正在 await session_started 时
                    // （resolver 还在 + _pendingSessionStartMode 记着请求模式），若到达的
                    // input_mode 与用户请求的不一致，这条 ack 属于并发的后台会话——典型是
                    // proactive / greeting 自起的 text 会话（它也是一次正常 start_session，
                    // 完成时会发 session_started(text)）。绝不能用它去 resolve 用户的 audio
                    // 启动 promise 或翻转 voiceChatActive/isTextSessionActive，否则用户点了
                    // 语音却被 text ack 收口 → 开麦但后端是 text 会话、UI 错配。直接忽略，
                    // 用户那次启动的真正 ack（后端跨模式撞车会等 in-flight 落定后改起本模式
                    // 会话再发，见 core.py start_session）随后到达时按下方正常流程收口。
                    // 注意要求 resolver 仍在：无 pending 启动时（如 chat.html 子窗口纯靠
                    // session_started 同步 hide 自己的输入框）不拦，维持多窗口原行为。
                    if (S._pendingSessionStartMode
                            && S.sessionStartedResolver
                            && response.input_mode !== S._pendingSessionStartMode) {
                        console.log('[App] ignore cross-mode session_started', response.input_mode,
                            'while pending', S._pendingSessionStartMode);
                        // 但麦克风必须先停。text session 把麦克风路由钉成 blocked，
                        // 而这条早退原本会让"用户在 A 点麦、B 同时发文本"这种多窗口
                        // 时序里，A 明明收到了 text session_started 却因为自己有
                        // audio 启动在途而直接 return，麦克风一直开着往一条已死的
                        // 路由上传。停麦是幂等的，且只在本窗口确实在录音时才做。
                        // Cancel an in-flight start first: S.isRecording is still
                        // false while getUserMedia()/addModule() are awaiting, so
                        // the stopRecording() below cannot reach that case and the
                        // pending start would complete and re-claim the revoked lease.
                        if (response.input_mode === 'text' && typeof window.invalidatePendingMicStart === 'function') window.invalidatePendingMicStart();
                        if (response.input_mode === 'text'
                            && S.isRecording === true
                            && typeof window.stopRecording === 'function') {
                            console.log('[App] text session installed; stopping the microphone (cross-mode)');
                            window.stopRecording({ notifyServer: false });
                        }
                        return;
                    }
                    // 请求标识守卫：这条 ack 是不是在回应**本窗口**那次启动。
                    //
                    // 模式守卫拦不住同模式的串台：抢麦的窗口会把 voice socket 换成
                    // 自己的，于是别人那次 audio start 的 ack 经 fan-out 落到本窗口。
                    // 本窗口据此清超时、resolve、按 ack 里的 blocked 路由直接
                    // abortVoiceStartForBlockedRoute()——而真正回应本窗口的那条 ack
                    // （带着重跑后的路由）到达时，麦克风流程早就放弃了。
                    //
                    // 只 gate「收口」（清超时 + resolve），不 gate 下面的 UI 同步：
                    // 后端确实起了一个会话，文本框显隐、停麦这些对本窗口照样成立。
                    // ack 不带标识时按「是我的」处理：后端内部路径（proactive /
                    // greeting / 断线自恢复）不经用户请求、没有标识，而它们撞上
                    // pending 启动的情形本就由上面的模式守卫负责。
                    //
                    // 主判据是 resolver 而不是标识本身：清 resolver 的地方有十来处，
                    // 指望每一处都记得连标识一起清是靠不住的，漏一处就会留下一个陈旧
                    // 标识，让本窗口从此把所有别人的 ack 都判成「不是我的」，连
                    // blocked latch 和准备中提示都不再处理（Codex P2）。没有 resolver
                    // 在等 = 本窗口没有启动在途 = 任何 ack 都按旧行为处理。
                    var _ackAnswersThisWindow = !S.sessionStartedResolver
                        || !S._pendingSessionStartRequestId
                        || !response.request_id
                        || response.request_id === S._pendingSessionStartRequestId;
                    if (!_ackAnswersThisWindow) {
                        console.log('[App] session_started answers another start',
                            response.request_id, 'pending', S._pendingSessionStartRequestId);
                    }
                    console.log(window.t('console.sessionStartedReceived'), response.input_mode);
                    S.suppressAssistantStreamUntilNextSession = false;
                    S.isTextSessionActive = response.input_mode === 'text';
                    S.voiceChatActive = response.input_mode !== 'text';
                    if (_ackAnswersThisWindow) S.voiceStartPending = false;
                    // NOTE: the fail-closed latch is deliberately NOT cleared
                    // here. lifecycle.py runs _start_independent_asr_if_enabled
                    // BEFORE send_session_started, so this ack always arrives
                    // AFTER the current session's route verdict -- clearing
                    // here would wipe a latch that verdict just set. It is
                    // re-armed on user intent instead, next to
                    // _pendingSessionStartMode = 'audio'.
                    //
                    // The ack now carries the SETTLED route, which covers the
                    // case the status-driven latch structurally cannot: a
                    // window that never received the ASR_INDEPENDENT_* verdict
                    // at all -- either because it went to a different socket,
                    // or because a competing lease claim fenced the failing
                    // start so no status was ever emitted. Without this, such a
                    // window opens the microphone onto a route that discards
                    // every frame, with no status and no recovery path.
                    //
                    // SET-ONLY, never cleared, on purpose: the latch is
                    // deliberately sticky (see tearDownBlockedVoiceRoute --
                    // BLOCKED is never re-sent, so the game-exit resume path
                    // relies on it surviving), and clearing it from an ack
                    // would undo that. Guarded on the field being present so an
                    // older backend keeps exactly today's behaviour.
                    // Gated on the request guard as well (CodeRabbit): the latch
                    // is set-only, so a blocked verdict belonging to ANOTHER
                    // window's start would stick, and this window's own healthy
                    // ack could not clear it -- the microphone would refuse to
                    // open even though the route re-decision succeeded. A window
                    // with no start pending still latches: that is the case with
                    // no other channel to learn the verdict from.
                    if (_ackAnswersThisWindow
                            && response.input_mode !== 'text'
                            && response.microphone_route === 'blocked') {
                        S.voiceInputRouteBlocked = true;
                    }

                    // 文本 session 装好后麦克风必须停：mic lease 只由前端持有，
                    // 后端任何 session 生命周期路径都不会重置它，而文本 session
                    // 把麦克风路由钉死在 blocked（asr_runtime.py
                    // _start_independent_asr_if_enabled 对非 audio 的 input_mode
                    // 直接 return）。留着录音的话每一帧 PCM 都会被 ingress 接收、
                    // 跑完整条降噪/VAD 流水线，然后在路由处静默丢弃——没有状态、
                    // 没有恢复路径，用户必须手动关开麦克风。用户刚刚显式选择了打字，
                    // 以最近一次显式动作为准停掉录音，比在 ingress 里反向重建
                    // audio session 更安全（不会和 start_session 撕重建打架）。
                    // Same window as the cross-mode branch above: a start still
                    // inside getUserMedia()/addModule() has not set S.isRecording
                    // yet, so only this reaches it. One line on purpose -- the
                    // multi-line `if (response.input_mode === 'text'` opener is
                    // the anchor test_text_session_start_stops_an_active_microphone
                    // slices on, and must stay unique to the teardown guard below.
                    if (response.input_mode === 'text' && typeof window.invalidatePendingMicStart === 'function') window.invalidatePendingMicStart();
                    if (response.input_mode === 'text'
                        && S.isRecording === true
                        && typeof window.stopRecording === 'function') {
                        console.log('[App] text session installed; stopping the microphone');
                        // notifyServer:false is load-bearing. The default path
                        // sends pause_session, which websocket_router.py maps to
                        // an UNGATED end_session() — and the session it would end
                        // is the text session the backend acknowledged one line
                        // above. app-buttons.js is still awaiting this very ack
                        // (the resolve fires 500 ms later, below) and only then
                        // sends the queued user text, so the teardown wins the
                        // race: CHARACTER_LEFT is pushed and the message either
                        // rebuilds a whole new session or lands mid-teardown.
                        // Nothing is given up by suppressing it — active_session_
                        // is_idle, the only other thing pause_session sets, has no
                        // reader in production. Capture teardown does not need the
                        // server either: stopRecording stops the tracks, closes the
                        // AudioContext and revokes the mic lease (refreshMicLease
                        // still emits lease_sync owner:"none"/engaged:false, which
                        // is how the backend learns the audio route is released)
                        // regardless of this flag; it only gates the pause_session
                        // send. stopMicCapture is NOT usable here — it rejects the
                        // in-flight text-start promise with 'Session aborted' and
                        // still sends pause_session, losing the message outright.
                        window.stopRecording({ notifyServer: false });
                    }

                    // Multi-window 文本框对偶 hide：每个 webview（index.html 主窗口、
                    // chat.html 子窗口）都通过自己的 ws 收到 session_started，借此
                    // 各自 hide 自己的 #text-input-area，不依赖
                    // startMicCapture/syncVoiceChatComposerHidden 的 BroadcastChannel
                    // 链路。原来 hide 只挂在主窗口 startMicCapture 上：
                    //   - chat.html 子窗口无麦按钮永不调 startMicCapture
                    //   - reload 后某些 audio session 启动路径不走 startMicCapture
                    //   - BroadcastChannel 在 reload init 时序窗口里错过事件
                    // 都会让子窗口的 #text-input-area 始终可见可输入，用户在
                    // audio session 中打字 → 后端 start_session(text) → 撕重建
                    // → 撞 PR #1176 修的 race（"neko 已离开"）。本路径与下方
                    // session_ended_by_server 1844-1846 的 unhide 对偶，移动端
                    // 维持原来"不 hide"设计（UI 上手机屏小希望保留文本框可见）。
                    var _tiaStarted = document.getElementById('text-input-area');
                    if (_tiaStarted) {
                        if (response.input_mode === 'text') {
                            _tiaStarted.classList.remove('hidden');
                        } else if (!window.appUtils || !window.appUtils.isMobile()) {
                            _tiaStarted.classList.add('hidden');
                        }
                    }
                    if (typeof window.syncVoiceChatComposerHidden === 'function') {
                        var _shouldHide = response.input_mode !== 'text'
                            && (!window.appUtils || !window.appUtils.isMobile());
                        window.syncVoiceChatComposerHidden(_shouldHide);
                    }

                    // 立即清掉启动超时：匹配的 ack 已到（已过上方 mode 守卫），若拖到下面
                    // 500ms 后才清，贴近 15s deadline 的 ack（如 14.8s，尤其跨模式等待+重启
                    // 链路）会被先一步触发的超时误 reject + end_session，把后端已接受的会话
                    // 打断（Codex P2）。resolve 仍延后做（留时间收尾 UI），但超时此刻就拆。
                    if (_ackAnswersThisWindow && S.sessionStartedResolver && window.sessionTimeoutId) {
                        clearTimeout(window.sessionTimeoutId);
                        window.sessionTimeoutId = null;
                    }

                    // Capture the pending start THIS ack belongs to. The resolve
                    // is deferred 500ms (to let the UI settle) but the slot is
                    // shared, and on mobile the composer stays visible during an
                    // audio session -- see _shouldHide above -- so the user can
                    // send text inside that window. app-buttons.js then installs
                    // a NEW resolver + mode for the text start, and this old
                    // audio timer would resolve that promise, clear its timeout
                    // and let the queued message go out before the backend has
                    // acknowledged the text session at all (Codex P2). Resolve
                    // only if the slot still holds the very start we acked.
                    var _ackedResolver = _ackAnswersThisWindow ? S.sessionStartedResolver : null;
                    setTimeout(function () {
                        // Not gated on the resolver: a window with no pending
                        // start (chat.html) still has to drop the banner. Gated
                        // on the request guard, though -- a window still waiting
                        // for ITS ack must keep showing "preparing".
                        if (_ackAnswersThisWindow && typeof window.hideVoicePreparingToast === 'function') window.hideVoicePreparingToast();
                        if (!_ackedResolver) return;
                        if (S.sessionStartedResolver === _ackedResolver) {
                            // Still ours: release the shared slot and its timer.
                            if (window.sessionTimeoutId) {
                                clearTimeout(window.sessionTimeoutId);
                                window.sessionTimeoutId = null;
                            }
                            S.sessionStartedResolver = null;
                            S.sessionStartedRejecter = null;
                            S._pendingSessionStartMode = null;
                            S._pendingSessionStartRequestId = null;
                        }
                        // Settle OUR promise either way, INCLUDING when the slot
                        // has moved on (Codex P2). Its timeout was already
                        // cleared when this ack arrived, so nothing else will
                        // ever settle it -- the identity guard alone left the
                        // mic-button handler suspended at `await
                        // sessionStartPromise` forever, with isMicStarting true
                        // and the button stuck active/disabled after the text
                        // session succeeded.
                        //
                        // Resolve rather than reject: the backend really did
                        // acknowledge this audio session, so resolving is the
                        // truthful outcome, and the downstream guards
                        // (ensureVoiceStartCurrent, then the
                        // S.voiceInputRouteBlocked check before startMicCapture)
                        // are what decide whether the mic should still open.
                        // Rejecting would instead run the handler's catch, which
                        // clears S.sessionStartedResolver / Rejecter /
                        // _pendingSessionStartMode unconditionally and would
                        // therefore tear down the NEWER start's slot -- the very
                        // cross-start damage this guard exists to stop.
                        _ackedResolver(response.input_mode);
                    }, 500);

                    // 语音模式：session 开始 5 秒内无 transcription，启动 proactive chat 计时器
                    if (response.input_mode !== 'text' && S.proactiveChatEnabled && !S.gameRouteActive) {
                        if (S._voiceSessionInitialTimer) {
                            clearTimeout(S._voiceSessionInitialTimer);
                        }
                        S._voiceSessionInitialTimer = setTimeout(function () {
                            S._voiceSessionInitialTimer = null;
                            if (S.isRecording && S.proactiveChatEnabled) {
                                console.log('[ProactiveChat] Session 开始 5 秒无 transcription，启动计时器');
                                if (typeof window.scheduleProactiveChat === 'function') window.scheduleProactiveChat();
                            }
                        }, 5000);
                    }

                // -------- session_failed --------
                } else if (response.type === 'session_failed') {
                    console.log(window.t('console.sessionFailedReceived'), response.input_mode);
                    // 跨模式 fail 守卫（与上方 session_started 守卫对偶）：用户的启动正在
                    // await 时，并发的后台会话（如 proactive 自起的 text）若启动失败会发
                    // session_failed(text)。它不该 reject 用户那次 audio 启动——后端跨模式
                    // 撞车会等 in-flight 落定后改起 audio（见 core.py start_session），用户的
                    // 真正 ack 随后到达。模式不一致就忽略这条 fail。session_failed 一定带
                    // input_mode（见后端 send_session_failed），故 mismatch 判定可靠。
                    if (S._pendingSessionStartMode
                            && S.sessionStartedRejecter
                            && response.input_mode
                            && response.input_mode !== S._pendingSessionStartMode) {
                        console.log('[App] ignore cross-mode session_failed', response.input_mode,
                            'while pending', S._pendingSessionStartMode);
                        return;
                    }
                    if (typeof window.hideVoicePreparingToast === 'function') window.hideVoicePreparingToast();
                    S.voiceChatActive = false;
                    S.voiceStartPending = false;
                    if (window.sessionTimeoutId) {
                        clearTimeout(window.sessionTimeoutId);
                        window.sessionTimeoutId = null;
                    }
                    if (S.sessionStartedRejecter) {
                        S.sessionStartedRejecter(new Error(response.message || (window.t ? window.t('app.sessionFailed') : 'Session启动失败')));
                    } else {
                        // Fallback: reset UI when Promise already consumed
                        var _mb2 = micButton();
                        if (_mb2) { _mb2.classList.remove('active'); _mb2.classList.remove('recording'); _mb2.disabled = false; }
                        var _mu2 = muteButton(); if (_mu2) _mu2.disabled = true;
                        var _sb2 = screenButton(); if (_sb2) _sb2.disabled = true;
                        var _st2 = stopButton(); if (_st2) _st2.disabled = true;
                        var _rs2 = resetSessionButton(); if (_rs2) _rs2.disabled = false;
                        if (typeof window.syncFloatingMicButtonState === 'function') window.syncFloatingMicButtonState(false);
                        if (typeof window.syncFloatingScreenButtonState === 'function') window.syncFloatingScreenButtonState(false);
                        window.isMicStarting = false;
                        S.voiceChatActive = false;
                        S.isSwitchingMode = false;
                        var _tia = document.getElementById('text-input-area');
                        if (_tia) _tia.classList.remove('hidden');
                        if (typeof window.syncVoiceChatComposerHidden === 'function') window.syncVoiceChatComposerHidden(false);
                    }
                    S.sessionStartedResolver = null;
                    S.sessionStartedRejecter = null;
                    S._pendingSessionStartMode = null;
                    S._pendingSessionStartRequestId = null;

                // -------- session_ended_by_server --------
                } else if (response.type === 'session_ended_by_server') {
                    console.log('[App] Session ended by server, input_mode:', response.input_mode);
                    window.dispatchEvent(new CustomEvent('neko:session-ended-by-server', { detail: response }));
                    removeExternalAsrPreview();
                    // The server ended the session, so the independent-ASR route is
                    // gone with it; reset the flags even when S.isRecording is already
                    // false (paused mic) and the stopRecording() below won't run.
                    S.independentAsrActive = false;
                    S.independentAsrProvider = '';
                    S.isTextSessionActive = false;
                    S.voiceChatActive = false;
                    S.voiceStartPending = false;
                    if (typeof window.stopScreening === 'function') window.stopScreening();
                    stopAssistantTextOutputOnSessionEnd('session_ended_by_server');
                    clearAssistantLifecycleOnDisconnect('session_ended_by_server');

                    if (S.sessionStartedRejecter) {
                        try { S.sessionStartedRejecter(new Error('Session ended by server')); } catch (_e2) { }
                    }
                    S.sessionStartedResolver = null;
                    S.sessionStartedRejecter = null;
                    S._pendingSessionStartMode = null;
                    S._pendingSessionStartRequestId = null;

                    if (window.sessionTimeoutId) {
                        clearTimeout(window.sessionTimeoutId);
                        window.sessionTimeoutId = null;
                    }

                    if (S.isRecording) {
                        // notifyServer:false — the server ALREADY ended this
                        // session, so pause_session is pure noise; worse, sent
                        // from a superseded recorder socket it is not a
                        // voice-path message, so the router treats it as a
                        // character switch, closes the socket, and the 3 s
                        // auto-reconnect re-steals the session identity from
                        // the window that legitimately owns it.
                        if (typeof window.stopRecording === 'function') window.stopRecording({ notifyServer: false });
                    }

                    (async function () {
                        if (typeof window.clearAudioQueue === 'function') await window.clearAudioQueue();
                    })();

                    if (typeof window.hideVoicePreparingToast === 'function') window.hideVoicePreparingToast();

                    // Restore UI to idle state
                    var _mb3 = micButton();
                    if (_mb3) { _mb3.classList.remove('active'); _mb3.classList.remove('recording'); _mb3.disabled = false; }
                    var _sb3 = screenButton(); if (_sb3) _sb3.classList.remove('active');
                    var _ts = textSendButton(); if (_ts) _ts.disabled = false;
                    var _ti = textInputBox(); if (_ti) _ti.disabled = false;
                    var _ss = screenshotButton(); if (_ss) _ss.disabled = false;
                    var _mu3 = muteButton(); if (_mu3) _mu3.disabled = true;
                    var _sb4 = screenButton(); if (_sb4) _sb4.disabled = true;
                    var _st3 = stopButton(); if (_st3) _st3.disabled = true;
                    var _rs3 = resetSessionButton(); if (_rs3) _rs3.disabled = true;
                    var _rt2 = returnSessionButton(); if (_rt2) _rt2.disabled = true;

                    var _tia2 = document.getElementById('text-input-area');
                    if (_tia2) _tia2.classList.remove('hidden');
                    window.isMicStarting = false;
                    if (typeof window.syncVoiceChatComposerHidden === 'function') window.syncVoiceChatComposerHidden(false);

                    if (typeof window.syncFloatingMicButtonState === 'function') window.syncFloatingMicButtonState(false);
                    if (typeof window.syncFloatingScreenButtonState === 'function') window.syncFloatingScreenButtonState(false);

                    S.isSwitchingMode = false;

                // -------- reload_page --------
                } else if (response.type === 'reload_page') {
                    console.log(window.t('console.reloadPageReceived'), response.message);
                    var reloadMsg = window.translateStatusMessage ? window.translateStatusMessage(response.message) : response.message;
                    if (typeof window.showStatusToast === 'function') {
                        window.showStatusToast(reloadMsg || (window.t ? window.t('app.configUpdated') : '配置已更新，页面即将刷新'), 3000);
                    }
                    // 后端在发 reload_page 之前已经 end_session，前端 2.5s 后才真
                    // reload。这 2.5s 内 isTextSessionActive 若残留 true，用户敲
                    // 文字会绕过 start_session action 直接送 stream_data，错过
                    // websocket_router 的 reset_session_start_circuit 守卫，触发
                    // 后端"未指定 ↔ 免费 音色切换后概率连接失败"那条路径。
                    S.isTextSessionActive = false;
                    setTimeout(function () {
                        console.log(window.t('console.reloadPageStarting'));
                        if (window.closeAllSettingsWindows) window.closeAllSettingsWindows();
                        window.location.reload();
                    }, 2500);

                // -------- auto_close_mic --------
                } else if (response.type === 'auto_close_mic') {
                    console.log(window.t('console.autoCloseMicReceived'));
                    S.voiceStartPending = false;
                    window.isMicStarting = false;
                    showAutoCloseMicToast(response);

                    Promise.resolve(resetVoiceUiAfterAutoClose({ keepSwitchingMode: true })).then(function () {
                        showAutoCloseMicToast(response);
                    }, function (error) {
                        console.warn('[App] auto_close_mic cleanup failed:', error);
                        showAutoCloseMicToast(response);
                    });

                // -------- music action --------
                } else if (response.action === 'music') {
                    var searchTerm = response.search_term;
                    if (searchTerm) {
                        console.log('[Music] Received music action with search term: ' + searchTerm);
                        if (typeof window.showStatusToast === 'function') {
                            var searchMsg = window.t('music.searching', { query: searchTerm, defaultValue: '正在为您搜索: ' + searchTerm });
                            window.showStatusToast(searchMsg, 2000);
                        }

                        window._currentMusicSearchEpoch = (window._currentMusicSearchEpoch || 0) + 1;
                        var myEpoch = window._currentMusicSearchEpoch;

                        fetch('/api/music/search?query=' + encodeURIComponent(searchTerm))
                            .then(function (res) { return res.json(); })
                            .then(function (result) {
                                if (typeof myEpoch !== 'undefined' && typeof window._currentMusicSearchEpoch !== 'undefined') {
                                    if (myEpoch !== window._currentMusicSearchEpoch) {
                                        console.log('[Music] 丢弃过期的搜索结果: ' + searchTerm);
                                        return;
                                    }
                                }
                                if (result.netease_cookie_invalid && typeof window.showStatusToast === 'function') {
                                    var now2 = Date.now();
                                    if (!window._cookieWarnLastTime || now2 - window._cookieWarnLastTime > 300000) {
                                        var musiccookieWarnMsg2 = (window.t && window.t('music.cookieExpired')) || '音乐Cookie已失效';
                                        window.showStatusToast(musiccookieWarnMsg2, 5000);
                                        window._cookieWarnLastTime = now2;
                                    }
                                }

                                if (result.success) {
                                    if (result.data && result.data.length > 0) {
                                        var track = result.data[0];
                                        if (typeof window.dispatchMusicPlay === 'function') window.dispatchMusicPlay(track);
                                    } else {
                                        console.warn('[Music] API did not find a song for: ' + searchTerm);
                                        if (typeof window.showStatusToast === 'function') {
                                            var notFoundMsg = window.t('music.notFound', { query: searchTerm, defaultValue: '找不到歌曲: ' + searchTerm });
                                            window.showStatusToast(notFoundMsg, 3000);
                                        }
                                    }
                                } else {
                                    console.error('[Music] Music search API returned error:', result.message || result.error);
                                    if (typeof window.showStatusToast === 'function') {
                                        var failMsg2 = window.safeT ? window.safeT('music.searchFailed', '音乐搜索失败') : '音乐搜索失败';
                                        var detailMsg = result.message || result.error || failMsg2;
                                        window.showStatusToast(detailMsg, 3000);
                                    }
                                }
                            })
                            .catch(function (e4) {
                                if (typeof myEpoch !== 'undefined' && typeof window._currentMusicSearchEpoch !== 'undefined') {
                                    if (myEpoch !== window._currentMusicSearchEpoch) return;
                                }
                                console.error('[Music] Music search API call failed:', e4);
                                if (typeof window.showStatusToast === 'function') {
                                    var failMsg3 = window.safeT ? window.safeT('music.searchFailed', '音乐搜索失败') : '音乐搜索失败';
                                    window.showStatusToast(failMsg3, 3000);
                                }
                            });
                    }
                // -------- music allowlist add --------
                } else if (response.type === 'music_allowlist_add') {
                    if (window.MusicPluginAPI && (response.domains || response.http_urls)) {
                        console.log('[Music] Received allowlist update from backend:', response.domains, response.http_urls);
                        window.MusicPluginAPI.addAllowlist(response.domains || [], response.http_urls || []);
                    }

                // -------- music play url --------
                } else if (response.type === 'music_play_url') {
                    handleMusicPlayUrlResponse(response);

                // -------- jukebox control --------
                } else if (response.type === 'jukebox_control') {
                    handleJukeboxControlResponse(response);

                // -------- repetition_warning --------
                } else if (response.type === 'repetition_warning') {
                    console.log(window.t('console.repetitionWarningReceived'), response.name);
                    var warningMessage = window.t
                        ? window.t('app.repetitionDetected', { name: response.name })
                        : ('检测到高重复度对话。建议您终止对话，让' + response.name + '休息片刻。');
                    if (typeof window.showStatusToast === 'function') window.showStatusToast(warningMessage, 8000);

                // -------- mini_game_invite_options --------
                // 后端投递 mini-game 邀请时跟 invite text 一起 push 这条 options。
                // 通用 ChoicePrompt 抽象，前端 ChoiceWindow 渲染三按钮（accept /
                // decline / later）。多窗口模式下消息走 RAW_MESSAGE forwarding 自然
                // 转给 chat.html，无需新 IPC channel。
                } else if (response.type === 'mini_game_invite_options') {
                    if (window.reactChatWindowHost
                            && typeof window.reactChatWindowHost.setMiniGameInvitePrompt === 'function') {
                        window.reactChatWindowHost.setMiniGameInvitePrompt({
                            sessionId: response.session_id || '',
                            gameType: response.game_type || '',
                            options: Array.isArray(response.options) ? response.options : [],
                        });
                    }

                // -------- mini_game_invite_resolved --------
                // 邀请被 resolve（任一 outcome：accept / cooldown / suppress）→
                // 前端 dismiss prompt UI（cross-window 一致性，pet + chat.html
                // 多窗口同时显示 prompt 时全部清掉）。accept 时 payload 同时带
                // game_url 当 launch 信号——前端 window.open 让 Electron 主进程
                // setWindowOpenHandler 拦截开独立 BrowserWindow，dedupe 由
                // launched session_id 保护防止双开。
                } else if (response.type === 'mini_game_invite_resolved') {
                    if (window.reactChatWindowHost
                            && typeof window.reactChatWindowHost.handleMiniGameInviteResolved === 'function') {
                        window.reactChatWindowHost.handleMiniGameInviteResolved({
                            sessionId: response.session_id || '',
                            action: response.action || '',
                            gameType: response.game_type || '',
                            url: response.game_url || '',
                        });
                    }

                // -------- activity_context_prompt --------
                // 后端活动 tracker 检测到用户「进入」游戏/娱乐（context='play'）或
                // 「进入」专注工作（context='work'）时推这条。前端（对所有用户、每会话
                // 每类一次）据此弹窗问要不要开/关主动搭话里的屏幕分享来源。去重都在
                // app-context-prompt.js（原 A/B 实验组 vision_chat_default_off 的机制已
                // 合并进 main）。
                } else if (response.type === 'activity_context_prompt') {
                    if (window.appContextPrompt
                            && typeof window.appContextPrompt.handle === 'function') {
                        window.appContextPrompt.handle(response.context || '');
                    }

                // -------- game_window_state_change --------
                // 后端 game_route_start 激活后推 'opened'，_finalize 翻 inactive
                // 后推 'closed'。前端把它转成 DOM 自定义事件让 chat.html / pet
                // index.html 各自挂监听做布局联动（chat.html → 触发内部 collapse
                // + 移到左下角；index.html → 加 body class 隐藏 live2d/vrm/mmd
                // 容器）。多窗口模式下 RAW_MESSAGE forwarding 把同一条 WS 转给
                // chat.html，两边监听同一个 DOM 事件名即可。
                } else if (response.type === 'game_window_state_change') {
                    try {
                        var detail = {
                            action: response.action || '',
                            lanlanName: response.lanlan_name || '',
                            gameType: response.game_type || '',
                            sessionId: response.session_id || '',
                            routeInstanceId: response.sdk_route_instance_id || ''
                        };
                        var currentGameSessionId = S.gameRouteSessionId || '';
                        var incomingGameSessionId = detail.sessionId || '';
                        var currentGameRouteInstanceId = S.gameRouteInstanceId || '';
                        var incomingGameRouteInstanceId = detail.routeInstanceId || '';
                        var isStaleGameWindowEvent = detail.action === 'closed'
                            && (
                                (incomingGameSessionId
                                    && currentGameSessionId
                                    && incomingGameSessionId !== currentGameSessionId)
                                || ((incomingGameRouteInstanceId || currentGameRouteInstanceId)
                                    && incomingGameRouteInstanceId !== currentGameRouteInstanceId)
                            );
                        if (isStaleGameWindowEvent) {
                            console.log(`[GameWindow] 忽略过期窗口事件 | action=${detail.action} incoming=${incomingGameSessionId} current=${currentGameSessionId}`);
                            // Same reasoning as the GAME_ROUTE_ENDED early returns above:
                            // `closed` is emitted only from route finalize, so this
                            // payload names a dead route. Its own identity only.
                            if (incomingGameSessionId) {
                                rememberEndedGameRouteIdentity(
                                    detail.gameType || '',
                                    incomingGameSessionId,
                                    incomingGameRouteInstanceId
                                );
                            }
                        } else if (detail.action === 'opened') {
                            advanceGameRouteStateRevision();
                            pruneRecentlyEndedGameRouteIdentities();
                            S.gameRouteActive = true;
                            S.gameRouteGameType = detail.gameType || '';
                            S.gameRouteLanlanName = detail.lanlanName || '';
                            S.gameRouteSessionId = incomingGameSessionId || '';
                            S.gameRouteInstanceId = incomingGameRouteInstanceId || '';
                            if (typeof window.stopProactiveChatSchedule === 'function') {
                                S.proactiveChatWasStoppedByGameRoute = !!S.proactiveChatEnabled;
                                window.stopProactiveChatSchedule();
                            }
                        } else if (detail.action === 'closed') {
                            advanceGameRouteStateRevision();
                            var wasGameRouteActive = !!S.gameRouteActive;
                            rememberEndedGameRouteIdentity(
                                detail.gameType || S.gameRouteGameType || '',
                                incomingGameSessionId || currentGameSessionId,
                                incomingGameRouteInstanceId || currentGameRouteInstanceId
                            );
                            S.gameRouteActive = false;
                            S.gameRouteGameType = '';
                            S.gameRouteLanlanName = '';
                            S.gameRouteSessionId = '';
                            S.gameRouteInstanceId = '';
                            if ((wasGameRouteActive || S.proactiveChatWasStoppedByGameRoute)
                                    && S.proactiveChatEnabled
                                    && typeof window.scheduleProactiveChat === 'function') {
                                window.scheduleProactiveChat();
                            }
                            S.proactiveChatWasStoppedByGameRoute = false;
                        }
                        if (!isStaleGameWindowEvent) {
                            window.dispatchEvent(new CustomEvent('neko-game-window-state-change', { detail: detail }));
                        }
                    } catch (gwErr) {
                        console.warn('[GameWindow] dispatch failed:', gwErr);
                    }

                }

            } catch (parseError) {
                console.error(window.t('console.messageProcessingFailed'), parseError);
            }
        };

        // ---- onclose ----
        S.socket.onclose = function () {
            // Stale onclose guard: background-tab throttling (or async scheduling) can
            // delay an old socket's onclose until after a replacement connectWebSocket()
            // has already run onopen and started a new session. In that case the mutations
            // below (heartbeat clear, recording/session reset, button state, audio queue)
            // would corrupt the live new session. Skip everything when this socket is stale.
            if (S.socket !== _thisSocket) {
                console.log('[WS] stale onclose skipped (socket already replaced)');
                return;
            }
            console.log(window.t('console.websocketClosed'));
            removeExternalAsrPreview();
            // Socket teardown ends the backend ASR route; drop the route flags so
            // the mic settings hint stops reporting independent ASR as active. A
            // reconnected session re-emits ASR_INDEPENDENT_* statuses on start.
            S.independentAsrActive = false;
            S.independentAsrProvider = '';
            clearAssistantLifecycleOnDisconnect('socket_close');

            // Clear heartbeat
            if (S.heartbeatInterval) {
                clearInterval(S.heartbeatInterval);
                S.heartbeatInterval = null;
                console.log(window.t('console.heartbeatStopped'));
            }

            // Reset text session state
            if (S.isTextSessionActive) {
                S.isTextSessionActive = false;
                console.log(window.t('console.websocketDisconnectedResetText'));
            }
            S.voiceChatActive = false;
            S.voiceStartPending = false;

            // Reset voice recording state & resources
            if (S.isRecording || window.isMicStarting) {
                console.log('WebSocket断开时重置语音录制状态');
                S.isRecording = false;
                window.isRecording = false;
                window.isMicStarting = false;
                window.currentGeminiMessage = null;
                S.lastVoiceUserMessage = null;
                S.lastVoiceUserMessageTime = 0;

                if (typeof window.stopSilenceDetection === 'function') window.stopSilenceDetection();
                S.inputAnalyser = null;

                if (S.stream) {
                    S.stream.getTracks().forEach(function (track) { track.stop(); });
                    S.stream = null;
                }

                if (S.audioContext && S.audioContext.state !== 'closed') {
                    S.audioContext.close();
                    S.audioContext = null;
                    S.workletNode = null;
                }
            }

            // Reset mode switching flag
            if (S.isSwitchingMode) {
                console.log('WebSocket断开时重置模式切换标志');
                S.isSwitchingMode = false;
            }

            // Clean up session Promise
            if (S.sessionStartedResolver || S.sessionStartedRejecter) {
                console.log('WebSocket断开时清理session Promise');
                if (S.sessionStartedRejecter) {
                    try { S.sessionStartedRejecter(new Error('WebSocket连接断开')); } catch (_e3) { }
                }
                S.sessionStartedResolver = null;
                S.sessionStartedRejecter = null;
            }

            if (window.sessionTimeoutId) {
                clearTimeout(window.sessionTimeoutId);
                window.sessionTimeoutId = null;
            }

            // Clear audio queue
            (async function () {
                if (typeof window.clearAudioQueue === 'function') await window.clearAudioQueue();
            })();

            if (typeof window.hideVoicePreparingToast === 'function') window.hideVoicePreparingToast();

            // Reset button states
            var _mb5 = micButton();
            if (_mb5) { _mb5.classList.remove('active'); _mb5.classList.remove('recording'); _mb5.disabled = false; }
            var _sb5 = screenButton(); if (_sb5) _sb5.classList.remove('active');
            var _ts2 = textSendButton(); if (_ts2) _ts2.disabled = false;
            var _ti2 = textInputBox(); if (_ti2) _ti2.disabled = false;
            var _ss2 = screenshotButton(); if (_ss2) _ss2.disabled = false;

            var _mu5 = muteButton(); if (_mu5) _mu5.disabled = true;
            var _sb6 = screenButton(); if (_sb6) _sb6.disabled = true;
            var _st4 = stopButton(); if (_st4) _st4.disabled = true;
            var _rs4 = resetSessionButton(); if (_rs4) _rs4.disabled = true;
            var _rt3 = returnSessionButton(); if (_rt3) _rt3.disabled = true;

            var _tia3 = document.getElementById('text-input-area');
            if (_tia3) _tia3.classList.remove('hidden');
            if (typeof window.syncVoiceChatComposerHidden === 'function') window.syncVoiceChatComposerHidden(false);

            if (typeof window.syncFloatingMicButtonState === 'function') window.syncFloatingMicButtonState(false);
            if (typeof window.syncFloatingScreenButtonState === 'function') window.syncFloatingScreenButtonState(false);

            // Auto-reconnect: skip if switching catgirl OR this socket was already
            // replaced by a newer connectWebSocket() call (prevents reconnect storm
            // when the old socket's onclose fires after the switch completes).
            if (!S.isSwitchingCatgirl && S.socket === _thisSocket) {
                S.autoReconnectTimeoutId = setTimeout(connectWebSocket, 3000);
            }
        };

        // ---- onerror ----
        S.socket.onerror = function (error) {
            console.error(window.t('console.websocketError'), error);
        };
    }
    mod.connectWebSocket = connectWebSocket;
    mod.ensureAssistantTurnStarted = ensureAssistantTurnStarted;
    mod.clearPendingAssistantTurnStart = clearPendingAssistantTurnStart;

    // ========================  Exported methods  ========================

    /** Send raw JSON action over WebSocket */
    mod.send = function (payload) {
        if (S.socket && S.socket.readyState === WebSocket.OPEN) {
            S.socket.send(typeof payload === 'string' ? payload : JSON.stringify(payload));
        }
    };

    /** Stop heartbeat (e.g. before intentional disconnect) */
    mod.stopHeartbeat = function () {
        if (S.heartbeatInterval) {
            clearInterval(S.heartbeatInterval);
            S.heartbeatInterval = null;
        }
    };

    /** Cancel any pending auto-reconnect timer */
    mod.cancelAutoReconnect = function () {
        if (S.autoReconnectTimeoutId) {
            clearTimeout(S.autoReconnectTimeoutId);
            S.autoReconnectTimeoutId = null;
        }
    };

    // ========================  Backward-compat globals  ========================
    window.connectWebSocket = connectWebSocket;
    window.ensureWebSocketOpen = ensureWebSocketOpen;
    window.refreshCoreApiCapability = refreshCoreApiCapability;
    window.ensureAssistantTurnStarted = ensureAssistantTurnStarted;
    window.clearPendingAssistantTurnStart = clearPendingAssistantTurnStart;

    // ========================  Greeting check (after model loaded)  ========================
    // 需要 WS 已连接 AND 模型已加载 两个条件同时满足才发送，
    // 无论哪个先就绪都由后到的那个触发。
    function _isElementVisible(el) {
        if (!el || el.hidden) return false;
        var style = window.getComputedStyle ? window.getComputedStyle(el) : null;
        if (style && (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0')) {
            return false;
        }
        var rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
        return !rect || rect.width > 0 || rect.height > 0;
    }
    function _hasVisibleGreetingBlocker(selectors) {
        for (var i = 0; i < selectors.length; i += 1) {
            var nodes = document.querySelectorAll(selectors[i]);
            for (var j = 0; j < nodes.length; j += 1) {
                if (_isElementVisible(nodes[j])) return true;
            }
        }
        return false;
    }
    function _isGreetingCheckBlocked() {
        if (!S.socket || S.socket.readyState !== WebSocket.OPEN) return true;
        if (S.isRecording || S.isPlaying) return true;
        if (S.assistantTurnId && S.assistantTurnId !== S.assistantTurnCompletedId) return true;
        if (S.assistantTurnAwaitingBubble || S.assistantSpeechActiveTurnId) return true;
        return _hasVisibleGreetingBlocker([
            '#prominent-notice-overlay',
            '.modal-overlay',
            '.modal-dialog',
            '#storage-location-overlay',
            '.storage-location-modal'
        ]);
    }
    function _resetGreetingCheckRetry(clearTimer) {
        S._greetingCheckRetryDelay = 0;
        if (clearTimer && S._greetingCheckRetryTimer) {
            clearTimeout(S._greetingCheckRetryTimer);
            S._greetingCheckRetryTimer = 0;
        }
    }
    function _scheduleGreetingCheckRetry() {
        if (S._greetingCheckRetryTimer) {
            clearTimeout(S._greetingCheckRetryTimer);
        }
        var delay = Number(S._greetingCheckRetryDelay) || GREETING_CHECK_RETRY_BASE_MS;
        S._greetingCheckRetryDelay = Math.min(delay * 2, GREETING_CHECK_RETRY_MAX_MS);
        S._greetingCheckRetryTimer = setTimeout(function () {
            S._greetingCheckRetryTimer = 0;
            _sendGreetingCheckIfReady();
        }, delay);
    }
    function _markGreetingCheckPending(isSwitch, reason) {
        S._greetingCheckPending = true;
        S._greetingCheckIsSwitch = !!isSwitch;
        S._greetingCheckReason = reason || '';
    }

    function consumeStartupGreetingReleasedDetail() {
        try {
            const detail = window.__NEKO_STARTUP_GREETING_RELEASED__;
            if (detail && detail.released === true) {
                delete window.__NEKO_STARTUP_GREETING_RELEASED__;
            }
            return detail && detail.released === true ? detail : null;
        } catch (_) {
            return null;
        }
    }

    function hasStartupGreetingReleaseProducer() {
        try {
            if (window.universalTutorialManager) {
                return true;
            }
        } catch (_) {}
        try {
            return !!document.querySelector('script[src*="/static/tutorial/core/universal-manager.js"],script[src*="tutorial/core/universal-manager.js"]');
        } catch (_) {
            return false;
        }
    }

    function isStartupTutorialActiveForGreeting() {
        try {
            var manager = window.universalTutorialManager || null;
            if (manager && manager.isTutorialRunning === true) return true;
            if (manager && manager.activeAvatarFloatingGuideRound) return true;
            if (document.body && document.body.classList && document.body.classList.contains('yui-taking-over')) {
                return true;
            }
        } catch (_) {}
        return false;
    }

    function scheduleStartupGreetingReleaseFallback() {
        if (S._startupGreetingReleaseFallbackTimer) {
            clearTimeout(S._startupGreetingReleaseFallbackTimer);
        }
        S._startupGreetingReleaseFallbackTimer = setTimeout(function () {
            S._startupGreetingReleaseFallbackTimer = 0;
            if (S._startupGreetingReleasePending) {
                if (isStartupTutorialActiveForGreeting()) {
                    scheduleStartupGreetingReleaseFallback();
                    return;
                }
                releaseStartupGreetingCheck('startup-greeting-release-timeout');
            }
        }, STARTUP_GREETING_RELEASE_FALLBACK_MS);
    }

    function sendStartupGreetingReleaseRequest(reason) {
        const released = consumeStartupGreetingReleasedDetail();
        if (released) {
            releaseStartupGreetingCheck(released.reason || 'startup-greeting-release');
            return;
        }
        if (!hasStartupGreetingReleaseProducer()) {
            releaseStartupGreetingCheck(reason || 'startup-greeting-no-release-producer');
            return;
        }
        S._startupGreetingReleasePending = true;
        S._startupGreetingReleaseReason = reason || 'ws-open';
        scheduleStartupGreetingReleaseFallback();
    }

    function releaseStartupGreetingCheck(reason) {
        if (!S._startupGreetingReleasePending && !S._greetingCheckPending) {
            return;
        }
        S._startupGreetingReleasePending = false;
        S._startupGreetingReleaseReason = '';
        if (S._startupGreetingReleaseFallbackTimer) {
            clearTimeout(S._startupGreetingReleaseFallbackTimer);
            S._startupGreetingReleaseFallbackTimer = 0;
        }
        if (reason) {
            S._greetingCheckReason = reason;
        }
        _sendGreetingCheckIfReady();
    }

    function _deferGreetingCheckForNewUserIcebreaker() {
        if (!isNewUserIcebreakerBlockingGreeting(S._greetingCheckReason)) return false;
        _scheduleGreetingCheckRetry();
        console.log('[greeting_check] deferred by active new-user icebreaker');
        return true;
    }
    function _sendGreetingCheckIfReady() {
        if (!S._greetingCheckPending || !S._modelReady) {
            if (!S._greetingCheckPending) _resetGreetingCheckRetry(true);
            return;
        }
        if (S.conversationLanguageHydrated !== true) {
            return;
        }
        if (S._startupGreetingReleasePending) {
            return;
        }
        if (_deferGreetingCheckForNewUserIcebreaker()) {
            return;
        }
        if (_isGreetingCheckBlocked()) {
            _scheduleGreetingCheckRetry();
            return;
        }
        try {
            if (S.socket && S.socket.readyState === WebSocket.OPEN) {
                // UI locale and conversation locale are independent.  Hydration
                // above resolves the durable per-character preference; only a
                // character with no explicit choice falls back to the UI locale.
                var greetingLang = getConversationLanguageForCurrentCharacter();
                var explicitGreetingLang = typeof getExplicitConversationLanguageForCurrentCharacter === 'function'
                    ? getExplicitConversationLanguageForCurrentCharacter()
                    : '';
                var greetingIsSwitch = !!S._greetingCheckIsSwitch;
                var greetingReason = S._greetingCheckReason || (greetingIsSwitch ? 'character-switch' : 'ws-open');
                var greetingMessage = {
                    action: 'greeting_check',
                    is_switch: greetingIsSwitch,
                    render_language: greetingLang,
                    reason: greetingReason
                };
                if (explicitGreetingLang) greetingMessage.language = explicitGreetingLang;
                S.socket.send(JSON.stringify(greetingMessage));
                S._greetingCheckPending = false;
                S._greetingCheckIsSwitch = false;
                S._greetingCheckReason = '';
                _resetGreetingCheckRetry(true);
                console.log('[greeting_check] sent, is_switch=' + greetingIsSwitch + ', reason=' + greetingReason);
            }
        } catch (e) {
            console.warn('[greeting_check] send failed:', e);
            _scheduleGreetingCheckRetry();
        }
    }
    function _onModelReady() {
        S._modelReady = true;
        _sendGreetingCheckIfReady();
    }
    // Live2D
    var _origOnModelLoaded = null;
    function _hookLive2dModelLoaded() {
        if (window.live2dManager && typeof window.live2dManager.onModelLoaded === 'function') {
            if (window.live2dManager.onModelLoaded._greetingHooked) return;
            _origOnModelLoaded = window.live2dManager.onModelLoaded;
        }
        var prevCb = _origOnModelLoaded;
        var hookedFn = function () {
            if (prevCb) prevCb.apply(this, arguments);
            _onModelReady();
        };
        hookedFn._greetingHooked = true;
        if (window.live2dManager) window.live2dManager.onModelLoaded = hookedFn;
    }
    // 延迟 hook：live2dManager 可能还没创建
    if (window.live2dManager) _hookLive2dModelLoaded();
    else window.addEventListener('DOMContentLoaded', function () { setTimeout(_hookLive2dModelLoaded, 500); });
    // VRM / MMD
    window.addEventListener('vrm-model-loaded', _onModelReady);
    window.addEventListener('mmd-model-loaded', _onModelReady);

    // Only the dedicated conversation-language event updates the explicit
    // template-language preference. UI locale changes still refresh the
    // render-only fallback used by sessions without an explicit preference.
    function _syncLanguageToBackend(lng) {
        if (!lng || typeof lng !== 'string') return;
        S._conversationLanguageClearPending = null;
        if (!S.socket || S.socket.readyState !== WebSocket.OPEN) return;
        try {
            S.socket.send(JSON.stringify({
                action: 'language_update',
                language: lng,
            }));
        } catch (e) {
            console.warn('[language_update] send failed:', e);
        }
    }
    function _syncRenderLanguageToBackend(lng) {
        if (!lng || typeof lng !== 'string') return;
        if (!S.socket || S.socket.readyState !== WebSocket.OPEN) return;
        try {
            S.socket.send(JSON.stringify({
                action: 'language_update',
                render_language: lng,
            }));
        } catch (e) {
            console.warn('[language_update] render language send failed:', e);
        }
    }
    function _syncClearedLanguageToBackend(lng, characterName) {
        if (!lng || typeof lng !== 'string') return;
        var currentName = getWebSocketLanlanName() || '';
        var targetName = characterName || currentName;
        if (!targetName || targetName !== currentName) return;
        S._conversationLanguageClearPending = {
            characterName: targetName
        };
        if (!S.socket || S.socket.readyState !== WebSocket.OPEN) return;
        try {
            S.socket.send(JSON.stringify({
                action: 'language_update',
                clear_language_preference: true,
                render_language: lng,
            }));
            S._conversationLanguageClearPending = null;
        } catch (e) {
            console.warn('[language_update] clear language send failed:', e);
        }
    }
    function _applyClearedConversationLanguage(characterName) {
        var currentName = getWebSocketLanlanName() || '';
        if (!characterName || characterName !== currentName) return;
        S._conversationLanguageHydrationId =
            (Number(S._conversationLanguageHydrationId) || 0) + 1;
        S.conversationLanguage = '';
        S.conversationLanguageExplicit = '';
        S.conversationLanguageHydrated = true;
        _syncClearedLanguageToBackend(
            getConversationLanguageForCurrentCharacter(),
            characterName
        );
        _sendGreetingCheckIfReady();
    }
    if (window.i18next && typeof window.i18next.on === 'function') {
        window.i18next.on('languageChanged', _syncRenderLanguageToBackend);
    } else {
        window.addEventListener('localechange', function () {
            try {
                var lng = (window.i18next && typeof window.i18next.language === 'string')
                    ? window.i18next.language : '';
                _syncRenderLanguageToBackend(lng);
            } catch (_) { /* noop */ }
        });
    }
    window.addEventListener('neko:conversation-language-changed', function (event) {
        var detail = event && event.detail ? event.detail : {};
        var currentName = getWebSocketLanlanName() || '';
        if (!detail.character_name || detail.character_name !== currentName) return;
        if (!detail.language || typeof detail.language !== 'string') return;
        S._conversationLanguageHydrationId =
            (Number(S._conversationLanguageHydrationId) || 0) + 1;
        S.conversationLanguage = detail.language;
        S.conversationLanguageExplicit = detail.language;
        S.conversationLanguageHydrated = true;
        _syncLanguageToBackend(detail.language);
        _sendGreetingCheckIfReady();
    });
    window.addEventListener('neko:conversation-language-cleared', function (event) {
        var detail = event && event.detail ? event.detail : {};
        _applyClearedConversationLanguage(detail.character_name || '');
    });
    window.addEventListener('storage', function (event) {
        var currentName = getWebSocketLanlanName() || '';
        var expectedKey = currentName
            ? 'nekoConversationLanguage:' + encodeURIComponent(currentName)
            : '';
        var expectedUntrustedKey = currentName
            ? 'nekoConversationLanguageUntrusted:' + encodeURIComponent(currentName)
            : '';
        if (expectedUntrustedKey && event.key === expectedUntrustedKey) {
            if (event.newValue === '1') {
                // A failed hydration in another window is weaker evidence than
                // this window's in-flight or already confirmed server result.
                // Let an active GET settle, and re-publish a confirmed explicit
                // value so the sibling marker converges back to trusted state.
                if (S.conversationLanguageHydrated !== true) return;
                if (S.conversationLanguageExplicit) {
                    if (typeof window.setConversationLanguagePreference === 'function') {
                        window.setConversationLanguagePreference(
                            S.conversationLanguageExplicit,
                            currentName,
                            { dispatch: false, source: 'server' }
                        );
                    }
                    return;
                }
                try {
                    if (typeof window.getCachedConversationLanguagePreference === 'function') {
                        S.conversationLanguage = window.getCachedConversationLanguagePreference(
                            currentName
                        ) || '';
                    }
                } catch (_) { S.conversationLanguage = ''; }
                S.conversationLanguageExplicit = '';
                S.conversationLanguageHydrated = true;
                _syncRenderLanguageToBackend(getConversationLanguageForCurrentCharacter());
                _sendGreetingCheckIfReady();
                return;
            }
            // Marker removal must not cancel stronger in-flight/server evidence.
            // A successful sibling write leaves a trusted explicit cache that can
            // be applied directly; only a true clear/no-evidence state needs GET.
            if (S.conversationLanguageHydrated !== true) return;
            if (S.conversationLanguageExplicit) return;
            var trustedExplicitLanguage = '';
            try {
                if (typeof window.getExplicitConversationLanguagePreference === 'function') {
                    trustedExplicitLanguage = window.getExplicitConversationLanguagePreference(
                        currentName
                    ) || '';
                }
            } catch (_) { trustedExplicitLanguage = ''; }
            if (trustedExplicitLanguage) {
                S.conversationLanguage = trustedExplicitLanguage;
                S.conversationLanguageExplicit = trustedExplicitLanguage;
                S.conversationLanguageHydrated = true;
                _syncLanguageToBackend(trustedExplicitLanguage);
                _sendGreetingCheckIfReady();
                return;
            }
            S._conversationLanguageHydration = hydrateConversationLanguage(currentName);
            return;
        }
        if (!expectedKey || event.key !== expectedKey) return;
        if (!event.newValue) {
            if (typeof window.clearConversationLanguagePreference === 'function') {
                window.clearConversationLanguagePreference(currentName, {
                    dispatch: false,
                    source: 'storage'
                });
            }
            _applyClearedConversationLanguage(currentName);
            return;
        }
        S._conversationLanguageHydrationId =
            (Number(S._conversationLanguageHydrationId) || 0) + 1;
        S.conversationLanguage = event.newValue;
        S.conversationLanguageExplicit = event.newValue;
        S.conversationLanguageHydrated = true;
        _syncLanguageToBackend(event.newValue);
        _sendGreetingCheckIfReady();
    });

    window.addEventListener('neko:new-user-icebreaker-ended', function () {
        _sendGreetingCheckIfReady();
    });

    window.addEventListener(STARTUP_GREETING_RELEASE_EVENT, function (event) {
        var detail = event && event.detail ? event.detail : {};
        if (detail.released === false) {
            return;
        }
        releaseStartupGreetingCheck(detail.reason || 'startup-greeting-release');
    });

    // 从猫咪形态变回猫娘（请她回来）时，按猫咪停留时长 + tier 请求一次专属问候。
    // 与 greeting_check 对偶，但走独立 action，时长由 app-auto-goodbye 测量传入。
    // 变回不重连 WS，所以这里直接在事件触发时发；若无连接则静默放弃（普通 greeting
    // 会在下次 WS 重连时按对话 gap 兜底）。
    window.addEventListener('neko:cat-greeting-check', function (event) {
        var detail = (event && event.detail && typeof event.detail === 'object') ? event.detail : {};
        if (!S.socket || S.socket.readyState !== WebSocket.OPEN) {
            return;
        }
        var durationSeconds = Number(detail.durationSeconds) || 0;
        var catMemorySummary = detail.catMemorySummary && typeof detail.catMemorySummary === 'object' &&
            !Array.isArray(detail.catMemorySummary)
            ? detail.catMemorySummary
            : null;
        if (durationSeconds < CAT_GREETING_SILENT_BELOW_SECONDS) {
            return;
        }
        var catLang = getConversationLanguageForCurrentCharacter();
        var explicitCatLang = typeof getExplicitConversationLanguageForCurrentCharacter === 'function'
            ? getExplicitConversationLanguageForCurrentCharacter()
            : '';
        try {
            var catGreetingMessage = {
                action: 'cat_greeting_check',
                cat_duration_seconds: durationSeconds,
                tier: detail.tier || '',
                was_auto: !!detail.wasAuto,
                render_language: catLang
            };
            if (explicitCatLang) catGreetingMessage.language = explicitCatLang;
            if (catMemorySummary) {
                catGreetingMessage.cat_memory_summary = catMemorySummary;
            }
            S.socket.send(JSON.stringify(catGreetingMessage));
            console.log('[cat_greeting_check] sent, duration=' + durationSeconds + 's tier=' + (detail.tier || '-') +
                ' was_auto=' + (!!detail.wasAuto));
        } catch (e) {
            console.warn('[cat_greeting_check] send failed:', e);
        }
    });

    // ========================  Export module  ========================
    window.appWebSocket = mod;
})();
