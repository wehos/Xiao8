/**
 * app-audio-playback.js — Audio playback, scheduling, lip-sync & speaker volume
 *
 * Extracted from the monolithic app.js.
 * Exposes functions via  window.appAudioPlayback  (mod)  and backward-compatible
 * window.xxx globals where the rest of the code expects them.
 *
 * Dependencies (must be loaded first):
 *   - app-state.js           → window.appState  (S), window.appConst (C), window.appUtils
 *   - ogg-opus-decoder-wrapper.js → resetOggOpusDecoder(), decodeOggOpusChunk()
 */
(function () {
    'use strict';

    const mod = {};
    const S = window.appState;
    const C = window.appConst;
    const DEFAULT_SPEAKER_DEVICE_ID = C.DEFAULT_SPEAKER_DEVICE_ID || 'default';
    const SELECTED_SPEAKER_STORAGE_KEY = 'neko_selected_speaker';
    let audioPlayerContextSetupPromise = null;
    let speakerTransitionPromise = Promise.resolve();
    let localSpeakerPreferenceRevision = 0;

    function normalizeAssistantPlaybackGain(value) {
        var gain = Number(value);
        if (!Number.isFinite(gain)) return 1;
        return Math.max(0, Math.min(2, gain));
    }

    function normalizeSpeakerDeviceId(deviceId) {
        return typeof deviceId === 'string' && deviceId.length > 0
            ? deviceId
            : DEFAULT_SPEAKER_DEVICE_ID;
    }

    function notifySpeakerDeviceChanged(reason) {
        window.dispatchEvent(new CustomEvent('neko:speaker-device-changed', {
            detail: {
                selectedDeviceId: normalizeSpeakerDeviceId(S.selectedSpeakerId),
                effectiveDeviceId: normalizeSpeakerDeviceId(S.effectiveSpeakerId),
                selectedDeviceAvailable: S.selectedSpeakerAvailable !== false,
                reason: reason || 'change'
            }
        }));
    }

    function persistSelectedSpeaker(deviceId) {
        var normalized = normalizeSpeakerDeviceId(deviceId);
        try {
            if (normalized === DEFAULT_SPEAKER_DEVICE_ID) {
                localStorage.removeItem(SELECTED_SPEAKER_STORAGE_KEY);
            } else {
                localStorage.setItem(SELECTED_SPEAKER_STORAGE_KEY, normalized);
            }
        } catch (error) {
            // Storage can be unavailable in restricted webviews. The current
            // session must keep the successfully routed device even then.
            console.warn('[Audio] 保存播放设备设置失败:', error);
        }
    }

    function getContextEffectiveSpeakerId(context, fallbackDeviceId) {
        if (context && typeof context.sinkId === 'string') {
            return normalizeSpeakerDeviceId(context.sinkId);
        }
        return normalizeSpeakerDeviceId(fallbackDeviceId);
    }

    async function applySpeakerDeviceToContext(context, deviceId) {
        if (!context) return false;
        var normalized = normalizeSpeakerDeviceId(deviceId);
        if (typeof context.setSinkId !== 'function') {
            if (normalized === DEFAULT_SPEAKER_DEVICE_ID) return false;
            var unsupportedError = new Error('Audio output device selection is not supported');
            unsupportedError.name = 'NotSupportedError';
            throw unsupportedError;
        }
        if (
            context.sinkId === normalized
            || (
                normalized === DEFAULT_SPEAKER_DEVICE_ID
                && context.sinkId === ''
            )
        ) return true;
        await context.setSinkId(normalized);
        return true;
    }

    function enqueueSpeakerTransition(operation) {
        var task = speakerTransitionPromise.catch(function () {
            // A failed transition must not poison later selections or recovery.
        }).then(operation);
        speakerTransitionPromise = task.catch(function () {
            // Keep exactly one bounded tail promise; callers still receive the
            // current task's result or error.
        });
        return task;
    }

    async function fallbackToDefaultSpeaker(context, error, reason, defaultAlreadyFailed) {
        console.warn('[Audio] 播放设备不可用，回退到系统默认播放设备:', error);
        if (normalizeSpeakerDeviceId(S.selectedSpeakerId) !== DEFAULT_SPEAKER_DEVICE_ID) {
            S.selectedSpeakerAvailable = false;
        }
        var fallbackError = defaultAlreadyFailed ? error : null;
        if (!fallbackError) {
            try {
                await applySpeakerDeviceToContext(context, DEFAULT_SPEAKER_DEVICE_ID);
            } catch (applyError) {
                fallbackError = applyError;
            }
        }
        if (fallbackError) {
            console.warn('[Audio] 应用系统默认播放设备失败:', fallbackError);
            S.effectiveSpeakerId = getContextEffectiveSpeakerId(
                context,
                S.effectiveSpeakerId
            );
            notifySpeakerDeviceChanged((reason || 'fallback') + '-failed');
            return S.effectiveSpeakerId;
        }
        S.effectiveSpeakerId = DEFAULT_SPEAKER_DEVICE_ID;
        // Keep selectedSpeakerId and its localStorage entry intact. A temporary
        // device outage must not erase the user's preferred output.
        notifySpeakerDeviceChanged(reason || 'fallback');
        return DEFAULT_SPEAKER_DEVICE_ID;
    }

    async function ensureAudioPlayerContext() {
        if (S.audioPlayerContext) {
            if (audioPlayerContextSetupPromise) {
                await audioPlayerContextSetupPromise;
            }
            await speakerTransitionPromise;
            return S.audioPlayerContext;
        }

        var AudioContextConstructor = window.AudioContext || window.webkitAudioContext;
        var context = new AudioContextConstructor();
        S.audioPlayerContext = context;
        if (typeof window.syncAudioGlobals === 'function') {
            window.syncAudioGlobals();
        }

        var preferredDeviceId = normalizeSpeakerDeviceId(S.selectedSpeakerId);
        var initialDeviceId = S.selectedSpeakerAvailable === false
            ? DEFAULT_SPEAKER_DEVICE_ID
            : preferredDeviceId;
        var setupPromise = enqueueSpeakerTransition(async function () {
            if (S.audioPlayerContext !== context) return null;
            try {
                await applySpeakerDeviceToContext(context, initialDeviceId);
                S.effectiveSpeakerId = initialDeviceId;
                return initialDeviceId;
            } catch (error) {
                return fallbackToDefaultSpeaker(
                    context,
                    error,
                    'context-fallback',
                    initialDeviceId === DEFAULT_SPEAKER_DEVICE_ID
                );
            }
        }).finally(function () {
            if (audioPlayerContextSetupPromise === setupPromise) {
                audioPlayerContextSetupPromise = null;
            }
        });
        audioPlayerContextSetupPromise = setupPromise;
        await setupPromise;
        return S.audioPlayerContext;
    }

    async function selectSpeakerDevice(deviceId) {
        var normalized = normalizeSpeakerDeviceId(deviceId);
        if (audioPlayerContextSetupPromise) {
            await audioPlayerContextSetupPromise;
        }
        return enqueueSpeakerTransition(async function () {
            var previousDeviceId = normalizeSpeakerDeviceId(S.selectedSpeakerId);
            var previousDeviceAvailable = S.selectedSpeakerAvailable;
            var previousEffectiveDeviceId = normalizeSpeakerDeviceId(S.effectiveSpeakerId);
            S.selectedSpeakerId = normalized;
            S.selectedSpeakerAvailable = true;

            try {
                if (S.audioPlayerContext) {
                    await applySpeakerDeviceToContext(S.audioPlayerContext, normalized);
                    S.effectiveSpeakerId = normalized;
                } else if (normalized !== DEFAULT_SPEAKER_DEVICE_ID) {
                    var AudioContextConstructor = window.AudioContext || window.webkitAudioContext;
                    if (
                        !AudioContextConstructor
                        || !AudioContextConstructor.prototype
                        || typeof AudioContextConstructor.prototype.setSinkId !== 'function'
                    ) {
                        var unsupportedError = new Error('Audio output device selection is not supported');
                        unsupportedError.name = 'NotSupportedError';
                        throw unsupportedError;
                    }
                }

                persistSelectedSpeaker(normalized);
                localSpeakerPreferenceRevision += 1;
                notifySpeakerDeviceChanged('selection');
                return true;
            } catch (error) {
                S.selectedSpeakerId = previousDeviceId;
                S.selectedSpeakerAvailable = previousDeviceAvailable;
                if (S.audioPlayerContext) {
                    try {
                        await applySpeakerDeviceToContext(
                            S.audioPlayerContext,
                            previousEffectiveDeviceId
                        );
                        S.effectiveSpeakerId = previousEffectiveDeviceId;
                    } catch (restoreError) {
                        S.effectiveSpeakerId = getContextEffectiveSpeakerId(
                            S.audioPlayerContext,
                            S.effectiveSpeakerId
                        );
                        console.warn('[Audio] 恢复原播放设备失败:', restoreError);
                    }
                }
                notifySpeakerDeviceChanged('selection-failed');
                throw error;
            }
        });
    }

    async function loadSelectedSpeaker() {
        var saved = null;
        try {
            saved = localStorage.getItem(SELECTED_SPEAKER_STORAGE_KEY);
        } catch (error) {
            console.warn('[Audio] 读取播放设备设置失败，使用系统默认播放设备:', error);
        }
        var savedDeviceId = normalizeSpeakerDeviceId(saved);
        if (audioPlayerContextSetupPromise) {
            await audioPlayerContextSetupPromise;
        }
        return enqueueSpeakerTransition(async function () {
            S.selectedSpeakerId = savedDeviceId;
            S.selectedSpeakerAvailable = savedDeviceId === DEFAULT_SPEAKER_DEVICE_ID
                ? true
                : null;
            if (!S.audioPlayerContext) return S.selectedSpeakerId;
            try {
                await applySpeakerDeviceToContext(
                    S.audioPlayerContext,
                    savedDeviceId
                );
                S.effectiveSpeakerId = savedDeviceId;
                S.selectedSpeakerAvailable = true;
            } catch (error) {
                await fallbackToDefaultSpeaker(
                    S.audioPlayerContext,
                    error,
                    'load-fallback',
                    savedDeviceId === DEFAULT_SPEAKER_DEVICE_ID
                );
            }
            return S.selectedSpeakerId;
        });
    }

    async function reconcileSelectedSpeakerDevices(devices) {
        if (S.audioPlayerContext && audioPlayerContextSetupPromise) {
            await audioPlayerContextSetupPromise;
        }
        var outputDevices = Array.isArray(devices)
            ? devices.filter(function (device) { return device && device.kind === 'audiooutput'; })
            : [];
        return enqueueSpeakerTransition(async function () {
            var preferredDeviceId = normalizeSpeakerDeviceId(S.selectedSpeakerId);
            var preferredAvailable = preferredDeviceId === DEFAULT_SPEAKER_DEVICE_ID
                || outputDevices.some(function (device) {
                    return device.deviceId === preferredDeviceId;
                });
            S.selectedSpeakerAvailable = preferredAvailable;
            var targetDeviceId = preferredAvailable
                ? preferredDeviceId
                : DEFAULT_SPEAKER_DEVICE_ID;

            if (!S.audioPlayerContext) {
                S.effectiveSpeakerId = DEFAULT_SPEAKER_DEVICE_ID;
                notifySpeakerDeviceChanged(
                    preferredAvailable ? 'devices-checked' : 'device-missing'
                );
                return preferredAvailable;
            }

            var currentSinkId = getContextEffectiveSpeakerId(
                S.audioPlayerContext,
                S.effectiveSpeakerId
            );
            if (
                targetDeviceId === normalizeSpeakerDeviceId(S.effectiveSpeakerId)
                && targetDeviceId === currentSinkId
            ) {
                // devicechange still re-enumerates and verifies existence. Avoid a
                // redundant setSinkId only after that verification succeeds.
                notifySpeakerDeviceChanged('devices-checked');
                return preferredAvailable;
            }

            try {
                await applySpeakerDeviceToContext(S.audioPlayerContext, targetDeviceId);
                S.effectiveSpeakerId = targetDeviceId;
                notifySpeakerDeviceChanged(
                    preferredAvailable ? 'device-restored' : 'device-missing'
                );
            } catch (error) {
                await fallbackToDefaultSpeaker(
                    S.audioPlayerContext,
                    error,
                    preferredAvailable ? 'device-restore-fallback' : 'device-missing',
                    targetDeviceId === DEFAULT_SPEAKER_DEVICE_ID
                );
                return false;
            }
            return preferredAvailable;
        });
    }

    window.addEventListener('storage', function (event) {
        if (event.key !== SELECTED_SPEAKER_STORAGE_KEY) return;
        var eventDeviceId = normalizeSpeakerDeviceId(event.newValue);
        var localRevisionAtArrival = localSpeakerPreferenceRevision;
        Promise.resolve(audioPlayerContextSetupPromise).then(function () {
            return enqueueSpeakerTransition(async function () {
                // A local selection that completed after this event arrived is
                // authoritative for this window. It will not receive a storage
                // event for its own write, so an older queued event must not
                // route back over it.
                if (localRevisionAtArrival !== localSpeakerPreferenceRevision) {
                    return;
                }
                var deviceId = eventDeviceId;
                try {
                    // Storage events may be delivered after newer writes. Read
                    // the value at transaction time instead of trusting the
                    // event snapshot captured before queued routes completed.
                    deviceId = normalizeSpeakerDeviceId(
                        localStorage.getItem(SELECTED_SPEAKER_STORAGE_KEY)
                    );
                } catch (error) {
                    console.warn('[Audio] 读取跨窗口播放设备设置失败:', error);
                }
                S.selectedSpeakerId = deviceId;
                S.selectedSpeakerAvailable = deviceId === DEFAULT_SPEAKER_DEVICE_ID
                    ? true
                    : null;
                if (S.audioPlayerContext) {
                    try {
                        await applySpeakerDeviceToContext(S.audioPlayerContext, deviceId);
                        S.effectiveSpeakerId = deviceId;
                        S.selectedSpeakerAvailable = true;
                        notifySpeakerDeviceChanged('storage');
                    } catch (error) {
                        await fallbackToDefaultSpeaker(
                            S.audioPlayerContext,
                            error,
                            'storage-fallback',
                            deviceId === DEFAULT_SPEAKER_DEVICE_ID
                        );
                    }
                } else {
                    notifySpeakerDeviceChanged('storage');
                }
            });
        }).catch(function (error) {
            console.warn('[Audio] 同步播放设备设置失败:', error);
        });
    });

    function normalizeAssistantTurnId(turnId) {
        if (turnId === undefined || turnId === null || turnId === '') {
            return null;
        }
        return String(turnId);
    }

    const ASSISTANT_TURN_COMPLETION_FALLBACK_MS = 700;
    const ASSISTANT_AUDIO_HEADER_STALL_MS = 1800;
    // 最后兜底：如果 turn-end 包压根没到（server 漏发 / packet 掉），
    // maybeFinalizeAssistantSpeech 永远 skip_completion_mismatch，
    // S.isPlaying / S.assistantSpeechActiveTurnId 没人清，proactive gate
    // 和 mic focus gate 都会卡死。此处守门：所有音频队列空了且 flag
    // 还粘着，30s 后强制 cancel 收尾。比 ASSISTANT_TURN_COMPLETION_FALLBACK_MS
    // 长一个数量级——前者覆盖正常 race，后者覆盖 server 漏包。
    const STUCK_SPEAKING_FALLBACK_MS = 30000;
    // 权威 audio_done 迟迟不来时的有界放弃时长。必须是独立计时器，不能复用
    // scheduleAssistantTurnCompletionFallback：后者 fire 前有 hasAssistantSpeechActivity
    // 守卫，会把"speech 仍 active 但已 drained、正在等 audio_done"误判成还在说话
    // 而跳过收尾——那正好是要兜的场景。漏发 audio_done 的 provider 靠它保证
    // 每轮最多多等这么久，而不是一路卡到 30s 看门狗。
    const ASSISTANT_AUDIO_STREAM_CLOSE_GIVEUP_MS = 700;
    let _assistantTurnCompletionFallbackTimer = 0;
    let _assistantTurnCompletionFallbackTurnId = null;
    let _audioStreamCloseGiveUpTimer = 0;
    let _audioStreamCloseGiveUpTurnId = null;
    let _pendingAudioMetaStallTimer = 0;
    let _stuckSpeakingFallbackTimer = 0;
    const SPEECH_PLAYBACK_STATE_KEY = 'neko_speech_playback_state';
    const SPEECH_PLAYBACK_CHANNEL_NAME = 'neko_speech_playback_channel';
    const SPEECH_PLAYBACK_STATE_HEARTBEAT_MS = 200;
    let _speechPlaybackChannel = null;
    let _speechPlaybackStateHeartbeatTimer = 0;

    function getSpeechPlaybackChannel() {
        if (_speechPlaybackChannel !== null) {
            return _speechPlaybackChannel;
        }
        _speechPlaybackChannel = false;
        try {
            if (typeof BroadcastChannel !== 'undefined') {
                _speechPlaybackChannel = new BroadcastChannel(SPEECH_PLAYBACK_CHANNEL_NAME);
            }
        } catch (err) {
            _speechPlaybackChannel = false;
            if (window.DEBUG_AUDIO) {
                console.warn('[Audio] playback BroadcastChannel init failed:', err);
            }
        }
        return _speechPlaybackChannel || null;
    }

    function clearSpeechPlaybackStateHeartbeat() {
        if (_speechPlaybackStateHeartbeatTimer) {
            clearTimeout(_speechPlaybackStateHeartbeatTimer);
            _speechPlaybackStateHeartbeatTimer = 0;
        }
    }

    function scheduleSpeechPlaybackStateHeartbeat() {
        if (_speechPlaybackStateHeartbeatTimer) {
            return;
        }
        _speechPlaybackStateHeartbeatTimer = setTimeout(function () {
            _speechPlaybackStateHeartbeatTimer = 0;
            publishSpeechPlaybackState('heartbeat');
        }, SPEECH_PLAYBACK_STATE_HEARTBEAT_MS);
    }

    function publishSpeechPlaybackState(reason, patch) {
        var audioTime = S.audioPlayerContext ? S.audioPlayerContext.currentTime : 0;
        var scheduledEnd = patch && Number.isFinite(patch.scheduledEndAudioTime)
            ? patch.scheduledEndAudioTime
            : (S.nextChunkTime || 0);
        var remaining = Math.max(0, scheduledEnd - audioTime);
        var pendingAudioWork = (
            S.pendingAudioChunkMetaQueue.length > 0 ||
            S.incomingAudioBlobQueue.length > 0 ||
            !!S.pendingDecoderReset ||
            !!S.decoderResetPromise ||
            !!S.isProcessingIncomingAudioBlob
        );
        var state = Object.assign({
            type: 'speech_playback_state',
            active: remaining > 0.05 || S.scheduledSources.length > 0 || S.audioBufferQueue.length > 0 || pendingAudioWork,
            speechId: S.currentPlayingSpeechId || null,
            correlationId: S.currentPlayingSpeechCorrelationId || '',
            turnId: S.assistantSpeechActiveTurnId || S.assistantTurnId || null,
            playbackTurnId: S.assistantSpeechPlaybackTurnId || null,
            playbackStartAudioTime: Number.isFinite(S.assistantSpeechPlaybackStartAudioTime) ? S.assistantSpeechPlaybackStartAudioTime : 0,
            playbackEndAudioTime: Number.isFinite(S.assistantSpeechPlaybackEndAudioTime) ? S.assistantSpeechPlaybackEndAudioTime : 0,
            scheduledEndAudioTime: scheduledEnd,
            audioContextTime: audioTime,
            audioContextState: S.audioPlayerContext ? S.audioPlayerContext.state : '',
            remainingSeconds: remaining,
            updatedAt: Date.now(),
            reason: reason || 'update',
            source: 'audio_playback'
        }, patch || {});

        if (!state.active) {
            state.remainingSeconds = 0;
        }

        window.NekoSpeechPlaybackState = state;
        try {
            localStorage.setItem(SPEECH_PLAYBACK_STATE_KEY, JSON.stringify(state));
        } catch (_) { /* noop */ }

        var channel = getSpeechPlaybackChannel();
        if (channel) {
            try { channel.postMessage(state); } catch (_) { /* noop */ }
        }

        if (state.active) {
            scheduleSpeechPlaybackStateHeartbeat();
        } else {
            clearSpeechPlaybackStateHeartbeat();
        }
        try {
            window.dispatchEvent(new CustomEvent('neko-speech-playback-state', {
                detail: state
            }));
        } catch (_) { /* noop */ }
        return state;
    }

    function audioTraceEnabled() {
        return window.NEKO_DEBUG_BUBBLE_LIFECYCLE === true;
    }

    function logAudioLifecycle(label, extra) {
        if (!audioTraceEnabled()) {
            return;
        }
        console.log('[AudioTrace]', label, Object.assign({
            assistantTurnId: S.assistantTurnId,
            pendingTurnServerId: S.assistantPendingTurnServerId,
            assistantTurnCompletedId: S.assistantTurnCompletedId,
            assistantTurnCompletionSource: S.assistantTurnCompletionSource,
            assistantSpeechActiveTurnId: S.assistantSpeechActiveTurnId,
            assistantSpeechStartedTurnId: S.assistantSpeechStartedTurnId,
            currentPlayingSpeechId: S.currentPlayingSpeechId,
            scheduledSources: S.scheduledSources.length,
            audioBufferQueue: S.audioBufferQueue.length,
            pendingAudioMetaQueue: S.pendingAudioChunkMetaQueue.length,
            incomingAudioBlobQueue: S.incomingAudioBlobQueue.length,
            isPlaying: S.isPlaying
        }, extra || {}));
    }

    function emitAssistantSpeechLifecycleEvent(eventName, detail) {
        window.dispatchEvent(new CustomEvent(eventName, {
            detail: Object.assign({
                timestamp: Date.now()
            }, detail || {})
        }));
    }

    // Report REAL audio playback boundaries to the backend so the proactive
    // inject gate keys off actual playback (queue drained) rather than the
    // realtime API's response.done (generation finished while audio is still
    // buffered/playing). Rides the same ws as every other action, including
    // the Electron chat.html WSProxy/IPC bridge → Pet real ws. readyState
    // may be undefined on a proxy socket — send anyway (try/catch guards).
    function sendVoicePlaybackSignal(action, turnId) {
        try {
            var sock = S.socket;
            if (sock && typeof sock.send === 'function' &&
                (sock.readyState === 1 || typeof sock.readyState === 'undefined')) {
                sock.send(JSON.stringify({
                    action: action,
                    turnId: turnId || null,
                    source: 'audio_playback'
                }));
            }
        } catch (_) { /* noop — best-effort signal */ }
    }

    function getActiveAvatarModelType() {
        // 优先按当前可见容器判断，避免 Live2D 全局引用残留时抢走 VRM/MMD 的口型同步。
        var vrmContainer = document.getElementById('vrm-container');
        if (vrmContainer && vrmContainer.style.display !== 'none' && !vrmContainer.classList.contains('hidden')) {
            return 'vrm';
        }

        var mmdContainer = document.getElementById('mmd-container');
        if (mmdContainer && mmdContainer.style.display !== 'none' && !mmdContainer.classList.contains('hidden')) {
            return 'mmd';
        }

        var pngtuberContainer = document.getElementById('pngtuber-container');
        if (pngtuberContainer && pngtuberContainer.style.display !== 'none' && !pngtuberContainer.classList.contains('hidden')) {
            return 'pngtuber';
        }

        var cfg = window.lanlan_config || {};
        var modelType = String(cfg.model_type || '').toLowerCase();
        if (modelType === 'pngtuber') {
            return 'pngtuber';
        }
        if (modelType === 'live3d') {
            var subType = String(cfg.live3d_sub_type || '').toLowerCase();
            if (subType === 'vrm' || subType === 'mmd') {
                return subType;
            }
        }
        if (modelType === 'vrm' || modelType === 'mmd') {
            return modelType;
        }
        return 'live2d';
    }

    function clearPendingAudioMetaStallTimer() {
        if (_pendingAudioMetaStallTimer) {
            clearTimeout(_pendingAudioMetaStallTimer);
            _pendingAudioMetaStallTimer = 0;
        }
    }

    function pruneStalledPendingAudioMetaQueue(nowMs) {
        var currentTimeMs = Number.isFinite(nowMs) ? nowMs : Date.now();
        if (!Array.isArray(S.pendingAudioChunkMetaQueue) || S.pendingAudioChunkMetaQueue.length === 0) {
            return [];
        }

        var retained = [];
        var removed = [];
        S.pendingAudioChunkMetaQueue.forEach(function (item) {
            if (!item) {
                return;
            }

            if (item.shouldSkip) {
                removed.push(item);
                return;
            }

            if (item.epoch !== S.incomingAudioEpoch ||
                !Number.isFinite(item.receivedAt)) {
                retained.push(item);
                return;
            }

            if (currentTimeMs - item.receivedAt >= ASSISTANT_AUDIO_HEADER_STALL_MS) {
                removed.push(item);
                return;
            }

            retained.push(item);
        });

        if (removed.length === 0) {
            return removed;
        }

        S.pendingAudioChunkMetaQueue = retained;
        if (removed.some(function (item) { return item && item.speechId && item.speechId === S.currentPlayingSpeechId; }) &&
            S.scheduledSources.length === 0 &&
            S.audioBufferQueue.length === 0 &&
            S.incomingAudioBlobQueue.length === 0 &&
            !S.assistantSpeechActiveTurnId) {
            S.currentPlayingSpeechId = null;
            S.currentPlayingSpeechCorrelationId = '';
        }

        logAudioLifecycle('pruneStalledPendingAudioMetaQueue:removed', {
            removedCount: removed.length,
            stallMs: ASSISTANT_AUDIO_HEADER_STALL_MS,
            turnIds: removed.map(function (item) { return item && item.turnId ? String(item.turnId) : null; }),
            speechIds: removed.map(function (item) { return item && item.speechId ? String(item.speechId) : null; })
        });
        return removed;
    }

    function schedulePendingAudioMetaStallCheck() {
        clearPendingAudioMetaStallTimer();

        var nextDueAt = 0;
        S.pendingAudioChunkMetaQueue.forEach(function (item) {
            if (!item ||
                item.shouldSkip ||
                item.epoch !== S.incomingAudioEpoch ||
                !Number.isFinite(item.receivedAt)) {
                return;
            }

            var dueAt = item.receivedAt + ASSISTANT_AUDIO_HEADER_STALL_MS;
            if (!nextDueAt || dueAt < nextDueAt) {
                nextDueAt = dueAt;
            }
        });

        if (!nextDueAt) {
            return;
        }

        _pendingAudioMetaStallTimer = window.setTimeout(function () {
            _pendingAudioMetaStallTimer = 0;
            var removed = pruneStalledPendingAudioMetaQueue(Date.now());
            if (removed.length > 0) {
                var candidateTurnId = null;
                removed.some(function (item) {
                    candidateTurnId = resolveAssistantAudioTurnId(item && item.turnId, item && item.speechId);
                    return !!candidateTurnId;
                });
                if (candidateTurnId) {
                    maybeFinalizeAssistantSpeech(candidateTurnId);
                } else {
                    maybeFinalizeAssistantSpeech();
                }
            }
            schedulePendingAudioMetaStallCheck();
        }, Math.max(0, nextDueAt - Date.now()));
    }

    function dispatchAssistantSpeechStart(turnId) {
        var normalizedTurnId = normalizeAssistantTurnId(turnId);
        // 新 chunk 入队 = 真实音频活动，无论是不是同一 turn 都要撤掉 stuck watchdog。
        // 否则 choppy stream 里：第一段播完 arm 表 → 第二段到来仍是同一 turn 早 return →
        // 表不撤 → 30s 到点时如果刚好在第 N 段间隙 → 误 fire cancel。
        if (normalizedTurnId) {
            clearStuckSpeakingFallback();
            // 新一阵音频到了 = 上一次"队列空了"不是流结束。撤掉正在跑的
            // give-up，等这阵放完时再重新排，否则它会在播放中途强制收尾。
            clearAudioStreamCloseGiveUp();
        }
        if (!normalizedTurnId || S.assistantSpeechActiveTurnId === normalizedTurnId) {
            return;
        }
        S.assistantSpeechActiveTurnId = normalizedTurnId;
        S.assistantSpeechStartedTurnId = normalizedTurnId;
        clearAssistantTurnCompletionFallback();
        logAudioLifecycle('dispatchAssistantSpeechStart', {
            turnId: normalizedTurnId
        });
        emitAssistantSpeechLifecycleEvent('neko-assistant-speech-start', {
            turnId: normalizedTurnId,
            source: 'audio_playback'
        });
        sendVoicePlaybackSignal('voice_play_start', normalizedTurnId);
    }

    function dispatchAssistantSpeechEnd(turnId) {
        var normalizedTurnId = normalizeAssistantTurnId(turnId);
        if (!normalizedTurnId || S.assistantSpeechActiveTurnId !== normalizedTurnId) {
            logAudioLifecycle('dispatchAssistantSpeechEnd:skip', {
                turnId: normalizedTurnId
            });
            return;
        }
        S.assistantSpeechActiveTurnId = null;
        if (S.assistantSpeechPlaybackTurnId === normalizedTurnId) {
            S.assistantSpeechPlaybackTurnId = null;
            S.assistantSpeechPlaybackStartAudioTime = 0;
            S.assistantSpeechPlaybackEndAudioTime = 0;
        }
        clearStuckSpeakingFallback();
        logAudioLifecycle('dispatchAssistantSpeechEnd', {
            turnId: normalizedTurnId
        });
        emitAssistantSpeechLifecycleEvent('neko-assistant-speech-end', {
            turnId: normalizedTurnId,
            source: 'audio_playback'
        });
        sendVoicePlaybackSignal('voice_play_end', normalizedTurnId);
    }

    function resolveAssistantSpeechCancelTurnId() {
        pruneStalledPendingAudioMetaQueue(Date.now());
        schedulePendingAudioMetaStallCheck();
        var normalizedTurnId = normalizeAssistantTurnId(S.assistantSpeechActiveTurnId);
        if (normalizedTurnId) {
            return normalizedTurnId;
        }

        var scheduledTurnId = null;
        S.scheduledSources.some(function (source) {
            scheduledTurnId = normalizeAssistantTurnId(source && source._nekoAssistantTurnId);
            return !!scheduledTurnId;
        });
        if (scheduledTurnId) {
            return scheduledTurnId;
        }

        var queuedTurnId = null;
        S.audioBufferQueue.some(function (item) {
            queuedTurnId = resolveAssistantAudioTurnId(item && item.turnId, item && item.speechId);
            return !!queuedTurnId;
        });
        if (queuedTurnId) {
            return queuedTurnId;
        }

        var pendingMetaTurnId = null;
        S.pendingAudioChunkMetaQueue.some(function (item) {
            if (!item || item.shouldSkip) {
                return false;
            }
            pendingMetaTurnId = resolveAssistantAudioTurnId(item.turnId, item.speechId);
            return !!pendingMetaTurnId;
        });
        if (pendingMetaTurnId) {
            return pendingMetaTurnId;
        }

        var incomingBlobTurnId = null;
        S.incomingAudioBlobQueue.some(function (item) {
            if (!item || item.shouldSkip) {
                return false;
            }
            incomingBlobTurnId = resolveAssistantAudioTurnId(item.turnId, item.speechId);
            return !!incomingBlobTurnId;
        });
        return incomingBlobTurnId;
    }

    function dispatchAssistantSpeechCancel(source) {
        var normalizedTurnId = resolveAssistantSpeechCancelTurnId();
        if (!normalizedTurnId) {
            logAudioLifecycle('dispatchAssistantSpeechCancel:skip', {
                source: source || 'audio_playback'
            });
            return;
        }
        S.assistantSpeechActiveTurnId = null;
        S.assistantSpeechPlaybackTurnId = null;
        S.assistantSpeechPlaybackStartAudioTime = 0;
        S.assistantSpeechPlaybackEndAudioTime = 0;
        clearStuckSpeakingFallback();
        logAudioLifecycle('dispatchAssistantSpeechCancel', {
            turnId: normalizedTurnId,
            source: source || 'audio_playback'
        });
        emitAssistantSpeechLifecycleEvent('neko-assistant-speech-cancel', {
            turnId: normalizedTurnId,
            source: source || 'audio_playback'
        });
        // Cancel/interruption also means audio playback has stopped → open
        // the proactive gate (same as a natural end).
        sendVoicePlaybackSignal('voice_play_end', normalizedTurnId);
    }

    function clearAssistantTurnCompletionFallback() {
        if (_assistantTurnCompletionFallbackTimer) {
            clearTimeout(_assistantTurnCompletionFallbackTimer);
            _assistantTurnCompletionFallbackTimer = 0;
        }
        _assistantTurnCompletionFallbackTurnId = null;
    }

    function clearStuckSpeakingFallback() {
        if (_stuckSpeakingFallbackTimer) {
            clearTimeout(_stuckSpeakingFallbackTimer);
            _stuckSpeakingFallbackTimer = 0;
        }
    }

    function clearAudioStreamCloseGiveUp() {
        if (_audioStreamCloseGiveUpTimer) {
            clearTimeout(_audioStreamCloseGiveUpTimer);
            _audioStreamCloseGiveUpTimer = 0;
        }
        _audioStreamCloseGiveUpTurnId = null;
    }

    // 把"本轮音频流已关闭"的记录连同 speech_id 映射一起清掉。挂在
    // clearAssistantTurnCompletion 上，于是 turn-start / speech-cancel /
    // clearAudioQueue / 正常收尾 四条路径都会走到。
    function resetAssistantAudioStreamClose() {
        clearAudioStreamCloseGiveUp();
        S.assistantAudioStreamClosedTurnId = null;
        S.assistantAudioStreamClosedEpoch = -1;
        S.assistantAudioTurnBySpeechId = {};
    }

    function isAssistantAudioStreamClosed(turnId) {
        var normalizedTurnId = normalizeAssistantTurnId(turnId);
        if (!normalizedTurnId) {
            return false;
        }
        // 打断会推 epoch。此后才到达的 audio_done 属于上一世代，作废——
        // 否则它会去收尾一个已经被取消的轮（#1566 的镜像 bug）。
        if (S.assistantAudioStreamClosedEpoch !== S.incomingAudioEpoch) {
            return false;
        }
        return normalizeAssistantTurnId(S.assistantAudioStreamClosedTurnId) === normalizedTurnId;
    }

    // 音频头只带 speech_id，audio_done 也只能按 speech_id 对账，所以在音频头
    // 到达时把当时解析出的 turnId 记下来。
    function rememberAssistantAudioSpeechTurn(speechId, turnId) {
        var sid = normalizeAssistantTurnId(speechId);
        var normalizedTurnId = normalizeAssistantTurnId(turnId);
        if (!sid || !normalizedTurnId) {
            return;
        }
        if (!S.assistantAudioTurnBySpeechId || typeof S.assistantAudioTurnBySpeechId !== 'object') {
            S.assistantAudioTurnBySpeechId = {};
        }
        S.assistantAudioTurnBySpeechId[sid] = normalizedTurnId;

        // 宣告关闭之后又收到本轮的音频头 = 那条 audio_done 已经过期。
        // 音频头和 audio_done 走同一条 ws、严格按序，所以这是"后端说完了却
        // 又发来音频"的确凿证据，不是猜测。omni 原生通路会遇到：它的
        // response.audio.done 是 per-response 的，而一轮里可能有第二个带音频
        // 的 response（工具调用后的续答）。作废后重新等下一条 audio_done，
        // 等不到就走 give-up。
        if (isAssistantAudioStreamClosed(normalizedTurnId)) {
            logAudioLifecycle('rememberAssistantAudioSpeechTurn:reopen_after_close', {
                speechId: sid,
                turnId: normalizedTurnId
            });
            S.assistantAudioStreamClosedTurnId = null;
            S.assistantAudioStreamClosedEpoch = -1;
        }
    }

    // 后端权威信号：该 speech_id 的音频流已关闭，之后不会再有属于它的 chunk。
    function noteAssistantAudioStreamClosed(speechId) {
        var sid = normalizeAssistantTurnId(speechId);
        var mapped = (sid && S.assistantAudioTurnBySpeechId)
            ? S.assistantAudioTurnBySpeechId[sid]
            : null;
        var turnId = normalizeAssistantTurnId(mapped);
        if (!turnId) {
            // 没见过这个 sid 的音频头 = 本机从没播过它的音频，这条信号跟当前
            // 正在放的那一轮毫无关系。回落到"当前轮"会把别人的结束信号安到
            // 正在说话的轮头上，重造一次提前收尾——后端确实存在零音频也发信号
            // 的轮（整轮 TTS 文本被标点过滤成空）。宁可忽略走 give-up。
            logAudioLifecycle('noteAssistantAudioStreamClosed:skip_unknown_speech', {
                speechId: sid
            });
            return false;
        }

        S.assistantAudioStreamClosedTurnId = turnId;
        S.assistantAudioStreamClosedEpoch = S.incomingAudioEpoch;
        logAudioLifecycle('noteAssistantAudioStreamClosed', {
            speechId: sid,
            turnId: turnId,
            epoch: S.incomingAudioEpoch
        });
        // 信号可能早于最后一段音频播完（它只承诺"不会再有新的了"），
        // 所以照样要过 drained 那一关；没过就等 onended 再来一次。
        return maybeFinalizeAssistantSpeech(turnId);
    }

    // 等 audio_done 的有界放弃。刻意不加 hasAssistantSpeechActivity 守卫：
    // 这里要兜的就是"speech 还挂着 active、队列已空、只差信号"这个状态。
    function scheduleAudioStreamCloseGiveUp(turnId) {
        var normalizedTurnId = normalizeAssistantTurnId(turnId);
        if (!normalizedTurnId) {
            return;
        }
        if (_audioStreamCloseGiveUpTimer && _audioStreamCloseGiveUpTurnId === normalizedTurnId) {
            return;
        }
        clearAudioStreamCloseGiveUp();

        _audioStreamCloseGiveUpTurnId = normalizedTurnId;
        logAudioLifecycle('audioStreamCloseGiveUp:armed', {
            turnId: normalizedTurnId,
            delayMs: ASSISTANT_AUDIO_STREAM_CLOSE_GIVEUP_MS
        });
        _audioStreamCloseGiveUpTimer = window.setTimeout(function () {
            var pendingTurnId = _audioStreamCloseGiveUpTurnId;
            _audioStreamCloseGiveUpTimer = 0;
            _audioStreamCloseGiveUpTurnId = null;

            if (!pendingTurnId || S.assistantTurnCompletedId !== pendingTurnId) {
                logAudioLifecycle('audioStreamCloseGiveUp:skip_completion_mismatch', {
                    turnId: pendingTurnId || normalizedTurnId
                });
                return;
            }
            logAudioLifecycle('audioStreamCloseGiveUp:fire', {
                turnId: pendingTurnId
            });
            // force 只放行"等 audio_done"这一道门。等待期间又涌进音频时，
            // maybeFinalizeAssistantSpeech 自己的 drained 检查会拦住，
            // 等那阵播完再重新排 —— 所以这里不需要也不该再复查一遍。
            maybeFinalizeAssistantSpeech(pendingTurnId, { force: true });
        }, ASSISTANT_AUDIO_STREAM_CLOSE_GIVEUP_MS);
    }

    // 4 个队列 + 3 个 in-flight async flag，覆盖所有"音频还在路上"的状态。
    // - 4 queue 对齐 isAssistantTurnPlaybackDrained（少查 pendingAudioChunkMetaQueue
    //   会在 header 到了 blob 还没到的窗口里误判空）
    // - 3 async flag 对齐 publishSpeechPlaybackState:86-92 的 pendingAudioWork：
    //   processIncomingAudioBlobQueue 会先 shift 出 blob 再 await handleAudioBlob，
    //   shift 之后 incomingAudioBlobQueue.length 是 0 但解码还在跑；decoder reset
    //   同理是个 Promise 在 flight。这些都属于"未真正 idle"，arm watchdog 就是误伤。
    // filter shouldSkip 是因为上游可能 mark 跳过项。
    function _hasPendingAudioWork() {
        var pendingMeta = S.pendingAudioChunkMetaQueue.some(function (item) {
            return item && !item.shouldSkip;
        });
        return (
            S.scheduledSources.length > 0 ||
            S.audioBufferQueue.length > 0 ||
            pendingMeta ||
            S.incomingAudioBlobQueue.length > 0 ||
            !!S.pendingDecoderReset ||
            !!S.decoderResetPromise ||
            !!S.isProcessingIncomingAudioBlob
        );
    }

    // 兜底：所有音频队列空了且 isPlaying / assistantSpeechActiveTurnId 还粘着
    // → STUCK_SPEAKING_FALLBACK_MS 后强制走 cancel 路径收尾。
    // 触发点是 source.onended（最后一段音频刚播完，maybeFinalizeAssistantSpeech
    // 因为没收到 turn-end 而 skip 的那一刻），fire 时再 re-check 一次，
    // 期间如果新音频进来或正常 finalize 走完，会被 clearStuckSpeakingFallback 撤掉。
    function maybeArmStuckSpeakingFallback() {
        if (_stuckSpeakingFallbackTimer) return;
        var flagsSet = !!(S.isPlaying || S.assistantSpeechActiveTurnId);
        if (_hasPendingAudioWork() || !flagsSet) return;

        logAudioLifecycle('stuckSpeakingFallback:armed', {
            isPlaying: S.isPlaying,
            assistantSpeechActiveTurnId: S.assistantSpeechActiveTurnId,
            assistantTurnId: S.assistantTurnId,
            assistantTurnCompletedId: S.assistantTurnCompletedId,
            delayMs: STUCK_SPEAKING_FALLBACK_MS
        });

        _stuckSpeakingFallbackTimer = window.setTimeout(function () {
            _stuckSpeakingFallbackTimer = 0;
            var hasPending = _hasPendingAudioWork();
            var flagsStillSet = !!(S.isPlaying || S.assistantSpeechActiveTurnId);
            if (hasPending || !flagsStillSet) {
                logAudioLifecycle('stuckSpeakingFallback:skip_resolved', {
                    hasPendingAudioWork: hasPending,
                    flagsStillSet: flagsStillSet
                });
                return;
            }
            var snapshot = {
                isPlaying: S.isPlaying,
                assistantSpeechActiveTurnId: S.assistantSpeechActiveTurnId,
                assistantTurnId: S.assistantTurnId,
                assistantTurnCompletedId: S.assistantTurnCompletedId,
                assistantTurnStartedAt: S.assistantTurnStartedAt,
                scheduledSources: S.scheduledSources.length,
                audioBufferQueue: S.audioBufferQueue.length,
                pendingAudioChunkMetaQueue: S.pendingAudioChunkMetaQueue.length,
                incomingAudioBlobQueue: S.incomingAudioBlobQueue.length,
                pendingDecoderReset: !!S.pendingDecoderReset,
                decoderResetPromise: !!S.decoderResetPromise,
                isProcessingIncomingAudioBlob: !!S.isProcessingIncomingAudioBlob
            };
            console.warn('[Audio] sticky speaking flag detected, force-resetting via cancel after ' +
                STUCK_SPEAKING_FALLBACK_MS + 'ms with empty queues', snapshot);
            logAudioLifecycle('stuckSpeakingFallback:fire', snapshot);
            // 走 cancel 通道：dispatchAssistantSpeechCancel 会清 assistantSpeechActiveTurnId
            // 并 dispatch neko-assistant-speech-cancel，下方 handler 会清 isPlaying。
            try { dispatchAssistantSpeechCancel('stuck_speaking_fallback'); } catch (_) { /* noop */ }
            // 双保险：上面任一步没把 isPlaying 抹掉就强清。
            if (S.isPlaying) {
                S.isPlaying = false;
            }
            if (S.assistantSpeechActiveTurnId) {
                S.assistantSpeechActiveTurnId = null;
            }
        }, STUCK_SPEAKING_FALLBACK_MS);
    }

    function clearAssistantTurnCompletion() {
        clearAssistantTurnCompletionFallback();
        resetAssistantAudioStreamClose();
        S.assistantTurnCompletedId = null;
        S.assistantTurnCompletionSource = null;
        S.assistantSpeechStartedTurnId = null;
        // settled 标记随完成状态一起清：turn-start / speech-cancel / clearAudioQueue
        // 都经由本函数，等于把 settledId 接进完整的 turn 生命周期收尾。
        // maybeFinalizeAssistantSpeech 在调用本函数之后再设 settledId（见那里），
        // 所以"干净收尾"路径的 settledId 不会被这里误清。
        S.assistantTurnSettledId = null;
    }

    function scheduleAssistantTurnCompletionFallback(turnId, source) {
        var normalizedTurnId = normalizeAssistantTurnId(turnId);
        clearAssistantTurnCompletionFallback();
        if (!normalizedTurnId) {
            return;
        }

        _assistantTurnCompletionFallbackTurnId = normalizedTurnId;
        logAudioLifecycle('scheduleAssistantTurnCompletionFallback:scheduled', {
            turnId: normalizedTurnId,
            source: source || null,
            delayMs: ASSISTANT_TURN_COMPLETION_FALLBACK_MS
        });
        _assistantTurnCompletionFallbackTimer = window.setTimeout(function () {
            var fallbackTurnId = _assistantTurnCompletionFallbackTurnId;
            _assistantTurnCompletionFallbackTimer = 0;
            _assistantTurnCompletionFallbackTurnId = null;

            if (!fallbackTurnId || S.assistantTurnCompletedId !== fallbackTurnId) {
                logAudioLifecycle('scheduleAssistantTurnCompletionFallback:skip_completion_mismatch', {
                    turnId: fallbackTurnId || normalizedTurnId
                });
                return;
            }
            if (hasAssistantSpeechActivity(fallbackTurnId)) {
                logAudioLifecycle('scheduleAssistantTurnCompletionFallback:skip_activity_resumed', {
                    turnId: fallbackTurnId
                });
                return;
            }

            logAudioLifecycle('scheduleAssistantTurnCompletionFallback:fire', {
                turnId: fallbackTurnId
            });
            maybeFinalizeAssistantSpeech(fallbackTurnId);
        }, ASSISTANT_TURN_COMPLETION_FALLBACK_MS);
    }

    function resolveAssistantAudioTurnId(turnId, speechId) {
        return normalizeAssistantTurnId(
            turnId ||
            S.assistantTurnId ||
            S.assistantPendingTurnServerId ||
            S.assistantTurnCompletedId ||
            S.assistantSpeechActiveTurnId ||
            speechId
        );
    }

    function isAssistantTurnPlaybackDrained(turnId) {
        pruneStalledPendingAudioMetaQueue(Date.now());
        schedulePendingAudioMetaStallCheck();
        var normalizedTurnId = normalizeAssistantTurnId(turnId);
        if (!normalizedTurnId) {
            return false;
        }

        // 四个队列之外还有一段真空洞：processIncomingAudioBlobQueue 先 shift 出队
        // 再 await 解码，那期间 chunk 不在任何队列里，只有 processingAudioBlobTurnId
        // 证明它属于本轮。不查 pendingDecoderReset —— 它是打断时 latch 的意图标记，
        // 会一直粘到下一条音频 header 才清，拿它当"音频在路上"会把纯文本轮判成
        // 永不 drained（settledId 不置位 → 切语音干等 15s 的老毛病复发）。
        // decoderResetPromise 的等待发生在同一个循环内，已被本标志覆盖。
        if (S.isProcessingIncomingAudioBlob &&
            normalizeAssistantTurnId(S.processingAudioBlobTurnId) === normalizedTurnId) {
            return false;
        }

        var hasScheduledSource = S.scheduledSources.some(function (source) {
            return normalizeAssistantTurnId(source && source._nekoAssistantTurnId) === normalizedTurnId;
        });
        if (hasScheduledSource) {
            return false;
        }

        var hasQueuedBuffer = S.audioBufferQueue.some(function (item) {
            return resolveAssistantAudioTurnId(item && item.turnId, item && item.speechId) === normalizedTurnId;
        });
        if (hasQueuedBuffer) {
            return false;
        }

        var hasPendingMeta = S.pendingAudioChunkMetaQueue.some(function (item) {
            return item &&
                !item.shouldSkip &&
                item.epoch === S.incomingAudioEpoch &&
                resolveAssistantAudioTurnId(item.turnId, item.speechId) === normalizedTurnId;
        });
        if (hasPendingMeta) {
            return false;
        }

        return !S.incomingAudioBlobQueue.some(function (item) {
            return item &&
                !item.shouldSkip &&
                item.epoch === S.incomingAudioEpoch &&
                resolveAssistantAudioTurnId(item.turnId, item.speechId) === normalizedTurnId;
        });
    }

    function hasAssistantSpeechActivity(turnId) {
        var normalizedTurnId = normalizeAssistantTurnId(turnId);
        if (!normalizedTurnId) {
            return false;
        }

        if (normalizeAssistantTurnId(S.assistantSpeechActiveTurnId) === normalizedTurnId) {
            return true;
        }

        return !isAssistantTurnPlaybackDrained(normalizedTurnId);
    }

    function stopActiveLipSync() {
        var activeModelType = getActiveAvatarModelType();
        if (activeModelType === 'vrm' && window.vrmManager && window.vrmManager.currentModel && window.vrmManager.animation) {
            if (typeof window.vrmManager.animation.stopLipSync === 'function') {
                window.vrmManager.animation.stopLipSync();
            }
            S.lipSyncActive = false;
        } else if (activeModelType === 'mmd' && window.mmdManager && window.mmdManager.currentModel && window.mmdManager.animationModule) {
            if (typeof window.mmdManager.animationModule.stopLipSync === 'function') {
                window.mmdManager.animationModule.stopLipSync();
                console.log('[Audio] MMD 口型同步已停止');
            }
            S.lipSyncActive = false;
        } else if (activeModelType === 'pngtuber' && window.pngtuberManager) {
            if (typeof window.pngtuberManager.stopLipSync === 'function') {
                window.pngtuberManager.stopLipSync();
            }
            S.lipSyncActive = false;
        } else if (window.LanLan1 && window.LanLan1.live2dModel) {
            stopLipSync(window.LanLan1.live2dModel);
        } else {
            S.lipSyncActive = false;
        }
    }

    function maybeFinalizeAssistantSpeech(turnId, options) {
        var force = !!(options && options.force);
        var normalizedTurnId = normalizeAssistantTurnId(
            turnId || S.assistantSpeechActiveTurnId || S.assistantTurnCompletedId
        );
        logAudioLifecycle('maybeFinalizeAssistantSpeech:enter', {
            requestedTurnId: normalizedTurnId,
            force: force
        });
        if (!normalizedTurnId || S.assistantTurnCompletedId !== normalizedTurnId) {
            logAudioLifecycle('maybeFinalizeAssistantSpeech:skip_completion_mismatch', {
                requestedTurnId: normalizedTurnId
            });
            return false;
        }
        if (!isAssistantTurnPlaybackDrained(normalizedTurnId)) {
            logAudioLifecycle('maybeFinalizeAssistantSpeech:skip_not_drained', {
                requestedTurnId: normalizedTurnId
            });
            return false;
        }
        // 队列空了只说明"此刻手里没有音频"。本轮真的放过音频时，这既可能是
        // 阵间空档也可能是流结束，两者在前端完全同构 —— 凭它收尾就是 #1566：
        // 口型停一下又重启、emotion/字幕早触发、后来的尾音成了孤儿（completedId
        // 已被清，没人再收尾）→ isPlaying 卡到 30s 看门狗。等后端的 audio_done，
        // 或等 give-up 计时器到点（force）。
        if (!force &&
            normalizeAssistantTurnId(S.assistantSpeechStartedTurnId) === normalizedTurnId &&
            !isAssistantAudioStreamClosed(normalizedTurnId)) {
            scheduleAudioStreamCloseGiveUp(normalizedTurnId);
            logAudioLifecycle('maybeFinalizeAssistantSpeech:await_audio_done', {
                requestedTurnId: normalizedTurnId
            });
            return false;
        }

        stopActiveLipSync();
        S.isPlaying = false;
        dispatchAssistantSpeechEnd(normalizedTurnId);
        var completionSource = S.assistantTurnCompletionSource;
        clearAssistantTurnCompletion();
        // 这一轮已干净收尾。clearAssistantTurnCompletion 刚把 completedId 清成 null，
        // 但 assistantTurnId 仍指向本轮（要等下条用户消息才清），若不标记 settled，
        // isAssistantTextResponseInFlight 会一直把"已说完的轮"误判成在路上 → 切语音
        // 干等 15s。这里在清空之后再标 settled，记下"turnId 这轮已收尾"。
        S.assistantTurnSettledId = normalizedTurnId;
        logAudioLifecycle('maybeFinalizeAssistantSpeech:completed', {
            requestedTurnId: normalizedTurnId,
            completionSource: completionSource
        });

        if (completionSource !== 'turn_end_agent_callback' && S.isRecording && S.proactiveChatEnabled) {
            if (typeof window.scheduleProactiveChat === 'function') {
                console.log('[ProactiveChat] AI 音频播放完成，重新调度计时器');
                window.scheduleProactiveChat();
            }
        }
        return true;
    }

    let _assistantSpeechLifecycleEventsBound = false;

    function bindAssistantSpeechLifecycleEvents() {
        if (_assistantSpeechLifecycleEventsBound) {
            return;
        }
        _assistantSpeechLifecycleEventsBound = true;

        window.addEventListener('neko-assistant-turn-start', function () {
            clearAssistantTurnCompletion();
            logAudioLifecycle('event:turn-start');
        });

        window.addEventListener('neko-assistant-turn-end', function (event) {
            var turnId = normalizeAssistantTurnId(event.detail && event.detail.turnId);
            var source = event.detail && event.detail.source;
            var speechStartedForTurn = normalizeAssistantTurnId(S.assistantSpeechStartedTurnId) === turnId;
            logAudioLifecycle('event:turn-end', {
                turnId: turnId,
                source: source,
                speechStartedForTurn: speechStartedForTurn
            });
            if (!turnId) {
                return;
            }
            // Some flows only emit the agent callback turn-end before audio drains.
            S.assistantTurnCompletedId = turnId;
            S.assistantTurnCompletionSource = source || null;
            if (!hasAssistantSpeechActivity(turnId)) {
                if (!speechStartedForTurn) {
                    clearAssistantTurnCompletionFallback();
                    logAudioLifecycle('event:turn-end:await_late_speech_start', {
                        turnId: turnId,
                        source: source
                    });
                    return;
                }
                logAudioLifecycle('event:turn-end:defer_finalize_until_speech', {
                    turnId: turnId,
                    source: source
                });
                scheduleAssistantTurnCompletionFallback(turnId, source);
                return;
            }
            maybeFinalizeAssistantSpeech(turnId);
        });

        window.addEventListener('neko-assistant-speech-cancel', function () {
            clearAssistantTurnCompletion();
            clearStuckSpeakingFallback();
            // [BUGFIX] 切换猫娘后语音模式 mic 永远 skip=focus 的根因：
            // 原来只清 turn-tracking 标志，S.isPlaying 留在 true。
            // 切换瞬间 emitAssistantSpeechCancel('character_switch') 被调，
            // 但 S.isPlaying 没人重置，mic workletNode.onmessage 里 focus
            // 模式把每一帧音频都 skip 掉，sent 永远是 0。
            // 这里强制重置 isPlaying，并把 turn-bound 的音频状态一起收尾，
            // 和正常 maybeFinalizeAssistantSpeech 的清理对齐。
            if (S.isPlaying) {
                S.isPlaying = false;
            }
            S.audioStartTime = 0;
            publishSpeechPlaybackState('speech_cancel', {
                active: false,
                speechId: S.currentPlayingSpeechId || null,
                turnId: S.assistantSpeechActiveTurnId || S.assistantTurnId || null,
                scheduledEndAudioTime: S.audioPlayerContext ? S.audioPlayerContext.currentTime : 0,
                remainingSeconds: 0
            });
            try { stopActiveLipSync(); } catch (_e) { /* ignore */ }
        });
    }

    // ======================== Lip-sync smoothing (module-local) ========================
    let _lastMouthOpen = 0;
    let _lipSyncSkipCounter = 0;
    const LIP_SYNC_EVERY_N_FRAMES = 2;

    // ======================== Audio queue management ========================

    function releaseAssistantPlaybackGraph(source) {
        if (!source) return;
        try { source.disconnect(); } catch (_) { /* noop */ }
        try { source._nekoPlaybackGainNode?.disconnect(); } catch (_) { /* noop */ }
        try { source._nekoPlaybackLimiterNode?.disconnect(); } catch (_) { /* noop */ }
        source._nekoPlaybackGainNode = null;
        source._nekoPlaybackLimiterNode = null;
    }

    /**
     * clearAudioQueue — stop all scheduled sources, empty the buffer queue
     * and reset the OGG Opus decoder.
     */
    async function clearAudioQueue() {
        dispatchAssistantSpeechCancel('clear_audio_queue');
        clearAssistantTurnCompletion();
        clearPendingAudioMetaStallTimer();
        clearScheduleAudioChunksTimer();
        S.scheduledSources.forEach(function (source) {
            try { source.stop(); } catch (_) { /* noop */ }
            releaseAssistantPlaybackGraph(source);
        });
        stopActiveLipSync();
        S.scheduledSources = [];
        S.audioBufferQueue = [];
        S.pendingAudioChunkMetaQueue = [];
        S.incomingAudioBlobQueue = [];
        S.processingAudioBlobTurnId = null;
        S.isPlaying = false;
        S.audioStartTime = 0;
        S.nextChunkTime = 0;
        publishSpeechPlaybackState('clear_audio_queue', {
            active: false,
            speechId: null,
            turnId: null,
            scheduledEndAudioTime: S.audioPlayerContext ? S.audioPlayerContext.currentTime : 0,
            remainingSeconds: 0
        });

        await resetOggOpusDecoder();
    }

    /**
     * clearAudioQueueWithoutDecoderReset — same as clearAudioQueue but does NOT
     * reset the decoder.  Used for precise interrupt control so that header info
     * is preserved until the next speech_id arrives.
     */
    function clearAudioQueueWithoutDecoderReset() {
        dispatchAssistantSpeechCancel('clear_audio_queue_without_decoder_reset');
        clearAssistantTurnCompletion();
        clearPendingAudioMetaStallTimer();
        clearScheduleAudioChunksTimer();
        S.scheduledSources.forEach(function (source) {
            try { source.stop(); } catch (_) { /* noop */ }
            releaseAssistantPlaybackGraph(source);
        });
        stopActiveLipSync();
        S.scheduledSources = [];
        S.audioBufferQueue = [];
        S.pendingAudioChunkMetaQueue = [];
        S.incomingAudioBlobQueue = [];
        S.processingAudioBlobTurnId = null;
        S.isPlaying = false;
        S.audioStartTime = 0;
        S.nextChunkTime = 0;
        publishSpeechPlaybackState('clear_audio_queue_without_decoder_reset', {
            active: false,
            speechId: null,
            turnId: null,
            scheduledEndAudioTime: S.audioPlayerContext ? S.audioPlayerContext.currentTime : 0,
            remainingSeconds: 0
        });
        // Note: decoder is NOT reset here.
    }

    // ======================== Global analyser initialisation ========================

    function initializeGlobalAnalyser() {
        if (S.audioPlayerContext) {
            if (S.audioPlayerContext.state === 'suspended') {
                S.audioPlayerContext.resume().catch(function (err) {
                    console.warn('[Audio] resume() failed:', err);
                });
            }
            if (!S.globalAnalyser) {
                try {
                    S.globalAnalyser = S.audioPlayerContext.createAnalyser();
                    S.globalAnalyser.fftSize = 2048;
                    // 频域平滑由这里统一定，不由某个消费者中途改写。
                    //
                    // 这个节点是共享的：Live2D 与 PNGTuber 的口型循环读时域
                    // (getByteTimeDomainData)，五元音共振峰分析器与 MMD 的旧单通道
                    // getLipSyncValue 读频域 (getByteFrequencyData)。按 WebAudio 规范
                    // smoothingTimeConstant 只作用于频域读取，两个时域消费者不受影响。
                    //
                    // 默认值 0.8 对元音判别太钝：实测把它降到 0.5 能让元音切换的响应
                    // 从 4-8 帧回到 2-3 帧（@60fps，约省 42ms），而日语音节本身才
                    // 120-200ms。保留 0.5 而非更激进的值，是仍要压住相邻 bin 间的
                    // 峰值抖动，避免嘴部颤动。
                    S.globalAnalyser.smoothingTimeConstant = 0.5;
                    // Audio graph:
                    //   source -> analyser -> spatialPanner -> spatialDistanceGain -> speakerGain -> destination
                    // spatialPanner / spatialDistanceGain 始终存在；当空间音频关闭时
                    // pan=0 / gain=1 形成 transparent passthrough，避免动态切换图结构。
                    S.spatialPannerNode = S.audioPlayerContext.createStereoPanner();
                    S.spatialPannerNode.pan.value = 0;
                    S.spatialDistanceGainNode = S.audioPlayerContext.createGain();
                    S.spatialDistanceGainNode.gain.value = 1;

                    S.speakerGainNode = S.audioPlayerContext.createGain();
                    var vol = (typeof window.getSpeakerVolume === 'function')
                        ? window.getSpeakerVolume() : 100;
                    S.speakerGainNode.gain.value = vol / 100;

                    S.globalAnalyser.connect(S.spatialPannerNode);
                    S.spatialPannerNode.connect(S.spatialDistanceGainNode);
                    S.spatialDistanceGainNode.connect(S.speakerGainNode);
                    S.speakerGainNode.connect(S.audioPlayerContext.destination);
                    console.log('[Audio] 全局分析器、空间音频与扬声器增益节点已创建并连接');

                    if (window.appSpatialAudio && typeof window.appSpatialAudio.attach === 'function') {
                        window.appSpatialAudio.attach();
                    }
                } catch (e) {
                    console.error('[Audio] 创建分析器失败:', e);
                    // 任意节点构造失败时，把整条链路上的 ref 全部 null 掉，
                    // 让 scheduleAudioChunks 的 hasAnalyser=!!globalAnalyser 路径
                    // 退化为 source.connect(destination) 直连，避免把音频灌进
                    // 一个未连接到 destination 的 dangling analyser 而静音。
                    S.globalAnalyser = null;
                    S.spatialPannerNode = null;
                    S.spatialDistanceGainNode = null;
                    S.speakerGainNode = null;
                }
            }
            // Always sync global references (even when no new nodes were created)
            window.syncAudioGlobals();

            if (window.DEBUG_AUDIO) {
                console.debug('[Audio] globalAnalyser 状态:', !!S.globalAnalyser);
            }
        } else {
            if (window.DEBUG_AUDIO) {
                console.warn('[Audio] audioPlayerContext 未初始化，无法创建分析器');
            }
        }
    }

    // ======================== Lip-sync ========================

    // 定时器驱动时的取消句柄（rAF 驱动时用 S.animationFrameId）
    let _lipSyncPacedCancel = null;

    // 取消已排的口型同步帧：rAF / 定时器两种驱动都覆盖
    function cancelLipSyncFrame() {
        if (S.animationFrameId) {
            cancelAnimationFrame(S.animationFrameId);
            S.animationFrameId = null;
        }
        if (_lipSyncPacedCancel) {
            try { _lipSyncPacedCancel(); } catch (_) {}
            _lipSyncPacedCancel = null;
        }
    }

    // 排下一帧：Electron Pet 里渲染后端切到定时器驱动时，本循环也走定时器（周期同渲染
    // tick），否则这条 rAF 链会单独把 Blink 主帧顶回显示器刷新率。返回是否为定时器驱动。
    function scheduleLipSyncFrame(animate) {
        const pacing = window.nekoFramePacing;
        if (pacing && typeof pacing.requestPacedFrame === 'function' &&
            typeof pacing.currentTimerTickFps === 'function' && pacing.currentTimerTickFps() != null) {
            S.animationFrameId = null;
            _lipSyncPacedCancel = pacing.requestPacedFrame(animate);
            return true;
        }
        _lipSyncPacedCancel = null;
        S.animationFrameId = requestAnimationFrame(animate);
        return false;
    }

    function startLipSync(model, analyser) {
        console.log('[LipSync] 开始口型同步', { hasModel: !!model, hasAnalyser: !!analyser });
        cancelLipSyncFrame();

        _lastMouthOpen = 0;
        _lipSyncSkipCounter = 0;

        var dataArray = new Uint8Array(analyser.fftSize);

        function animate() {
            if (!analyser) return;
            const pacedByTimer = scheduleLipSyncFrame(animate);

            // 定时器驱动时周期已经是渲染 tick（≤ 配置帧率），不再隔帧；rAF 驱动才按
            // LIP_SYNC_EVERY_N_FRAMES 隔帧采样
            if (!pacedByTimer && ++_lipSyncSkipCounter < LIP_SYNC_EVERY_N_FRAMES) return;
            _lipSyncSkipCounter = 0;

            analyser.getByteTimeDomainData(dataArray);

            var sum = 0;
            for (var i = 0; i < dataArray.length; i++) {
                var val = (dataArray[i] - 128) / 128;
                sum += val * val;
            }
            var rms = Math.sqrt(sum / dataArray.length);

            var mouthOpen = Math.min(1, rms * 10);
            mouthOpen = _lastMouthOpen * 0.5 + mouthOpen * 0.5;
            _lastMouthOpen = mouthOpen;

            if (window.LanLan1 && typeof window.LanLan1.setMouth === 'function') {
                window.LanLan1.setMouth(mouthOpen);
            }
        }

        animate();
    }

    function stopLipSync(model) {
        console.log('[LipSync] 停止口型同步');
        cancelLipSyncFrame();
        if (window.LanLan1 && typeof window.LanLan1.setMouth === 'function') {
            window.LanLan1.setMouth(0);
        } else if (model && model.internalModel && model.internalModel.coreModel) {
            // Fallback
            try { model.internalModel.coreModel.setParameterValueById("ParamMouthOpenY", 0); } catch (_) { /* noop */ }
        }
        S.lipSyncActive = false;
    }

    // ======================== Audio chunk scheduling ========================

    // 取消调度链待触发的下一拍（中断/清队列路径用，让停链意图显式化）
    function clearScheduleAudioChunksTimer() {
        if (S.scheduleAudioChunksTimer) {
            clearTimeout(S.scheduleAudioChunksTimer);
            S.scheduleAudioChunksTimer = null;
        }
    }

    function scheduleAudioChunks() {
        if (S.scheduleAudioChunksRunning) return;
        S.scheduleAudioChunksRunning = true;
        // 单飞行：外部直接调用时吞掉已排队的下一拍，避免并存多条 25ms 自续链
        // （旧实现的定时器 id 不保存、无条件自续，每次外部触发都会多叠一条永动链）。
        clearScheduleAudioChunksTimer();

        try {
            var scheduleAheadTime = 5;

            initializeGlobalAnalyser();
            // If init still failed, fall back to connecting sources directly to destination
            var hasAnalyser = !!S.globalAnalyser;

            // Pre-schedule all chunks within the lookahead window.
            // 只在有 chunk 可 schedule 时才 clamp nextChunkTime，
            // 避免空转循环中把 nextChunkTime 无谓前推——对于 qwen-tts 等
            // server_commit 模式 provider，服务端在韵律边界有天然的处理间隙
            // （200-300ms），空转 clamp 会把这个间隙转化为用户可感知的停顿。
            while (S.nextChunkTime < S.audioPlayerContext.currentTime + scheduleAheadTime) {
                if (S.audioBufferQueue.length > 0) {
                    // Clamp: 防止 stale nextChunkTime 导致多个 chunk 被 schedule 到过去
                    // （Web Audio 会同时播放过去时刻的 source），只在真正要 schedule 时才修正。
                    if (S.nextChunkTime < S.audioPlayerContext.currentTime) {
                        S.nextChunkTime = S.audioPlayerContext.currentTime;
                    }
                    var item = S.audioBufferQueue.shift();
                    var nextBuffer = item.buffer;
                    if (window.DEBUG_AUDIO) {
                        console.log('ctx', S.audioPlayerContext.sampleRate,
                            'buf', nextBuffer.sampleRate);
                    }

                    var source = S.audioPlayerContext.createBufferSource();
                    source.buffer = nextBuffer;
                    source._nekoAssistantTurnId = resolveAssistantAudioTurnId(item.turnId, item.speechId);
                    var playbackGain = normalizeAssistantPlaybackGain(item.playbackGain);
                    var playbackGainNode = S.audioPlayerContext.createGain();
                    playbackGainNode.gain.value = playbackGain;
                    source._nekoPlaybackGainNode = playbackGainNode;
                    source.connect(playbackGainNode);
                    var playbackTailNode = playbackGainNode;
                    if (playbackGain > 1 && typeof S.audioPlayerContext.createDynamicsCompressor === 'function') {
                        // Boosted game speech gets a per-source peak limiter so 2x gain
                        // remains usable without altering the user's global speaker mix.
                        var limiter = S.audioPlayerContext.createDynamicsCompressor();
                        limiter.threshold.value = -3;
                        limiter.knee.value = 0;
                        limiter.ratio.value = 20;
                        limiter.attack.value = 0.003;
                        limiter.release.value = 0.1;
                        source._nekoPlaybackLimiterNode = limiter;
                        playbackGainNode.connect(limiter);
                        playbackTailNode = limiter;
                    }
                    if (hasAnalyser) {
                        playbackTailNode.connect(S.globalAnalyser);
                    } else {
                        playbackTailNode.connect(S.audioPlayerContext.destination);
                    }

                    if (source._nekoAssistantTurnId) {
                        dispatchAssistantSpeechStart(source._nekoAssistantTurnId);
                    }

                    if (hasAnalyser && !S.lipSyncActive) {
                        if (window.DEBUG_AUDIO) {
                            console.log('[Audio] 尝试启动口型同步:', {
                                hasLanLan1: !!window.LanLan1,
                                hasLive2dModel: !!(window.LanLan1 && window.LanLan1.live2dModel),
                                hasVrmManager: !!window.vrmManager,
                                hasVrmModel: !!(window.vrmManager && window.vrmManager.currentModel),
                                hasMmdManager: !!window.mmdManager,
                                hasMmdCurrentModel: !!(window.mmdManager && window.mmdManager.currentModel),
                                hasMmdAnimationModule: !!(window.mmdManager && window.mmdManager.animationModule),
                                hasAnalyser: hasAnalyser
                            });
                        }
                        var activeModelType = getActiveAvatarModelType();
                        if (activeModelType === 'vrm' && window.vrmManager && window.vrmManager.currentModel && window.vrmManager.animation) {
                            if (typeof window.vrmManager.animation.startLipSync === 'function') {
                                window.vrmManager.animation.startLipSync(S.globalAnalyser);
                                S.lipSyncActive = true;
                            }
                        } else if (activeModelType === 'mmd' && window.mmdManager && window.mmdManager.currentModel && window.mmdManager.animationModule) {
                            if (typeof window.mmdManager.animationModule.startLipSync === 'function') {
                                window.mmdManager.animationModule.startLipSync(S.globalAnalyser);
                                S.lipSyncActive = true;
                                console.log('[Audio] MMD 口型同步已启动');
                            }
                        } else if (activeModelType === 'pngtuber' && window.pngtuberManager) {
                            if (typeof window.pngtuberManager.startLipSync === 'function') {
                                window.pngtuberManager.startLipSync(S.globalAnalyser);
                                S.lipSyncActive = true;
                                console.log('[Audio] PNGTuber lip sync started');
                            }
                        } else if (window.LanLan1 && window.LanLan1.live2dModel) {
                            startLipSync(window.LanLan1.live2dModel, S.globalAnalyser);
                            S.lipSyncActive = true;
                        } else {
                            if (window.DEBUG_AUDIO) {
                                console.warn('[Audio] 无法启动口型同步：没有可用的模型');
                            }
                        }
                    }

                    var scheduledStartTime = S.nextChunkTime;
                    var scheduledEndTime = scheduledStartTime + nextBuffer.duration;
                    if (source._nekoAssistantTurnId) {
                        if (S.assistantSpeechPlaybackTurnId !== source._nekoAssistantTurnId ||
                            !Number.isFinite(S.assistantSpeechPlaybackStartAudioTime) ||
                            S.assistantSpeechPlaybackStartAudioTime <= 0 ||
                            scheduledStartTime < S.assistantSpeechPlaybackStartAudioTime) {
                            S.assistantSpeechPlaybackTurnId = source._nekoAssistantTurnId;
                            S.assistantSpeechPlaybackStartAudioTime = scheduledStartTime;
                        }
                        S.assistantSpeechPlaybackEndAudioTime = Math.max(
                            Number.isFinite(S.assistantSpeechPlaybackEndAudioTime) ? S.assistantSpeechPlaybackEndAudioTime : 0,
                            scheduledEndTime
                        );
                    }

                    // Precise time scheduling
                    source.start(scheduledStartTime);
                    source._nekoSpeechId = normalizeAssistantTurnId(item.speechId);
                    source._nekoScheduledEndAudioTime = scheduledEndTime;

                    // On-ended callback: handle lip sync stop & cleanup
                    source.onended = (function (src) {
                        return function () {
                            var index = S.scheduledSources.indexOf(src);
                            if (index !== -1) {
                                S.scheduledSources.splice(index, 1);
                            }
                            releaseAssistantPlaybackGraph(src);
                            publishSpeechPlaybackState('source_ended', {
                                active: S.scheduledSources.length > 0 || S.audioBufferQueue.length > 0 || S.incomingAudioBlobQueue.length > 0,
                                speechId: S.currentPlayingSpeechId || src._nekoSpeechId || null,
                                turnId: src._nekoAssistantTurnId || null
                            });
                            var finalized = maybeFinalizeAssistantSpeech(src._nekoAssistantTurnId);
                            // 兜底：finalize 没走通（多半是 turn-end 没到），队列已空但 flag 还粘着 → 30s 后强制收尾。
                            if (!finalized) {
                                maybeArmStuckSpeakingFallback();
                            }
                        };
                    })(source);

                    // Update next chunk time
                    S.nextChunkTime = scheduledEndTime;

                    S.scheduledSources.push(source);
                    publishSpeechPlaybackState('chunk_scheduled', {
                        active: true,
                        speechId: normalizeAssistantTurnId(item.speechId) || S.currentPlayingSpeechId || null,
                        turnId: source._nekoAssistantTurnId || null,
                        playbackTurnId: S.assistantSpeechPlaybackTurnId || null,
                        playbackStartAudioTime: S.assistantSpeechPlaybackStartAudioTime || 0,
                        playbackEndAudioTime: S.assistantSpeechPlaybackEndAudioTime || scheduledEndTime,
                        scheduledEndAudioTime: S.nextChunkTime
                    });
                } else {
                    break;
                }
            }

            // 只在仍有工作时自续（复用 _hasPendingAudioWork：含 meta 队列、解码中的
            // blob、decoder reset 等 in-flight 状态，避免解码窗口期误停链）；
            // 完全空闲时停链，由 handleAudioBlob 在新音频到达时重新拉起。
            // 旧实现无条件自续，首次播放后循环以 40Hz 永久空转（且可叠加多条链）。
            if (_hasPendingAudioWork()) {
                S.scheduleAudioChunksTimer = setTimeout(scheduleAudioChunks, 25);
            }

        } finally {
            S.scheduleAudioChunksRunning = false;
        }
    }

    // ======================== Audio blob handling ========================

    async function handleAudioBlob(blob, expectedEpoch, speechId, turnId, playbackGain) {
        if (expectedEpoch === undefined) expectedEpoch = S.incomingAudioEpoch;

        var arrayBuffer = await blob.arrayBuffer();
        if (expectedEpoch !== S.incomingAudioEpoch) {
            return;
        }
        if (!arrayBuffer || arrayBuffer.byteLength === 0) {
            console.warn('收到空的音频数据，跳过处理');
            return;
        }

        await ensureAudioPlayerContext();
        if (expectedEpoch !== S.incomingAudioEpoch) {
            return;
        }

        if (S.audioPlayerContext.state === 'suspended') {
            await S.audioPlayerContext.resume();
            if (expectedEpoch !== S.incomingAudioEpoch) {
                return;
            }
        }

        // Detect OGG format (magic number "OggS" = 0x4F 0x67 0x67 0x53)
        var header = new Uint8Array(arrayBuffer, 0, 4);
        var isOgg = header[0] === 0x4F && header[1] === 0x67 && header[2] === 0x67 && header[3] === 0x53;

        var float32Data;
        var sampleRate = 48000;

        if (isOgg) {
            // OGG OPUS: decode with WASM streaming decoder
            try {
                var result = await decodeOggOpusChunk(new Uint8Array(arrayBuffer));
                if (expectedEpoch !== S.incomingAudioEpoch) {
                    return;
                }
                if (!result) {
                    // Not enough data yet
                    return;
                }
                float32Data = result.float32Data;
                sampleRate = result.sampleRate;
            } catch (e) {
                console.error('OGG OPUS 解码失败:', e);
                return;
            }
        } else {
            // PCM Int16: direct conversion
            var int16Array = new Int16Array(arrayBuffer);
            float32Data = new Float32Array(int16Array.length);
            for (var i = 0; i < int16Array.length; i++) {
                float32Data[i] = int16Array[i] / 32768.0;
            }
        }

        if (!float32Data || float32Data.length === 0) {
            return;
        }
        if (expectedEpoch !== S.incomingAudioEpoch) {
            return;
        }

        var audioBuffer = S.audioPlayerContext.createBuffer(1, float32Data.length, sampleRate);
        audioBuffer.copyToChannel(float32Data, 0);

        var bufferObj = {
            seq: S.seqCounter++,
            buffer: audioBuffer,
            turnId: resolveAssistantAudioTurnId(turnId, speechId),
            speechId: normalizeAssistantTurnId(speechId),
            playbackGain: normalizeAssistantPlaybackGain(playbackGain)
        };
        S.audioBufferQueue.push(bufferObj);

        var j = S.audioBufferQueue.length - 1;
        while (j > 0 && S.audioBufferQueue[j].seq < S.audioBufferQueue[j - 1].seq) {
            var tmp = S.audioBufferQueue[j];
            S.audioBufferQueue[j] = S.audioBufferQueue[j - 1];
            S.audioBufferQueue[j - 1] = tmp;
            j--;
        }

        if (!S.isPlaying) {
            var gap = (S.seqCounter <= 1) ? 0.03 : 0;
            S.nextChunkTime = Math.max(
                S.audioPlayerContext.currentTime + gap,
                S.nextChunkTime
            );
            S.isPlaying = true;
            scheduleAudioChunks();
        } else if (!S.scheduleAudioChunksTimer && !S.scheduleAudioChunksRunning) {
            // isPlaying=true 但调度链已因队列见底而停止（流式间隙）：
            // 新 chunk 到达时必须重新拉起，否则后续音频永远不会被调度。
            scheduleAudioChunks();
        }
    }

    // ======================== Incoming audio blob queue ========================

    function enqueueIncomingAudioBlob(blob) {
        pruneStalledPendingAudioMetaQueue(Date.now());
        var meta = null;
        while (S.pendingAudioChunkMetaQueue.length > 0) {
            meta = S.pendingAudioChunkMetaQueue.shift();
            if (!meta) {
                continue;
            }
            if (meta.shouldSkip) {
                logAudioLifecycle('enqueueIncomingAudioBlob:discard_skip_meta', {
                    turnId: meta.turnId || null,
                    speechId: meta.speechId || null
                });
                meta = null;
                continue;
            }
            break;
        }
        schedulePendingAudioMetaStallCheck();
        if (!meta) {
            logAudioLifecycle('enqueueIncomingAudioBlob:missing_meta');
            if (window.DEBUG_AUDIO) {
                console.warn('[Audio] 收到无匹配 header 的音频 blob，已丢弃');
            }
            return;
        }
        if (!meta.speechId) {
            logAudioLifecycle('enqueueIncomingAudioBlob:missing_speech_id', {
                turnId: meta.turnId || null
            });
            if (window.DEBUG_AUDIO) {
                console.warn('[Audio] 收到 speechId 为空的音频 blob，已丢弃');
            }
            return;
        }
        logAudioLifecycle('enqueueIncomingAudioBlob', {
            turnId: meta.turnId || null,
            speechId: meta.speechId,
            shouldSkip: !!meta.shouldSkip
        });
        S.incomingAudioBlobQueue.push({
            blob: blob,
            shouldSkip: !!meta.shouldSkip,
            speechId: meta.speechId,
            turnId: resolveAssistantAudioTurnId(meta.turnId, meta.speechId),
            playbackGain: normalizeAssistantPlaybackGain(meta.playbackGain),
            epoch: meta.epoch
        });
        if (!S.isProcessingIncomingAudioBlob) {
            void processIncomingAudioBlobQueue();
        }
    }

    async function processIncomingAudioBlobQueue() {
        if (S.isProcessingIncomingAudioBlob) return;
        S.isProcessingIncomingAudioBlob = true;

        try {
            while (S.incomingAudioBlobQueue.length > 0) {
                var item = S.incomingAudioBlobQueue.shift();
                S.processingAudioBlobTurnId = null;
                if (!item) continue;
                if (item.epoch !== S.incomingAudioEpoch) {
                    continue;
                }

                if (item.shouldSkip) {
                    logAudioLifecycle('processIncomingAudioBlobQueue:skip_item', {
                        turnId: item.turnId || null,
                        speechId: item.speechId
                    });
                    if (window.DEBUG_AUDIO) {
                        console.log('[Audio] 跳过被打断的音频 blob', item.speechId);
                    }
                    continue;
                }

                // 出队之后、解码完成之前这个 chunk 不在任何队列里。记下它属于哪一轮，
                // 让 isAssistantTurnPlaybackDrained 看得见这段空洞。
                S.processingAudioBlobTurnId = item.turnId || null;

                if (S.decoderResetPromise) {
                    var resetTask = S.decoderResetPromise;
                    try {
                        await resetTask;
                    } catch (e) {
                        console.warn('等待 OGG OPUS 解码器重置失败:', e);
                    } finally {
                        // Only clear current task; avoid overwriting a newly-set promise
                        if (S.decoderResetPromise === resetTask) {
                            S.decoderResetPromise = null;
                        }
                    }
                }
                if (item.epoch !== S.incomingAudioEpoch) {
                    continue;
                }

                await handleAudioBlob(
                    item.blob,
                    item.epoch,
                    item.speechId,
                    item.turnId,
                    item.playbackGain
                );
                logAudioLifecycle('processIncomingAudioBlobQueue:handled', {
                    turnId: item.turnId || null,
                    speechId: item.speechId
                });
            }
        } finally {
            S.processingAudioBlobTurnId = null;
            S.isProcessingIncomingAudioBlob = false;
            maybeFinalizeAssistantSpeech();
            schedulePendingAudioMetaStallCheck();
            if (S.incomingAudioBlobQueue.length > 0) {
                void processIncomingAudioBlobQueue();
            }
        }
    }

    // ======================== Speaker volume control ========================

    function saveSpeakerVolumeSetting() {
        try {
            localStorage.setItem('neko_speaker_volume', String(S.speakerVolume));
            console.log('扬声器音量设置已保存: ' + S.speakerVolume + '%');
        } catch (err) {
            console.error('保存扬声器音量设置失败:', err);
        }
    }

    function loadSpeakerVolumeSetting() {
        try {
            var saved = localStorage.getItem('neko_speaker_volume');
            if (saved !== null) {
                var vol = parseInt(saved, 10);
                if (!isNaN(vol) && vol >= 0 && vol <= C.MAX_SPEAKER_VOLUME) {
                    S.speakerVolume = vol;
                    console.log('已加载扬声器音量设置: ' + S.speakerVolume + '%');
                } else {
                    console.warn('无效的扬声器音量值 ' + saved + '，使用默认值 ' + C.DEFAULT_SPEAKER_VOLUME + '%');
                    S.speakerVolume = C.DEFAULT_SPEAKER_VOLUME;
                }
            } else {
                console.log('未找到扬声器音量设置，使用默认值 ' + C.DEFAULT_SPEAKER_VOLUME + '%');
                S.speakerVolume = C.DEFAULT_SPEAKER_VOLUME;
            }

            // Apply immediately to audio pipeline if already initialised
            if (S.speakerGainNode) {
                S.speakerGainNode.gain.setTargetAtTime(S.speakerVolume / 100, S.speakerGainNode.context.currentTime, 0.05);
            }
        } catch (err) {
            console.error('加载扬声器音量设置失败:', err);
            S.speakerVolume = C.DEFAULT_SPEAKER_VOLUME;
        }
    }

    // ======================== Window-level backward-compat exports ========================

    window.setSpeakerVolume = function (vol) {
        if (vol >= 0 && vol <= C.MAX_SPEAKER_VOLUME) {
            S.speakerVolume = vol;
            if (S.speakerGainNode) {
                S.speakerGainNode.gain.setTargetAtTime(vol / 100, S.speakerGainNode.context.currentTime, 0.05);
            }
            saveSpeakerVolumeSetting();
            // Update UI slider if it exists — slider 走非线性轨道(0..1000)，需反向映射；
            // 色值与 app-audio-capture.js 的 applySpeakerVolumeVisual 保持一致
            var slider = document.getElementById('speaker-volume-slider');
            var valueDisplay = document.getElementById('speaker-volume-value');
            var color = vol > C.DEFAULT_SPEAKER_VOLUME ? '#ff9f43' : '#4f8cff';
            if (slider) {
                slider.value = String(Math.round(window.appUtils.valueToKneeTrack(
                    vol, C.DEFAULT_SPEAKER_VOLUME, C.MAX_SPEAKER_VOLUME, C.SPEAKER_VOLUME_KNEE_RATIO
                ) * 1000));
                slider.style.accentColor = color;
            }
            if (valueDisplay) {
                valueDisplay.textContent = vol + '%';
                valueDisplay.style.color = color;
            }
            console.log('扬声器音量已设置: ' + vol + '%');
        }
    };

    window.getSpeakerVolume = function () {
        return S.speakerVolume;
    };

    // ======================== Module exports ========================

    mod.clearAudioQueue = clearAudioQueue;
    mod.clearAudioQueueWithoutDecoderReset = clearAudioQueueWithoutDecoderReset;
    mod.initializeGlobalAnalyser = initializeGlobalAnalyser;
    mod.startLipSync = startLipSync;
    mod.stopLipSync = stopLipSync;
    mod.scheduleAudioChunks = scheduleAudioChunks;
    mod.normalizeAssistantPlaybackGain = normalizeAssistantPlaybackGain;
    mod.handleAudioBlob = handleAudioBlob;
    mod.enqueueIncomingAudioBlob = enqueueIncomingAudioBlob;
    mod.processIncomingAudioBlobQueue = processIncomingAudioBlobQueue;
    mod.schedulePendingAudioMetaStallCheck = schedulePendingAudioMetaStallCheck;
    mod.rememberAssistantAudioSpeechTurn = rememberAssistantAudioSpeechTurn;
    mod.noteAssistantAudioStreamClosed = noteAssistantAudioStreamClosed;
    mod.ensureAudioPlayerContext = ensureAudioPlayerContext;
    mod.applySpeakerDeviceToContext = applySpeakerDeviceToContext;
    mod.selectSpeakerDevice = selectSpeakerDevice;
    mod.loadSelectedSpeaker = loadSelectedSpeaker;
    mod.reconcileSelectedSpeakerDevices = reconcileSelectedSpeakerDevices;
    mod.saveSpeakerVolumeSetting = saveSpeakerVolumeSetting;
    mod.loadSpeakerVolumeSetting = loadSpeakerVolumeSetting;

    bindAssistantSpeechLifecycleEvents();

    // Backward-compatible window globals so existing callers keep working
    window.clearAudioQueue = clearAudioQueue;
    window.clearAudioQueueWithoutDecoderReset = clearAudioQueueWithoutDecoderReset;
    window.initializeGlobalAnalyser = initializeGlobalAnalyser;
    window.startLipSync = startLipSync;
    window.stopLipSync = stopLipSync;
    window.scheduleAudioChunks = scheduleAudioChunks;
    window.handleAudioBlob = handleAudioBlob;
    window.enqueueIncomingAudioBlob = enqueueIncomingAudioBlob;
    window.processIncomingAudioBlobQueue = processIncomingAudioBlobQueue;
    window.schedulePendingAudioMetaStallCheck = schedulePendingAudioMetaStallCheck;
    window.ensureAudioPlayerContext = ensureAudioPlayerContext;
    window.selectSpeakerDevice = selectSpeakerDevice;
    window.loadSelectedSpeaker = loadSelectedSpeaker;
    window.reconcileSelectedSpeakerDevices = reconcileSelectedSpeakerDevices;
    window.saveSpeakerVolumeSetting = saveSpeakerVolumeSetting;
    window.loadSpeakerVolumeSetting = loadSpeakerVolumeSetting;

    window.appAudioPlayback = mod;
})();
