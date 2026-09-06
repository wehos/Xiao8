/**
 * app-audio-capture.js — 麦克风捕获 / 释放 / 增益 / 静音检测 / 音量可视化
 *
 * 依赖：app-state.js（window.appState / window.appConst / window.appUtils）
 *
 * 导出：window.appAudioCapture
 */
(function () {
    'use strict';

    const mod = {};
    const S = window.appState;
    const C = window.appConst;
    const MIC_LEASE = Object.freeze({
        NONE: 'none',
        GAME: 'game',
        CORE: 'core'
    });
    let voiceLeaseGeneration = 0;
    let lastVoiceLeaseFingerprint = '';
    // Cancellation token for an IN-FLIGHT microphone start (Codex P2).
    // S.isRecording only flips at the very END of startAudioWorklet, after
    // getUserMedia() and audioWorklet.addModule() have both awaited — so every
    // "stop the mic" guard keyed on S.isRecording === true is a no-op for the
    // whole startup window. The pending start then completed anyway, set
    // recording true and re-claimed through refreshMicLease() the very lease the
    // backend had just revoked, uploading PCM into a blocked route. Bumping this
    // invalidates any start still inside that window.
    //
    // The token each attempt compares against is a LOCAL const in
    // startMicCapture, deliberately not a module field. A module-level
    // "pending token" is re-armed by the NEXT startMicCapture: attempt #1 is
    // invalidated (generation moves past its token), attempt #2 then writes
    // both the token and the generation to the same new value, and #1's guard
    // compares equal again and commits -- re-claiming through refreshMicLease()
    // the exact lease this counter exists to protect.
    let micStartGeneration = 0;
    // Shared microphone controls can be painted before the capture pipeline
    // commits. Keep exactly one bounded owner token so a stale attempt cannot
    // restore the composer or clear the recording affordance while a newer
    // attempt is still pending. The owner is released by that attempt's
    // finally block on every success, cancellation and failure path.
    let pendingMicStartUiOwnerToken = null;
    // Device ids are not a sufficient change token: a rapid A -> B -> A
    // sequence ends at the same id while still superseding the in-flight
    // selection attempt. Increment this on every authoritative selection write
    // so async ownership checks cannot lose intermediate changes.
    let microphoneSelectionGeneration = 0;

    function invalidatePendingMicStart() {
        micStartGeneration += 1;
    }

    function setSelectedMicrophoneId(deviceId) {
        S.selectedMicrophoneId = deviceId;
        microphoneSelectionGeneration += 1;
    }

    function currentVoiceInputControlState() {
        return {
            owner: resolveMicLeaseOwner(),
            hard_muted: S.isMicMuted === true,
            focus_suppressed: (
                S.focusModeEnabled === true
                && S.isPlaying === true
            ),
            // Engagement marker for the backend voice-connection claim gate:
            // the onopen force-sync also fires from windows that merely
            // opened (second /chat_full window). A snapshot stamped
            // engaged: false is provably passive — neither recording nor
            // starting a voice session — and must not steal the voice
            // connection identity from a window that is actively recording.
            engaged: (
                S.isRecording === true
                || S.voiceStartPending === true
                || window.isMicStarting === true
            )
        };
    }

    function sendVoiceInputControlState(force) {
        if (!S.socket || S.socket.readyState !== WebSocket.OPEN) return false;
        const state = currentVoiceInputControlState();
        const fingerprint = JSON.stringify(state);
        if (force !== true && fingerprint === lastVoiceLeaseFingerprint) return true;
        voiceLeaseGeneration += 1;
        S.socket.send(JSON.stringify({
            action: 'voice_input_control',
            event: 'lease_sync',
            owner: state.owner,
            hard_muted: state.hard_muted,
            focus_suppressed: state.focus_suppressed,
            engaged: state.engaged,
            lease_generation: voiceLeaseGeneration
        }));
        lastVoiceLeaseFingerprint = fingerprint;
        return true;
    }

    function syncVoiceInputControlState(socket) {
        if (socket && socket !== S.socket) return false;
        if (!S.socket || S.socket.readyState !== WebSocket.OPEN) return false;

        // 每条 WebSocket 都有独立的 generation scope；第一条消息就是完整状态。
        voiceLeaseGeneration = 0;
        lastVoiceLeaseFingerprint = '';
        return sendVoiceInputControlState(true);
    }

    function setVoiceInputLifecycleState(state) {
        const allowed = new Set([
            'off', 'local_listen', 'prewarming', 'active', 'draining',
            'warm_idle', 'deep_sleep', 'backoff', 'blocked', 'suspended'
        ]);
        if (!allowed.has(state) || S.voiceInputLifecycleState === state) return;
        S.voiceInputLifecycleState = state;
        document.documentElement.setAttribute('data-voice-input-state', state);
        window.dispatchEvent(new CustomEvent('voice-input-lifecycle-changed', {
            detail: { state, route_mode: S.independentAsrActive ? 'independent' : 'blocked' }
        }));
    }

    window.addEventListener('voice-input-socket-open', function (event) {
        syncVoiceInputControlState(event && event.detail && event.detail.socket);
    });

    function setMicLeaseOwner(owner) {
        if (!Object.values(MIC_LEASE).includes(owner)) {
            throw new Error(`Invalid microphone lease owner: ${owner}`);
        }
        if (S.micLeaseOwner !== owner) {
            S.micLeaseOwner = owner;
            window.dispatchEvent(new CustomEvent('mic-lease-changed', {
                detail: { owner }
            }));
        }
        if (owner === MIC_LEASE.NONE || S.isMicMuted === true) {
            setVoiceInputLifecycleState('off');
        } else if (owner === MIC_LEASE.GAME) {
            setVoiceInputLifecycleState('suspended');
        } else if (
            S.voiceInputLifecycleState === 'off'
            || S.voiceInputLifecycleState === 'suspended'
        ) {
            setVoiceInputLifecycleState('local_listen');
        }
        sendVoiceInputControlState(false);
        return owner;
    }

    function resolveMicLeaseOwner() {
        if (!S.isRecording) return MIC_LEASE.NONE;
        if (S.gameVoiceSttGateActive) return MIC_LEASE.GAME;
        return MIC_LEASE.CORE;
    }

    function refreshMicLease() {
        // setMicLeaseOwner() already sends the (fingerprint-deduped) control
        // snapshot — no extra send here.
        return setMicLeaseOwner(resolveMicLeaseOwner());
    }

    function canUploadOrdinaryMicFrame() {
        if (refreshMicLease() !== MIC_LEASE.CORE) return false;
        const state = currentVoiceInputControlState();
        return !state.hard_muted && !state.focus_suppressed;
    }

    // ======================== DOM 辅助 ========================

    function micButton()          { return document.getElementById('micButton'); }
    function muteButton()         { return document.getElementById('muteButton'); }
    function screenButton()       { return document.getElementById('screenButton'); }
    function stopButton()         { return document.getElementById('stopButton'); }
    function resetSessionButton() { return document.getElementById('resetSessionButton'); }
    function statusElement()      { return document.getElementById('status'); }

    // ======================== 屏幕共享开关按钮（设置面板内嵌） ========================
    // 开关按钮从屏幕源子窗口底部移到「屏幕共享」与「选择麦克风」两个设置项中间；
    // 启用时由滑块起点扩散蓝色波面，填满后浮出少量四角星光。
    // 共享状态以隐藏的 #screenButton 的 .active class 为准（见 common_ui.js）。

    var shareToggleButtonRegistry = [];
    var shareToggleStateObserver = null;
    var shareToggleObserverRetryTimer = null;

    function isScreenShareActive() {
        var btn = screenButton();
        return !!(btn && btn.classList.contains('active'));
    }

    function injectShareToggleStyles() {
        if (document.getElementById('neko-share-toggle-styles')) return;
        var style = document.createElement('style');
        style.id = 'neko-share-toggle-styles';
        style.textContent = [
            // 未启用：白色胶囊轨道（仿参考视频）
            '.neko-share-toggle-btn{position:relative;isolation:isolate;overflow:visible;width:100%;box-sizing:border-box;min-height:44px;padding:10px 48px;margin:4px 0 6px;border:1px solid rgba(0,0,0,.07);border-radius:999px;background:#f4f4f7;color:var(--neko-popup-text,#333);cursor:pointer;font-size:14px;font-weight:600;pointer-events:auto;transition:color .2s ease,box-shadow .2s ease,transform .1s ease;--neko-share-wave-x:20px;--neko-share-wave-radius:148%;}',
            '.neko-share-toggle-btn:hover{box-shadow:inset 0 0 0 1px rgba(0,0,0,.05);}',
            '.neko-share-toggle-btn:focus-visible{outline:2px solid var(--neko-popup-accent,#44b7fe);outline-offset:2px;}',
            '.neko-share-toggle-btn:active{transform:scale(.97);}',
            '.neko-share-toggle-btn:disabled{opacity:.6;cursor:default;}',
            // 未开语音会话时点击的抖动提示（明确反馈「点到了但不能用」）
            '@keyframes nekoShareToggleNudge{0%,100%{transform:translateX(0);}25%{transform:translateX(-3px);}75%{transform:translateX(3px);}}',
            '.neko-share-toggle-btn.is-nudged{animation:nekoShareToggleNudge .12s ease 2;}',
            '.neko-share-toggle-btn.is-active{color:#fff;}',
            // 蓝色波面：以未开启时滑块中心为圆心，向外扩散并填满整个胶囊。
            '.neko-share-toggle-btn .neko-share-toggle-wave{position:absolute;inset:0;z-index:0;border-radius:inherit;background:linear-gradient(105deg,#61ccff 0%,#44b7fe 52%,#269fe8 100%);clip-path:circle(0 at var(--neko-share-wave-x) 50%);pointer-events:none;transition:clip-path .64s cubic-bezier(.4,0,.2,1);}',
            '.neko-share-toggle-btn.is-active .neko-share-toggle-wave{clip-path:circle(var(--neko-share-wave-radius) at var(--neko-share-wave-x) 50%);}',
            '.neko-share-toggle-btn .neko-share-toggle-label{position:relative;z-index:1;display:block;text-align:center;pointer-events:none;transition:color .2s ease;}',
            '.neko-share-toggle-btn.is-active .neko-share-toggle-label{transition:color .2s ease .16s;}',
            // 白色滑块：默认在左端，启用后滑到右端。
            '.neko-share-toggle-btn .neko-share-toggle-knob{position:absolute;z-index:3;top:4px;bottom:4px;left:4px;width:32px;border-radius:10px;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.18);pointer-events:none;transition:left .46s cubic-bezier(.4,0,.2,1);}',
            '.neko-share-toggle-btn.is-active .neko-share-toggle-knob{left:calc(100% - 36px);}',
            // 四角星光：波面填满后从背景中向上浮出，SVG 轮廓参考产品给定的星型。
            '.neko-share-toggle-btn .neko-share-toggle-sparkles{position:absolute;inset:0;z-index:2;overflow:visible;pointer-events:none;}',
            '.neko-share-toggle-btn .neko-share-toggle-spark{position:absolute;left:var(--neko-spark-x);bottom:var(--neko-spark-y);width:var(--neko-spark-size);height:var(--neko-spark-size);opacity:0;pointer-events:none;}',
            '.neko-share-toggle-btn .neko-share-toggle-spark svg{display:block;width:100%;height:100%;overflow:visible;}',
            '@keyframes nekoShareSparkRise{0%{opacity:0;transform:translate3d(0,4px,0) scale(.2) rotate(0deg);}18%{opacity:1;}68%{opacity:.92;}100%{opacity:0;transform:translate3d(var(--neko-spark-drift),var(--neko-spark-rise),0) scale(.92) rotate(18deg);}}',
            '.neko-share-toggle-btn.is-sparkling .neko-share-toggle-spark{animation:nekoShareSparkRise var(--neko-spark-duration) cubic-bezier(.16,.8,.25,1) var(--neko-spark-delay) both;}',
            // 迷你版：嵌在「屏幕共享」设置行右侧的行内胶囊开关（未开启为灰色轨道 + 白色旋钮）
            '.neko-share-toggle-btn.neko-share-toggle-mini{display:inline-block;width:64px;min-height:26px;height:26px;padding:0;margin:0;flex-shrink:0;align-self:center;cursor:pointer;background:#e2e2e8;border-color:rgba(0,0,0,.05);--neko-share-wave-x:12px;--neko-share-wave-radius:116%;}',
            '.neko-share-toggle-mini .neko-share-toggle-label{display:none;}',
            '.neko-share-toggle-mini .neko-share-toggle-knob{width:18px;top:3px;bottom:3px;left:3px;border-radius:7px;}',
            '.neko-share-toggle-mini.is-active .neko-share-toggle-knob{left:calc(100% - 21px);}',
            '.neko-share-toggle-btn.is-instant .neko-share-toggle-wave,.neko-share-toggle-btn.is-instant .neko-share-toggle-label,.neko-share-toggle-btn.is-instant .neko-share-toggle-knob{transition:none!important;}',
            '@media (prefers-reduced-motion:reduce){.neko-share-toggle-btn .neko-share-toggle-wave,.neko-share-toggle-btn .neko-share-toggle-label,.neko-share-toggle-btn .neko-share-toggle-knob{transition:none!important;}.neko-share-toggle-btn.is-sparkling .neko-share-toggle-spark{animation:none!important;}}',
            '.neko-share-toggle-btn.is-busy{opacity:.6;cursor:default;}'
        ].join('\n');
        document.head.appendChild(style);
    }

    var SHARE_WAVE_FILL_MS = 640;
    var SHARE_SPARKLE_LIFETIME_MS = 1200;
    var SHARE_SPARKLE_CONFIGS = [
        { x: '8%', y: '4px', size: '8px', drift: '-4px', rise: '-21px', delay: '0ms', duration: '760ms' },
        { x: '28%', y: '10px', size: '6px', drift: '1px', rise: '-28px', delay: '110ms', duration: '800ms' },
        { x: '48%', y: '3px', size: '10px', drift: '-2px', rise: '-24px', delay: '40ms', duration: '820ms' },
        { x: '68%', y: '7px', size: '7px', drift: '3px', rise: '-30px', delay: '150ms', duration: '780ms' },
        { x: '88%', y: '4px', size: '9px', drift: '1px', rise: '-22px', delay: '80ms', duration: '840ms' }
    ];

    function createShareSparkleLayer() {
        var layer = document.createElement('span');
        layer.className = 'neko-share-toggle-sparkles';
        layer.setAttribute('aria-hidden', 'true');

        SHARE_SPARKLE_CONFIGS.forEach(function (config) {
            var sparkle = document.createElement('span');
            sparkle.className = 'neko-share-toggle-spark';
            sparkle.style.setProperty('--neko-spark-x', config.x);
            sparkle.style.setProperty('--neko-spark-y', config.y);
            sparkle.style.setProperty('--neko-spark-size', config.size);
            sparkle.style.setProperty('--neko-spark-drift', config.drift);
            sparkle.style.setProperty('--neko-spark-rise', config.rise);
            sparkle.style.setProperty('--neko-spark-delay', config.delay);
            sparkle.style.setProperty('--neko-spark-duration', config.duration);

            var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.setAttribute('viewBox', '0 0 24 24');
            var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            path.setAttribute('d', 'M12 1.5C13.6 7.9 16.1 10.4 22.5 12C16.1 13.6 13.6 16.1 12 22.5C10.4 16.1 7.9 13.6 1.5 12C7.9 10.4 10.4 7.9 12 1.5Z');
            path.setAttribute('fill', 'rgba(255,255,255,0.96)');
            path.setAttribute('stroke', '#00aeef');
            path.setAttribute('stroke-width', '0.9');
            path.setAttribute('stroke-linejoin', 'round');
            path.setAttribute('vector-effect', 'non-scaling-stroke');
            svg.appendChild(path);
            sparkle.appendChild(svg);
            layer.appendChild(sparkle);
        });

        return layer;
    }

    function createShareWaveFx(button) {
        var sparkleStartTimer = null;
        var sparkleCleanupTimer = null;

        function prefersReducedMotion() {
            return typeof window.matchMedia === 'function'
                && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        }

        function stopSparkles() {
            if (sparkleStartTimer) { clearTimeout(sparkleStartTimer); sparkleStartTimer = null; }
            if (sparkleCleanupTimer) { clearTimeout(sparkleCleanupTimer); sparkleCleanupTimer = null; }
            button.classList.remove('is-sparkling');
        }

        function startSparkles() {
            sparkleStartTimer = null;
            if (!button.isConnected || !button._nekoShareActive || prefersReducedMotion()) return;
            button.classList.remove('is-sparkling');
            void button.offsetWidth;
            button.classList.add('is-sparkling');
            sparkleCleanupTimer = setTimeout(function () {
                sparkleCleanupTimer = null;
                button.classList.remove('is-sparkling');
            }, SHARE_SPARKLE_LIFETIME_MS);
        }

        function setActiveClass(active, instant) {
            if (instant) button.classList.add('is-instant');
            button.classList.toggle('is-active', active);
            if (instant) {
                void button.offsetWidth;
                button.classList.remove('is-instant');
            }
        }

        return {
            activate: function (instant) {
                stopSparkles();
                setActiveClass(true, instant);
                if (!instant && !prefersReducedMotion()) {
                    sparkleStartTimer = setTimeout(startSparkles, SHARE_WAVE_FILL_MS);
                }
            },
            deactivate: function (instant) {
                stopSparkles();
                setActiveClass(false, instant);
            },
            cleanup: stopSparkles
        };
    }

    function pruneShareToggleButtons() {
        var connectedButtons = [];
        shareToggleButtonRegistry.forEach(function (btn) {
            if (btn.isConnected) {
                connectedButtons.push(btn);
            } else if (typeof btn._nekoShareFxCleanup === 'function') {
                btn._nekoShareFxCleanup();
            }
        });
        shareToggleButtonRegistry = connectedButtons;
    }

    function syncShareToggleButtons(instant) {
        pruneShareToggleButtons();
        var active = isScreenShareActive();
        shareToggleButtonRegistry.forEach(function (btn) {
            if (typeof btn._nekoSetShareActive === 'function') btn._nekoSetShareActive(active, !!instant);
        });
    }

    function ensureShareToggleStateObserver() {
        if (shareToggleStateObserver) return;
        var target = screenButton();
        if (!target) {
            if (!shareToggleObserverRetryTimer) {
                shareToggleObserverRetryTimer = setTimeout(function () {
                    shareToggleObserverRetryTimer = null;
                    ensureShareToggleStateObserver();
                }, 500);
            }
            return;
        }
        shareToggleStateObserver = new MutationObserver(function () { syncShareToggleButtons(false); });
        shareToggleStateObserver.observe(target, { attributes: true, attributeFilter: ['class'] });
    }

    function createScreenShareToggleButton(options) {
        injectShareToggleStyles();
        ensureShareToggleStateObserver();

        var mini = !!(options && options.mini);
        // The control is a sibling of the action trigger in the shared row, so
        // it can use native button semantics without nesting interactive UI.
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'neko-share-toggle-btn' + (mini ? ' neko-share-toggle-mini' : '');
        button.setAttribute('aria-busy', 'false');
        button.dataset.nekoScreenShareAction = 'toggle';

        var wave = document.createElement('span');
        wave.className = 'neko-share-toggle-wave';
        wave.setAttribute('aria-hidden', 'true');

        var label = document.createElement('span');
        label.className = 'neko-share-toggle-label';

        var sparkles = createShareSparkleLayer();

        var knob = document.createElement('span');
        knob.className = 'neko-share-toggle-knob';
        knob.setAttribute('aria-hidden', 'true');

        button.appendChild(wave);
        button.appendChild(label);
        button.appendChild(sparkles);
        button.appendChild(knob);

        function shareLabel() { return window.t ? window.t('buttons.screenShare') : 'Screen Share'; }
        function stopLabel() { return window.t ? window.t('voiceControl.stopShare') : 'Stop Sharing'; }

        var waveFx = createShareWaveFx(button);
        button._nekoShareFxCleanup = waveFx.cleanup;
        button._nekoSetShareActive = function (active, instant) {
            var accessibleLabel = active ? stopLabel() : shareLabel();
            label.textContent = accessibleLabel;
            button.title = accessibleLabel;
            button.setAttribute('aria-label', accessibleLabel);
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
            if (button._nekoShareActive === active) return;
            button._nekoShareActive = active;
            if (active) {
                waveFx.activate(!!instant);
            } else {
                waveFx.deactivate(!!instant);
            }
        };

        var shareToggleOperationGeneration = 0;

        function setShareToggleBusy(busy) {
            button._nekoShareBusy = busy;
            if (busy) {
                button.classList.add('is-busy');
                button.setAttribute('aria-busy', 'true');
            } else {
                button.classList.remove('is-busy');
                button.setAttribute('aria-busy', 'false');
            }
        }

        function finishShareToggleOperation(generation) {
            // A cancelled browser picker can settle after a replacement start.
            // Its old finally must not clear the replacement operation's state.
            if (shareToggleOperationGeneration !== generation) return;
            setShareToggleBusy(false);
            syncShareToggleButtons(false);
        }

        async function handleToggleClick(event) {
            event.stopPropagation();
            var startPending = typeof window.isScreenSharingStartPending === 'function'
                && window.isScreenSharingStartPending();
            if (startPending && typeof window.stopScreenSharing === 'function') {
                if (button._nekoShareCancelBusy) return;
                var cancelGeneration = ++shareToggleOperationGeneration;
                button._nekoShareCancelBusy = true;
                setShareToggleBusy(true);
                try {
                    await window.stopScreenSharing();
                } catch (e) {
                    console.warn('[屏幕共享开关] 取消待处理启动失败:', e);
                } finally {
                    if (shareToggleOperationGeneration === cancelGeneration) {
                        button._nekoShareCancelBusy = false;
                    }
                    finishShareToggleOperation(cancelGeneration);
                }
                return;
            }
            if (button._nekoShareBusy) return;
            var active = isScreenShareActive();
            console.log('[屏幕共享开关] 点击, 当前状态:', active ? '共享中' : '未共享', ', 语音会话:', !!window.isRecording);
            var operationGeneration = ++shareToggleOperationGeneration;
            setShareToggleBusy(true);
            try {
                if (active && typeof window.stopScreenSharing === 'function') {
                    await window.stopScreenSharing();
                } else if (!active && typeof window.startScreenSharing === 'function') {
                    if (!window.isRecording) {
                        // 抖动提示 + Toast，明确告知需要先开语音会话
                        button.classList.remove('is-nudged');
                        void button.offsetWidth;
                        button.classList.add('is-nudged');
                        setTimeout(function () { button.classList.remove('is-nudged'); }, 300);
                        if (typeof window.showStatusToast === 'function') {
                            window.showStatusToast(
                                window.t ? window.t('app.screenShareRequiresVoice') : '屏幕分享仅用于音视频通话',
                                3000
                            );
                        }
                        return;
                    }
                    await window.startScreenSharing();
                }
            } finally {
                finishShareToggleOperation(operationGeneration);
            }
        }

        button.addEventListener('click', handleToggleClick);

        pruneShareToggleButtons();
        shareToggleButtonRegistry.push(button);
        // 每次重新显示（弹窗重渲染）时，若共享处于开启状态则重播一次开启动画；未开启则直接落位
        button._nekoSetShareActive(isScreenShareActive(), !isScreenShareActive());
        return button;
    }

    // ======================== 游戏语音 STT Gate ========================

    function getGameVoiceSpeechRecognition() {
        return window.SpeechRecognition || window.webkitSpeechRecognition || null;
    }

    function publishGameVoiceBrowserTranscriptionState(ready, reason) {
        if (
            window.appWebSocket
            && typeof window.appWebSocket.setGameVoiceTranscriptionState === 'function'
        ) {
            window.appWebSocket.setGameVoiceTranscriptionState({
                transcription_mode: ready ? 'browser_fallback' : 'unavailable',
                provider: 'browser',
                ready: ready === true,
                reason: String(reason || '')
            });
        }
    }

    function gameVoiceRequestId() {
        return `game-voice-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    }

    function logGameVoiceSttDiagnostics(reason) {
        const tracks = S.stream instanceof MediaStream
            ? S.stream.getAudioTracks().map(track => ({
                label: track.label || '',
                enabled: track.enabled,
                muted: track.muted,
                readyState: track.readyState
            }))
            : [];
        console.log('[GameVoiceSTT][Diag] env:', {
            reason,
            speechRecognition: !!getGameVoiceSpeechRecognition(),
            secureContext: !!window.isSecureContext,
            protocol: window.location ? window.location.protocol : '',
            visibility: document.visibilityState,
            selectedMicrophoneId: S.selectedMicrophoneId || '',
            ordinaryStreamTracks: tracks
        });
        if (S.selectedMicrophoneId) {
            console.warn('[GameVoiceSTT][Diag] SpeechRecognition 不能指定 selectedMicrophoneId，会使用浏览器默认麦克风；若默认麦不是当前项目麦，可能 no-speech。');
        }
        if (navigator.permissions && typeof navigator.permissions.query === 'function') {
            navigator.permissions.query({ name: 'microphone' }).then(function (status) {
                console.log('[GameVoiceSTT][Diag] microphone permission:', status && status.state);
            }).catch(function (error) {
                console.log('[GameVoiceSTT][Diag] microphone permission query unavailable:', error && error.message ? error.message : error);
            });
        }
        if (navigator.mediaDevices && typeof navigator.mediaDevices.enumerateDevices === 'function') {
            navigator.mediaDevices.enumerateDevices().then(function (devices) {
                const audioInputs = devices
                    .filter(device => device.kind === 'audioinput')
                    .map(device => ({
                        deviceId: device.deviceId,
                        label: device.label || '',
                        groupId: device.groupId || ''
                    }));
                console.log('[GameVoiceSTT][Diag] audio inputs:', audioInputs);
            }).catch(function (error) {
                console.log('[GameVoiceSTT][Diag] enumerate audio inputs failed:', error && error.message ? error.message : error);
            });
        }
    }

    function getGameVoiceSttRouteSnapshot() {
        return {
            gameType: S.gameVoiceSttGameType || S.gameRouteGameType || '',
            sessionId: S.gameVoiceSttSessionId || S.gameRouteSessionId || '',
            routeInstanceId: S.gameRouteInstanceId || ''
        };
    }

    async function submitGameVoiceSttTranscript(transcript, routeSnapshot) {
        const text = String(transcript || '').trim();
        if (!text) return;

        const lanlanName = S.gameRouteLanlanName || (window.lanlan_config && window.lanlan_config.lanlan_name) || '';
        if (!lanlanName) {
            console.warn('[GameVoiceSTT] missing lanlan_name, drop transcript');
            return;
        }

        const frozenRoute = routeSnapshot || getGameVoiceSttRouteSnapshot();
        const gameType = frozenRoute.gameType || '';
        const sessionId = frozenRoute.sessionId || '';
        const routeInstanceId = frozenRoute.routeInstanceId || '';
        if (!gameType || !sessionId) {
            console.warn('[GameVoiceSTT] missing source route identity, drop transcript');
            return;
        }
        const requestId = gameVoiceRequestId();
        console.log(`[GameVoiceSTT] 最终转写 | game=${gameType} request=${requestId} text="${text}"`);
        try {
            const response = await fetch(`/api/game/${encodeURIComponent(gameType)}/route/voice-transcript`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    lanlan_name: lanlanName,
                    session_id: sessionId,
                    sdk_route_instance_id: routeInstanceId,
                    transcript: text,
                    request_id: requestId,
                    source: 'main_voice_stt_gate'
                })
            });
            const result = await response.json().catch(() => null);
            if (!response.ok) {
                console.warn('[GameVoiceSTT] transcript route failed:', response.status, result);
                return;
            }
            if (result && result.handled === false && result.reason === 'session_id_mismatch') {
                console.info('[GameVoiceSTT] session mismatch, restarting hidden STT gate with the current route session');
                stopGameVoiceSttGate({ keepActive: true, restoreOrdinaryMic: false });
                if (S.gameVoiceSttGateActive && S.isRecording && !S.isMicMuted) {
                    S.gameVoiceSttRestartTimer = setTimeout(startGameVoiceSttGate, 250);
                }
                return;
            }
            if (result && result.handled === false && result.reason === 'game_route_inactive') {
                console.info('[GameVoiceSTT] game route inactive, stopping hidden STT gate');
                stopGameVoiceSttGate();
                return;
            }
            console.log(`[GameVoiceSTT] 已提交小游戏路由 | game=${gameType} request=${requestId} handled=${result ? result.handled !== false : 'unknown'} text="${text}"`);
        } catch (error) {
            console.warn('[GameVoiceSTT] transcript submit failed:', error);
        }
    }

    function releaseOrdinaryMicCaptureForGameVoiceSttGate() {
        if (S.workletNode) {
            try { S.workletNode.disconnect(); } catch (_) { /* noop */ }
            S.workletNode = null;
        }
        S.inputAnalyser = null;
        S.micGainNode = null;

        if (S.stream instanceof MediaStream) {
            S.stream.getTracks().forEach(track => track.stop());
            S.stream = null;
        }

        if (S.audioContext) {
            const context = S.audioContext;
            S.audioContext = null;
            if (context.state !== 'closed') {
                context.close().catch((error) => console.warn('[GameVoiceSTT] close ordinary audio context failed:', error));
            }
        }

        stopSilenceDetection();
    }

    function restoreOrdinaryMicCaptureAfterGameVoiceSttFailure(reason, error) {
        console.warn('[GameVoiceSTT] restoring ordinary mic capture after STT gate failure:', reason, error || '');
        stopGameVoiceSttGate({ restoreOrdinaryMic: false });
        if (S.isRecording && typeof startMicCapture === 'function') {
            Promise.resolve(startMicCapture()).catch(function (restoreError) {
                console.warn('[GameVoiceSTT] restore ordinary mic capture failed:', restoreError);
            });
        }
    }

    function restoreOrdinaryMicCaptureAfterGameVoiceSttStop(reason) {
        if (!S.isRecording || typeof startMicCapture !== 'function') {
            return;
        }
        const ordinaryPipelineAlive = !!(S.stream && S.audioContext && S.workletNode);
        if (ordinaryPipelineAlive) {
            return;
        }
        Promise.resolve(startMicCapture()).catch(function (restoreError) {
            console.warn(`[GameVoiceSTT] restore ordinary mic capture after ${reason || 'stop'} failed:`, restoreError);
        });
    }

    function startGameVoiceSttGate() {
        if (!S.gameVoiceSttGateActive || !S.isRecording || S.isMicMuted) {
            return false;
        }
        setMicLeaseOwner(MIC_LEASE.GAME);
        if (S.gameVoiceSttListening) {
            releaseOrdinaryMicCaptureForGameVoiceSttGate();
            return true;
        }

        const SpeechRecognition = getGameVoiceSpeechRecognition();
        if (!SpeechRecognition) {
            publishGameVoiceBrowserTranscriptionState(false, 'browser_unsupported');
            if (!S.gameVoiceSttUnsupportedNotified) {
                S.gameVoiceSttUnsupportedNotified = true;
                console.warn('[GameVoiceSTT] 当前浏览器不支持 SpeechRecognition，无法启动游戏语音 STT gate');
                if (typeof window.showStatusToast === 'function') {
                    window.showStatusToast(window.t ? window.t('app.gameVoiceSttNotSupported') : '当前浏览器不支持游戏语音转写，请暂时使用文本输入。', 4000);
                }
            }
            return false;
        }

        const routeSnapshot = getGameVoiceSttRouteSnapshot();
        if (S.gameVoiceSttRecognition) {
            try { S.gameVoiceSttRecognition.abort(); } catch (_) { /* noop */ }
            S.gameVoiceSttRecognition = null;
        }

        const recognition = new SpeechRecognition();
        recognition.lang = (function () {
            const raw = (typeof window.i18next !== 'undefined' && window.i18next.language)
                || (typeof navigator !== 'undefined' && navigator.language)
                || 'zh-CN';
            const tag = String(raw).toLowerCase();
            if (tag.startsWith('zh-tw') || tag === 'zh-hant' || tag.startsWith('zh-hk')) return 'zh-TW';
            if (tag.startsWith('zh')) return 'zh-CN';
            if (tag.startsWith('en')) return 'en-US';
            if (tag.startsWith('ja')) return 'ja-JP';
            if (tag.startsWith('ko')) return 'ko-KR';
            if (tag.startsWith('ru')) return 'ru-RU';
            if (tag.startsWith('es')) return 'es-ES';
            if (tag.startsWith('pt')) return 'pt-BR';
            return raw;
        })();
        recognition.continuous = true;
        recognition.interimResults = false;
        recognition.maxAlternatives = 1;
        recognition._gameVoiceRouteSnapshot = routeSnapshot;
        recognition.onstart = function () {
            if (S.gameVoiceSttRecognition !== recognition) return;
            S.gameVoiceSttListening = true;
            S.gameVoiceSttStopping = false;
            publishGameVoiceBrowserTranscriptionState(true, 'browser_ready');
            console.log('[GameVoiceSTT][Diag] recognition start');
        };
        recognition.onaudiostart = function () {
            console.log('[GameVoiceSTT][Diag] audio start');
        };
        recognition.onsoundstart = function () {
            console.log('[GameVoiceSTT][Diag] sound start');
        };
        recognition.onspeechstart = function () {
            console.log('[GameVoiceSTT][Diag] speech start');
        };
        recognition.onspeechend = function () {
            console.log('[GameVoiceSTT][Diag] speech end');
        };
        recognition.onsoundend = function () {
            console.log('[GameVoiceSTT][Diag] sound end');
        };
        recognition.onaudioend = function () {
            console.log('[GameVoiceSTT][Diag] audio end');
        };
        recognition.onnomatch = function (event) {
            console.warn('[GameVoiceSTT][Diag] no match:', event);
        };
        recognition.onresult = function (event) {
            let finalText = '';
            const startIndex = typeof event.resultIndex === 'number' ? event.resultIndex : 0;
            console.log('[GameVoiceSTT][Diag] result event:', {
                resultIndex: startIndex,
                resultCount: event.results ? event.results.length : 0
            });
            for (let i = startIndex; i < event.results.length; i++) {
                const result = event.results[i];
                if (!result || result.isFinal === false) continue;
                finalText += (result[0] && result[0].transcript) || '';
            }
            if (finalText.trim()) {
                void submitGameVoiceSttTranscript(finalText, recognition._gameVoiceRouteSnapshot);
            }
        };
        recognition.onerror = function (event) {
            const errorCode = (event && event.error) || 'unknown';
            console.warn('[GameVoiceSTT] recognition error:', errorCode, event);
            // Same staleness guard its onstart/onend siblings carry, which this
            // handler was missing. An abandoned recognizer still fires onerror,
            // and the not-allowed branch below calls
            // restoreOrdinaryMicCaptureAfterGameVoiceSttFailure -> a fresh
            // startMicCapture: a permission toast for a recognizer nobody uses
            // any more, plus a microphone restart over a healthy live pipeline.
            // Logged before returning so the diagnostics this handler exists
            // for survive; only the side effects are gated.
            if (S.gameVoiceSttRecognition !== recognition) {
                console.warn('[GameVoiceSTT] ignoring error from a superseded recognizer');
                return;
            }
            if (errorCode === 'no-speech') {
                console.warn('[GameVoiceSTT][Diag] no-speech: 识别器启动了但没有形成可用语音。优先检查默认麦克风是否正确、是否有 audio/sound/speech start 日志。');
            } else {
                publishGameVoiceBrowserTranscriptionState(false, errorCode);
            }
            if (errorCode === 'not-allowed' || errorCode === 'service-not-allowed') {
                if (typeof window.showStatusToast === 'function') {
                    window.showStatusToast(window.t ? window.t('app.gameVoiceSttMicPermissionDenied') : '游戏语音转写没有麦克风权限，请检查浏览器权限。', 4000);
                }
                restoreOrdinaryMicCaptureAfterGameVoiceSttFailure(errorCode, event);
            }
        };
        recognition.onend = function () {
            if (S.gameVoiceSttRecognition !== recognition) return;
            S.gameVoiceSttListening = false;
            if (S.gameVoiceSttRestartTimer) {
                clearTimeout(S.gameVoiceSttRestartTimer);
                S.gameVoiceSttRestartTimer = null;
            }
            if (S.gameVoiceSttGateActive && S.isRecording && !S.isMicMuted && !S.gameVoiceSttStopping) {
                S.gameVoiceSttRestartTimer = setTimeout(startGameVoiceSttGate, 250);
            }
            S.gameVoiceSttStopping = false;
        };
        S.gameVoiceSttRecognition = recognition;

        try {
            S.gameVoiceSttStopping = false;
            logGameVoiceSttDiagnostics('start');
            releaseOrdinaryMicCaptureForGameVoiceSttGate();
            S.gameVoiceSttRecognition.start();
            S.gameVoiceSttListening = true;
            console.log(`[GameVoiceSTT] STT gate 已启动 | game=${S.gameVoiceSttGameType || S.gameRouteGameType || '-'} recording=${!!S.isRecording} ordinary_mic=released`);
            return true;
        } catch (error) {
            if (error && error.name === 'InvalidStateError') {
                S.gameVoiceSttListening = true;
                console.log('[GameVoiceSTT] STT gate 已在运行');
                return true;
            }
            console.warn('[GameVoiceSTT] recognition start failed:', error);
            S.gameVoiceSttListening = false;
            publishGameVoiceBrowserTranscriptionState(false, 'browser_start_failed');
            restoreOrdinaryMicCaptureAfterGameVoiceSttFailure('recognition_start_failed', error);
            return false;
        }
    }

    function stopGameVoiceSttGate(options) {
        const keepActive = options && options.keepActive === true;
        const restoreOrdinaryMic = !(options && options.restoreOrdinaryMic === false);
        if (!keepActive) {
            S.gameVoiceSttGateActive = false;
            S.gameVoiceSttGameType = '';
            S.gameVoiceSttSessionId = '';
        }
        if (S.gameVoiceSttRestartTimer) {
            clearTimeout(S.gameVoiceSttRestartTimer);
            S.gameVoiceSttRestartTimer = null;
        }
        S.gameVoiceSttStopping = true;
        const recognition = S.gameVoiceSttRecognition;
        S.gameVoiceSttRecognition = null;
        if (recognition) {
            try {
                recognition.stop();
            } catch (error) {
                try { recognition.abort(); } catch (_) { /* noop */ }
            }
        }
        S.gameVoiceSttListening = false;
        S.gameVoiceSttStopping = false;
        if (!keepActive && restoreOrdinaryMic) {
            restoreOrdinaryMicCaptureAfterGameVoiceSttStop('gate stop');
        }
        refreshMicLease();
    }

    // ======================== 麦克风设备选择 ========================

    async function selectMicrophone(deviceId) {
        setSelectedMicrophoneId(deviceId);

        // 获取设备名称用于状态提示
        let deviceName = '系统默认麦克风';
        if (deviceId) {
            try {
                const devices = await navigator.mediaDevices.enumerateDevices();
                const audioInputs = devices.filter(device => device.kind === 'audioinput');
                const selectedDevice = audioInputs.find(device => device.deviceId === deviceId);
                if (selectedDevice) {
                    deviceName = selectedDevice.label || `麦克风 ${audioInputs.indexOf(selectedDevice) + 1}`;
                }
            } catch (error) {
                console.error(window.t('console.getDeviceNameFailed'), error);
            }
        }

        // 更新UI选中状态
        const options = document.querySelectorAll('.mic-option');
        options.forEach(option => {
            if ((option.classList.contains('default') && deviceId === null) ||
                (option.dataset.deviceId === deviceId && deviceId !== null)) {
                option.classList.add('selected');
            } else {
                option.classList.remove('selected');
            }
        });

        // 保存选择到服务器
        await saveSelectedMicrophone(deviceId);

        // 如果正在录音，先显示选择提示，然后延迟重启录音
        if (S.isRecording) {
            const wasRecording = S.isRecording;
            // 先显示选择提示
            window.showStatusToast(window.t ? window.t('app.deviceSelected', { device: deviceName }) : `已选择 ${deviceName}`, 3000);

            // 保存需要恢复的状态
            const shouldRestartProactiveVision = S.proactiveVisionEnabled && S.isRecording;
            const shouldRestartScreening = S.videoSenderInterval !== undefined && S.videoSenderInterval !== null;

            // 防止并发切换导致状态混乱
            if (window._isSwitchingMicDevice) {
                console.warn(window.t('console.deviceSwitchingWait'));
                window.showStatusToast(window.t ? window.t('app.deviceSwitching') : '设备切换中...', 2000);
                return;
            }
            window._isSwitchingMicDevice = true;

            try {
                // 停止语音期间主动视觉定时
                if (typeof window.stopProactiveVisionDuringSpeech === 'function') {
                    window.stopProactiveVisionDuringSpeech();
                }
                // 停止屏幕共享
                if (typeof window.stopScreening === 'function') {
                    window.stopScreening();
                }
                // 停止静音检测
                stopSilenceDetection();
                // 清理输入analyser
                S.inputAnalyser = null;
                // 停止所有轨道
                if (S.stream instanceof MediaStream) {
                    S.stream.getTracks().forEach(track => track.stop());
                    S.stream = null;
                }
                // 清理 AudioContext 本地资源
                if (S.audioContext) {
                    if (S.audioContext.state !== 'closed') {
                        await S.audioContext.close().catch((e) => console.warn(window.t('console.audioContextCloseFailed'), e));
                    }
                    S.audioContext = null;
                }
                S.workletNode = null;

                // Snapshot the cancellation counter BEFORE the delay below.
                // wasRecording was taken even earlier, so on its own it cannot
                // see a teardown that lands during the wait -- and the restart
                // then MINTS A NEWER TOKEN, which defeats the very
                // invalidation that teardown performed. An auto_close_mic, a
                // text-session takeover or a plain stopRecording() inside this
                // window would be silently overridden and the hardware
                // microphone reopened, re-claiming a lease the backend had
                // already released (Codex P2).
                //
                // This function's own teardown above closes the graph directly
                // rather than through stopRecording(), so it does not bump the
                // counter and cannot cancel its own restart.
                const restartGeneration = micStartGeneration;

                // 等待一小段时间，确保选择提示显示出来
                await new Promise(resolve => setTimeout(resolve, 500));

                if (micStartGeneration !== restartGeneration) {
                    console.log('[App] microphone switch superseded during the restart delay; not reopening');
                    return;
                }

                if (wasRecording) {
                    while (true) {
                        const selectionGenerationForRestart = microphoneSelectionGeneration;
                        // startMicCapture claims the next generation
                        // synchronously, before its first await. If anything
                        // else advances the counter, a stop/takeover occurred
                        // and this switch must not reopen the microphone.
                        const expectedRestartGeneration = micStartGeneration + 1;
                        const microphoneStarted = await startMicCapture();
                        if (microphoneStarted === true) {
                            break;
                        }
                        const latestSelectionNeedsRetry = (
                            microphoneSelectionGeneration !== selectionGenerationForRestart
                            && micStartGeneration === expectedRestartGeneration
                            && S.voiceInputRouteBlocked !== true
                        );
                        if (!latestSelectionNeedsRetry) {
                            console.log('[App] microphone switch restart was cancelled before commit');
                            return;
                        }
                        console.log('[App] microphone selection changed while opening; retrying the latest device');
                    }

                    // 重启屏幕共享（如果之前正在共享）
                    if (shouldRestartScreening) {
                        if (typeof window.startScreenSharing === 'function') {
                            try {
                                await window.startScreenSharing();
                            } catch (e) {
                                console.warn(window.t('console.restartScreenShareFailed'), e);
                            }
                        }
                    }
                    // 重启主动视觉（如果之前已启用）
                    if (shouldRestartProactiveVision) {
                        if (typeof window.acquireProactiveVisionStream === 'function') {
                            await window.acquireProactiveVisionStream();
                        }
                        if (typeof window.startProactiveVisionDuringSpeech === 'function') {
                            window.startProactiveVisionDuringSpeech();
                        }
                    }
                }
            } catch (e) {
                console.error(window.t('console.switchMicrophoneFailed'), e);
                window.showStatusToast(window.t ? window.t('app.deviceSwitchFailed') : '设备切换失败', 3000);

                // 完整清理：重置状态
                S.isRecording = false;
                window.isRecording = false;

                // 重置所有按钮状态
                const _mic = micButton();
                const _mute = muteButton();
                const _screen = screenButton();
                const _stop = stopButton();

                if (_mic) _mic.classList.remove('recording', 'active');
                if (_mute) _mute.classList.remove('recording', 'active');
                if (_screen) _screen.classList.remove('active');
                if (_stop) _stop.classList.remove('recording', 'active');

                // 同步浮动按钮状态
                if (typeof window.syncFloatingMicButtonState === 'function') {
                    window.syncFloatingMicButtonState(false);
                }
                if (typeof window.syncFloatingScreenButtonState === 'function') {
                    window.syncFloatingScreenButtonState(false);
                }

                // 启用/禁用按钮状态
                if (_mic)  _mic.disabled = false;
                if (_mute) _mute.disabled = true;
                if (_screen) _screen.disabled = true;
                if (_stop) _stop.disabled = true;

                // 显示文本输入区域
                S.voiceChatActive = false;
                const textInputArea = document.getElementById('text-input-area');
                if (textInputArea) {
                    textInputArea.classList.remove('hidden');
                }
                if (typeof window.syncVoiceChatComposerHidden === 'function') {
                    window.syncVoiceChatComposerHidden(false);
                }

                // 清理资源
                if (typeof window.stopScreening === 'function') {
                    window.stopScreening();
                }
                stopSilenceDetection();
                S.inputAnalyser = null;

                if (S.stream instanceof MediaStream) {
                    S.stream.getTracks().forEach(track => track.stop());
                    S.stream = null;
                }

                if (S.audioContext) {
                    if (S.audioContext.state !== 'closed') {
                        await S.audioContext.close().catch((err) => console.warn('AudioContext close 失败:', err));
                    }
                    S.audioContext = null;
                }
                S.workletNode = null;

                // 通知后端
                if (S.socket && S.socket.readyState === WebSocket.OPEN) {
                    S.socket.send(JSON.stringify({ action: 'pause_session' }));
                }

                // 如果主动搭话已启用且选择了搭话方式，重置并开始定时
                if (S.proactiveChatEnabled && typeof window.hasAnyChatModeEnabled === 'function' && window.hasAnyChatModeEnabled()) {
                    window.lastUserInputTime = Date.now();
                    if (typeof window.resetProactiveChatBackoff === 'function') {
                        window.resetProactiveChatBackoff();
                    }
                }

                window._isSwitchingMicDevice = false;
                return;
            } finally {
                window._isSwitchingMicDevice = false;
            }
        } else {
            // 如果不在录音，直接显示选择提示
            window.showStatusToast(window.t ? window.t('app.deviceSelected', { device: deviceName }) : `已选择 ${deviceName}`, 3000);
        }
    }

    // 保存选择的麦克风到服务器和 localStorage
    async function saveSelectedMicrophone(deviceId) {
        try {
            if (deviceId) {
                localStorage.setItem('neko_selected_microphone', deviceId);
            } else {
                localStorage.removeItem('neko_selected_microphone');
            }
        } catch (e) { }

        try {
            const response = await fetch('/api/characters/set_microphone', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    microphone_id: deviceId
                })
            });

            if (!response.ok) {
                console.error(window.t('console.saveMicrophoneSelectionFailed'));
            }
        } catch (err) {
            console.error(window.t('console.saveMicrophoneSelectionError'), err);
        }
    }

    // 加载上次选择的麦克风（优先从 localStorage 加载，快速恢复）
    function loadSelectedMicrophone() {
        try {
            const saved = localStorage.getItem('neko_selected_microphone');
            if (saved) {
                setSelectedMicrophoneId(saved);
                console.log(`已加载麦克风设置: ${saved}`);
            }
        } catch (e) {
            setSelectedMicrophoneId(null);
        }
    }

    // ======================== 麦克风增益 ========================

    // 保存麦克风增益设置到 localStorage（保存分贝值）
    function saveMicGainSetting() {
        try {
            localStorage.setItem('neko_mic_gain_db', String(S.microphoneGainDb));
            console.log(`麦克风增益设置已保存: ${S.microphoneGainDb}dB`);
        } catch (err) {
            console.error('保存麦克风增益设置失败:', err);
        }
    }

    // 从 localStorage 加载麦克风增益设置
    function loadMicGainSetting() {
        try {
            const savedGainDb = localStorage.getItem('neko_mic_gain_db');
            if (savedGainDb !== null) {
                const gainDb = parseFloat(savedGainDb);
                // 验证增益值在有效范围内
                if (!isNaN(gainDb) && gainDb >= C.MIN_MIC_GAIN_DB && gainDb <= C.MAX_MIC_GAIN_DB) {
                    S.microphoneGainDb = gainDb;
                    console.log(`已加载麦克风增益设置: ${S.microphoneGainDb}dB`);
                } else {
                    console.warn(`无效的增益值 ${savedGainDb}dB，使用默认值 ${C.DEFAULT_MIC_GAIN_DB}dB`);
                    S.microphoneGainDb = C.DEFAULT_MIC_GAIN_DB;
                }
            } else {
                console.log(`未找到麦克风增益设置，使用默认值 ${C.DEFAULT_MIC_GAIN_DB}dB`);
            }
        } catch (err) {
            console.error('加载麦克风增益设置失败:', err);
            S.microphoneGainDb = C.DEFAULT_MIC_GAIN_DB;
        }
    }

    // ======================== 降噪开关 ========================

    function saveNoiseReductionSetting() {
        try {
            localStorage.setItem('neko_noise_reduction', S.noiseReductionEnabled ? '1' : '0');
        } catch (e) { }
        // Route through the shared CAS client so cross-window toggles carry
        // If-Match and reconcile 412 snapshots instead of bypassing ordering.
        try {
            if (window.appSettings
                && typeof window.appSettings.saveSettings === 'function') {
                window.appSettings.saveSettings();
            }
        } catch (e) { }
    }

    function loadNoiseReductionSetting() {
        try {
            var saved = localStorage.getItem('neko_noise_reduction');
            if (saved !== null) {
                S.noiseReductionEnabled = saved === '1';
            }
        } catch (e) { }
    }

    // 格式化增益显示（带正负号）
    function formatGainDisplay(db) {
        if (db > 0) {
            return `+${db}dB`;
        } else if (db === 0) {
            return '0dB';
        } else {
            return `${db}dB`;
        }
    }

    // 更新麦克风增益（供外部调用，参数为分贝值）
    window.setMicrophoneGain = function (gainDb) {
        if (gainDb >= C.MIN_MIC_GAIN_DB && gainDb <= C.MAX_MIC_GAIN_DB) {
            S.microphoneGainDb = gainDb;
            if (S.micGainNode) {
                S.micGainNode.gain.value = window.appUtils.dbToLinear(gainDb);
            }
            saveMicGainSetting();
            // 更新 UI 滑块（如果存在）
            const slider = document.getElementById('mic-gain-slider');
            const valueDisplay = document.getElementById('mic-gain-value');
            if (slider) slider.value = String(gainDb);
            if (valueDisplay) valueDisplay.textContent = formatGainDisplay(gainDb);
            console.log(`麦克风增益已设置: ${gainDb}dB`);
        }
    };

    // 获取当前麦克风增益（返回分贝值）
    window.getMicrophoneGain = function () {
        return S.microphoneGainDb;
    };

    // ======================== 静音检测 ========================

    function startSilenceDetection() {
        // 重置检测状态
        S.hasSoundDetected = false;

        // 清除之前的定时器(如果有)
        if (S.silenceDetectionTimer) {
            clearTimeout(S.silenceDetectionTimer);
        }

        // 启动5秒定时器
        S.silenceDetectionTimer = setTimeout(() => {
            if (!S.hasSoundDetected && S.isRecording) {
                window.showStatusToast(window.t ? window.t('app.micNoSound') : '⚠️ 麦克风无声音，请检查麦克风设置', 5000);
                console.warn('麦克风静音检测：5秒内未检测到声音');
            }
        }, 5000);
    }

    // 停止麦克风静音检测
    function stopSilenceDetection() {
        if (S.silenceDetectionTimer) {
            clearTimeout(S.silenceDetectionTimer);
            S.silenceDetectionTimer = null;
        }
        S.hasSoundDetected = false;
    }

    // 排下一次音量监测：Electron Pet 里渲染后端切到定时器驱动时，本循环也改走
    // 定时器（frame-pacing.requestPacedFrame），否则这条 rAF 链会单独把 Blink 主帧
    // 顶回显示器刷新率。非 Pet 页面没有 nekoFramePacing，保持 rAF。
    function scheduleMonitorInputVolume() {
        const pacing = window.nekoFramePacing;
        if (pacing && typeof pacing.requestPacedFrame === 'function') {
            pacing.requestPacedFrame(monitorInputVolume);
            return;
        }
        requestAnimationFrame(monitorInputVolume);
    }

    // 监测音频输入音量
    function monitorInputVolume() {
        if (!S.inputAnalyser || !S.isRecording) {
            return;
        }

        // mute 状态下 audio 在 worklet onmessage 处被丢弃，根本没送到后端，
        // 此时 analyser 仍连在增益链上能听到本地噪声（键盘/风扇/呼吸）。
        // 把这部分 RMS 当 0：不读、不写 userRecentSpeechTime，避免 proactive
        // guard 把"本地噪声"误判成"用户在说话"导致语音模式 nudge 被静默
        // skip 卡死 (`_isUserRecentlySpeaking()` 8s 窗口拖尾)。
        if (S.isMicMuted) {
            scheduleMonitorInputVolume();
            return;
        }

        const dataArray = new Uint8Array(S.inputAnalyser.fftSize);
        S.inputAnalyser.getByteTimeDomainData(dataArray);

        // 计算音量(RMS)
        let sum = 0;
        for (let i = 0; i < dataArray.length; i++) {
            const val = (dataArray[i] - 128) / 128.0;
            sum += val * val;
        }
        const rms = Math.sqrt(sum / dataArray.length);

        // 如果音量超过阈值(0.01),认为检测到声音
        if (rms > 0.01) {
            // C: 为前端 proactive guard 持续打点"最近一次有声音"。
            // 阈值与下面 hasSoundDetected 共用 0.01；这里每帧都写（~16ms 一次），
            // 不做去抖，保证 proactive tick 能读到最新值。与后端
            // _user_recent_activity_time 对称：不等 sustain、不等 VAD 判定，
            // 只要麦克风真的收到过声音就算"用户可能在说话"。
            S.userRecentSpeechTime = Date.now();
            if (!S.hasSoundDetected) {
                S.hasSoundDetected = true;
                console.log('麦克风静音检测：检测到声音，RMS =', rms);

                // 如果之前显示了无声音警告，现在检测到声音了，恢复正常状态显示
                const noSoundText = window.t ? window.t('voiceControl.noSound') : '麦克风无声音';
                const _status = statusElement();
                if (_status && _status.textContent.includes(noSoundText)) {
                    window.showStatusToast(window.t ? window.t('app.speaking') : '正在语音...', 2000);
                    console.log('麦克风静音检测：检测到声音，已清除警告');
                }
            }
        }

        // 持续监测
        if (S.isRecording) {
            scheduleMonitorInputVolume();
        }
    }

    // ======================== AudioWorklet ========================

    /**
     * Open the capture pipeline for ONE microphone-start attempt.
     *
     * ``startToken`` is that attempt's identity, taken from micStartGeneration
     * before its first await. Returns true when the pipeline was committed and
     * false when the attempt was superseded and unwound -- the caller MUST
     * check, because an unwound attempt has torn the hardware down and must not
     * continue into its success path.
     *
     * A caller that passes no token cannot prove it is current, so it unwinds.
     * That is deliberate: this whole subsystem is fail-closed, and the only
     * consumer is startMicCapture below (the module export exists for tests).
     */
    async function startAudioWorklet(
        mediaStream,
        startToken,
        selectedMicrophoneIdAtStart,
        microphoneSelectionGenerationAtStart
    ) {
        // Entry gate, before ANY shared state is touched. An attempt can be
        // superseded while it is still in startMicCapture's getUserMedia (a
        // cold device open is slow; the newer attempt hits a warm one and
        // commits first), and it then arrives here already having lost. The
        // teardown immediately below is not otherwise token-aware, so it would
        // close the WINNER's freshly published AudioContext -- verified: the
        // winner is left recording with a closed context, S.audioContext null
        // and a microphone that has stopped producing, while the UI still says
        // recording. Nothing has been allocated yet at this point, so bailing
        // costs only this attempt's own device handle.
        if (
            startToken !== micStartGeneration
            || S.voiceInputRouteBlocked === true
            || S.selectedMicrophoneId !== selectedMicrophoneIdAtStart
            || microphoneSelectionGeneration !== microphoneSelectionGenerationAtStart
        ) {
            console.log('[App] microphone start was superseded before opening; unwinding');
            try {
                if (mediaStream && typeof mediaStream.getTracks === 'function') {
                    mediaStream.getTracks().forEach(track => track.stop());
                }
            } catch (_) {
                // best-effort teardown
            }
            return false;
        }

        // 先清理旧的音频上下文，防止多个 worklet 同时发送数据导致 QPS 超限
        //
        // Pinned to a local across the await: `await S.audioContext.close()`
        // yields, and another attempt can publish a NEW pipeline during it.
        // Nulling the shared fields afterwards without re-checking would then
        // erase the handles to that live pipeline while its worklet keeps
        // uploading -- an unstoppable microphone with every handle null.
        const previousContext = S.audioContext;
        if (previousContext) {
            if (previousContext.state !== 'closed') {
                try {
                    await previousContext.close();
                } catch (e) {
                    console.warn('关闭旧音频上下文时出错:', e);
                    // 强制复位所有状态，防止状态不一致
                    const _mic = micButton();
                    if (_mic) _mic.classList.remove('recording', 'active');
                    if (typeof window.syncFloatingMicButtonState === 'function') {
                        window.syncFloatingMicButtonState(false);
                    }
                    if (typeof window.syncFloatingScreenButtonState === 'function') {
                        window.syncFloatingScreenButtonState(false);
                    }
                    const _mute = muteButton();
                    const _screen = screenButton();
                    const _stop = stopButton();
                    if (_mic) _mic.disabled = false;
                    if (_mute) _mute.disabled = true;
                    if (_screen) _screen.disabled = true;
                    if (_stop) _stop.disabled = true;
                    window.showStatusToast(window.t ? window.t('app.audioContextError') : '音频系统异常，请重试', 3000);
                    throw e;
                }
            }
            if (S.audioContext === previousContext) {
                // Still the pipeline we just closed. Clear ALL of it: leaving
                // micGainNode / inputAnalyser behind kept nodes from a closed
                // context addressable, so the volume meter and the gain slider
                // went on reading and poking a graph that no longer exists.
                S.audioContext = null;
                S.workletNode = null;
                S.micGainNode = null;
                S.inputAnalyser = null;
                // Reconcile the recording flag with what just happened. The
                // pipeline that was feeding it is closed, so leaving
                // S.isRecording true describes a microphone that no longer
                // exists: the mic button keeps its recording/active styling and
                // the floating button stays lit, canUploadOrdinaryMicFrame's
                // callers still believe frames are flowing, and if THIS attempt
                // then unwinds (superseded, fail-closed, addModule failure)
                // nothing ever puts it right -- the caller's UI restore is
                // skipped precisely because S.isRecording is true.
                //
                // The winning attempt sets it back to true at its own commit,
                // so a successful restart is unchanged; only the window between
                // teardown and commit now tells the truth.
                if (S.isRecording) {
                    S.isRecording = false;
                    window.isRecording = false;
                    if (typeof window.syncFloatingMicButtonState === 'function') {
                        window.syncFloatingMicButtonState(false);
                    }
                }
            }
        }

        // 创建音频上下文，强制使用 48kHz 采样率
        //
        // Everything this attempt builds stays ATTEMPT-LOCAL until the token
        // gate below says it won; only then is it published into S.*. Those
        // fields are module globals shared by every concurrent attempt, and
        // building through them directly cost two separate defects:
        //
        //   * the unwind reset them blindly, so a loser stopped the WINNER's
        //     tracks and nulled its context, and the winner then threw on
        //     S.audioContext.sampleRate -- no microphone at all;
        //   * even with the teardown scoped, the loser's post-await SETUP
        //     still ran through them (Codex P2): resuming from addModule() it
        //     built its worklet on the winner's context, overwrote
        //     S.workletNode and spliced itself into the winner's gain node --
        //     two live worklets on one microphone, both uploading duplicate
        //     PCM, with the winner's own node orphaned where no later
        //     teardown could reach it.
        //
        // One publish point at the end removes the whole class: before the
        // gate this attempt is invisible, after it the four fields describe
        // exactly one pipeline.
        const ownContext = new AudioContext({ sampleRate: 48000 });
        console.log("音频上下文采样率 (强制48kHz):", ownContext.sampleRate);

        let source = null;
        let ownGainNode = null;
        let ownAnalyser = null;
        try {
            // 创建媒体流源
            source = ownContext.createMediaStreamSource(mediaStream);

            // 创建增益节点用于麦克风音量放大
            ownGainNode = ownContext.createGain();
            const linearGain = window.appUtils.dbToLinear(S.microphoneGainDb);
            ownGainNode.gain.value = linearGain;
            console.log(`麦克风增益已设置: ${S.microphoneGainDb}dB (${linearGain.toFixed(2)}x)`);

            // 创建analyser节点用于监测输入音量
            ownAnalyser = ownContext.createAnalyser();
            ownAnalyser.fftSize = 2048;
            ownAnalyser.smoothingTimeConstant = 0.8;

            // 连接 source → gainNode → analyser（用于音量检测，检测增益后的音量）
            source.connect(ownGainNode);
            ownGainNode.connect(ownAnalyser);
        } catch (graphError) {
            // The context is already constructed but nothing is published and
            // discardOwnPipeline is not defined yet, so a throw from any of
            // these node constructors would strand a live AudioContext that no
            // teardown path can reach -- and Blink caps them at about six per
            // document, so a repeating failure eventually makes `new
            // AudioContext()` itself throw (Codex P2). The caller's catch
            // releases the microphone track; this releases the context.
            if (ownContext.state !== 'closed') {
                ownContext.close();
            }
            throw graphError;
        }

        let ownWorkletNode = null;

        // Dispose of everything this attempt built. Shared by the supersede /
        // fail-closed gate and by the catch below: an attempt that never
        // publishes is unreachable from S.*, so nothing else can ever close
        // its AudioContext or stop its microphone track. Touches the shared
        // fields only where they provably still describe THIS attempt.
        const discardOwnPipeline = () => {
            try {
                if (mediaStream && typeof mediaStream.getTracks === 'function') {
                    mediaStream.getTracks().forEach(track => track.stop());
                }
            } catch (_) {
                // best-effort teardown
            }
            if (S.stream && S.stream === mediaStream) {
                // Already stopped above; only drop the reference, and only
                // while it is still OUR stream.
                S.stream = null;
            }
            try {
                if (ownWorkletNode) {
                    // Kill the handler before disconnecting: an in-flight port
                    // message must not upload a frame from a pipeline that
                    // just lost.
                    ownWorkletNode.port.onmessage = null;
                    ownWorkletNode.disconnect();
                }
                // Nullable since the graph construction above can throw
                // partway; that path closes the context and rethrows without
                // reaching here, but the guards keep this honest.
                if (ownGainNode) ownGainNode.disconnect();
                if (ownAnalyser) ownAnalyser.disconnect();
                if (source) source.disconnect();
            } catch (_) {
                // best-effort teardown
            }
            if (ownContext.state !== 'closed') {
                ownContext.close();
            }
            if (S.audioContext === null) {
                // No pipeline is live at all (the fail-closed route case, or a
                // supersede with nothing committed since). The graph fields
                // still name the pipeline the top of this function tore down,
                // so clear them rather than leave nodes from a closed context
                // addressable. When a winner IS live, S.audioContext is its
                // context and none of this runs.
                S.workletNode = null;
                S.micGainNode = null;
                S.inputAnalyser = null;
                stopSilenceDetection();
            }
        };

        try {
            // 加载AudioWorklet处理器
            await ownContext.audioWorklet.addModule('/static/audio-processor.js');

            // 根据连接类型确定目标采样率
            const isMobile = window.appUtils.isMobile;
            const targetSampleRate = isMobile() ? 16000 : 48000;
            console.log(`音频采样率配置: 原始=${ownContext.sampleRate}Hz, 目标=${targetSampleRate}Hz, 移动端=${isMobile()}`);

            // 创建AudioWorkletNode
            ownWorkletNode = new AudioWorkletNode(ownContext, 'audio-processor', {
                processorOptions: {
                    originalSampleRate: ownContext.sampleRate,
                    targetSampleRate: targetSampleRate
                }
            });

            // 监听处理器发送的消息
            ownWorkletNode.port.onmessage = (event) => {
                const audioData = event.data;

                if (!canUploadOrdinaryMicFrame()) {
                    return;
                }

                if (S.isRecording && S.socket && S.socket.readyState === WebSocket.OPEN) {
                    // 8-byte header: ASCII "NEKO" + little-endian sample rate，
                    // 后续直接附 PCM16，避免每帧 JSON 数组的带宽与 GC 开销。
                    const pcm16 = audioData instanceof Int16Array
                        ? audioData
                        : new Int16Array(audioData);
                    const frame = new ArrayBuffer(8 + pcm16.byteLength);
                    const header = new DataView(frame);
                    header.setUint8(0, 0x4E);
                    header.setUint8(1, 0x45);
                    header.setUint8(2, 0x4B);
                    header.setUint8(3, 0x4F);
                    header.setUint32(4, targetSampleRate, true);
                    // 字节级拷贝按平台字节序输出 PCM，而 wire 格式要求
                    // little-endian。所有主流浏览器 / JS 引擎都是 LE，这里
                    // 显式依赖该假设以保持每帧热路径零逐样本开销；若未来
                    // 出现 BE 平台，需改为 DataView 逐样本 setInt16(LE)。
                    new Uint8Array(frame, 8).set(new Uint8Array(
                        pcm16.buffer,
                        pcm16.byteOffset,
                        pcm16.byteLength
                    ));
                    S.socket.send(frame);
                }
            };

            // 连接节点：gainNode → workletNode（音频经过增益处理后发送）
            ownGainNode.connect(ownWorkletNode);

            // Last gate before the commit. Everything above only awaited; this
            // is where the microphone actually becomes live and where
            // refreshMicLease() would re-claim the lease. A text takeover (or
            // any fail-closed verdict) that landed while getUserMedia() and
            // addModule() were in flight found S.isRecording still false, so its
            // stopRecording() early-returned and could not prevent this. Unwind
            // instead of committing: without this the pending start re-claims a
            // lease the backend just revoked and feeds a blocked route.
            if (
                startToken !== micStartGeneration
                || S.voiceInputRouteBlocked === true
                || S.selectedMicrophoneId !== selectedMicrophoneIdAtStart
                || microphoneSelectionGeneration !== microphoneSelectionGenerationAtStart
            ) {
                console.log('[App] microphone start was superseded while opening; unwinding');
                // Nothing above was published, so this tears down ONLY what
                // this attempt built. There is no re-entrancy guard on
                // startMicCapture and several callers are fire-and-forget (the
                // two game-STT restore paths above, and app-websocket.js's
                // mic-pipeline repair), so a winner may well be live in S.*
                // right now -- and it must not be touched here.
                discardOwnPipeline();
                // Deliberately NOT refreshMicLease(): the lease is the
                // backend's now, and re-emitting a snapshot from a window that
                // never started recording is exactly the re-claim being
                // prevented. S.isRecording was never set, so nothing to reset.
                //
                // false, not a bare return: the caller awaits this and would
                // otherwise run its whole success path -- disabling the mic
                // button, toasting "speaking", lighting the floating button and
                // silencing proactive chat -- against hardware that no longer
                // exists.
                return false;
            }

            // This attempt won. Publish the pipeline as one unit -- the only
            // place S.* learns about it, so the five fields are always the
            // same live graph and never a mix of two attempts.
            //
            // The gain is re-read here rather than trusted from construction
            // time: setMicrophoneGain writes S.microphoneGainDb and then pokes
            // S.micGainNode, which this attempt was deliberately absent from
            // for the whole open window, so a slider move during it would
            // otherwise persist and display the new dB while the live
            // microphone stayed at the old one until the next restart.
            ownGainNode.gain.value = window.appUtils.dbToLinear(S.microphoneGainDb);
            S.stream = mediaStream;
            S.audioContext = ownContext;
            S.micGainNode = ownGainNode;
            S.inputAnalyser = ownAnalyser;
            S.workletNode = ownWorkletNode;

            // 用户主动开麦，意味着要讲话；focus mode 的 isPlaying guard 此刻必须让路。
            // 切档案后自动触发的 greeting 音频播完如果没把 isPlaying 复位（finalize
            // 路径的前置条件没兜住就会粘住），下一次开麦每一帧都会被 focus 拦掉，
            // 表现为"Electron 显示可以说话但 STT 无反应"。用户此刻的意图是明确的，
            // 不管 flag 是粘住还是真在播 AI 音频，都应该让位给用户输入。
            // Moved below the gate with the publish: a superseded attempt has
            // no business clearing the winner's playback guard.
            S.isPlaying = false;

            // 所有初始化成功后，才标记为录音状态
            S.isRecording = true;
            window.isRecording = true;
            refreshMicLease();
            return true;

        } catch (err) {
            console.error('加载AudioWorklet失败:', err);
            console.dir(err);
            window.showStatusToast(window.t ? window.t('app.audioWorkletFailed') : 'AudioWorklet加载失败', 5000);
            // Nothing was published, so this graph is unreachable from S.* --
            // without an explicit discard its AudioContext and the microphone
            // track would leak on every failed addModule(), with no later
            // attempt able to find and close them. (The old code leaked the
            // stream too, and reached stopSilenceDetection() unconditionally,
            // which would stop a concurrent WINNER's detection; discard only
            // does that when no pipeline is live.)
            discardOwnPipeline();
            // RETHROW. `false` means "this attempt was deliberately cancelled"
            // -- superseded, or the route came back fail-closed -- and the
            // caller treats it as benign: it restores the pre-start UI and
            // returns without error. A real setup failure returned the same
            // value, so app-buttons.js sailed past `await startMicCapture()`
            // into the success path: ready-to-speak toast, proactive vision,
            // the neko:voice-session-started event, and never the error path
            // that sends end_session -- announcing a live voice call with no
            // capture pipeline behind it (Codex P2).
            //
            // Marked so startMicCapture's own catch does not stack a generic
            // "cannot access microphone" toast on top of the accurate one
            // already shown above.
            err.voiceWorkletSetupFailed = true;
            throw err;
        }
    }

    // ======================== 录音开始/停止 ========================

    // 开麦，按钮on click
    function abortVoiceStartForBlockedRoute() {
        // Unwind the "starting voice" UI after startMicCapture refused a
        // fail-closed route. Deliberately NOT a thrown error: the generic
        // catch would replace the accurate ASR failure toast with a generic
        // "session start failed".
        const _mic = micButton();
        const _mute = muteButton();
        const _screen = screenButton();
        if (_mic) {
            _mic.classList.remove('recording');
            _mic.classList.remove('active');
            _mic.disabled = false;
        }
        if (_mute) _mute.disabled = true;
        if (_screen) _screen.disabled = true;
        // Also cancel a start still inside its getUserMedia/addModule window:
        // clearing S.isRecording cannot reach one that has not set it yet.
        invalidatePendingMicStart();
        S.isRecording = false;
        window.isRecording = false;
        S.voiceChatActive = false;
        S.voiceStartPending = false;
        window.isMicStarting = false;
        if (typeof window.hideVoicePreparingToast === 'function') {
            window.hideVoicePreparingToast();
        }
        const textInputArea = document.getElementById('text-input-area');
        if (textInputArea) textInputArea.classList.remove('hidden');
        if (typeof window.syncVoiceChatComposerHidden === 'function') {
            window.syncVoiceChatComposerHidden(false);
        }
        if (typeof window.syncFloatingMicButtonState === 'function') {
            window.syncFloatingMicButtonState(false);
        }
        refreshMicLease();
    }

    function stopMicrophoneStreamTracks(stream) {
        try {
            if (stream && typeof stream.getTracks === 'function') {
                stream.getTracks().forEach(track => track.stop());
            }
        } catch (_) {
            // best-effort teardown
        }
    }

    function hasLiveMicrophoneTrack(stream) {
        if (!stream || typeof stream.getAudioTracks !== 'function') {
            return false;
        }
        return stream.getAudioTracks().some(track => track && track.readyState !== 'ended');
    }

    function hasLiveCommittedMicrophonePipeline() {
        return (
            S.isRecording === true
            && S.voiceInputRouteBlocked !== true
            && hasLiveMicrophoneTrack(S.stream)
            && !!S.audioContext
            && S.audioContext.state !== 'closed'
            && !!S.workletNode
        );
    }

    function finishCancelledMicStart(micElement, micStartToken) {
        // A newer attempt may already own the shared pipeline and UI. In that
        // case the stale caller must report the live winner without repainting
        // the controls as stopped.
        if (hasLiveCommittedMicrophonePipeline()) {
            return true;
        }
        if (
            pendingMicStartUiOwnerToken !== null
            && pendingMicStartUiOwnerToken !== micStartToken
        ) {
            return false;
        }
        S.isRecording = false;
        window.isRecording = false;
        if (micElement) {
            micElement.classList.remove('recording');
            micElement.classList.remove('active');
        }
        const cancelledTextInputArea = document.getElementById('text-input-area');
        if (cancelledTextInputArea) {
            cancelledTextInputArea.classList.remove('hidden');
        }
        if (typeof window.syncVoiceChatComposerHidden === 'function') {
            window.syncVoiceChatComposerHidden(false);
        }
        if (typeof window.syncFloatingMicButtonState === 'function') {
            window.syncFloatingMicButtonState(false);
        }
        return false;
    }

    async function requestUsableMicrophoneStream(constraints) {
        const stream = await navigator.mediaDevices.getUserMedia(constraints);
        if (hasLiveMicrophoneTrack(stream)) {
            return stream;
        }

        stopMicrophoneStreamTracks(stream);
        const error = new Error(
            window.t ? window.t('app.micAccessDenied') : 'Cannot access microphone'
        );
        error.name = 'NotReadableError';
        throw error;
    }

    function isSelectedMicrophoneFallbackEligibleError(error) {
        const errorName = error && error.name;
        return (
            errorName === 'NotFoundError'
            || errorName === 'OverconstrainedError'
            || errorName === 'NotReadableError'
        );
    }

    function applySystemDefaultMicrophoneSelection() {
        setSelectedMicrophoneId(null);
        updateMicListSelection();

        const defaultLabel = window.t
            ? window.t('microphone.defaultDevice')
            : 'System Default Microphone';
        document.querySelectorAll('[data-neko-mic-action="device"] .neko-mic-action-sub-label').forEach(labelEl => {
            labelEl.textContent = defaultLabel;
        });

        // localStorage is updated synchronously before saveSelectedMicrophone's
        // network await. The backend write is best-effort and must not delay an
        // already-open default microphone stream.
        void saveSelectedMicrophone(null);
    }

    async function openMicrophoneStreamWithFallback(
        baseAudioConstraints,
        micStartToken,
        selectedMicrophoneId,
        microphoneSelectionGenerationAtStart
    ) {
        const defaultConstraints = { audio: baseAudioConstraints };
        const startStillOwnsMicrophoneRequest = () => (
            micStartToken === micStartGeneration
            && S.voiceInputRouteBlocked !== true
            && S.selectedMicrophoneId === selectedMicrophoneId
            && microphoneSelectionGeneration === microphoneSelectionGenerationAtStart
        );
        const cancelledOpenResult = () => ({
            stream: null,
            fallbackFromMicrophoneId: null,
            cancelled: true
        });
        const requestOwnedMicrophoneStream = async (constraints) => {
            try {
                const stream = await requestUsableMicrophoneStream(constraints);
                if (!startStillOwnsMicrophoneRequest()) {
                    stopMicrophoneStreamTracks(stream);
                    return null;
                }
                return stream;
            } catch (error) {
                if (!startStillOwnsMicrophoneRequest()) {
                    return null;
                }
                throw error;
            }
        };

        if (!selectedMicrophoneId) {
            const defaultStream = await requestOwnedMicrophoneStream(defaultConstraints);
            if (!defaultStream) {
                return cancelledOpenResult();
            }
            return {
                stream: defaultStream,
                fallbackFromMicrophoneId: null
            };
        }

        try {
            const selectedStream = await requestOwnedMicrophoneStream({
                audio: {
                    ...baseAudioConstraints,
                    deviceId: { exact: selectedMicrophoneId }
                }
            });
            if (!selectedStream) {
                return cancelledOpenResult();
            }
            return {
                stream: selectedStream,
                fallbackFromMicrophoneId: null
            };
        } catch (selectedMicrophoneError) {
            // A superseded/blocked attempt must not change the user's saved
            // device choice or open another hardware device.
            if (
                micStartToken !== micStartGeneration
                || S.voiceInputRouteBlocked === true
                || S.selectedMicrophoneId !== selectedMicrophoneId
                || microphoneSelectionGeneration !== microphoneSelectionGenerationAtStart
            ) {
                return cancelledOpenResult();
            }

            // Permission, security-context, abort and programming errors apply
            // to the capture request itself, not just the selected device.
            // Retrying those against the default device would prompt/open
            // unnecessarily and could erase a still-valid saved selection.
            if (!isSelectedMicrophoneFallbackEligibleError(selectedMicrophoneError)) {
                throw selectedMicrophoneError;
            }

            console.warn(
                '[App] selected microphone unavailable; trying the system default microphone',
                selectedMicrophoneError
            );

            const fallbackStream = await requestOwnedMicrophoneStream(defaultConstraints);
            if (!fallbackStream) {
                return cancelledOpenResult();
            }

            // Commit the selection change and notification only after the
            // worklet pipeline also commits. Otherwise an important fallback
            // toast could hide a later, more accurate setup error.
            return {
                stream: fallbackStream,
                fallbackFromMicrophoneId: selectedMicrophoneId
            };
        }
    }

    async function startMicCapture() {
        // Refuse to open the hardware microphone onto a route the backend has
        // already fail-closed. This is THE guard that closes the startup-failure
        // hole: on a cold voice start the mic is opened only AFTER
        // session_started, i.e. after the ASR_INDEPENDENT_* failure status, so a
        // server-side lease revoke has nothing to revoke yet -- and this
        // function's own refreshMicLease() would re-claim the lease from
        // scratch anyway (_handle_voice_input_control enforces only generation
        // monotonicity, and the revoke reset the generation to -1, so the next
        // client snapshot wins unconditionally). Placed at the top rather than
        // at the two await-sessionStartPromise call sites because three more
        // callers -- the device-change restore paths below -- can also reopen
        // the mic on a dead route, and one guard covers all five.
        if (S.voiceInputRouteBlocked === true) {
            console.log('[App] voice route is fail-closed; refusing to open the microphone');
            return false;
        }
        // Claim this attempt BEFORE the first await. Anything that invalidates
        // pending starts from here on makes the commit at the end of
        // startAudioWorklet a no-op, which is the only way to cover the
        // getUserMedia() half of the window as well.
        micStartGeneration += 1;
        const micStartToken = micStartGeneration;
        pendingMicStartUiOwnerToken = micStartToken;
        const _mic = micButton();
        const _mute = muteButton();
        const _screen = screenButton();
        const _stop = stopButton();
        const _reset = resetSessionButton();

        // Declared OUTSIDE the try so the catch below can still reach it.
        // `const` inside the try block is invisible to `catch (err)` -- a
        // separate block scope -- and the stream is deliberately not published
        // to S.stream until the attempt wins, so on any throw between
        // acquisition and the commit (a failing `new AudioContext()`, which
        // Blink caps at ~6 per document, the source/gain/analyser setup, or
        // the previous context's close()) NOTHING could stop its tracks: the
        // UI reported a failed start while the browser microphone stayed live.
        let ownStream = null;
        try {
            // 开始录音前添加录音状态类到两个按钮
            if (_mic) _mic.classList.add('recording');

            // 隐藏文本输入区（仅非移动端），确保语音/文本互斥
            const textInputArea = document.getElementById('text-input-area');
            if (textInputArea && !window.appUtils.isMobile()) {
                textInputArea.classList.add('hidden');
            }
            if (!window.appUtils.isMobile() && typeof window.syncVoiceChatComposerHidden === 'function') {
                window.syncVoiceChatComposerHidden(true);
            }

            if (typeof window.ensureAudioPlayerContext === 'function') {
                await window.ensureAudioPlayerContext();
            } else if (!S.audioPlayerContext) {
                // Backward-compatible fallback for isolated route/test harnesses
                // that load capture without the playback module.
                S.audioPlayerContext = new (window.AudioContext || window.webkitAudioContext)();
                if (typeof window.syncAudioGlobals === 'function') {
                    window.syncAudioGlobals();
                }
            }
            if (
                micStartToken !== micStartGeneration
                || S.voiceInputRouteBlocked === true
            ) {
                return finishCancelledMicStart(_mic, micStartToken);
            }

            if (S.audioPlayerContext.state === 'suspended') {
                await S.audioPlayerContext.resume();
                if (
                    micStartToken !== micStartGeneration
                    || S.voiceInputRouteBlocked === true
                ) {
                    return finishCancelledMicStart(_mic, micStartToken);
                }
            }

            // 获取麦克风流，使用选择的麦克风设备ID
            const baseAudioConstraints = {
                noiseSuppression: false,
                echoCancellation: true,
                autoGainControl: true,
                channelCount: 1
            };

            // Attempt-local, for the same reason the audio graph is: publishing
            // the stream here put it OUTSIDE the single publish point in
            // startAudioWorklet, and this write lands after an await, so it is
            // unordered with respect to the token. Two overlapping starts whose
            // getUserMedia settle out of token order let the LOSER write
            // S.stream last; its unwind then legitimately sees
            // `S.stream === mediaStream` and nulls it, leaving the winner
            // recording with S.stream === null -- stopRecording's `if (S.stream)`
            // never stops those tracks, so the OS microphone indicator stays lit
            // for the life of the page, and the `S.stream && S.audioContext &&
            // S.workletNode` liveness probes read dead against a live pipeline
            // and open a second microphone on top of it.
            const selectedMicrophoneIdAtStart = S.selectedMicrophoneId;
            const microphoneSelectionGenerationAtStart = microphoneSelectionGeneration;
            const microphoneOpenResult = await openMicrophoneStreamWithFallback(
                baseAudioConstraints,
                micStartToken,
                selectedMicrophoneIdAtStart,
                microphoneSelectionGenerationAtStart
            );
            if (microphoneOpenResult.cancelled === true) {
                // A newer attempt can commit while this one is still awaiting
                // getUserMedia. Its pipeline and UI are shared globals, so the
                // late loser must report the live winner instead of painting
                // "not recording" over it.
                return finishCancelledMicStart(_mic, micStartToken);
            }
            ownStream = microphoneOpenResult.stream;

            // 检查音频轨道状态
            const audioTracks = ownStream.getAudioTracks();
            console.log(window.t('console.audioTrackCount'), audioTracks.length);
            console.log(window.t('console.audioTrackStatus'), audioTracks.map(track => ({
                label: track.label,
                enabled: track.enabled,
                muted: track.muted,
                readyState: track.readyState
            })));

            if (audioTracks.length === 0) {
                console.error(window.t('console.noAudioTrackAvailable'));
                window.showStatusToast(window.t ? window.t('app.micAccessDenied') : '无法访问麦克风', 4000);
                if (_mic) {
                    _mic.classList.remove('recording');
                    _mic.classList.remove('active');
                }
                // Never published, so nothing else can reach it to release it.
                try {
                    ownStream.getTracks().forEach(track => track.stop());
                } catch (_) {
                    // best-effort teardown
                }
                throw new Error('没有可用的音频轨道');
            }

            const micStartCommitted = await startAudioWorklet(
                ownStream,
                micStartToken,
                selectedMicrophoneIdAtStart,
                microphoneSelectionGenerationAtStart
            );
            if (!micStartCommitted) {
                // Superseded or fail-closed while opening: the hardware is
                // already torn down, so restore the pre-start UI and leave
                // WITHOUT the success path below. Not an error -- no toast, and
                // nothing to throw at a caller who did nothing wrong.
                //
                // Skipped when a newer attempt has since COMMITTED: this UI is
                // global, same as the S.* fields the unwind is careful about,
                // and painting "not recording" over a window that is recording
                // is the display-plane half of the same bug.
                // A normal cancellation has no device/worklet error to
                // propagate, but the outer voice starter must distinguish it
                // from a committed capture before publishing session success.
                return finishCancelledMicStart(_mic, micStartToken);
            }
            if (
                microphoneOpenResult.fallbackFromMicrophoneId
                && S.selectedMicrophoneId === microphoneOpenResult.fallbackFromMicrophoneId
            ) {
                applySystemDefaultMicrophoneSelection();
                if (typeof window.showStatusToast === 'function') {
                    window.showStatusToast(
                        window.t
                            ? window.t('app.microphoneFallbackToDefault')
                            : '所选麦克风无法使用，已自动切换到系统默认麦克风',
                        6000,
                        { important: true }
                    );
                }
            }
            if (S.gameVoiceSttGateActive) {
                startGameVoiceSttGate();
            }

            if (_mic)    _mic.disabled = true;
            if (_mute)   _mute.disabled = false;
            if (_screen) _screen.disabled = false;
            if (_stop)   _stop.disabled = true;
            if (_reset)  _reset.disabled = false;
            window.showStatusToast(window.t ? window.t('app.speaking') : '正在语音...', 2000);

            // 确保active类存在
            if (_mic && !_mic.classList.contains('active')) {
                _mic.classList.add('active');
            }
            if (typeof window.syncFloatingMicButtonState === 'function') {
                window.syncFloatingMicButtonState(true);
            }

            // 立即更新音量显示状态（显示"检测中"）
            updateMicVolumeStatusNow(true);

            // 开始录音时，停止主动搭话定时器
            if (typeof window.stopProactiveChatSchedule === 'function') {
                window.stopProactiveChatSchedule();
            }
            return true;
        } catch (err) {
            console.error(window.t('console.getMicrophonePermissionFailed'), err);
            // A worklet setup failure already showed its own, more accurate
            // toast before rethrowing; do not stack "cannot access microphone"
            // on top of "AudioWorklet failed to load" -- microphone access
            // demonstrably succeeded in that case.
            if (!(err && err.voiceWorkletSetupFailed)) {
                window.showStatusToast(window.t ? window.t('app.micAccessDenied') : '无法访问麦克风', 4000);
            }

            // Release a device this attempt opened but never published. Guarded
            // on `S.stream !== ownStream` so a throw from the SUCCESS path (the
            // UI updates and startGameVoiceSttGate() below the commit) cannot
            // stop the microphone of a pipeline that is now live and owned.
            if (ownStream && S.stream !== ownStream) {
                try {
                    ownStream.getTracks().forEach(track => track.stop());
                } catch (_) {
                    // best-effort teardown
                }
            }

            const hasOuterVoiceStartLifecycle = !!(S.voiceStartPending || window.isMicStarting);

            const ownsPendingMicUi = pendingMicStartUiOwnerToken === micStartToken;
            if (_mic && ownsPendingMicUi) {
                _mic.classList.remove('recording');
                _mic.classList.remove('active');
            }
            if (!hasOuterVoiceStartLifecycle && ownsPendingMicUi) {
                S.isRecording = false;
                window.isRecording = false;
                S.voiceChatActive = false;
                const textInputArea = document.getElementById('text-input-area');
                if (textInputArea) {
                    textInputArea.classList.remove('hidden');
                }
                if (typeof window.syncVoiceChatComposerHidden === 'function') {
                    window.syncVoiceChatComposerHidden(false);
                }
            }
            stopGameVoiceSttGate({ restoreOrdinaryMic: false });
            throw err;
        } finally {
            if (pendingMicStartUiOwnerToken === micStartToken) {
                pendingMicStartUiOwnerToken = null;
            }
        }
    }

    // 闭麦，按钮on click
    async function stopMicCapture() {
        S.isSwitchingMode = true;

        // 隐藏语音准备提示（防止残留）
        if (typeof window.hideVoicePreparingToast === 'function') {
            window.hideVoicePreparingToast();
        }

        // 清理 session Promise 相关状态
        if (window.sessionTimeoutId) {
            clearTimeout(window.sessionTimeoutId);
            window.sessionTimeoutId = null;
        }
        if (S.sessionStartedRejecter) {
            try {
                S.sessionStartedRejecter(new Error('Session aborted'));
            } catch (e) { /* ignore already handled */ }
            S.sessionStartedRejecter = null;
        }
        if (S.sessionStartedResolver) {
            S.sessionStartedResolver = null;
        }

        const _mic = micButton();
        const _mute = muteButton();
        const _screen = screenButton();
        const _stop = stopButton();
        const _reset = resetSessionButton();

        // 停止录音时移除录音状态类
        if (_mic) {
            _mic.classList.remove('recording');
            _mic.classList.remove('active');
        }
        if (_screen) _screen.classList.remove('active');

        // 同步浮动按钮状态
        if (typeof window.syncFloatingMicButtonState === 'function') {
            window.syncFloatingMicButtonState(false);
        }
        if (typeof window.syncFloatingScreenButtonState === 'function') {
            window.syncFloatingScreenButtonState(false);
        }

        // 立即更新音量显示状态（显示"未录音"）
        updateMicVolumeStatusNow(false);

        stopRecording();

        if (_mic)    _mic.disabled = false;
        if (_mute)   _mute.disabled = true;
        if (_screen) _screen.disabled = true;
        if (_stop)   _stop.disabled = true;
        if (_reset)  _reset.disabled = false;

        // 显示文本输入区
        S.voiceChatActive = false;
        const textInputArea = document.getElementById('text-input-area');
        if (textInputArea) textInputArea.classList.remove('hidden');
        if (typeof window.syncVoiceChatComposerHidden === 'function') {
            window.syncVoiceChatComposerHidden(false);
        }

        // 停止录音后，重置主动搭话退避级别并开始定时
        if (S.proactiveChatEnabled && typeof window.hasAnyChatModeEnabled === 'function' && window.hasAnyChatModeEnabled()) {
            window.lastUserInputTime = Date.now();
            if (typeof window.resetProactiveChatBackoff === 'function') {
                window.resetProactiveChatBackoff();
            }
        }

        // 显示待机状态
        const lanlanName = (window.lanlan_config && window.lanlan_config.lanlan_name) || '';
        window.showStatusToast(window.t ? window.t('app.standby', { name: lanlanName }) : `${lanlanName}待机中...`, 2000);

        // 延迟重置模式切换标志
        setTimeout(() => {
            S.isSwitchingMode = false;
        }, 500);
    }

    // 停止录音（内部辅助，清理音频管道与后端通信）
    function stopRecording(options) {
        options = options || {};
        const notifyServer = options.notifyServer !== false;
        // 停止语音期间主动视觉定时
        if (typeof window.stopProactiveVisionDuringSpeech === 'function') {
            window.stopProactiveVisionDuringSpeech();
        }
        // 输入结束/打断时重置搜歌任务
        if (typeof window.invalidatePendingMusicSearch === 'function') {
            window.invalidatePendingMusicSearch();
        }

        if (typeof window.stopScreening === 'function') {
            window.stopScreening();
        }
        stopGameVoiceSttGate({ restoreOrdinaryMic: false });
        if (typeof window.removeExternalAsrPreview === 'function') {
            window.removeExternalAsrPreview();
        }
        // Ordinary user stop must also drop the independent-ASR route flags.
        // Only failure paths (BLOCKED / terminal ASR_INDEPENDENT_* statuses in
        // app-websocket.js) reset them otherwise, so the mic settings hint
        // would keep claiming "Independent ASR active" after the session
        // ended. The next voice session re-derives both fields from fresh
        // ASR_INDEPENDENT_* status events (lifecycle.py _start_session_activate
        // re-runs the route on every start_session).
        S.independentAsrActive = false;
        S.independentAsrProvider = '';
        // Cancel a start still inside its getUserMedia()/addModule() window,
        // BEFORE the isRecording early-out below. S.isRecording only flips at
        // the very end of startAudioWorklet, so every "stop the mic" path --
        // the user pressing stop, the server's auto_close_mic, a websocket
        // close -- used to early-return here and leave the in-flight attempt to
        // commit afterwards: the UI flipped back to recording and the client
        // re-claimed the lease from a backend that had just released it. Only
        // the text-takeover and blocked-route aborts bumped this counter, so
        // every other teardown was unable to cancel a start it had every right
        // to cancel.
        //
        // Safe for the restart flows: each of them calls startMicCapture()
        // afterwards, which mints its own token, so invalidating here cannot
        // cancel the start they are about to make.
        invalidatePendingMicStart();
        if (!S.isRecording) return;

        S.isRecording = false;
        window.isRecording = false;
        refreshMicLease();
        window.currentGeminiMessage = null;

        // 重置语音模式用户转录合并追踪
        S.lastVoiceUserMessage = null;
        S.lastVoiceUserMessageTime = 0;

        // 清理 AI 回复相关的队列和缓冲区
        window._realisticGeminiQueue = [];
        window._realisticGeminiBuffer = '';
        window._geminiTurnFullText = '';
        window._geminiTurnEndSealed = false;
        window._pendingMusicCommand = '';
        window._realisticGeminiVersion = (window._realisticGeminiVersion || 0) + 1;
        window.currentTurnGeminiBubbles = [];
        window._isProcessingRealisticQueue = false;
        window._realisticProcessingOwner = null;

        // 停止静音检测
        stopSilenceDetection();

        // 清理输入analyser
        S.inputAnalyser = null;

        // 停止所有轨道
        if (S.stream) {
            S.stream.getTracks().forEach(track => track.stop());
        }

        // 关闭AudioContext
        if (S.audioContext) {
            if (S.audioContext.state !== 'closed') {
                S.audioContext.close();
            }
            S.audioContext = null;
            S.workletNode = null;
        }

        // 通知服务器暂停会话
        if (notifyServer && S.socket && S.socket.readyState === WebSocket.OPEN) {
            S.socket.send(JSON.stringify({
                action: 'pause_session'
            }));
        }
    }

    // ======================== 音量可视化 ========================

    // 启动麦克风音量可视化
    function startMicVolumeVisualization() {
        // 先停止现有的动画
        stopMicVolumeVisualization();

        // 缓存 DOM 引用，仅在元素被销毁时重新查询
        let cachedBarFill = document.getElementById('mic-volume-bar-fill');
        let cachedStatus = document.getElementById('mic-volume-status');
        let cachedHint = document.getElementById('mic-volume-hint');
        let cachedPopup = document.getElementById('live2d-popup-mic') || document.getElementById('vrm-popup-mic') || document.getElementById('mmd-popup-mic');
        // 时域采样 buffer 提到闭包级复用，避免每帧分配 ~8KB Float32Array
        // 在 60fps 下产生 ~480KB/s 的 GC 抖动。
        let timeDomainBuffer = null;

        function updateVolumeDisplay() {
            // 仅当缓存元素被移出 DOM 时才重新查询（popup 重建场景）
            if (!cachedBarFill || !cachedBarFill.isConnected) {
                cachedBarFill = document.getElementById('mic-volume-bar-fill');
                cachedStatus = document.getElementById('mic-volume-status');
                cachedHint = document.getElementById('mic-volume-hint');
                cachedPopup = document.getElementById('live2d-popup-mic') || document.getElementById('vrm-popup-mic') || document.getElementById('mmd-popup-mic');
            }

            if (!cachedBarFill) {
                stopMicVolumeVisualization();
                return;
            }

            // 检查弹出框是否仍然可见：不可见时只需低频探测它何时重新出现，
            // 不必按刷新率排 rAF（弹窗关着也让 Blink 每 vsync 跑主帧）
            if (!cachedPopup || cachedPopup.style.display === 'none' || !cachedPopup.offsetParent) {
                scheduleMicVolumeFrame(MIC_VOLUME_HIDDEN_POLL_MS);
                return;
            }

            // 检查是否正在录音且有 analyser
            if (S.isRecording && S.inputAnalyser) {
                // 用时域数据反映 worklet/AI 实际收到的线性振幅。
                // 频域 + 默认 dB 刻度（-100..-30dB）会在人声常见电平就饱和，
                // 软件增益和过载在条上看不出区别，正是用户反馈的根因。
                //
                // 必须用 getFloatTimeDomainData 而不是 byte：byte 量化步长 1/128，
                // byte=255 实际覆盖 [127/128, ∞) 浮点区间，loud-but-clean 信号
                // (峰值 0.99 但 worklet 不会硬切) 也会被误判成 clip。
                const fftSize = S.inputAnalyser.fftSize;
                if (!timeDomainBuffer || timeDomainBuffer.length !== fftSize) {
                    timeDomainBuffer = new Float32Array(fftSize);
                }
                S.inputAnalyser.getFloatTimeDomainData(timeDomainBuffer);

                let peak = 0;
                let sumSq = 0;
                let clippedCount = 0;
                for (let i = 0; i < fftSize; i++) {
                    const val = timeDomainBuffer[i];
                    const abs = val < 0 ? -val : val;
                    if (abs > peak) peak = abs;
                    sumSq += val * val;
                    // worklet 的 `Math.max(-1, Math.min(1, x))*0x7FFF` 只在浮点
                    // 严格越过 ±1 时才硬切。0.999 留一点浮点比较容差。
                    if (abs >= 0.999) clippedCount++;
                }
                const rms = Math.sqrt(sumSq / fftSize);

                // 显示用 peak（更直观地反映"接近削顶"的距离），
                // 状态判定结合 RMS：信号能量高于 noise floor 才进入分级。
                const volumePercent = Math.min(100, peak * 100);
                // 一帧内 >=0.5% 样本撞到 ±1 视作过载（≈10/2048）。worklet
                // 的 `Math.max(-1, Math.min(1, x))*0x7FFF` 在这个边界硬切，
                // 失真无关用户是否说话，所以唯一无歧义的红色告警就是 clip。
                const isClipping = clippedCount >= fftSize * 0.005;
                // hasSignal：RMS 高于后端 AGC noise floor（0.015）的半档，
                // 视作"用户在说话"——只有这种情况才对偏低/正常做颜色提示，
                // 没说话时不能用警告色把用户吓到。
                const hasSignal = rms >= 0.008;
                const lowVolume = hasSignal && peak < 0.15;
                // high 必须门控 hasSignal：静默期键盘/桌面敲击等瞬态噪声
                // peak 可能短暂 > 0.85 但 RMS 仍低于 noise floor，没有 hasSignal
                // 守住会让"等待中"被误判为"音量较高"。
                const high = hasSignal && !isClipping && peak > 0.85;

                // 更新音量条（条宽始终跟着 peak，没说话时自然就短）
                cachedBarFill.style.width = `${volumePercent}%`;

                // 根据状态设置颜色
                if (isClipping) {
                    cachedBarFill.style.backgroundColor = '#dc3545'; // 红 - 过载（唯一警告）
                } else if (high) {
                    cachedBarFill.style.backgroundColor = '#fd7e14'; // 橙 - 接近过载
                } else if (lowVolume) {
                    cachedBarFill.style.backgroundColor = '#ffc107'; // 黄 - 在说话但偏低
                } else if (hasSignal) {
                    cachedBarFill.style.backgroundColor = '#28a745'; // 绿 - 正常
                } else {
                    cachedBarFill.style.backgroundColor = '#4f8cff'; // 蓝 - 静默/等待
                }

                // 更新状态文字
                if (cachedStatus) {
                    if (isClipping) {
                        cachedStatus.textContent = window.t ? window.t('microphone.volumeClipping') : '过载';
                        cachedStatus.style.color = '#dc3545';
                    } else if (high) {
                        cachedStatus.textContent = window.t ? window.t('microphone.volumeHigh') : '音量较高';
                        cachedStatus.style.color = '#fd7e14';
                    } else if (lowVolume) {
                        cachedStatus.textContent = window.t ? window.t('microphone.volumeLow') : '音量偏低';
                        cachedStatus.style.color = '#ffc107';
                    } else if (hasSignal) {
                        cachedStatus.textContent = window.t ? window.t('microphone.volumeNormal') : '正常';
                        cachedStatus.style.color = '#28a745';
                    } else {
                        cachedStatus.textContent = window.t ? window.t('microphone.volumeWaiting') : '等待声音';
                        cachedStatus.style.color = 'var(--neko-popup-text-sub)';
                    }
                }

                // 更新提示文字（分支顺序与上面的 status 保持一致：
                // clipping → high → lowVolume → hasSignal → idle）
                if (cachedHint) {
                    if (isClipping) {
                        cachedHint.textContent = window.t ? window.t('microphone.volumeHintClipping') : '麦克风增益过高，音频被削顶，AI 可能识别异常，请调低增益';
                    } else if (high) {
                        cachedHint.textContent = window.t ? window.t('microphone.volumeHintHigh') : '音量偏高，建议调低增益';
                    } else if (lowVolume) {
                        cachedHint.textContent = window.t ? window.t('microphone.volumeHintLow') : '音量较低，建议调高增益';
                    } else if (hasSignal) {
                        cachedHint.textContent = window.t ? window.t('microphone.volumeHintOk') : '麦克风工作正常';
                    } else {
                        cachedHint.textContent = window.t ? window.t('microphone.volumeHintWaiting') : '麦克风正在监听，请说话';
                    }
                }
            } else {
                // 未录音状态
                cachedBarFill.style.width = '0%';
                cachedBarFill.style.backgroundColor = '#4f8cff';
                if (cachedStatus) {
                    cachedStatus.textContent = window.t ? window.t('microphone.volumeIdle') : '未录音';
                    cachedStatus.style.color = 'var(--neko-popup-text-sub)';
                }
                if (cachedHint) {
                    cachedHint.textContent = window.t ? window.t('microphone.volumeHint') : '开始录音后可查看音量';
                }
            }

            // 继续下一帧
            scheduleMicVolumeFrame();
        }

        // 排下一帧：渲染后端在定时器驱动时跟随其周期（frame-pacing），否则 rAF；
        // 传 delayMs 时固定用该延时（弹窗不可见的低频探测）
        function scheduleMicVolumeFrame(delayMs) {
            cancelMicVolumeFrame();
            if (Number(delayMs) > 0) {
                const id = setTimeout(updateVolumeDisplay, Number(delayMs));
                _micVolumePacedCancel = () => clearTimeout(id);
                return;
            }
            const pacing = window.nekoFramePacing;
            if (pacing && typeof pacing.requestPacedFrame === 'function') {
                _micVolumePacedCancel = pacing.requestPacedFrame(updateVolumeDisplay);
                return;
            }
            S.micVolumeAnimationId = requestAnimationFrame(updateVolumeDisplay);
        }

        // 启动动画循环
        scheduleMicVolumeFrame();
    }

    // 弹窗不可见时探测它重新出现的周期
    const MIC_VOLUME_HIDDEN_POLL_MS = 250;
    // 定时器驱动路径的取消句柄（rAF 路径用 S.micVolumeAnimationId）
    let _micVolumePacedCancel = null;

    function cancelMicVolumeFrame() {
        if (S.micVolumeAnimationId) {
            cancelAnimationFrame(S.micVolumeAnimationId);
            S.micVolumeAnimationId = null;
        }
        if (_micVolumePacedCancel) {
            try { _micVolumePacedCancel(); } catch (_) {}
            _micVolumePacedCancel = null;
        }
    }

    // 停止麦克风音量可视化
    function stopMicVolumeVisualization() {
        cancelMicVolumeFrame();
    }

    // 立即更新音量显示状态（用于录音状态变化时立即反映）
    function updateMicVolumeStatusNow(recording) {
        const volumeBarFill = document.getElementById('mic-volume-bar-fill');
        const volumeStatus = document.getElementById('mic-volume-status');
        const volumeHint = document.getElementById('mic-volume-hint');

        if (recording) {
            if (volumeStatus) {
                volumeStatus.textContent = window.t ? window.t('microphone.volumeDetecting') : '检测中...';
                volumeStatus.style.color = '#4f8cff';
            }
            if (volumeHint) {
                volumeHint.textContent = window.t ? window.t('microphone.volumeHintDetecting') : '正在检测麦克风输入...';
            }
            if (volumeBarFill) {
                volumeBarFill.style.backgroundColor = '#4f8cff';
            }
        } else {
            if (volumeBarFill) {
                volumeBarFill.style.width = '0%';
                volumeBarFill.style.backgroundColor = '#4f8cff';
            }
            if (volumeStatus) {
                volumeStatus.textContent = window.t ? window.t('microphone.volumeIdle') : '未录音';
                volumeStatus.style.color = 'var(--neko-popup-text-sub)';
            }
            if (volumeHint) {
                volumeHint.textContent = window.t ? window.t('microphone.volumeHint') : '开始录音后可查看音量';
            }
        }
    }

    // ======================== 暴露到 window（向后兼容） ========================
    window.startMicCapture = startMicCapture;
    window.stopMicCapture = stopMicCapture;
    window.abortVoiceStartForBlockedRoute = abortVoiceStartForBlockedRoute;
    window.invalidatePendingMicStart = invalidatePendingMicStart;
    window.stopRecording = stopRecording;
    window.startSilenceDetection = startSilenceDetection;
    window.stopSilenceDetection = stopSilenceDetection;
    window.monitorInputVolume = monitorInputVolume;
    window.selectMicrophone = selectMicrophone;
    window.loadSelectedMicrophone = loadSelectedMicrophone;
    window.saveSelectedMicrophone = saveSelectedMicrophone;
    window.saveMicGainSetting = saveMicGainSetting;
    window.loadMicGainSetting = loadMicGainSetting;
    window.formatGainDisplay = formatGainDisplay;
    window.startMicVolumeVisualization = startMicVolumeVisualization;
    window.stopMicVolumeVisualization = stopMicVolumeVisualization;
    window.updateMicVolumeStatusNow = updateMicVolumeStatusNow;
    window.startGameVoiceSttGate = startGameVoiceSttGate;
    window.stopGameVoiceSttGate = stopGameVoiceSttGate;

    function isTutorialShortcutBlockedForMicMute() {
        if (typeof window.isNekoShortcutBlockedByTutorial === 'function') {
            return window.isNekoShortcutBlockedByTutorial();
        }
        return window.isInTutorial === true;
    }

    window.toggleMicMute = function(showToast = true) {
        if (isTutorialShortcutBlockedForMicMute()) {
            console.log('[Electron Shortcut] toggleMicMute: blocked - tutorial active');
            return S.isMicMuted;
        }
        S.isMicMuted = !S.isMicMuted;
        refreshMicLease();
        if (S.isMicMuted) {
            stopSilenceDetection();
            // 立刻清掉"用户最近在说话"的时间戳。否则 mute 前最后一帧
            // RMS 写入的 userRecentSpeechTime 会在 8s 内继续让
            // _isUserRecentlySpeaking() 返回 true，proactive nudge
            // 在窗口期内仍会被 skip。
            S.userRecentSpeechTime = 0;
        } else if (S.isRecording) {
            startSilenceDetection();
        }
        if (S.gameVoiceSttGateActive) {
            if (S.isMicMuted) {
                stopGameVoiceSttGate({ keepActive: true });
            } else {
                startGameVoiceSttGate();
            }
        }
        window.dispatchEvent(new CustomEvent('mic-mute-state-changed', {
            detail: { muted: S.isMicMuted }
        }));
        if (showToast && typeof window.showStatusToast === 'function') {
            const message = S.isMicMuted
                ? (window.t ? window.t('app.micMuted') : '麦克风已静音')
                : (window.t ? window.t('app.micUnmuted') : '麦克风已取消静音');
            window.showStatusToast(message, 2000);
        }
        return S.isMicMuted;
    };

    window.setMicMuted = function(muted, showToast = false) {
        S.isMicMuted = muted;
        refreshMicLease();
        if (S.isMicMuted) {
            stopSilenceDetection();
            // 与 toggleMicMute 对齐：进入 muted 时清掉时间戳，避免拖尾。
            S.userRecentSpeechTime = 0;
        } else if (S.isRecording) {
            startSilenceDetection();
        }
        if (S.gameVoiceSttGateActive) {
            if (S.isMicMuted) {
                stopGameVoiceSttGate({ keepActive: true });
            } else {
                startGameVoiceSttGate();
            }
        }
        window.dispatchEvent(new CustomEvent('mic-mute-state-changed', {
            detail: { muted: S.isMicMuted }
        }));
        if (showToast && typeof window.showStatusToast === 'function') {
            const message = S.isMicMuted
                ? (window.t ? window.t('app.micMuted') : '麦克风已静音')
                : (window.t ? window.t('app.micUnmuted') : '麦克风已取消静音');
            window.showStatusToast(message, 2000);
        }
    };

    window.isMicMuted = function() {
        return S.isMicMuted;
    };
    // setMicrophoneGain / getMicrophoneGain 已在上方直接定义为 window 属性

    // ======================== 模块导出 ========================
    mod.selectMicrophone = selectMicrophone;
    mod.saveSelectedMicrophone = saveSelectedMicrophone;
    mod.loadSelectedMicrophone = loadSelectedMicrophone;
    mod.saveMicGainSetting = saveMicGainSetting;
    mod.loadMicGainSetting = loadMicGainSetting;
    mod.loadNoiseReductionSetting = loadNoiseReductionSetting;
    mod.saveNoiseReductionSetting = saveNoiseReductionSetting;
    mod.formatGainDisplay = formatGainDisplay;
    mod.startSilenceDetection = startSilenceDetection;
    mod.stopSilenceDetection = stopSilenceDetection;
    mod.monitorInputVolume = monitorInputVolume;
    mod.startAudioWorklet = startAudioWorklet;
    mod.startMicCapture = startMicCapture;
    mod.stopMicCapture = stopMicCapture;
    mod.invalidatePendingMicStart = invalidatePendingMicStart;
    mod.stopRecording = stopRecording;
    mod.startMicVolumeVisualization = startMicVolumeVisualization;
    mod.stopMicVolumeVisualization = stopMicVolumeVisualization;
    mod.updateMicVolumeStatusNow = updateMicVolumeStatusNow;
    mod.startGameVoiceSttGate = startGameVoiceSttGate;
    mod.stopGameVoiceSttGate = stopGameVoiceSttGate;
    mod.refreshMicLease = refreshMicLease;
    mod.sendVoiceInputControlState = sendVoiceInputControlState;
    mod.canUploadOrdinaryMicFrame = canUploadOrdinaryMicFrame;
    mod.setVoiceInputLifecycleState = setVoiceInputLifecycleState;

    // ======================== 麦克风设备列表 UI ========================

    var micPermissionGranted = false;
    var cachedMicDevices = null;
    var cachedSpeakerDevices = null;
    var mediaDeviceChangeGeneration = 0;
    var mediaDeviceEnumerationGeneration = 0;
    var latestMediaDeviceEnumerationPromise = Promise.resolve(null);
    var disposeVoiceRecognitionPopover = null;
    var voiceRecognitionPopoverRenderGeneration = 0;

    function ensureMicPopupScrollbarStyle() {
        if (document.getElementById('neko-mic-popup-scrollbar-style')) return;
        var style = document.createElement('style');
        style.id = 'neko-mic-popup-scrollbar-style';
        style.textContent = [
            '#live2d-popup-mic.neko-mic-popup-surface,',
            '#vrm-popup-mic.neko-mic-popup-surface,',
            '#mmd-popup-mic.neko-mic-popup-surface{overflow-y:hidden!important;scrollbar-width:none;}',
            '#live2d-popup-mic.neko-mic-popup-surface::-webkit-scrollbar,',
            '#vrm-popup-mic.neko-mic-popup-surface::-webkit-scrollbar,',
            '#mmd-popup-mic.neko-mic-popup-surface::-webkit-scrollbar,',
            '.neko-mic-popup-scroll::-webkit-scrollbar{width:0;height:0;}',
            '.neko-mic-popup-scroll{scrollbar-width:none;-ms-overflow-style:none;}',
            '.neko-mic-popup-scrollbar-thumb{position:absolute;right:3px;top:0;width:4px;min-height:18px;border-radius:999px;background:rgba(128,128,128,0.55);opacity:0;transition:opacity 120ms ease;pointer-events:none;z-index:2;}',
            '.neko-mic-popup-scrollbar-thumb.is-visible{opacity:1;}'
        ].join('');
        document.head.appendChild(style);
    }

    function attachTransientMicPopupScrollbar(scrollNode, hostNode) {
        var thumb = document.createElement('div');
        thumb.className = 'neko-mic-popup-scrollbar-thumb';
        hostNode.appendChild(thumb);
        var hideTimer = null;
        var frameId = null;
        var updateThumb = function () {
            frameId = null;
            var maxScroll = Math.max(0, scrollNode.scrollHeight - scrollNode.clientHeight);
            if (maxScroll <= 0) {
                thumb.classList.remove('is-visible');
                return;
            }
            var scrollRect = scrollNode.getBoundingClientRect();
            var hostRect = hostNode.getBoundingClientRect();
            var trackTop = scrollRect.top - hostRect.top;
            var trackHeight = scrollNode.clientHeight;
            var thumbHeight = Math.max(18, Math.round((scrollNode.clientHeight / scrollNode.scrollHeight) * trackHeight));
            var thumbTop = trackTop + Math.round((scrollNode.scrollTop / maxScroll) * Math.max(0, trackHeight - thumbHeight));
            thumb.style.height = thumbHeight + 'px';
            thumb.style.transform = 'translateY(' + thumbTop + 'px)';
        };
        var showScrollbar = function () {
            thumb.classList.add('is-visible');
            if (!frameId) frameId = window.requestAnimationFrame(updateThumb);
            if (hideTimer) window.clearTimeout(hideTimer);
            hideTimer = window.setTimeout(function () {
                thumb.classList.remove('is-visible');
                hideTimer = null;
            }, 850);
        };
        scrollNode.addEventListener('scroll', showScrollbar, { passive: true });
        scrollNode.addEventListener('wheel', showScrollbar, { passive: true });
        scrollNode.addEventListener('touchmove', showScrollbar, { passive: true });
        return function cleanupTransientMicPopupScrollbar() {
            if (hideTimer) {
                window.clearTimeout(hideTimer);
                hideTimer = null;
            }
            if (frameId) {
                window.cancelAnimationFrame(frameId);
                frameId = null;
            }
            scrollNode.removeEventListener('scroll', showScrollbar);
            scrollNode.removeEventListener('wheel', showScrollbar);
            scrollNode.removeEventListener('touchmove', showScrollbar);
            if (thumb.parentNode) thumb.parentNode.removeChild(thumb);
        };
    }

    async function enumerateAndCacheMediaDevices() {
        var enumerationGeneration = ++mediaDeviceEnumerationGeneration;
        var ownEnumerationPromise = navigator.mediaDevices.enumerateDevices().then(async function (devices) {
            if (enumerationGeneration !== mediaDeviceEnumerationGeneration) {
                return null;
            }
            cachedMicDevices = devices.filter(function (device) { return device.kind === 'audioinput'; });
            cachedSpeakerDevices = devices.filter(function (device) { return device.kind === 'audiooutput'; });
            // Cache ownership and speaker reconciliation form one transition.
            // If a newer enumeration starts while this route is queued, it
            // also queues its own reconciliation on the playback module's
            // bounded transaction tail and therefore becomes the final route.
            if (typeof window.reconcileSelectedSpeakerDevices === 'function') {
                await window.reconcileSelectedSpeakerDevices(devices);
            }
            if (enumerationGeneration !== mediaDeviceEnumerationGeneration) {
                return null;
            }
            return {
                devices: devices,
                generation: enumerationGeneration
            };
        });
        var committedEnumerationPromise = ownEnumerationPromise.then(function (result) {
            if (result) return result;
            // A generation can become stale only after a newer invocation has
            // synchronously replaced this scalar. Reuse that committed result
            // so callers never reconcile or render an older device snapshot.
            return latestMediaDeviceEnumerationPromise;
        });
        latestMediaDeviceEnumerationPromise = committedEnumerationPromise;
        return committedEnumerationPromise;
    }

    /** 请求麦克风权限并缓存设备列表 */
    async function ensureMicrophonePermission() {
        if (micPermissionGranted && cachedMicDevices && cachedMicDevices.length > 0) {
            return cachedMicDevices;
        }
        try {
            var tempStream = await navigator.mediaDevices.getUserMedia({ audio: true });
            tempStream.getTracks().forEach(function (track) { track.stop(); });
            micPermissionGranted = true;
            console.log('麦克风权限已获取');
            await enumerateAndCacheMediaDevices();
            return cachedMicDevices || [];
        } catch (error) {
            console.warn('请求麦克风权限失败:', error);
            try {
                await enumerateAndCacheMediaDevices();
                return cachedMicDevices || [];
            } catch (enumError) {
                console.error('获取设备列表失败:', enumError);
                return [];
            }
        }
    }

    // 监听设备变化，更新缓存
    if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
        navigator.mediaDevices.addEventListener('devicechange', async function () {
            var deviceChangeGeneration = ++mediaDeviceChangeGeneration;
            console.log('检测到设备变化，刷新麦克风列表...');
            try {
                var enumerationResult = await enumerateAndCacheMediaDevices();
                if (deviceChangeGeneration !== mediaDeviceChangeGeneration) return;
                if (
                    deviceChangeGeneration !== mediaDeviceChangeGeneration
                    || enumerationResult.generation !== mediaDeviceEnumerationGeneration
                ) return;
                var micPopup = document.getElementById('live2d-popup-mic') || document.getElementById('vrm-popup-mic') || document.getElementById('mmd-popup-mic');
                if (micPopup && micPopup.style.display === 'flex') {
                    await window.renderFloatingMicList();
                }
            } catch (error) {
                console.error('设备变化后更新列表失败:', error);
            }
        });
    }

    /** 为浮动弹出框渲染麦克风列表 */
    window.renderFloatingMicList = async function (popupArg) {
        var micPopup = popupArg || document.getElementById('live2d-popup-mic') || document.getElementById('vrm-popup-mic') || document.getElementById('mmd-popup-mic');
        if (!micPopup) return false;
        var renderGeneration = ++voiceRecognitionPopoverRenderGeneration;
        var coreApiCapabilityRefreshedAt = 0;
        if (disposeVoiceRecognitionPopover) {
            var previousDispose = disposeVoiceRecognitionPopover;
            disposeVoiceRecognitionPopover = null;
            previousDispose();
        }
        var popupId = micPopup.id;
        var isPopupAvailable = function () {
            if (!micPopup || !micPopup.isConnected) return false;
            if (popupId && document.getElementById(popupId) !== micPopup) return false;
            return micPopup.style.display === 'flex' && micPopup.style.opacity !== '0';
        };
        if (!isPopupAvailable()) return false;

        try {
            if (typeof window.refreshCoreApiCapability === 'function') {
                coreApiCapabilityRefreshedAt = Date.now();
                // Capability is tri-state and null is deliberately fail-open.
                // Refresh in the background so a slow config endpoint can
                // never hold the microphone device list hostage; the helper's
                // change event updates this panel when the response arrives.
                Promise.resolve(
                    window.refreshCoreApiCapability({ force: true })
                ).catch(function () { /* refresh helper owns reporting */ });
            }
            ensureMicPopupScrollbarStyle();
            micPopup.classList.add('neko-mic-popup-surface');
            micPopup.style.minWidth = '220px';
            micPopup.style.width = '220px';
            micPopup.style.maxWidth = '220px';
            micPopup.style.boxSizing = 'border-box';
            micPopup.style.overflowY = 'hidden';
            var audioInputs = cachedMicDevices;
            if (!audioInputs || audioInputs.length === 0 || !micPermissionGranted) {
                audioInputs = await ensureMicrophonePermission();
            }
            var allMediaDevices = null;
            if (!cachedSpeakerDevices) {
                var enumerationResult = await enumerateAndCacheMediaDevices();
                allMediaDevices = enumerationResult.devices;
            }
            if (typeof window.reconcileSelectedSpeakerDevices === 'function') {
                await window.reconcileSelectedSpeakerDevices(
                    allMediaDevices || cachedMicDevices.concat(cachedSpeakerDevices)
                );
            }
            var defaultSpeakerDeviceId = C.DEFAULT_SPEAKER_DEVICE_ID || 'default';
            function isPhysicalSpeakerDevice(device) {
                return device.deviceId !== defaultSpeakerDeviceId
                    && device.deviceId !== 'communications';
            }
            var audioOutputs = cachedSpeakerDevices.filter(isPhysicalSpeakerDevice);
            if (
                renderGeneration !== voiceRecognitionPopoverRenderGeneration
                || !isPopupAvailable()
            ) return false;
if (typeof micPopup.__nekoMicScrollbarCleanup === 'function') {
                micPopup.__nekoMicScrollbarCleanup();
                micPopup.__nekoMicScrollbarCleanup = null;
            }
            micPopup.innerHTML = '';

            var hasMicrophoneDevices = audioInputs.length > 0;

            // ===== 双栏布局 =====
            var leftColumn = document.createElement('div');
            leftColumn.className = 'neko-mic-popup-scroll';
            Object.assign(leftColumn.style, { flex: '0 0 100%', width: '100%', minWidth: '0', minHeight: '0', maxWidth: '100%', display: 'flex', flexDirection: 'column', overflowY: 'auto' });
            micPopup.__nekoMicScrollbarCleanup = attachTransientMicPopupScrollbar(leftColumn, micPopup);

            if (micPopup.id) {
                document.querySelectorAll('[data-neko-sidepanel-owner="' + micPopup.id + '"].neko-mic-subwindow').forEach(function (panel) {
                    panel.remove();
                });
            }

            // ===== 左栏 1. 扬声器音量 =====
            var speakerContainer = document.createElement('div');
            speakerContainer.className = 'speaker-volume-container';
            speakerContainer.style.padding = '8px 12px';

            var speakerHeader = document.createElement('div');
            Object.assign(speakerHeader.style, { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' });

            var speakerLabel = document.createElement('span');
            speakerLabel.textContent = window.t ? window.t('speaker.volumeLabel') : '扬声器音量';
            speakerLabel.setAttribute('data-i18n', 'speaker.volumeLabel');
            Object.assign(speakerLabel.style, { fontSize: '13px', color: 'var(--neko-popup-text)', fontWeight: '500' });

            var speakerValue = document.createElement('span');
            speakerValue.id = 'speaker-volume-value';
            speakerValue.textContent = S.speakerVolume + '%';
            Object.assign(speakerValue.style, { fontSize: '12px', color: '#4f8cff', fontWeight: '500' });

            speakerHeader.appendChild(speakerLabel);
            speakerHeader.appendChild(speakerValue);
            speakerContainer.appendChild(speakerHeader);

            // 非线性轨道：thumb 位置走 0..SPEAKER_SLIDER_TRACK_MAX（千分比精度），
            // 经膝点映射成 0-200% 音量，使常规的 0-100% 占满轨道前 3/4、100% 落在锚点处。
            var SPEAKER_SLIDER_TRACK_MAX = 1000;
            // Matches Chromium/Electron's native range thumb; the anchor tick is cosmetic.
            var SPEAKER_SLIDER_THUMB_SIZE = 16;
            var SPEAKER_VOLUME_SNAP_RADIUS = 1;
            var SPEAKER_VOLUME_NORMAL_COLOR = '#4f8cff';
            var SPEAKER_VOLUME_BOOST_COLOR = '#ff9f43';

            function speakerTrackPosFromVolume(vol) {
                return Math.round(window.appUtils.valueToKneeTrack(
                    vol, C.DEFAULT_SPEAKER_VOLUME, C.MAX_SPEAKER_VOLUME, C.SPEAKER_VOLUME_KNEE_RATIO
                ) * SPEAKER_SLIDER_TRACK_MAX);
            }
            function speakerVolumeFromTrackPos(pos) {
                return Math.round(window.appUtils.kneeTrackToValue(
                    pos / SPEAKER_SLIDER_TRACK_MAX, C.DEFAULT_SPEAKER_VOLUME, C.MAX_SPEAKER_VOLUME, C.SPEAKER_VOLUME_KNEE_RATIO
                ));
            }
            var speakerVolumeAnchorTrackPos = speakerTrackPosFromVolume(C.DEFAULT_SPEAKER_VOLUME);
            var speakerSliderPointerActive = false;
            var speakerSliderHadPointerInput = false;
            function speakerThumbAlignedLeft(trackPos) {
                var ratio = trackPos / SPEAKER_SLIDER_TRACK_MAX;
                var halfThumb = SPEAKER_SLIDER_THUMB_SIZE / 2;
                var thumbOffsetPx = halfThumb * (1 - 2 * ratio);
                return 'calc(' + (ratio * 100) + '% + ' + thumbOffsetPx.toFixed(2) + 'px)';
            }
            function snapSpeakerTrackPos(pos) {
                return Math.abs(speakerVolumeFromTrackPos(pos) - C.DEFAULT_SPEAKER_VOLUME) <= SPEAKER_VOLUME_SNAP_RADIUS
                    ? speakerVolumeAnchorTrackPos
                    : pos;
            }
            function applySpeakerTrackPos(trackPos) {
                var newVol = speakerVolumeFromTrackPos(trackPos);
                S.speakerVolume = newVol;
                applySpeakerVolumeVisual(newVol);
                if (S.speakerGainNode) {
                    S.speakerGainNode.gain.setTargetAtTime(newVol / 100, S.speakerGainNode.context.currentTime, 0.05);
                }
            }

            var speakerSlider = document.createElement('input');
            speakerSlider.type = 'range';
            speakerSlider.id = 'speaker-volume-slider';
            speakerSlider.min = '0';
            speakerSlider.max = String(SPEAKER_SLIDER_TRACK_MAX);
            speakerSlider.step = '1';
            speakerSlider.value = String(speakerTrackPosFromVolume(S.speakerVolume));
            Object.assign(speakerSlider.style, { width: '100%', height: '6px', borderRadius: '3px', cursor: 'pointer', accentColor: SPEAKER_VOLUME_NORMAL_COLOR, position: 'relative', zIndex: '2', margin: '0' });

            // 超过标准音量（>100%）时数值与轨道染暖色，把增强区从常规区里区分出来
            function applySpeakerVolumeVisual(vol) {
                var color = vol > C.DEFAULT_SPEAKER_VOLUME ? SPEAKER_VOLUME_BOOST_COLOR : SPEAKER_VOLUME_NORMAL_COLOR;
                speakerValue.textContent = vol + '%';
                speakerValue.style.color = color;
                speakerSlider.style.accentColor = color;
            }
            applySpeakerVolumeVisual(S.speakerVolume);

            speakerSlider.addEventListener('pointerdown', function () {
                speakerSliderPointerActive = true;
                speakerSliderHadPointerInput = true;
            });
            speakerSlider.addEventListener('pointerup', function () {
                speakerSliderPointerActive = false;
            });
            speakerSlider.addEventListener('pointercancel', function () {
                speakerSliderPointerActive = false;
            });
            speakerSlider.addEventListener('input', function (e) {
                var trackPos = parseInt(e.target.value, 10);
                if (isNaN(trackPos)) return;
                if (speakerSliderPointerActive) {
                    var snappedTrackPos = snapSpeakerTrackPos(trackPos);
                    if (snappedTrackPos !== trackPos) {
                        trackPos = snappedTrackPos;
                        speakerSlider.value = String(snappedTrackPos);
                    }
                }
                applySpeakerTrackPos(trackPos);
            });
            speakerSlider.addEventListener('change', function (e) {
                var trackPos = parseInt(e.target.value, 10);
                if (!isNaN(trackPos) && speakerSliderHadPointerInput) {
                    var snappedTrackPos = snapSpeakerTrackPos(trackPos);
                    if (snappedTrackPos !== trackPos) {
                        speakerSlider.value = String(snappedTrackPos);
                        applySpeakerTrackPos(snappedTrackPos);
                    }
                }
                speakerSliderHadPointerInput = false;
                if (typeof window.saveSpeakerVolumeSetting === 'function') window.saveSpeakerVolumeSetting();
            });

            // 用相对定位容器在轨道 75% 处画一条 100% 标准锚点，告诉用户「这条线以上是增强」
            var speakerSliderWrap = document.createElement('div');
            Object.assign(speakerSliderWrap.style, { position: 'relative', width: '100%', height: '18px', display: 'flex', alignItems: 'center' });
            var speakerAnchorTick = document.createElement('div');
            Object.assign(speakerAnchorTick.style, {
                position: 'absolute', left: speakerThumbAlignedLeft(speakerVolumeAnchorTrackPos), top: '0', bottom: '0',
                width: '2px', transform: 'translateX(-50%)',
                backgroundColor: 'var(--neko-popup-text-sub)', opacity: '0.28', borderRadius: '1px', pointerEvents: 'none', zIndex: '0'
            });
            speakerSliderWrap.appendChild(speakerSlider);
            speakerSliderWrap.appendChild(speakerAnchorTick);
            speakerContainer.appendChild(speakerSliderWrap);

            var speakerHint = document.createElement('div');
            speakerHint.textContent = window.t ? window.t('speaker.volumeHint') : '调节AI语音的播放音量';
            speakerHint.setAttribute('data-i18n', 'speaker.volumeHint');
            Object.assign(speakerHint.style, { fontSize: '11px', color: 'var(--neko-popup-text-sub)', marginTop: '6px' });
            speakerContainer.appendChild(speakerHint);
            leftColumn.appendChild(speakerContainer);

            // ===== 左栏 1.2. 空间音频开关（多屏立体声 + 距离衰减）=====
            var spatialContainer = document.createElement('div');
            spatialContainer.style.padding = '8px 12px';

            var spatialRow = document.createElement('div');
            Object.assign(spatialRow.style, { display: 'flex', justifyContent: 'space-between', alignItems: 'center' });

            var spatialLabel = document.createElement('span');
            spatialLabel.textContent = window.t ? window.t('speaker.spatialAudioLabel') : '空间音频';
            spatialLabel.setAttribute('data-i18n', 'speaker.spatialAudioLabel');
            Object.assign(spatialLabel.style, { fontSize: '13px', color: 'var(--neko-popup-text)', fontWeight: '500' });

            var spatialEnabled = (window.appSpatialAudio && typeof window.appSpatialAudio.getEnabled === 'function')
                ? window.appSpatialAudio.getEnabled()
                : !!S.spatialAudioEnabled;

            var spatialToggle = document.createElement('label');
            Object.assign(spatialToggle.style, { position: 'relative', display: 'inline-block', width: '36px', height: '20px', flexShrink: '0' });
            var spatialInput = document.createElement('input');
            spatialInput.type = 'checkbox';
            spatialInput.checked = spatialEnabled;
            Object.assign(spatialInput.style, { opacity: '0', width: '0', height: '0' });
            var spatialSliderEl = document.createElement('span');
            Object.assign(spatialSliderEl.style, { position: 'absolute', cursor: 'pointer', top: '0', left: '0', right: '0', bottom: '0', backgroundColor: spatialEnabled ? '#4f8cff' : '#ccc', borderRadius: '10px', transition: 'background-color 0.2s' });
            var spatialKnob = document.createElement('span');
            Object.assign(spatialKnob.style, { position: 'absolute', content: '""', height: '16px', width: '16px', left: spatialEnabled ? '18px' : '2px', bottom: '2px', backgroundColor: 'white', borderRadius: '50%', transition: 'left 0.2s' });
            spatialSliderEl.appendChild(spatialKnob);
            spatialToggle.appendChild(spatialInput);
            spatialToggle.appendChild(spatialSliderEl);

            spatialInput.addEventListener('change', function () {
                var on = spatialInput.checked;
                spatialSliderEl.style.backgroundColor = on ? '#4f8cff' : '#ccc';
                spatialKnob.style.left = on ? '18px' : '2px';
                if (window.appSpatialAudio && typeof window.appSpatialAudio.setEnabled === 'function') {
                    window.appSpatialAudio.setEnabled(on);
                } else {
                    S.spatialAudioEnabled = on;
                }
            });

            spatialRow.appendChild(spatialLabel);
            spatialRow.appendChild(spatialToggle);
            spatialContainer.appendChild(spatialRow);

            var spatialHint = document.createElement('div');
            spatialHint.textContent = window.t ? window.t('speaker.spatialAudioHint') : '根据猫娘窗口相对主屏的位置做立体声与距离衰减';
            spatialHint.setAttribute('data-i18n', 'speaker.spatialAudioHint');
            Object.assign(spatialHint.style, { fontSize: '11px', color: 'var(--neko-popup-text-sub)', marginTop: '6px' });
            spatialContainer.appendChild(spatialHint);
            leftColumn.appendChild(spatialContainer);

            // 分隔线
            var sep1 = document.createElement('div');
            Object.assign(sep1.style, { height: '1px', backgroundColor: 'var(--neko-popup-separator)', margin: '8px 0' });
            leftColumn.appendChild(sep1);

            // Voice recognition uses the same main-action/subwindow pipeline as
            // screen sharing and microphone selection. The trigger is assembled
            // with those actions after the shared helpers are defined below.
            var asrSummary = null;

            function createVoiceSettingToggle(checked, onChange) {
                var focusStyle = document.getElementById(
                    'neko-voice-setting-toggle-focus-style'
                );
                if (!focusStyle) {
                    focusStyle = document.createElement('style');
                    focusStyle.id = 'neko-voice-setting-toggle-focus-style';
                    focusStyle.textContent = [
                        '.neko-voice-setting-toggle-input:focus-visible',
                        '+ .neko-voice-setting-toggle-slider{',
                        'box-shadow:0 0 0 2px #4f8cff;',
                        '}'
                    ].join('');
                    document.head.appendChild(focusStyle);
                }
                var toggle = document.createElement('label');
                Object.assign(toggle.style, {
                    position: 'relative',
                    display: 'inline-block',
                    width: '36px',
                    height: '20px',
                    flexShrink: '0',
                    cursor: 'pointer'
                });
                var input = document.createElement('input');
                input.className = 'neko-voice-setting-toggle-input';
                input.type = 'checkbox';
                input.checked = checked;
                Object.assign(input.style, {
                    position: 'absolute',
                    inset: '0',
                    width: '100%',
                    height: '100%',
                    margin: '0',
                    opacity: '0',
                    cursor: 'pointer',
                    zIndex: '2'
                });
                var slider = document.createElement('span');
                slider.className = 'neko-voice-setting-toggle-slider';
                Object.assign(slider.style, {
                    position: 'absolute',
                    inset: '0',
                    backgroundColor: checked ? '#4f8cff' : '#9aa0a6',
                    borderRadius: '10px',
                    transition: 'background-color 0.2s'
                });
                var knob = document.createElement('span');
                Object.assign(knob.style, {
                    position: 'absolute',
                    height: '16px',
                    width: '16px',
                    left: checked ? '18px' : '2px',
                    bottom: '2px',
                    backgroundColor: 'white',
                    borderRadius: '50%',
                    transition: 'left 0.2s'
                });
                slider.appendChild(knob);
                toggle.appendChild(input);
                toggle.appendChild(slider);
                function syncToggleVisual() {
                    slider.style.backgroundColor = input.checked
                        ? '#4f8cff'
                        : '#9aa0a6';
                    knob.style.left = input.checked ? '18px' : '2px';
                }
                toggle.addEventListener('click', function (event) {
                    event.stopPropagation();
                });
                toggle.addEventListener('pointerup', function (event) {
                    event.stopPropagation();
                });
                input.addEventListener('change', function () {
                    syncToggleVisual();
                    onChange(input.checked);
                });
                input.addEventListener('neko:toggle-visual-sync', syncToggleVisual);
                return {
                    element: toggle,
                    input: input,
                    setDisabled: function (disabled) {
                        input.disabled = disabled;
                        toggle.style.cursor = disabled ? 'not-allowed' : 'pointer';
                        input.style.cursor = disabled ? 'not-allowed' : 'pointer';
                        toggle.style.opacity = disabled ? '0.5' : '1';
                    },
                    setChecked: function (value) {
                        input.checked = value;
                        syncToggleVisual();
                    }
                };
            }

            function persistVoiceSettingChange() {
                if (
                    !window.appSettings
                    || typeof window.appSettings.saveSettings !== 'function'
                ) return;
                if (typeof window.appSettings.syncSettingsToServer !== 'function') {
                    window.appSettings.saveSettings();
                    return;
                }
                // Preserve the existing session-start ownership fence: persist
                // locally now, then expose the serialized server sync promise
                // for ensureWebSocketOpen() to await before start_session.
                window.appSettings.saveSettings({ skipServerSync: true });
                var syncPromise = Promise.resolve(
                    window.appSettings.syncSettingsToServer({ userInitiated: true })
                )
                    .catch(function () {
                        // syncSettingsToServer owns failure reporting.
                    })
                    .then(function () {
                        if (S.pendingSettingsSyncPromise === syncPromise) {
                            S.pendingSettingsSyncPromise = null;
                        }
                    });
                S.pendingSettingsSyncPromise = syncPromise;
            }

            function markVoiceSettingsPending(activeRouteSnapshot) {
                var targetEpoch = (Number(S.voiceSessionStartEpoch) || 0) + 1;
                if (
                    activeRouteSnapshot !== undefined
                    && (
                        S.voiceSettingsPendingUntilEpoch !== targetEpoch
                        || S.pendingVoiceRouteIndependentAsr === null
                    )
                ) {
                    S.pendingVoiceRouteIndependentAsr = activeRouteSnapshot;
                }
                S.voiceSettingsPendingUntilEpoch = targetEpoch;
            }

            function coreApiDisablesIndependentAsr() {
                return S.coreApiSupportsIndependentAsr === false;
            }

            var asrToggle = createVoiceSettingToggle(
                !coreApiDisablesIndependentAsr()
                    && S.independentAsrEnabled === true,
                function (enabled) {
                    // The disabled switch is an effective view only. Keep the
                    // persisted preference untouched so switching back to a
                    // capable Core restores the user's previous choice.
                    if (coreApiDisablesIndependentAsr()) {
                        updateVoiceRecognitionUi();
                        return;
                    }
                    var activeRouteSnapshot = S.voiceChatActive === true
                        ? (
                            S.independentAsrActive === true
                            || (
                                S.voiceInputLifecycleState === 'blocked'
                                && S.independentAsrEnabled === true
                            )
                        )
                        : null;
                    S.independentAsrEnabled = enabled;
                    markVoiceSettingsPending(activeRouteSnapshot);
                    updateVoiceRecognitionUi();
                    persistVoiceSettingChange();
                }
            );
            var voicePanelId = (popupId || 'neko-mic')
                + '-voice-recognition-settings';
            var voicePanel = null;
            var voicePopupObserver = null;
            var noiseToggle = null;
            var optimizationToggle = null;
            var optimizationHint = null;
            var voiceStatus = null;

            function providerDisplayName(provider) {
                var value = String(provider || '').trim();
                if (!value) return '';
                var known = {
                    qwen: 'Qwen',
                    soniox: 'Soniox',
                    glm: 'GLM',
                    gemini: 'Gemini',
                    openai: 'OpenAI',
                    step: 'Step',
                    grok: 'Grok'
                };
                return known[value.toLowerCase()] || value;
            }

            function appendVoicePanelSetting(
                panelBody,
                labelKey,
                fallbackLabel,
                hintKey,
                fallbackHint,
                toggle
            ) {
                var block = document.createElement('div');
                block.style.marginBottom = '14px';
                var row = document.createElement('div');
                Object.assign(row.style, {
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    gap: '12px'
                });
                var label = document.createElement('span');
                var settingId = (
                    voicePanelId + '-' + labelKey
                ).replace(/[^a-z0-9_-]+/gi, '-');
                label.id = settingId + '-label';
                label.textContent = window.t ? window.t(labelKey) : fallbackLabel;
                label.setAttribute('data-i18n', labelKey);
                Object.assign(label.style, {
                    fontSize: '13px',
                    fontWeight: '500'
                });
                row.appendChild(label);
                row.appendChild(toggle.element);
                var hint = document.createElement('div');
                hint.id = settingId + '-hint';
                hint.textContent = window.t ? window.t(hintKey) : fallbackHint;
                hint.setAttribute('data-i18n', hintKey);
                Object.assign(hint.style, {
                    fontSize: '11px',
                    color: 'var(--neko-popup-text-sub)',
                    marginTop: '5px',
                    lineHeight: '1.45'
                });
                block.appendChild(row);
                block.appendChild(hint);
                toggle.input.setAttribute('aria-labelledby', label.id);
                toggle.input.setAttribute('aria-describedby', hint.id);
                panelBody.appendChild(block);
                return hint;
            }

            function updateVoiceRecognitionUi() {
                var capabilityUnavailable = coreApiDisablesIndependentAsr();
                var enabled = !capabilityUnavailable
                    && S.independentAsrEnabled === true;
                if (
                    S.voiceSettingsPendingUntilEpoch !== null
                    && (Number(S.voiceSessionStartEpoch) || 0)
                        >= S.voiceSettingsPendingUntilEpoch
                ) {
                    S.voiceSettingsPendingUntilEpoch = null;
                    S.pendingVoiceRouteIndependentAsr = null;
                }
                var summaryUsesIndependentAsr = capabilityUnavailable
                    ? false
                    : (
                        S.voiceSettingsPendingUntilEpoch !== null
                        && S.pendingVoiceRouteIndependentAsr !== null
                            ? S.pendingVoiceRouteIndependentAsr
                            : enabled
                    );
                var provider = providerDisplayName(S.independentAsrProvider);
                var blocked = S.voiceInputLifecycleState === 'blocked';
                asrToggle.setChecked(enabled);
                asrToggle.setDisabled(capabilityUnavailable);
                if (asrSummary) {
                    asrSummary.textContent = summaryUsesIndependentAsr
                        ? (
                            window.t
                                ? window.t(
                                    provider
                                        ? 'microphone.independentAsrSummary'
                                        : 'microphone.independentAsrSummaryGeneric',
                                    { provider: provider }
                                )
                                : ('独立 ASR' + (provider ? ' · ' + provider : ''))
                        )
                        : (
                            window.t
                                ? window.t('microphone.voiceRecognitionDisabled')
                                : '当前使用 Omni 原生语音识别'
                        );
                }
                if (
                    !voicePanel
                    || !voicePanel.isConnected
                    || !noiseToggle
                    || !optimizationToggle
                    || !optimizationHint
                    || !voiceStatus
                ) return;
                // RNNoise is local PCM preprocessing shared by both the
                // independent-ASR and Omni-native routes.
                noiseToggle.setDisabled(false);
                optimizationToggle.setDisabled(!enabled);
                if (capabilityUnavailable) {
                    voiceStatus.textContent = window.t
                        ? window.t('microphone.voiceRecognitionNativeCoreHint')
                        : '当前核心使用免费API；独立 ASR 相关开关不适用';
                } else if (S.voiceSettingsPendingUntilEpoch !== null) {
                    voiceStatus.textContent = window.t
                        ? window.t('microphone.voiceRecognitionSettingsPending')
                        : '◐ 设置将在下次语音会话生效';
                } else if (!enabled) {
                    voiceStatus.textContent = window.t
                        ? window.t('microphone.voiceRecognitionDisabledHint')
                        : '独立 ASR 已关闭；语音输入使用 Omni 原生语音识别';
                } else if (blocked) {
                    voiceStatus.textContent = window.t
                        ? window.t('microphone.voiceRecognitionUnavailable')
                        : '本次独立语音识别已停止，不会切换到其他 Provider 或 Omni';
                } else if (S.independentAsrActive) {
                    voiceStatus.textContent = window.t
                        ? window.t('microphone.voiceRecognitionStatusReady')
                        : '● 当前运行正常';
                } else {
                    voiceStatus.textContent = window.t
                        ? window.t('microphone.voiceRecognitionSettingsPending')
                        : '◐ 设置将在下次语音会话生效';
                }
                var optimizationEnabled = !capabilityUnavailable
                    && S.voiceInputResourceOptimizationEnabled !== false;
                optimizationToggle.setChecked(optimizationEnabled);
                optimizationHint.textContent = window.t
                    ? window.t(
                        capabilityUnavailable
                            ? 'microphone.voiceRecognitionNativeCoreHint'
                            : (
                                optimizationEnabled
                                    ? 'microphone.voiceResourceOptimizationHintOn'
                                    : 'microphone.voiceResourceOptimizationHintOff'
                            )
                    )
                    : (
                        capabilityUnavailable
                            ? '当前核心使用免费API；独立 ASR 相关开关不适用'
                            : (
                                optimizationEnabled
                                    ? '空闲时减少连接和音频上传'
                                    : '持续保持语音识别，可能增加网络和资源占用'
                            )
                    );
            }

            function onVoiceLifecycleChanged() {
                updateVoiceRecognitionUi();
            }

            function onVoiceSessionStarted() {
                if (
                    S.voiceSettingsPendingUntilEpoch === null
                    || (Number(S.voiceSessionStartEpoch) || 0)
                        < S.voiceSettingsPendingUntilEpoch
                ) return;
                S.voiceSettingsPendingUntilEpoch = null;
                S.pendingVoiceRouteIndependentAsr = null;
                updateVoiceRecognitionUi();
            }

            function onVoiceSettingsPendingChanged() {
                updateVoiceRecognitionUi();
            }

            function onCoreApiCapabilityChanged() {
                updateVoiceRecognitionUi();
            }

            var voiceControlsDisposed = false;
            var voiceWindowListeners = [];

            function addVoiceWindowListener(type, listener) {
                voiceWindowListeners.push([type, listener]);
                window.addEventListener(type, listener);
            }

            function destroyVoiceRecognitionControls() {
                if (voiceControlsDisposed) return;
                voiceControlsDisposed = true;
                if (voicePopupObserver) {
                    voicePopupObserver.disconnect();
                    voicePopupObserver = null;
                }
                voiceWindowListeners.forEach(function (entry) {
                    window.removeEventListener(entry[0], entry[1]);
                });
                voiceWindowListeners = [];
                // Tear down the shared action state as well as its DOM. This
                // clears an old render's pending hover-collapse timer so it
                // cannot remove a subwindow created by the next render.
                closeMicSubwindow();
                voicePanel = null;
                noiseToggle = null;
                optimizationToggle = null;
                optimizationHint = null;
                voiceStatus = null;
                asrSummary = null;
                if (
                    disposeVoiceRecognitionPopover
                    === destroyVoiceRecognitionControls
                ) {
                    disposeVoiceRecognitionPopover = null;
                }
            }

            disposeVoiceRecognitionPopover = destroyVoiceRecognitionControls;
            addVoiceWindowListener(
                'voice-input-lifecycle-changed',
                onVoiceLifecycleChanged
            );
            addVoiceWindowListener(
                'neko:voice-session-started',
                onVoiceSessionStarted
            );
            addVoiceWindowListener(
                'neko:core-api-capability-changed',
                onCoreApiCapabilityChanged
            );
            addVoiceWindowListener(
                'neko:voice-settings-pending-changed',
                onVoiceSettingsPendingChanged
            );
            voicePopupObserver = new MutationObserver(function () {
                if (!isPopupAvailable()) destroyVoiceRecognitionControls();
            });
            var popupAncestor = micPopup.parentNode;
            while (popupAncestor) {
                voicePopupObserver.observe(popupAncestor, {
                    childList: true
                });
                popupAncestor = popupAncestor.parentNode;
            }
            voicePopupObserver.observe(micPopup, {
                attributes: true,
                attributeFilter: ['style', 'class']
            });
            updateVoiceRecognitionUi();


            // ===== 左栏 2. 麦克风增益 =====
            var gainContainer = document.createElement('div');
            gainContainer.className = 'mic-gain-container';
            gainContainer.style.padding = '8px 12px';

            var gainHeader = document.createElement('div');
            Object.assign(gainHeader.style, { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' });

            var gainLabel = document.createElement('span');
            gainLabel.textContent = window.t ? window.t('microphone.gainLabel') : '麦克风增益';
            Object.assign(gainLabel.style, { fontSize: '13px', color: 'var(--neko-popup-text)', fontWeight: '500' });

            var gainValueEl = document.createElement('span');
            gainValueEl.id = 'mic-gain-value';
            gainValueEl.textContent = formatGainDisplay(S.microphoneGainDb);
            Object.assign(gainValueEl.style, { fontSize: '12px', color: '#4f8cff', fontWeight: '500' });

            gainHeader.appendChild(gainLabel);
            gainHeader.appendChild(gainValueEl);
            gainContainer.appendChild(gainHeader);

            var gainSlider = document.createElement('input');
            gainSlider.type = 'range';
            gainSlider.id = 'mic-gain-slider';
            gainSlider.min = String(C.MIN_MIC_GAIN_DB);
            gainSlider.max = String(C.MAX_MIC_GAIN_DB);
            gainSlider.step = '1';
            gainSlider.value = String(S.microphoneGainDb);
            Object.assign(gainSlider.style, { width: '100%', height: '6px', borderRadius: '3px', cursor: 'pointer', accentColor: '#4f8cff' });
            if (!hasMicrophoneDevices) {
                gainSlider.disabled = true;
                gainSlider.style.cursor = 'not-allowed';
                gainSlider.style.opacity = '0.55';
                gainLabel.style.color = 'var(--neko-popup-text-sub)';
                gainValueEl.style.color = 'var(--neko-popup-text-sub)';
            }

            gainSlider.addEventListener('input', function (e) {
                var newGainDb = parseFloat(e.target.value);
                S.microphoneGainDb = newGainDb;
                gainValueEl.textContent = formatGainDisplay(newGainDb);
                if (S.micGainNode) {
                    S.micGainNode.gain.value = window.appUtils.dbToLinear(newGainDb);
                }
            });
            gainSlider.addEventListener('change', function () { saveMicGainSetting(); });
            gainContainer.appendChild(gainSlider);

            var gainHint = document.createElement('div');
            gainHint.textContent = window.t ? window.t('microphone.gainHint') : '如果麦克风声音太小，可以调高增益';
            Object.assign(gainHint.style, { fontSize: '11px', color: 'var(--neko-popup-text-sub)', marginTop: '6px' });
            gainContainer.appendChild(gainHint);
            leftColumn.appendChild(gainContainer);

            var sep2 = document.createElement('div');
            Object.assign(sep2.style, { height: '1px', backgroundColor: 'var(--neko-popup-separator)', margin: '8px 0' });
            leftColumn.appendChild(sep2);

            // ===== 左栏 3. 音量可视化 =====
            var volumeContainer = document.createElement('div');
            volumeContainer.className = 'mic-volume-container';
            volumeContainer.style.padding = '8px 12px';

            var volumeLabelDiv = document.createElement('div');
            Object.assign(volumeLabelDiv.style, { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' });

            var volumeLabelText = document.createElement('span');
            volumeLabelText.textContent = window.t ? window.t('microphone.volumeLabel') : '实时麦克风音量';
            Object.assign(volumeLabelText.style, { fontSize: '13px', color: 'var(--neko-popup-text)', fontWeight: '500' });

            var volumeStatus = document.createElement('span');
            volumeStatus.id = 'mic-volume-status';
            volumeStatus.textContent = window.t ? window.t('microphone.volumeIdle') : '未录音';
            Object.assign(volumeStatus.style, { fontSize: '11px', color: 'var(--neko-popup-text-sub)' });

            volumeLabelDiv.appendChild(volumeLabelText);
            volumeLabelDiv.appendChild(volumeStatus);
            volumeContainer.appendChild(volumeLabelDiv);

            var volumeBarBg = document.createElement('div');
            volumeBarBg.id = 'mic-volume-bar-bg';
            Object.assign(volumeBarBg.style, { width: '100%', height: '8px', backgroundColor: 'var(--neko-mic-volume-bg, #e9ecef)', borderRadius: '4px', overflow: 'hidden', position: 'relative' });

            var volumeBarFill = document.createElement('div');
            volumeBarFill.id = 'mic-volume-bar-fill';
            Object.assign(volumeBarFill.style, { width: '0%', height: '100%', backgroundColor: '#4f8cff', borderRadius: '4px', transition: 'width 0.05s ease-out, background-color 0.1s ease' });

            volumeBarBg.appendChild(volumeBarFill);
            volumeContainer.appendChild(volumeBarBg);

            var volumeHint = document.createElement('div');
            volumeHint.id = 'mic-volume-hint';
            volumeHint.textContent = window.t ? window.t('microphone.volumeHint') : '开始录音后可查看音量';
            Object.assign(volumeHint.style, { fontSize: '11px', color: 'var(--neko-popup-text-sub)', marginTop: '6px' });
            volumeContainer.appendChild(volumeHint);
            leftColumn.appendChild(volumeContainer);

            var MIC_ACTION_HOVER_COLLAPSE_MS = 260;
            var activeMicActionKey = null;
            var micActionHoverCollapseTimer = null;
            var micActionHoverOpenGeneration = 0;

            function getOwnedMicSubwindow() {
                var ownerSelector = micPopup.id
                    ? '[data-neko-sidepanel-owner="' + micPopup.id + '"].neko-mic-subwindow'
                    : '.neko-mic-subwindow';
                return document.querySelector(ownerSelector);
            }

            function clearMicActionHoverCollapseTimer() {
                if (micActionHoverCollapseTimer) {
                    clearTimeout(micActionHoverCollapseTimer);
                    micActionHoverCollapseTimer = null;
                }
            }

            function isMicActionHoverSurfaceActive() {
                var hoveredActionRow = leftColumn.querySelector(
                    '[data-neko-mic-main-action-row]:hover'
                );
                if (hoveredActionRow) return true;
                var hoveredAction = leftColumn.querySelector('[data-neko-mic-main-action]:hover');
                if (hoveredAction) return true;
                var panel = getOwnedMicSubwindow();
                return !!(panel && panel.isConnected && panel.matches(':hover'));
            }

            function closeMicSubwindow() {
                clearMicActionHoverCollapseTimer();
                micActionHoverOpenGeneration += 1;
                activeMicActionKey = null;
                var ownerSelector = micPopup.id ? '[data-neko-sidepanel-owner="' + micPopup.id + '"]' : '.neko-mic-subwindow';
                document.querySelectorAll(ownerSelector + '.neko-mic-subwindow').forEach(function (panel) {
                    panel.remove();
                });
            }

            function scheduleMicActionHoverCollapse() {
                clearMicActionHoverCollapseTimer();
                // Screen-source settings contain text input and OS-mediated
                // interactions (for example IME candidate windows). Leaving
                // the panel is not an intent to dismiss it; it closes only
                // when the pointer returns to the owning menu or that menu is
                // disposed. Other lightweight action panels keep the shared
                // delayed hover-collapse behavior.
                if (activeMicActionKey === 'screen') return;
                micActionHoverCollapseTimer = setTimeout(function () {
                    micActionHoverCollapseTimer = null;
                    if (isMicActionHoverSurfaceActive()) return;
                    closeMicSubwindow();
                    leftColumn.querySelectorAll(
                        '[data-neko-mic-main-action-row], [data-neko-mic-main-action]'
                    ).forEach(function (surface) {
                        surface.style.background = 'transparent';
                    });
                }, MIC_ACTION_HOVER_COLLAPSE_MS);
            }

            leftColumn.addEventListener('mouseenter', function () {
                if (activeMicActionKey !== 'screen') return;
                var panel = getOwnedMicSubwindow();
                if (!panel || !panel.isConnected) return;
                closeMicSubwindow();
                leftColumn.querySelectorAll(
                    '[data-neko-mic-main-action-row], [data-neko-mic-main-action]'
                ).forEach(function (surface) {
                    surface.style.background = 'transparent';
                });
            });

            function wireMicSubwindowHoverBridge(panel) {
                if (!panel || panel._nekoMicHoverBridgeWired) return;
                panel._nekoMicHoverBridgeWired = true;
                panel.addEventListener('mouseenter', function () {
                    clearMicActionHoverCollapseTimer();
                });
                panel.addEventListener('mouseleave', function () {
                    scheduleMicActionHoverCollapse();
                });
            }

            function openMicActionPanel(actionKey, openFn) {
                clearMicActionHoverCollapseTimer();
                var existing = getOwnedMicSubwindow();
                if (activeMicActionKey === actionKey && existing && existing.isConnected) {
                    wireMicSubwindowHoverBridge(existing);
                    return Promise.resolve(existing);
                }
                activeMicActionKey = actionKey;
                var generation = ++micActionHoverOpenGeneration;
                return Promise.resolve(openFn()).then(function () {
                    if (generation !== micActionHoverOpenGeneration || activeMicActionKey !== actionKey) return null;
                    var panel = getOwnedMicSubwindow();
                    if (panel) {
                        panel.setAttribute('data-neko-mic-action-key', actionKey);
                        wireMicSubwindowHoverBridge(panel);
                    }
                    return panel;
                });
            }

            function positionMicSubwindow(panel) {
                if (!panel || !micPopup || !micPopup.isConnected) return;
                var rect = micPopup.getBoundingClientRect();
                var panelWidth = panel.offsetWidth || 320;
                var panelHeight = panel.offsetHeight || 360;
                var gap = 8;
                var left = rect.right + gap;
                var opensLeft = micPopup.dataset && micPopup.dataset.opensLeft === 'true';
                if (opensLeft || left + panelWidth > window.innerWidth - gap) {
                    left = rect.left - panelWidth - gap;
                }
                left = Math.max(gap, Math.min(left, window.innerWidth - panelWidth - gap));
                var top = Math.max(gap, Math.min(rect.top, window.innerHeight - panelHeight - gap));
                panel.style.left = left + 'px';
                panel.style.top = top + 'px';
            }

            function createMicSubwindow(title, iconText, width) {
                // Keep activeMicActionKey; only tear down the previous DOM panel.
                clearMicActionHoverCollapseTimer();
                var ownerSelector = micPopup.id ? '[data-neko-sidepanel-owner="' + micPopup.id + '"]' : '.neko-mic-subwindow';
                document.querySelectorAll(ownerSelector + '.neko-mic-subwindow').forEach(function (panel) {
                    panel.remove();
                });
                var panel = document.createElement('div');
                panel.className = 'neko-mic-subwindow';
                if (micPopup.id) panel.setAttribute('data-neko-sidepanel-owner', micPopup.id);
                panel.setAttribute('data-neko-sidepanel', '');
                Object.assign(panel.style, {
                    position: 'fixed',
                    zIndex: '100003',
                    width: width || '320px',
                    maxHeight: 'min(420px, calc(100vh - 16px))',
                    overflowY: 'hidden',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '4px',
                    padding: '8px',
                    boxSizing: 'border-box',
                    background: 'var(--neko-popup-bg, rgba(255, 255, 255, 0.82))',
                    backdropFilter: 'saturate(180%) blur(20px)',
                    border: 'var(--neko-popup-border, 1px solid rgba(255, 255, 255, 0.18))',
                    borderRadius: '8px',
                    boxShadow: 'var(--neko-popup-shadow, 0 8px 24px rgba(0,0,0,0.16))',
                    pointerEvents: 'auto',
                    cursor: 'default',
                    color: 'var(--neko-popup-text)'
                });

                var stopSubwindowEvent = function (e) {
                    if (document.body.classList.contains('neko-model-dragging')) return;
                    e.stopPropagation();
                };
                ['pointerdown', 'pointermove', 'pointerup', 'mousedown', 'mousemove', 'mouseup', 'touchstart', 'touchmove', 'touchend'].forEach(function (evt) {
                    panel.addEventListener(evt, stopSubwindowEvent, true);
                });
                panel.addEventListener('click', stopSubwindowEvent);

                var header = document.createElement('div');
                Object.assign(header.style, {
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: '8px',
                    padding: '4px 6px 8px',
                    borderBottom: '1px solid var(--neko-popup-separator)',
                    marginBottom: '4px',
                    flexShrink: '0'
                });

                var titleWrap = document.createElement('div');
                Object.assign(titleWrap.style, { display: 'flex', alignItems: 'center', gap: '6px', minWidth: '0', color: '#4f8cff', fontSize: '13px', fontWeight: '600' });
                var titleEl = document.createElement('span');
                titleEl.textContent = title;
                Object.assign(titleEl.style, { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' });
                if (iconText) {
                    var icon = document.createElement('span');
                    icon.textContent = iconText;
                    icon.style.fontSize = '14px';
                    titleWrap.appendChild(icon);
                }
                titleWrap.appendChild(titleEl);

                var closeBtn = document.createElement('button');
                closeBtn.type = 'button';
                closeBtn.textContent = 'x';
                closeBtn.setAttribute('aria-label', 'Close');
                Object.assign(closeBtn.style, {
                    width: '24px',
                    height: '24px',
                    border: 'none',
                    borderRadius: '6px',
                    background: 'transparent',
                    color: 'var(--neko-popup-text-sub)',
                    cursor: 'pointer',
                    flexShrink: '0'
                });
                closeBtn.addEventListener('mouseenter', function () { closeBtn.style.background = 'var(--neko-popup-hover)'; });
                closeBtn.addEventListener('mouseleave', function () { closeBtn.style.background = 'transparent'; });
                closeBtn.addEventListener('click', function (e) {
                    e.stopPropagation();
                    closeMicSubwindow();
                });

                var headerActions = document.createElement('div');
                Object.assign(headerActions.style, {
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'flex-end',
                    gap: '7px',
                    flexShrink: '0'
                });
                headerActions.appendChild(closeBtn);

                header.appendChild(titleWrap);
                header.appendChild(headerActions);
                panel.appendChild(header);
                panel._nekoMicSubwindowHeaderActions = headerActions;

                var body = document.createElement('div');
                body.className = 'neko-mic-popup-scroll neko-mic-subwindow-body';
                Object.assign(body.style, {
                    display: 'flex',
                    flex: '1 1 auto',
                    flexDirection: 'column',
                    gap: '4px',
                    minHeight: '0',
                    overflowY: 'auto'
                });
                panel.appendChild(body);
                panel._nekoMicSubwindowBody = body;
                attachTransientMicPopupScrollbar(body, panel);

                document.body.appendChild(panel);
                requestAnimationFrame(function () { positionMicSubwindow(panel); });
                return panel;
            }

            function createMainActionButton(iconText, label, subLabel, actionKey, onClick, interactionOptions) {
                interactionOptions = interactionOptions || {};
                var button = document.createElement('button');
                button.type = 'button';
                button.dataset.nekoMicMainAction = actionKey;
                Object.assign(button.style, {
                    width: '100%',
                    minWidth: '0',
                    maxWidth: '100%',
                    boxSizing: 'border-box',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    padding: '9px 10px',
                    border: 'none',
                    borderRadius: '6px',
                    background: 'transparent',
                    color: 'var(--neko-popup-text)',
                    cursor: 'pointer',
                    textAlign: 'left',
                    transition: 'background 0.2s ease'
                });
                var textWrap = document.createElement('span');
                textWrap.className = 'neko-mic-action-text';
                Object.assign(textWrap.style, { display: 'flex', flexDirection: 'column', minWidth: '0', width: '0', maxWidth: '100%', flex: '1 1 0%', overflow: 'hidden' });
                var labelEl = document.createElement('span');
                labelEl.textContent = label;
                Object.assign(labelEl.style, { display: 'block', maxWidth: '100%', fontSize: '13px', fontWeight: '600', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' });
                var subEl = document.createElement('span');
                subEl.className = 'neko-mic-action-sub-label';
                subEl.textContent = subLabel == null ? '' : String(subLabel);
                Object.assign(subEl.style, { display: 'block', maxWidth: '100%', fontSize: '11px', color: 'var(--neko-popup-text-sub)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' });
                // Match settings menu chevron (Chat Settings / Animation / Advanced).
                var arrow = document.createElement('span');
                arrow.textContent = '\u203A';
                Object.assign(arrow.style, {
                    fontSize: '16px',
                    color: 'var(--neko-popup-text-sub, #999)',
                    lineHeight: '1',
                    flexShrink: '0'
                });
                textWrap.appendChild(labelEl);
                if (subLabel != null) textWrap.appendChild(subEl);
                if (iconText) {
                    var icon = document.createElement('span');
                    icon.textContent = iconText;
                    icon.style.fontSize = '15px';
                    button.appendChild(icon);
                }
                button.appendChild(textWrap);
                button.appendChild(arrow);

                function actionSurface() {
                    return button._nekoMicActionRow || button;
                }

                function openActionPanel(event) {
                    actionSurface().style.background = 'var(--neko-popup-hover)';
                    return openMicActionPanel(actionKey, onClick).catch(function (error) {
                        console.error('[麦克风弹窗] 子窗口打开失败:', error);
                    });
                }

                // Most settings side panels may expand on hover. Screen-source
                // enumeration is different: on Linux it can invoke
                // xdg-desktop-portal and show an OS sharing dialog, so that
                // action must require an explicit click/user gesture.
                if (interactionOptions.openOnHover !== false) {
                    button.addEventListener('mouseenter', function (event) {
                        openActionPanel(event);
                    });
                } else {
                    button.addEventListener('mouseenter', function () {
                        clearMicActionHoverCollapseTimer();
                        actionSurface().style.background = 'var(--neko-popup-hover)';
                    });
                }
                button.addEventListener('mouseleave', function () {
                    // Shared rows own the full hover surface, including any
                    // sibling toggle. Their mouseleave handler closes the panel.
                    if (button._nekoMicActionRow) return;
                    actionSurface().style.background = 'transparent';
                    scheduleMicActionHoverCollapse();
                });
                button.addEventListener('click', function (e) {
                    e.stopPropagation();
                    openActionPanel(e);
                });
                return button;
            }

            function createMainActionRow(actionButton, trailingControl) {
                var row = document.createElement('div');
                row.className = 'neko-mic-main-action-row';
                row.dataset.nekoMicMainActionRow = actionButton.dataset.nekoMicMainAction;
                Object.assign(row.style, {
                    width: '100%',
                    minWidth: '0',
                    maxWidth: '100%',
                    boxSizing: 'border-box',
                    display: 'flex',
                    alignItems: 'center',
                    borderRadius: '6px',
                    background: 'transparent',
                    transition: 'background 0.2s ease'
                });
                actionButton._nekoMicActionRow = row;
                actionButton.style.width = '0';
                actionButton.style.flex = '1 1 0%';
                row.appendChild(actionButton);
                if (trailingControl) {
                    var arrow = actionButton.lastElementChild;
                    if (arrow) arrow.remove();
                    trailingControl.style.marginRight = '10px';
                    row.appendChild(trailingControl);
                }
                row.addEventListener('mouseenter', function () {
                    clearMicActionHoverCollapseTimer();
                });
                row.addEventListener('mouseleave', function () {
                    row.style.background = 'transparent';
                    scheduleMicActionHoverCollapse();
                });
                return row;
            }

            function createMicDeviceOption(label, deviceId) {
                var option = document.createElement('button');
                option.type = 'button';
                option.className = 'mic-option';
                if (deviceId !== null) option.dataset.deviceId = deviceId;
                option.textContent = label;
                var isSelected = (deviceId === null && S.selectedMicrophoneId === null) || deviceId === S.selectedMicrophoneId;
                if (isSelected) option.classList.add('selected');
                Object.assign(option.style, { padding: '8px 12px', cursor: 'pointer', border: 'none', background: isSelected ? 'var(--neko-popup-selected-bg)' : 'transparent', borderRadius: '6px', transition: 'background 0.2s ease', fontSize: '13px', width: '100%', textAlign: 'left', color: isSelected ? '#4f8cff' : 'var(--neko-popup-text)', fontWeight: isSelected ? '500' : '400' });
                option.addEventListener('mouseenter', function () { if (!option.classList.contains('selected')) option.style.background = 'var(--neko-popup-hover)'; });
                option.addEventListener('mouseleave', function () { if (!option.classList.contains('selected')) option.style.background = 'transparent'; });
                option.addEventListener('click', async function (e) {
                    e.stopPropagation();
                    await selectMicrophone(deviceId);
                    updateMicListSelection();
                    document.querySelectorAll('[data-neko-mic-action="device"] .neko-mic-action-sub-label').forEach(function (labelEl) {
                        labelEl.textContent = label;
                    });
                });
                return option;
            }

            function updateSpeakerOptionSelection() {
                document.querySelectorAll('.speaker-option').forEach(function (entry) {
                    var selected = entry.dataset.deviceId === S.selectedSpeakerId;
                    entry.classList.toggle('selected', selected);
                    entry.setAttribute('aria-pressed', selected ? 'true' : 'false');
                    entry.style.background = selected ? 'var(--neko-popup-selected-bg)' : 'transparent';
                    entry.style.color = selected ? '#4f8cff' : 'var(--neko-popup-text)';
                    entry.style.fontWeight = selected ? '500' : '400';
                });
            }

            function createSpeakerDeviceOption(label, deviceId) {
                var option = document.createElement('button');
                option.type = 'button';
                option.className = 'speaker-option';
                option.dataset.deviceId = deviceId;
                var isSelected = deviceId === S.selectedSpeakerId;
                option.textContent = label;
                if (isSelected) option.classList.add('selected');
                option.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
                Object.assign(option.style, { padding: '8px 12px', cursor: 'pointer', border: 'none', background: isSelected ? 'var(--neko-popup-selected-bg)' : 'transparent', borderRadius: '6px', transition: 'background 0.2s ease', fontSize: '13px', width: '100%', textAlign: 'left', color: isSelected ? '#4f8cff' : 'var(--neko-popup-text)', fontWeight: isSelected ? '500' : '400' });
                option.addEventListener('mouseenter', function () { if (!option.classList.contains('selected')) option.style.background = 'var(--neko-popup-hover)'; });
                option.addEventListener('mouseleave', function () { if (!option.classList.contains('selected')) option.style.background = 'transparent'; });
                option.addEventListener('click', async function (e) {
                    e.stopPropagation();
                    try {
                        if (typeof window.selectSpeakerDevice !== 'function') {
                            throw new Error('Speaker device selection is unavailable');
                        }
                        var applied = await window.selectSpeakerDevice(deviceId);
                        if (applied === false) {
                            throw new Error('Speaker device selection was not applied');
                        }
                        updateSpeakerOptionSelection();
                        document.querySelectorAll('[data-neko-mic-main-action="speaker-device"] .neko-mic-action-sub-label').forEach(function (labelEl) {
                            labelEl.textContent = label;
                        });
                    } catch (error) {
                        console.error('[Audio] 切换播放设备失败:', error);
                        if (typeof window.showStatusToast === 'function') {
                            window.showStatusToast(
                                window.t ? window.t('speaker.switchFailed') : '切换播放设备失败',
                                3000
                            );
                        }
                    }
                });
                return option;
            }

            function openVoiceRecognitionSubwindow() {
                var panel = createMicSubwindow(
                    window.t
                        ? window.t('microphone.voiceRecognitionSettings')
                        : '语音识别设置',
                    null,
                    '280px'
                );
                panel.id = voicePanelId;
                panel.classList.add('neko-mic-voice-subwindow');
                voicePanel = panel;
                var panelBody = panel._nekoMicSubwindowBody || panel;

                noiseToggle = createVoiceSettingToggle(
                    S.noiseReductionEnabled === true,
                    function (enabled) {
                        S.noiseReductionEnabled = enabled;
                        saveNoiseReductionSetting();
                    }
                );
                appendVoicePanelSetting(
                    panelBody,
                    'microphone.noiseReduction',
                    '降噪',
                    'microphone.noiseReductionHint',
                    '让输入语音更加清晰',
                    noiseToggle
                );

                optimizationToggle = createVoiceSettingToggle(
                    S.voiceInputResourceOptimizationEnabled !== false,
                    function (enabled) {
                        S.voiceInputResourceOptimizationEnabled = enabled;
                        markVoiceSettingsPending();
                        updateVoiceRecognitionUi();
                        persistVoiceSettingChange();
                    }
                );
                optimizationHint = appendVoicePanelSetting(
                    panelBody,
                    'microphone.voiceResourceOptimization',
                    '智能资源优化',
                    'microphone.voiceResourceOptimizationHintOn',
                    '空闲时减少连接和音频上传',
                    optimizationToggle
                );

                voiceStatus = document.createElement('div');
                voiceStatus.className = 'neko-voice-recognition-status';
                voiceStatus.setAttribute('role', 'status');
                voiceStatus.setAttribute('aria-live', 'polite');
                Object.assign(voiceStatus.style, {
                    borderTop: '1px solid var(--neko-popup-separator)',
                    paddingTop: '11px',
                    fontSize: '11px',
                    lineHeight: '1.45',
                    color: 'var(--neko-popup-text-sub)'
                });
                panelBody.appendChild(voiceStatus);

                updateVoiceRecognitionUi();
                if (
                    typeof window.refreshCoreApiCapability === 'function'
                    && Date.now() - coreApiCapabilityRefreshedAt >= 1000
                ) {
                    var openedPanel = panel;
                    Promise.resolve(
                        window.refreshCoreApiCapability({ force: true })
                    ).then(function () {
                        coreApiCapabilityRefreshedAt = Date.now();
                        if (
                            voicePanel === openedPanel
                            && openedPanel.isConnected
                        ) updateVoiceRecognitionUi();
                    }).catch(function () {
                        // refreshCoreApiCapability owns failure reporting.
                    });
                }
                return panel;
            }

            async function openMicDeviceSubwindow() {
                var panel = createMicSubwindow(
                    window.t ? window.t('microphone.deviceTitle') : 'Select Microphone',
                    null,
                    '280px'
                );
                panel.classList.add('neko-mic-device-subwindow');
                var panelBody = panel._nekoMicSubwindowBody || panel;
                var listBody = document.createElement('div');
                Object.assign(listBody.style, { display: 'flex', flexDirection: 'column', gap: '4px' });
                panelBody.appendChild(listBody);

                var loadingItem = document.createElement('div');
                loadingItem.textContent = window.t ? window.t('app.screenSource.loading') : 'Loading...';
                Object.assign(loadingItem.style, { padding: '8px 12px', color: 'var(--neko-popup-text-sub)', fontSize: '13px' });
                listBody.appendChild(loadingItem);
                positionMicSubwindow(panel);

                var devices = cachedMicDevices;
                if (!devices || devices.length === 0 || !micPermissionGranted) {
                    devices = await ensureMicrophonePermission();
                }
                listBody.innerHTML = '';

                var defaultLabel = window.t ? window.t('microphone.defaultDevice') : 'System Default Microphone';
                listBody.appendChild(createMicDeviceOption(defaultLabel, null));

                if (!devices || devices.length === 0) {
                    var noMicItem = document.createElement('div');
                    noMicItem.textContent = window.t ? window.t('microphone.noDevices') : 'No microphone devices detected';
                    Object.assign(noMicItem.style, { padding: '8px 12px', color: 'var(--neko-popup-text-sub)', fontSize: '13px' });
                    listBody.appendChild(noMicItem);
                    requestAnimationFrame(function () { positionMicSubwindow(panel); });
                    return;
                }

                var sep = document.createElement('div');
                Object.assign(sep.style, { height: '1px', backgroundColor: 'var(--neko-popup-separator)', margin: '5px 0' });
                listBody.appendChild(sep);

                devices.forEach(function (device, idx) {
                    var label = device.label || (window.t ? window.t('microphone.deviceLabel', { index: idx + 1 }) : 'Microphone ' + (idx + 1));
                    listBody.appendChild(createMicDeviceOption(label, device.deviceId));
                });
                requestAnimationFrame(function () { positionMicSubwindow(panel); });
            }

            async function openSpeakerDeviceSubwindow() {
                var panel = createMicSubwindow(
                    window.t ? window.t('speaker.title') : '选择播放设备',
                    null,
                    '280px'
                );
                panel.classList.add('neko-speaker-device-subwindow');
                var panelBody = panel._nekoMicSubwindowBody || panel;
                var listBody = document.createElement('div');
                Object.assign(listBody.style, { display: 'flex', flexDirection: 'column', gap: '4px' });
                panelBody.appendChild(listBody);

                var defaultLabel = window.t ? window.t('speaker.defaultDevice') : '系统默认播放设备';
                listBody.appendChild(createSpeakerDeviceOption(
                    defaultLabel,
                    defaultSpeakerDeviceId
                ));

                var currentAudioOutputs = getCurrentSpeakerOutputs();
                if (currentAudioOutputs.length === 0) {
                    var noSpeakerItem = document.createElement('div');
                    noSpeakerItem.textContent = window.t ? window.t('speaker.noDevices') : '没有检测到播放设备';
                    Object.assign(noSpeakerItem.style, { padding: '8px 12px', color: 'var(--neko-popup-text-sub)', fontSize: '13px' });
                    listBody.appendChild(noSpeakerItem);
                    requestAnimationFrame(function () { positionMicSubwindow(panel); });
                    return;
                }

                var sep = document.createElement('div');
                Object.assign(sep.style, { height: '1px', backgroundColor: 'var(--neko-popup-separator)', margin: '5px 0' });
                listBody.appendChild(sep);

                currentAudioOutputs.forEach(function (device, idx) {
                    var label = device.label || (window.t ? window.t('speaker.deviceLabel', { index: idx + 1 }) : '播放设备 ' + (idx + 1));
                    listBody.appendChild(createSpeakerDeviceOption(label, device.deviceId));
                });
                requestAnimationFrame(function () { positionMicSubwindow(panel); });
            }

            async function openScreenSourceSubwindow() {
                var panel = createMicSubwindow(
                    window.t ? window.t('buttons.screenShare') : 'Screen Share',
                    null,
                    '360px'
                );
                var headerActions = panel._nekoMicSubwindowHeaderActions;
                if (headerActions
                    && typeof window.isScreenSourceTitleMatchEnabled === 'function'
                    && typeof window.setScreenSourceTitleMatchEnabled === 'function') {
                    var rememberWrap = document.createElement('div');
                    rememberWrap.className = 'neko-screen-source-remember-control';
                    Object.assign(rememberWrap.style, {
                        display: 'flex',
                        alignItems: 'center',
                        gap: '6px',
                        color: 'var(--neko-popup-text-sub)',
                        fontSize: '11px',
                        fontWeight: '500',
                        whiteSpace: 'nowrap'
                    });
                    var rememberText = document.createElement('span');
                    rememberText.textContent = window.t
                        ? window.t('app.screenSource.rememberWindow')
                        : '记住窗口';
                    var rememberToggle = createVoiceSettingToggle(
                        window.isScreenSourceTitleMatchEnabled(),
                        function (enabled) {
                            window.setScreenSourceTitleMatchEnabled(enabled);
                        }
                    );
                    rememberToggle.input.classList.add('neko-screen-source-title-match-toggle');
                    rememberToggle.input.setAttribute('aria-label', rememberText.textContent);
                    rememberToggle.input.title = rememberText.textContent;
                    rememberWrap.appendChild(rememberText);
                    rememberWrap.appendChild(rememberToggle.element);
                    headerActions.insertBefore(rememberWrap, headerActions.firstChild);
                }
                var panelBody = panel._nekoMicSubwindowBody || panel;
                var screenSourceList = document.createElement('div');
                screenSourceList.id = micPopup.id ? micPopup.id + '-screen-sources' : 'neko-mic-popup-screen-sources';
                screenSourceList.className = 'neko-mic-popup-screen-sources';
                Object.assign(screenSourceList.style, {
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '2px',
                    minHeight: '80px'
                });
                panelBody.appendChild(screenSourceList);
                // 开始/停止共享开关已移至设置面板「屏幕共享」与「选择麦克风」之间
                // （createScreenShareToggleButton），子窗口仅保留屏幕/窗口源列表。
                positionMicSubwindow(panel);
                if (typeof window.renderFloatingScreenSourceList === 'function') {
                    await window.renderFloatingScreenSourceList(screenSourceList, { requireVisible: false });
                    positionMicSubwindow(panel);
                }
            }

            var deviceButtonLabel = window.t ? window.t('microphone.deviceTitle') : 'Select Microphone';
            var currentMicLabel = S.selectedMicrophoneId === null
                ? (window.t ? window.t('microphone.defaultDevice') : 'System Default Microphone')
                : (audioInputs.find(function (device) { return device.deviceId === S.selectedMicrophoneId; }) || {}).label || deviceButtonLabel;
            var screenButtonLabel = window.t ? window.t('buttons.screenShare') : 'Screen Share';
            var speakerButtonLabel = window.t ? window.t('speaker.title') : '选择播放设备';
            function getCurrentSpeakerOutputs() {
                return (cachedSpeakerDevices || audioOutputs || []).filter(isPhysicalSpeakerDevice);
            }
            function getCurrentSpeakerLabel() {
                if (S.selectedSpeakerId === defaultSpeakerDeviceId) {
                    if (S.effectiveSpeakerId !== defaultSpeakerDeviceId) {
                        return window.t
                            ? window.t('speaker.unavailableFallbackFailed')
                            : '所选设备不可用，且无法切换到系统默认播放设备';
                    }
                    return window.t ? window.t('speaker.defaultDevice') : '系统默认播放设备';
                }
                if (S.selectedSpeakerAvailable === false) {
                    if (S.effectiveSpeakerId !== defaultSpeakerDeviceId) {
                        return window.t
                            ? window.t('speaker.unavailableFallbackFailed')
                            : '所选设备不可用，且无法切换到系统默认播放设备';
                    }
                    return window.t
                        ? window.t('speaker.unavailableFallback')
                        : '所选设备不可用，正使用系统默认播放设备';
                }
                return (getCurrentSpeakerOutputs().find(function (device) { return device.deviceId === S.selectedSpeakerId; }) || {}).label || speakerButtonLabel;
            }
            var currentSpeakerLabel = getCurrentSpeakerLabel();

            var firstContent = leftColumn.firstChild;
            var screenActionButton = createMainActionButton(
                null,
                screenButtonLabel,
                window.t ? window.t('app.screenSource.screens') : 'Screens',
                'screen',
                openScreenSourceSubwindow,
                { openOnHover: false }
            );
            var shareToggleButton = createScreenShareToggleButton({ mini: true });
            var screenActionRow = createMainActionRow(
                screenActionButton,
                shareToggleButton
            );
            leftColumn.insertBefore(screenActionRow, firstContent);
            // 主按钮展开屏幕源，右侧独立按钮开始/停止共享；二者共用行级悬停生命周期。
            // 屏幕共享行：标题允许换行显示（去掉省略号截断），
            // 保证葡语 "Compartilhamento de tela"、俄语 "Демонстрация экрана" 等长文案也能完整显示
            var screenTextWrap = screenActionButton.querySelector('.neko-mic-action-text');
            if (screenTextWrap && screenTextWrap.firstChild) {
                screenTextWrap.firstChild.style.whiteSpace = 'normal';
                screenTextWrap.firstChild.style.lineHeight = '1.2';
                screenTextWrap.firstChild.style.overflow = 'visible';
            }
            var micActionButton = createMainActionButton(
                null,
                deviceButtonLabel,
                currentMicLabel,
                'device',
                openMicDeviceSubwindow
            );
            micActionButton.dataset.nekoMicAction = 'device';
            var micActionRow = createMainActionRow(micActionButton, null);
            leftColumn.insertBefore(micActionRow, firstContent);

            var voiceButtonLabel = window.t
                ? window.t('microphone.independentAsr')
                : '语音识别';
            var asrActionButton = createMainActionButton(
                null,
                voiceButtonLabel,
                '',
                'voice-recognition',
                openVoiceRecognitionSubwindow
            );
            asrToggle.input.setAttribute('aria-label', voiceButtonLabel);
            asrSummary = asrActionButton.querySelector(
                '.neko-mic-action-sub-label'
            );
            var asrActionRow = createMainActionRow(
                asrActionButton,
                asrToggle.element
            );
            leftColumn.insertBefore(asrActionRow, firstContent);
            updateVoiceRecognitionUi();

            var speakerActionButton = createMainActionButton(
                null,
                speakerButtonLabel,
                currentSpeakerLabel,
                'speaker-device',
                openSpeakerDeviceSubwindow
            );
            var speakerActionRow = createMainActionRow(speakerActionButton, null);
            leftColumn.insertBefore(speakerActionRow, firstContent);
            var speakerSummary = speakerActionButton.querySelector('.neko-mic-action-sub-label');
            if (speakerSummary) {
                speakerSummary.setAttribute('aria-live', 'polite');
                speakerSummary.title = currentSpeakerLabel;
            }
            addVoiceWindowListener('neko:speaker-device-changed', function () {
                updateSpeakerOptionSelection();
                if (!speakerSummary) return;
                var nextLabel = getCurrentSpeakerLabel();
                speakerSummary.textContent = nextLabel;
                speakerSummary.title = nextLabel;
            });

            // 组装
            micPopup.appendChild(leftColumn);

            startMicVolumeVisualization();
            return true;
        } catch (error) {
            if (
                renderGeneration !== voiceRecognitionPopoverRenderGeneration
                || !isPopupAvailable()
            ) return false;
            console.error('渲染麦克风列表失败:', error);
            if (disposeVoiceRecognitionPopover) {
                disposeVoiceRecognitionPopover();
            }
            micPopup.innerHTML = '';
            var errorItem = document.createElement('div');
            errorItem.textContent = window.t ? window.t('microphone.loadFailed') : '获取麦克风列表失败';
            Object.assign(errorItem.style, { padding: '8px 12px', color: '#dc3545', fontSize: '13px' });
            micPopup.appendChild(errorItem);
            return true;
        }
    };

    /** 轻量级更新：仅更新选中状态 */
    function updateMicListSelection() {
        var options = document.querySelectorAll('#live2d-popup-mic .mic-option, #vrm-popup-mic .mic-option, #mmd-popup-mic .mic-option, .neko-mic-device-subwindow .mic-option');
        options.forEach(function (option) {
            var deviceId = option.dataset.deviceId;
            var isSelected = (deviceId === undefined && S.selectedMicrophoneId === null) ||
                (deviceId === S.selectedMicrophoneId);
            if (isSelected) {
                option.classList.add('selected');
                option.style.background = 'var(--neko-popup-selected-bg)';
                option.style.color = '#4f8cff';
                option.style.fontWeight = '500';
            } else {
                option.classList.remove('selected');
                option.style.background = 'transparent';
                option.style.color = 'var(--neko-popup-text)';
                option.style.fontWeight = '400';
            }
        });
    }

    // 页面加载后预请求麦克风权限
    setTimeout(async function () {
        console.log('[麦克风] 页面加载，预先请求麦克风权限...');
        try {
            await ensureMicrophonePermission();
            console.log('[麦克风] 权限预请求完成，设备列表已缓存');
            window.dispatchEvent(new CustomEvent('mic-permission-ready'));
        } catch (error) {
            console.warn('[麦克风] 预请求权限失败:', error);
        }
    }, 500);

    // 延迟渲染麦克风列表
    setTimeout(function () {
        window.renderFloatingMicList();
    }, 1500);

    mod.ensureMicrophonePermission = ensureMicrophonePermission;
    mod.updateMicListSelection = updateMicListSelection;

    window.appAudioCapture = mod;
})();
