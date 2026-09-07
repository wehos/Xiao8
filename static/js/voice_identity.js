(function () {
    'use strict';

    const TARGET_SAMPLE_RATE = 48000;
    const REFERENCE_RECORDING_MS = 3000;
    const VERIFICATION_RECORDING_MS = 5000;
    const STREAMING_RESAMPLE_MARGIN_MS = 100;
    const REQUIRED_SEGMENTS = 4;
    const SEGMENT_PREPARATION_MS = 2000;
    const PREPARATION_TICK_MS = 1000;
    const CAPTURE_TIMEOUT_GRACE_MS = 1000;
    const WINDOW_CLOSE_START_WAIT_MS = 500;
    const SESSION_HEADER = 'X-Voice-Identity-Enrollment';
    const PROFILE_HEADER = 'X-Voice-Identity-Profile';
    const SEGMENT_HEADER = 'X-Voice-Identity-Segment';
    const AUDIO_CONTRACT_HEADER = 'X-Voice-Audio-Contract';
    const AUDIO_CONTRACT_ID = 'owner-campplus-desktop-v1';
    const PCM_CONTENT_TYPE = 'audio/pcm;format=pcm_s16le;rate=48000;channels=1';
    const SELECTED_MICROPHONE_STORAGE_KEY = 'neko_selected_microphone';
    const MICROPHONE_GAIN_STORAGE_KEY = 'neko_mic_gain_db';
    const DEFAULT_MICROPHONE_GAIN_DB = 0;
    const MIN_MICROPHONE_GAIN_DB = -5;
    const MAX_MICROPHONE_GAIN_DB = 25;
    const API_ROOT = '/api/voice-identity';
    const READING_PROMPT_KEYS = Object.freeze([
        'voiceIdentity.readingPrompt1', 'voiceIdentity.readingPrompt2',
        'voiceIdentity.readingPrompt3', 'voiceIdentity.readingPrompt4',
        'voiceIdentity.readingPrompt5', 'voiceIdentity.readingPrompt6',
        'voiceIdentity.readingPrompt7', 'voiceIdentity.readingPrompt8',
        'voiceIdentity.readingPrompt9', 'voiceIdentity.readingPrompt10',
        'voiceIdentity.readingPrompt11', 'voiceIdentity.readingPrompt12'
    ]);
    const READING_PROMPT_FALLBACKS = Object.freeze([
        '今天天气不错，我想出去走走。',
        '桌上的热茶，正慢慢冒着香气。',
        '晚上有空，我会听一会儿音乐。',
        '现在请确认，这确实是我的声音。',
        '清晨的阳光，让房间变得很温暖。',
        '窗外的微风，轻轻吹过树叶。',
        '忙完今天的事情，我想休息一会儿。',
        '这段时间很安静，说话也很自然。',
        '柔和的灯光，正照在桌面上。',
        '我喜欢用平常的声音和朋友聊天。',
        '前面的道路，看起来清晰又平静。',
        '我正在用自己平时的声音说话。'
    ]);
    const EFFECTIVE_REASON_KEYS = Object.freeze({
        disabled: 'voiceIdentity.reasonDisabled', ready: 'voiceIdentity.profileReady',
        no_profile: 'voiceIdentity.profileMissing', model_unavailable: 'voiceIdentity.reasonModelUnavailable',
        profile_incompatible: 'voiceIdentity.reasonProfileIncompatible',
        audio_contract_mismatch: 'voiceIdentity.reasonAudioContractMismatch',
        secure_storage_unavailable: 'voiceIdentity.reasonSecureStorageUnavailable',
        enrollment_active: 'voiceIdentity.reasonEnrollmentActive',
        runtime_degraded: 'voiceIdentity.reasonRuntimeDegraded',
        activation_pending: 'voiceIdentity.reasonActivationPending',
        unsupported_asr_route: 'voiceIdentity.reasonUnsupportedAsrRoute',
        shadow_mode: 'voiceIdentity.reasonShadowMode'
    });
    const ENROLLMENT_ERROR_MESSAGES = Object.freeze({
        model_unavailable: ['voiceIdentity.errorModelUnavailable', '声纹模型不可用，请修复模型资源后重试。'],
        audio_processing_unavailable: ['voiceIdentity.errorAudioProcessingUnavailable', '录音处理暂时不可用，请重新启动麦克风后重试。'],
        invalid_pcm: ['voiceIdentity.errorInvalidPcm', '录音格式无效，请重录当前段。'],
        speech_too_short: ['voiceIdentity.errorSpeechTooShort', '没有检测到足够的人声，请重录当前段。'],
        silence: ['voiceIdentity.errorVolumeTooLow', '录音音量过低，请重录当前段。'],
        volume_too_low: ['voiceIdentity.errorVolumeTooLow', '录音音量过低，请重录当前段。'],
        severe_clipping: ['voiceIdentity.errorSevereClipping', '录音出现严重爆音，请重录当前段。'],
        no_speech_detected: ['voiceIdentity.errorNoSpeechDetected', '没有检测到足够的人声，请重录当前段。'],
        audio_too_long: ['voiceIdentity.errorAudioTooLong', '录音时间过长，请重录当前段。'],
        voice_samples_inconsistent: ['voiceIdentity.errorVoiceSamplesInconsistent', '几段声音差异较大，请从第一段重新录入。'],
        owner_verification_failed: ['voiceIdentity.errorOwnerVerificationFailed', '生成的声纹未能稳定识别你，请按当前进度重录。'],
        segment_out_of_order: ['voiceIdentity.errorSegmentOutOfOrder', '录入进度已变化，请按当前步骤继续。'],
        segment_in_progress: ['voiceIdentity.errorSegmentInProgress', '当前录音仍在检查，请稍后继续。'],
        stale_enrollment: ['voiceIdentity.errorStaleEnrollment', '本次录入已过期，请重新开始。'],
        secure_storage_unavailable: ['voiceIdentity.errorSecureStorageUnavailable', '本机安全存储不可用，无法保存声纹。']
    });
    const state = {
        csrfToken: '', enrollmentId: null, profileId: null,
        profileAvailable: false, profileRevision: null, requestedEnabled: false,
        effectiveEnabled: false, effectiveReason: 'no_profile', nextSegmentIndex: 1,
        acceptedSegments: 0, enrollmentPhase: 'collecting_reference',
        remainingSeconds: null, remainingObservedAt: 0, mediaStream: null,
        audioContext: null, captureAbort: null, uploadAbort: null,
        preparing: false, preparationSeconds: null, preparationContext: null, preparationAbort: null,
        recording: false, saving: false, cancelPending: false, filterPending: false,
        busy: false, initialized: false, closeStarted: false, startSettled: null,
        operationNonce: 0, ttlTimer: null, ttlSettling: false,
        message: { kind: 'text', text: '', isError: false, verification: null },
        passiveRefreshPromise: null, passiveRefreshContext: null,
        passiveRefreshQueued: false, refreshAfterCompletion: false
    };
    const elements = {};

    function translate(key, fallback, options) {
        if (typeof window.t === 'function') {
            const value = window.t(key, options || {});
            if (typeof value === 'string' && value && value !== key) return value;
        }
        return fallback;
    }

    function cacheElements() {
        for (const [name, id] of Object.entries({
            statusDot: 'voice-identity-status-dot', profileStatus: 'voice-identity-profile-status',
            enrollment: 'voice-identity-enrollment', captureStatus: 'voice-identity-capture-status',
            captureLabel: 'voice-identity-capture-label', timer: 'voice-identity-timer',
            message: 'voice-identity-message', start: 'voice-identity-start',
            cancel: 'voice-identity-cancel', profileControls: 'voice-identity-profile-controls',
            reenroll: 'voice-identity-reenroll', delete: 'voice-identity-delete',
            filter: 'voice-identity-filter', progressLabel: 'voice-identity-progress-label',
            phase: 'voice-identity-phase', remaining: 'voice-identity-remaining',
            readingPrompt: 'voice-identity-reading-prompt',
            readingText: 'voice-identity-reading-text',
            verificationHelp: 'voice-identity-verification-help'
        })) elements[name] = document.getElementById(id);
        elements.steps = Array.from(document.querySelectorAll('[data-voice-segment]'));
    }

    function readingPromptOrder(enrollmentId) {
        let seed = 2166136261;
        for (let index = 0; index < enrollmentId.length; index += 1) {
            seed ^= enrollmentId.charCodeAt(index);
            seed = Math.imul(seed, 16777619) >>> 0;
        }
        const order = READING_PROMPT_KEYS.map((key, index) => ({
            key,
            fallback: READING_PROMPT_FALLBACKS[index]
        }));
        for (let index = order.length - 1; index > 0; index -= 1) {
            seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
            const swapIndex = seed % (index + 1);
            [order[index], order[swapIndex]] = [order[swapIndex], order[index]];
        }
        return order.slice(0, REQUIRED_SEGMENTS);
    }

    function currentReadingPrompt() {
        if (!state.enrollmentId) return '';
        if (state.nextSegmentIndex === REQUIRED_SEGMENTS) {
            return translate(
                'voiceIdentity.verificationPrompt',
                '现在请用平常的语气完整读完这句话，让我确认这段声音确实来自同一个人。'
            );
        }
        const prompt = readingPromptOrder(state.enrollmentId)[state.nextSegmentIndex - 1];
        return prompt ? translate(prompt.key, prompt.fallback) : '';
    }

    async function loadCsrfToken() {
        const response = await fetch('/api/config/page_config', { cache: 'no-store', credentials: 'same-origin' });
        if (!response.ok) throw new Error('page_config_unavailable');
        const payload = await response.json();
        state.csrfToken = typeof payload.autostart_csrf_token === 'string' ? payload.autostart_csrf_token : '';
        if (!state.csrfToken) throw new Error('csrf_token_unavailable');
    }

    async function apiRequest(path, options) {
        const config = options || {};
        const method = String(config.method || 'GET').toUpperCase();
        const mutation = !['GET', 'HEAD', 'OPTIONS'].includes(method);
        async function send() {
            const headers = new Headers(config.headers || {});
            if (mutation) headers.set('X-CSRF-Token', state.csrfToken);
            if (state.enrollmentId && !headers.has(SESSION_HEADER)) headers.set(SESSION_HEADER, state.enrollmentId);
            const response = await fetch(`${API_ROOT}${path}`, {
                credentials: 'same-origin', cache: 'no-store', ...config, headers
            });
            let payload = {};
            try { payload = await response.json(); } catch (_) {}
            return { response, payload };
        }
        let result = await send();
        if (mutation && result.response.status === 403 && result.payload.error_code === 'csrf_validation_failed') {
            await loadCsrfToken();
            result = await send();
        }
        if (!result.response.ok) {
            const error = new Error(result.payload.error_code || 'request_failed');
            error.status = result.response.status;
            throw error;
        }
        return result.payload;
    }

    function valueFrom(sources, names, type, fallback) {
        for (const source of sources) {
            if (!source || typeof source !== 'object') continue;
            for (const name of names) {
                const value = source[name];
                if (type === 'scalar' && (typeof value === 'string' || typeof value === 'number')) return value;
                if (typeof value === type && (type !== 'string' || value)) return value;
            }
        }
        return fallback;
    }

    function applyStatus(payload) {
        const status = payload && typeof payload === 'object' ? payload : {};
        const enrollment = status.enrollment && typeof status.enrollment === 'object' ? status.enrollment : null;
        const profile = status.profile && typeof status.profile === 'object' ? status.profile : {};
        const filter = status.filter && typeof status.filter === 'object' ? status.filter : {};
        const enrollmentId = valueFrom([status, enrollment], ['enrollment_id', 'id', 'session_id'], 'string', null);
        const active = valueFrom([status, enrollment], ['enrollment_active', 'active'], 'boolean', Boolean(enrollmentId));
        if (active && enrollmentId) {
            state.enrollmentId = enrollmentId;
            state.profileId = valueFrom([enrollment, status], ['profile_id'], 'string', state.profileId);
            const next = enrollment.next_segment_index;
            const accepted = enrollment.accepted_segments;
            state.nextSegmentIndex = Number.isInteger(next) && next >= 1 && next <= 4 ? next : state.nextSegmentIndex;
            state.acceptedSegments = Number.isInteger(accepted) && accepted >= 0 && accepted <= 4
                ? accepted : Math.max(0, state.nextSegmentIndex - 1);
            state.enrollmentPhase = valueFrom([enrollment], ['phase'], 'string', 'collecting_reference');
            state.remainingSeconds = typeof enrollment.remaining_seconds === 'number' && Number.isFinite(enrollment.remaining_seconds)
                ? Math.max(0, enrollment.remaining_seconds) : null;
            state.remainingObservedAt = performance.now();
            startTtlClock();
        } else if ('enrollment' in status || 'enrollment_active' in status) {
            stopTtlClock();
            state.enrollmentId = null;
            state.profileId = null;
            state.nextSegmentIndex = 1;
            state.acceptedSegments = 0;
            state.enrollmentPhase = 'collecting_reference';
            state.remainingSeconds = null;
        }
        state.profileAvailable = valueFrom([status, profile], ['has_profile', 'profile_available', 'available'], 'boolean', state.profileAvailable);
        state.profileRevision = valueFrom([status, profile], ['profile_generation'], 'scalar', state.profileRevision);
        state.requestedEnabled = valueFrom([status, filter], ['requested_enabled', 'enabled'], 'boolean', state.requestedEnabled);
        state.effectiveEnabled = valueFrom([status, filter], ['effective_enabled'], 'boolean', state.requestedEnabled && state.profileAvailable);
        state.effectiveReason = valueFrom([status, filter], ['effective_reason', 'reason'], 'string', state.effectiveEnabled ? 'ready' : (state.profileAvailable ? 'disabled' : 'no_profile'));
        if (!state.profileAvailable) state.effectiveEnabled = false;
        if (state.preparationContext && !preparationMatches(state.preparationContext)) {
            stopPreparation('stale_enrollment');
        }
        render();
    }

    async function getCanonicalStatus() { return apiRequest('/status', { method: 'GET' }); }
    async function reconcileStatus() {
        try {
            const payload = await getCanonicalStatus();
            applyStatus(payload);
            return payload;
        } catch (_) { return null; }
    }
    function completionMatches(payload, enrollmentId, profileId) {
        return Boolean(payload && payload.has_profile === true
            && payload.last_completed_enrollment_id === enrollmentId
            && payload.profile_generation === profileId);
    }
    function setMessage(message, isError) {
        state.message = {
            kind: 'text', text: message || '', isError: Boolean(isError), verification: null
        };
        renderMessage();
    }
    function setCompletionMessage(verification) {
        state.message = {
            kind: 'completion', text: '', isError: false,
            verification: verification ? {
                passed: verification.passed,
                matchPercent: verification.matchPercent
            } : null
        };
        renderMessage();
    }
    function renderMessage() {
        const message = state.message;
        const text = message.kind === 'completion'
            ? (message.verification
                ? verificationMessage(message.verification)
                : enrollmentCompleteMessage())
            : message.text;
        elements.message.textContent = text || '';
        elements.message.classList.toggle('error', Boolean(message.isError));
    }
    function enrollmentErrorMessage(error) {
        const configured = error && ENROLLMENT_ERROR_MESSAGES[error.message];
        return configured ? translate(configured[0], configured[1])
            : translate('voiceIdentity.requestFailed', '操作失败，请稍后重试。');
    }
    function reasonMessage() {
        if (!state.profileAvailable) {
            if (['disabled', 'no_profile'].includes(state.effectiveReason)) {
                return translate('voiceIdentity.profileMissing', '尚未录入 Owner 声纹');
            }
            return translate(EFFECTIVE_REASON_KEYS[state.effectiveReason] || 'voiceIdentity.reasonRuntimeDegraded', '声纹暂时不可用，独立 ASR 将正常放行');
        }
        if (state.effectiveEnabled) return translate('voiceIdentity.profileReady', 'Owner 声纹已保存并启用');
        if (!state.requestedEnabled || state.effectiveReason === 'disabled') {
            return translate('voiceIdentity.profileSavedDisabled', 'Owner 声纹已保存，过滤当前关闭');
        }
        if (state.effectiveReason === 'activation_pending') {
            return translate(EFFECTIVE_REASON_KEYS.activation_pending, '设置已保存，等待语音链路就绪');
        }
        return translate(EFFECTIVE_REASON_KEYS[state.effectiveReason] || 'voiceIdentity.reasonRuntimeDegraded', '声纹暂时不可用，独立 ASR 将正常放行');
    }
    function enrollmentCompleteMessage() {
        if (state.effectiveEnabled) return translate('voiceIdentity.enrollmentComplete', 'Owner 声纹已保存并启用。');
        if (!state.requestedEnabled) return translate('voiceIdentity.profileSavedDisabled', 'Owner 声纹已保存，过滤当前关闭');
        return reasonMessage();
    }
    function phaseMessage() {
        const phases = {
            collecting_reference: ['voiceIdentity.phaseCollectingReference', '正在收集参考语音'],
            checking_consistency: ['voiceIdentity.phaseCheckingConsistency', '正在检查几段声音是否一致'],
            verifying: ['voiceIdentity.phaseVerifying', '正在验证 Owner 声纹'],
            committing: ['voiceIdentity.phaseCommitting', '正在安全保存声纹']
        };
        const configured = phases[state.enrollmentPhase] || phases.collecting_reference;
        return translate(configured[0], configured[1]);
    }
    function remainingSeconds() {
        if (state.remainingSeconds === null) return null;
        return Math.max(0, Math.ceil(state.remainingSeconds - Math.max(0, performance.now() - state.remainingObservedAt) / 1000));
    }
    function createPreparationContext(nonce, enrollmentId, profileId, segmentIndex) {
        return Object.freeze({
            operationNonce: nonce,
            enrollmentId,
            profileId,
            segmentIndex,
            deadline: performance.now() + SEGMENT_PREPARATION_MS
        });
    }
    function preparationMatches(context) {
        return Boolean(context
            && state.preparationContext === context
            && state.operationNonce === context.operationNonce
            && state.enrollmentId === context.enrollmentId
            && state.profileId === context.profileId
            && state.nextSegmentIndex === context.segmentIndex
            && !state.cancelPending
            && !state.closeStarted);
    }
    function preparationMatchesForCapture(context) {
        return preparationMatches(context)
            && state.preparing
            && !state.ttlSettling
            && remainingSeconds() !== 0;
    }
    function stopPreparation(reason) {
        const abort = state.preparationAbort;
        state.preparationAbort = null;
        state.preparationContext = null;
        state.preparationSeconds = null;
        state.preparing = false;
        if (abort) abort(reason || 'preparation_cancelled');
    }
    async function prepareCurrentSegment(context) {
        stopPreparation('preparation_replaced');
        state.preparationContext = context;
        state.preparationSeconds = Math.max(1, Math.ceil(
            (context.deadline - performance.now()) / PREPARATION_TICK_MS
        ));
        state.preparing = true;
        state.recording = false;
        state.saving = false;
        render();

        let result = 'stale';
        let timeoutId = null;
        let abortPreparation = null;
        try {
            result = await new Promise(function (resolve) {
                let settled = false;
                function finish(value) {
                    if (settled) return;
                    settled = true;
                    if (timeoutId !== null) window.clearTimeout(timeoutId);
                    timeoutId = null;
                    resolve(value);
                }
                abortPreparation = function () { finish('stale'); };
                state.preparationAbort = abortPreparation;
                function tick() {
                    timeoutId = null;
                    if (!preparationMatches(context)) {
                        finish('stale');
                        return;
                    }
                    if (remainingSeconds() === 0) {
                        finish('stale');
                        expireEnrollment().catch(function () {});
                        return;
                    }
                    const remainingMs = context.deadline - performance.now();
                    if (remainingMs <= 0) {
                        finish('ready');
                        return;
                    }
                    const seconds = Math.ceil(remainingMs / PREPARATION_TICK_MS);
                    if (state.preparationSeconds !== seconds) {
                        state.preparationSeconds = seconds;
                        render();
                    }
                    timeoutId = window.setTimeout(tick, Math.min(PREPARATION_TICK_MS, remainingMs));
                }
                tick();
            });
            return result;
        } finally {
            if (state.preparationContext === context) {
                if (state.preparationAbort === abortPreparation) state.preparationAbort = null;
                if (result !== 'ready') {
                    state.preparationContext = null;
                    state.preparationSeconds = null;
                    state.preparing = false;
                    render();
                }
            }
        }
    }
    function stopTtlClock() {
        if (state.ttlTimer !== null) {
            window.clearInterval(state.ttlTimer);
            state.ttlTimer = null;
        }
    }
    function startTtlClock() {
        if (state.ttlTimer !== null || state.remainingSeconds === null) return;
        if (remainingSeconds() === 0) {
            Promise.resolve().then(function () {
                expireEnrollment().catch(function () {});
            });
            return;
        }
        state.ttlTimer = window.setInterval(function () {
            renderEnrollment();
            if (remainingSeconds() === 0) expireEnrollment().catch(function () {});
        }, 1000);
    }
    async function expireEnrollment() {
        if (state.ttlSettling || !state.enrollmentId) return;
        state.ttlSettling = true;
        stopTtlClock();
        const expiredEnrollmentId = state.enrollmentId;
        ++state.operationNonce;
        stopMicrophone('stale_enrollment');
        try {
            const canonical = await reconcileStatus();
            if (canonical && state.enrollmentId === expiredEnrollmentId && remainingSeconds() > 0) {
                startTtlClock();
                return;
            }
            if (state.enrollmentId === expiredEnrollmentId) {
                try { await cancelSession(); } catch (_) {}
            }
            setMessage(translate('voiceIdentity.errorStaleEnrollment', '本次录入已过期，请重新开始。'), true);
        } finally {
            state.busy = false;
            state.cancelPending = false;
            state.ttlSettling = false;
            render();
        }
    }
    function renderProfile() {
        const enrollmentVisible = !state.profileAvailable || state.busy || state.cancelPending || Boolean(state.enrollmentId);
        elements.enrollment.hidden = !enrollmentVisible;
        elements.profileControls.hidden = !state.profileAvailable || enrollmentVisible;
        elements.statusDot.className = 'status-dot';
        if (state.effectiveEnabled) elements.statusDot.classList.add('ready');
        else if (state.profileAvailable) elements.statusDot.classList.add('warning');
        elements.profileStatus.textContent = reasonMessage();
        const pending = !state.initialized || state.busy || state.cancelPending || state.filterPending;
        const unavailable = ['model_unavailable', 'secure_storage_unavailable'].includes(state.effectiveReason);
        elements.start.hidden = state.busy || state.cancelPending || (state.profileAvailable && !state.enrollmentId);
        elements.start.disabled = pending || unavailable;
        elements.start.textContent = state.enrollmentId
            ? translate('voiceIdentity.continueEnrollment', '继续当前录入')
            : translate('voiceIdentity.enrollAndEnable', '录入并启用声纹');
        elements.cancel.hidden = !state.busy && !state.cancelPending && !state.enrollmentId;
        elements.cancel.disabled = state.cancelPending;
        elements.reenroll.disabled = pending || unavailable;
        elements.delete.disabled = pending;
        if (!state.filterPending) elements.filter.checked = state.requestedEnabled;
        elements.filter.disabled = pending;
    }
    function renderEnrollment() {
        const readingPrompt = currentReadingPrompt();
        elements.readingPrompt.hidden = !readingPrompt;
        elements.readingText.textContent = readingPrompt;
        elements.verificationHelp.hidden = !state.enrollmentId || state.nextSegmentIndex !== REQUIRED_SEGMENTS;
        elements.captureStatus.hidden = !state.preparing && !state.recording && !state.saving;
        elements.captureStatus.classList.toggle('preparing', state.preparing);
        elements.captureStatus.classList.toggle('saving', state.saving);
        const captureLabel = state.preparing
            ? translate('voiceIdentity.preparingRecording', '准备录音…')
            : (state.recording ? translate('voiceIdentity.recording', '正在录音…') : phaseMessage());
        if (elements.captureLabel.textContent !== captureLabel) elements.captureLabel.textContent = captureLabel;
        if (state.preparing) {
            const seconds = state.preparationSeconds === null ? 2 : state.preparationSeconds;
            elements.timer.textContent = translate(
                'voiceIdentity.recordingStartsInSeconds',
                `${seconds} 秒后开始`,
                { seconds }
            );
        } else if (!state.recording) {
            elements.timer.textContent = '';
        }
        elements.progressLabel.textContent = translate('voiceIdentity.segmentProgress', `第 ${state.nextSegmentIndex}/4 段`, { current: state.nextSegmentIndex, total: 4 });
        elements.phase.textContent = state.enrollmentId ? phaseMessage() : '';
        const remaining = remainingSeconds();
        elements.remaining.textContent = remaining === null || !state.enrollmentId ? ''
            : translate('voiceIdentity.expiresInSeconds', `本次录入剩余 ${remaining} 秒`, { seconds: remaining });
        for (const step of elements.steps) {
            const index = Number(step.getAttribute('data-voice-segment'));
            step.classList.toggle('completed', index < state.nextSegmentIndex);
            step.classList.toggle('current', index === state.nextSegmentIndex);
            if (index === state.nextSegmentIndex) step.setAttribute('aria-current', 'step');
            else step.removeAttribute('aria-current');
        }
    }
    function render() { renderProfile(); renderEnrollment(); renderMessage(); }

    function passiveRefreshIdentity() {
        return Object.freeze({
            operationNonce: state.operationNonce,
            enrollmentId: state.enrollmentId,
            profileId: state.profileId,
            profileRevision: state.profileRevision
        });
    }
    function isIdleForPassiveRefresh() {
        return Boolean(state.initialized
            && document.visibilityState !== 'hidden'
            && !state.busy
            && !state.cancelPending
            && !state.filterPending
            && !state.enrollmentId
            && !state.preparing
            && !state.recording
            && !state.saving
            && !state.ttlSettling
            && !state.closeStarted);
    }
    function passiveRefreshMatches(identity) {
        return Boolean(identity
            && state.operationNonce === identity.operationNonce
            && state.enrollmentId === identity.enrollmentId
            && state.profileId === identity.profileId
            && state.profileRevision === identity.profileRevision);
    }
    function refreshStatusWhenIdle() {
        if (state.passiveRefreshPromise) {
            if (isIdleForPassiveRefresh()
                && !passiveRefreshMatches(state.passiveRefreshContext)) {
                state.passiveRefreshQueued = true;
            }
            return state.passiveRefreshPromise;
        }
        if (!isIdleForPassiveRefresh()) return Promise.resolve(null);
        const identity = passiveRefreshIdentity();
        const request = getCanonicalStatus().then(function (payload) {
            if (!isIdleForPassiveRefresh() || !passiveRefreshMatches(identity)) return null;
            applyStatus(payload);
            return payload;
        }).catch(function () {
            return null;
        }).finally(function () {
            if (state.passiveRefreshPromise !== request) return;
            state.passiveRefreshPromise = null;
            state.passiveRefreshContext = null;
            const queued = state.passiveRefreshQueued;
            state.passiveRefreshQueued = false;
            if (queued) refreshStatusWhenIdle();
        });
        state.passiveRefreshPromise = request;
        state.passiveRefreshContext = identity;
        return request;
    }

    function localStorageValue(key) {
        try { return window.localStorage ? window.localStorage.getItem(key) : null; }
        catch (_) { return null; }
    }
    function selectedMicrophoneId() {
        const value = localStorageValue(SELECTED_MICROPHONE_STORAGE_KEY);
        return typeof value === 'string' && value ? value : null;
    }
    function microphoneGain() {
        const saved = Number.parseFloat(localStorageValue(MICROPHONE_GAIN_STORAGE_KEY));
        const gainDb = Number.isFinite(saved)
            && saved >= MIN_MICROPHONE_GAIN_DB && saved <= MAX_MICROPHONE_GAIN_DB
            ? saved : DEFAULT_MICROPHONE_GAIN_DB;
        return Math.pow(10, gainDb / 20);
    }
    function microphoneConstraints(deviceId) {
        const audio = {
            noiseSuppression: false,
            echoCancellation: true,
            autoGainControl: true,
            channelCount: 1
        };
        if (deviceId) audio.deviceId = { exact: deviceId };
        return { audio, video: false };
    }
    function selectedMicrophoneFallbackEligible(error) {
        return Boolean(error && ['NotFoundError', 'OverconstrainedError', 'NotReadableError'].includes(error.name));
    }
    async function openMicrophone() {
        const deviceId = selectedMicrophoneId();
        if (!deviceId) {
            return {
                stream: await navigator.mediaDevices.getUserMedia(microphoneConstraints(null)),
                fallbackFromSelected: false
            };
        }
        try {
            return {
                stream: await navigator.mediaDevices.getUserMedia(microphoneConstraints(deviceId)),
                fallbackFromSelected: false
            };
        } catch (error) {
            if (!selectedMicrophoneFallbackEligible(error)) throw error;
            return {
                stream: await navigator.mediaDevices.getUserMedia(microphoneConstraints(null)),
                fallbackFromSelected: true
            };
        }
    }
    async function ensureMicrophone() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw new Error('media_devices_unavailable');
        let fallbackFromSelected = false;
        if (!state.mediaStream) {
            const opened = await openMicrophone();
            state.mediaStream = opened.stream;
            fallbackFromSelected = opened.fallbackFromSelected;
        }
        if (!state.audioContext) {
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextClass || typeof AudioWorkletNode !== 'function') throw new Error('audio_worklet_unavailable');
            const context = new AudioContextClass({ sampleRate: TARGET_SAMPLE_RATE });
            try {
                if (context.sampleRate !== TARGET_SAMPLE_RATE) throw new Error('unsupported_audio_sample_rate');
                await context.audioWorklet.addModule('/static/audio-processor.js');
            }
            catch (error) { await context.close(); throw error; }
            state.audioContext = context;
        }
        if (fallbackFromSelected) {
            try { window.localStorage.removeItem(SELECTED_MICROPHONE_STORAGE_KEY); } catch (_) {}
        }
    }
    async function capturePcm16(recordingDurationMs) {
        await ensureMicrophone();
        const context = state.audioContext;
        const source = context.createMediaStreamSource(state.mediaStream);
        const processor = new AudioWorkletNode(context, 'audio-processor', {
            numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
            processorOptions: { originalSampleRate: context.sampleRate, targetSampleRate: TARGET_SAMPLE_RATE }
        });
        const gain = context.createGain();
        const mute = context.createGain();
        const chunks = [];
        const captureDurationMs = recordingDurationMs === VERIFICATION_RECORDING_MS
            ? recordingDurationMs : recordingDurationMs + STREAMING_RESAMPLE_MARGIN_MS;
        const targetSamples = TARGET_SAMPLE_RATE * captureDurationMs / 1000;
        let capturedSamples = 0;
        let finishCapture = null;
        gain.gain.value = microphoneGain();
        mute.gain.value = 0;
        source.connect(gain); gain.connect(processor); processor.connect(mute); mute.connect(context.destination);
        await context.resume();
        const startedAt = performance.now();
        const timer = window.setInterval(function () {
            const elapsed = Math.min(recordingDurationMs, performance.now() - startedAt);
            elements.timer.textContent = translate('voiceIdentity.recordingSeconds', `${(elapsed / 1000).toFixed(1)} 秒`, { seconds: (elapsed / 1000).toFixed(1) });
            renderEnrollment();
        }, 100);
        try {
            await new Promise(function (resolve, reject) {
                let settled = false;
                const timeoutId = window.setTimeout(function () { finishCapture(new Error('incomplete_capture')); }, captureDurationMs + CAPTURE_TIMEOUT_GRACE_MS);
                finishCapture = function (error) {
                    if (settled) return;
                    settled = true;
                    window.clearTimeout(timeoutId);
                    error ? reject(error) : resolve();
                };
                state.captureAbort = finishCapture;
                processor.port.onmessage = function (event) {
                    const view = event.data instanceof Int16Array ? event.data : new Int16Array(event.data);
                    if (!view.length) return;
                    const owned = new Int16Array(view);
                    chunks.push(owned);
                    capturedSamples += owned.length;
                    if (capturedSamples >= targetSamples) finishCapture();
                };
            });
            if (capturedSamples < targetSamples) throw new Error('incomplete_capture');
            const pcm = new Int16Array(targetSamples);
            let offset = 0;
            for (const chunk of chunks) {
                const count = Math.min(chunk.length, targetSamples - offset);
                if (count <= 0) break;
                pcm.set(chunk.subarray(0, count), offset);
                offset += count;
            }
            return pcm.buffer;
        } finally {
            state.captureAbort = null;
            window.clearInterval(timer);
            processor.port.onmessage = null;
            if (typeof processor.port.close === 'function') processor.port.close();
            processor.disconnect(); source.disconnect(); gain.disconnect(); mute.disconnect();
            elements.timer.textContent = '';
            for (const chunk of chunks) chunk.fill(0);
        }
    }
    function stopMicrophone(reason) {
        stopPreparation(reason);
        const captureAbort = state.captureAbort;
        state.captureAbort = null;
        if (captureAbort) captureAbort(new Error(reason || 'capture_cancelled'));
        const uploadAbort = state.uploadAbort;
        state.uploadAbort = null;
        if (uploadAbort) uploadAbort.abort();
        if (state.mediaStream) {
            state.mediaStream.getTracks().forEach(function (track) { track.stop(); });
            state.mediaStream = null;
        }
        if (state.audioContext) {
            const context = state.audioContext;
            state.audioContext = null;
            Promise.resolve(context.close()).catch(function () {});
        }
    }
    function createProfileId() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
        if (!window.crypto || typeof window.crypto.getRandomValues !== 'function') throw new Error('crypto_unavailable');
        const bytes = new Uint8Array(16);
        window.crypto.getRandomValues(bytes);
        bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
        const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
        bytes.fill(0);
        return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }
    async function cancelSession(options) {
        const config = options || {};
        const enrollmentId = state.enrollmentId;
        if (!enrollmentId) return;
        const headers = new Headers({ 'X-CSRF-Token': state.csrfToken, [SESSION_HEADER]: enrollmentId });
        if (config.keepalive) {
            state.enrollmentId = null; state.profileId = null;
            await fetch(`${API_ROOT}/enrollment/cancel`, { method: 'POST', headers, credentials: 'same-origin', keepalive: true });
            return;
        }
        const payload = await apiRequest('/enrollment/cancel', { method: 'POST', headers });
        state.enrollmentId = null; state.profileId = null; applyStatus(payload);
    }
    function isMicrophoneError(error) {
        return Boolean(error && (['NotAllowedError', 'NotFoundError', 'NotReadableError'].includes(error.name)
            || ['audio_worklet_unavailable', 'media_devices_unavailable', 'unsupported_audio_sample_rate'].includes(error.message)));
    }
    function enrollmentVerification(payload) {
        const verification = payload && typeof payload.verification === 'object'
            ? payload.verification : null;
        if (!verification || typeof verification.passed !== 'boolean'
            || !Number.isInteger(verification.match_percent)
            || verification.match_percent < 0 || verification.match_percent > 100) return null;
        return { passed: verification.passed, matchPercent: verification.match_percent };
    }
    function verificationMessage(verification) {
        if (verification.passed) {
            const status = enrollmentCompleteMessage();
            return translate(
                'voiceIdentity.verificationPassed',
                `最低声纹相似度 ${verification.matchPercent}%，验证通过。${status}`,
                { percent: verification.matchPercent, status }
            );
        }
        return translate(
            'voiceIdentity.verificationRetry',
            `最低声纹相似度 ${verification.matchPercent}%，请保持自然语气重新验证。`,
            { percent: verification.matchPercent }
        );
    }
    function finishEnrollment(payload, enrollmentId, profileId, verification) {
        if (!completionMatches(payload, enrollmentId, profileId)) throw new Error('profile_not_confirmed');
        applyStatus(payload);
        stopTtlClock();
        state.enrollmentId = null; state.profileId = null;
        stopMicrophone();
        setCompletionMessage(verification);
        state.refreshAfterCompletion = true;
    }
    async function submitCurrentSegment(nonce) {
        const index = state.nextSegmentIndex;
        const enrollmentId = state.enrollmentId;
        const profileId = state.profileId;
        const context = createPreparationContext(nonce, enrollmentId, profileId, index);
        if (await prepareCurrentSegment(context) !== 'ready') return 'stale';
        if (!preparationMatchesForCapture(context)) {
            const expired = remainingSeconds() === 0;
            stopPreparation(expired ? 'stale_enrollment' : 'preparation_cancelled');
            if (expired) expireEnrollment().catch(function () {});
            return 'stale';
        }
        state.preparationContext = null;
        state.preparationSeconds = null;
        state.preparing = false;
        state.recording = true;
        state.saving = false;
        render();
        let pcm = null;
        try {
            const recordingDurationMs = index === REQUIRED_SEGMENTS
                ? VERIFICATION_RECORDING_MS : REFERENCE_RECORDING_MS;
            pcm = await capturePcm16(recordingDurationMs);
            if (nonce !== state.operationNonce) return 'stale';
            state.recording = false; state.saving = true;
            state.enrollmentPhase = index === 3 ? 'checking_consistency' : (index === 4 ? 'verifying' : 'collecting_reference');
            render();
            const controller = typeof AbortController === 'function' ? new AbortController() : null;
            state.uploadAbort = controller;
            let payload;
            try {
                payload = await apiRequest('/enrollment/segment', {
                    method: 'PUT', body: pcm, signal: controller ? controller.signal : undefined,
                    headers: {
                        'Content-Type': PCM_CONTENT_TYPE,
                        [AUDIO_CONTRACT_HEADER]: AUDIO_CONTRACT_ID,
                        [SESSION_HEADER]: enrollmentId,
                        [PROFILE_HEADER]: profileId,
                        [SEGMENT_HEADER]: String(index)
                    }
                });
            } catch (error) {
                if (nonce !== state.operationNonce) return 'stale';
                const canonical = await reconcileStatus();
                if (canonical && completionMatches(canonical, enrollmentId, profileId)) {
                    finishEnrollment(canonical, enrollmentId, profileId, null); return 'complete';
                }
                if (canonical && state.enrollmentId === enrollmentId) {
                    setMessage(enrollmentErrorMessage(error), true); return 'retry';
                }
                throw error;
            } finally {
                if (state.uploadAbort === controller) state.uploadAbort = null;
            }
            if (nonce !== state.operationNonce) return 'stale';
            const verification = index === REQUIRED_SEGMENTS ? enrollmentVerification(payload) : null;
            if (completionMatches(payload, enrollmentId, profileId)) {
                if (verification && !verification.passed) throw new Error('profile_not_confirmed');
                finishEnrollment(payload, enrollmentId, profileId, verification); return 'complete';
            }
            applyStatus(payload);
            if (state.enrollmentId !== enrollmentId) throw new Error('profile_not_confirmed');
            if (verification) {
                if (verification.passed) throw new Error('profile_not_confirmed');
                setMessage(verificationMessage(verification), true);
                return 'retry';
            }
            if (state.nextSegmentIndex <= index) {
                setMessage(translate('voiceIdentity.errorSegmentInProgress', '当前录音仍在检查，请稍后继续。'), true);
                return 'retry';
            }
            setMessage('');
            return 'continue';
        } finally {
            if (pcm) new Uint8Array(pcm).fill(0);
            state.recording = false; state.saving = false; render();
        }
    }
    async function startEnrollment() {
        if (state.busy || state.filterPending || state.cancelPending) return;
        const nonce = ++state.operationNonce;
        stopPreparation('preparation_replaced');
        let startSettled = null;
        let settleStart = null;
        state.busy = true; setMessage(''); render();
        try {
            await ensureMicrophone();
            if (nonce !== state.operationNonce || state.closeStarted) return;
            if (!state.enrollmentId) {
                state.profileId = createProfileId();
                startSettled = new Promise(resolve => { settleStart = resolve; });
                state.startSettled = startSettled;
                let started;
                try {
                    started = await apiRequest('/enrollment/start', { method: 'POST' });
                } catch (error) {
                    const canonical = await reconcileStatus();
                    if (!canonical || !state.enrollmentId) throw error;
                    started = canonical;
                }
                finally {
                    if (settleStart) settleStart();
                    if (state.startSettled === startSettled) state.startSettled = null;
                }
                if (nonce !== state.operationNonce) {
                    const startedEnrollmentId = valueFrom(
                        [started, started && started.enrollment],
                        ['enrollment_id', 'id', 'session_id'],
                        'string',
                        null
                    );
                    if (startedEnrollmentId) {
                        state.enrollmentId = startedEnrollmentId;
                        try { await cancelSession({ keepalive: state.closeStarted }); } catch (_) {}
                    }
                    return;
                }
                applyStatus(started);
                if (!state.enrollmentId) throw new Error('enrollment_id_missing');
            }
            if (!state.profileId) state.profileId = createProfileId();
            while (nonce === state.operationNonce && state.enrollmentId && !state.cancelPending && !state.closeStarted) {
                if (await submitCurrentSegment(nonce) !== 'continue') break;
            }
        } catch (error) {
            if (nonce !== state.operationNonce) return;
            stopMicrophone();
            try { await cancelSession(); } catch (_) {}
            if (!state.cancelPending && !state.closeStarted) {
                setMessage(isMicrophoneError(error)
                    ? translate('voiceIdentity.microphoneDenied', '无法使用麦克风，请检查权限和设备。')
                    : enrollmentErrorMessage(error), true);
            }
        } finally {
            let refreshAfterCompletion = false;
            if (settleStart && state.startSettled === startSettled) {
                state.startSettled = null; settleStart();
            }
            if (nonce === state.operationNonce) {
                stopPreparation('preparation_cancelled');
                state.recording = false; state.saving = false; state.busy = false; state.cancelPending = false;
                refreshAfterCompletion = state.refreshAfterCompletion;
                state.refreshAfterCompletion = false;
            }
            render();
            if (refreshAfterCompletion) refreshStatusWhenIdle();
        }
    }
    async function cancelEnrollment(options) {
        const config = options || {};
        ++state.operationNonce;
        stopTtlClock();
        state.cancelPending = true; stopMicrophone('capture_cancelled'); render();
        try { await cancelSession(config); if (!config.silent) setMessage(''); }
        catch (_) {
            if (!config.keepalive) {
                const reconciled = await reconcileStatus();
                if (!config.silent && (!reconciled || state.enrollmentId)) {
                    setMessage(translate('voiceIdentity.requestFailed', '操作失败，请稍后重试。'), true);
                }
            }
        } finally { state.busy = false; state.cancelPending = false; render(); }
    }
    async function deleteProfile() {
        if (state.busy || state.filterPending) return;
        state.busy = true; setMessage(''); render();
        try {
            const message = translate('voiceIdentity.deleteConfirm', '删除后需要重新录入才能使用声纹过滤。');
            const confirmed = typeof window.showConfirm === 'function'
                ? await window.showConfirm(message, translate('voiceIdentity.delete', '删除声纹'), { danger: true })
                : (typeof window.confirm === 'function' && window.confirm(message));
            if (!confirmed) return;
            const payload = await apiRequest('/profile', { method: 'DELETE' });
            applyStatus(payload);
            if (state.profileAvailable) await reconcileStatus();
        } catch (_) {
            const reconciled = await reconcileStatus();
            if (!reconciled || state.profileAvailable) setMessage(translate('voiceIdentity.requestFailed', '操作失败，请稍后重试。'), true);
        } finally { state.busy = false; render(); }
    }
    async function updateFilter() {
        if (state.filterPending || state.busy) return;
        const desired = elements.filter.checked;
        state.filterPending = true; setMessage(''); render();
        try {
            applyStatus(await apiRequest('/filter', { method: 'PUT', body: JSON.stringify({ enabled: desired }), headers: { 'Content-Type': 'application/json' } }));
        } catch (_) {
            const reconciled = await reconcileStatus();
            if (!reconciled || state.requestedEnabled !== desired) {
                elements.filter.checked = state.requestedEnabled;
                setMessage(translate('voiceIdentity.requestFailed', '操作失败，请稍后重试。'), true);
            }
        } finally { state.filterPending = false; render(); }
    }
    function bindEvents() {
        elements.start.addEventListener('click', startEnrollment);
        elements.reenroll.addEventListener('click', startEnrollment);
        elements.cancel.addEventListener('click', () => cancelEnrollment().catch(function () {}));
        elements.delete.addEventListener('click', deleteProfile);
        elements.filter.addEventListener('change', updateFilter);
        window.addEventListener('localechange', render);
        window.addEventListener('focus', function () { refreshStatusWhenIdle(); });
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') refreshStatusWhenIdle();
        });
        window.nekoBeforeWindowClose = async function () {
            state.closeStarted = true; ++state.operationNonce; state.cancelPending = true;
            stopTtlClock();
            stopMicrophone('capture_cancelled');
            const pendingStart = state.startSettled;
            if (pendingStart) {
                let timeoutId = null;
                const limit = new Promise(resolve => { timeoutId = window.setTimeout(resolve, WINDOW_CLOSE_START_WAIT_MS); });
                await Promise.race([pendingStart, limit]);
                if (timeoutId !== null) window.clearTimeout(timeoutId);
            }
            cancelEnrollment({ keepalive: true, silent: true }).catch(function () {});
            return true;
        };
        window.addEventListener('pagehide', () => { window.nekoBeforeWindowClose().catch(function () {}); });
        window.addEventListener('pageshow', function (event) {
            if (!event.persisted) return;
            stopPreparation('stale_enrollment');
            state.closeStarted = false; state.cancelPending = false; state.busy = false; render();
            refreshStatusWhenIdle();
        });
    }
    async function initialize() {
        cacheElements(); bindEvents(); state.busy = true; render();
        try {
            await loadCsrfToken();
            const status = await getCanonicalStatus();
            state.initialized = true; applyStatus(status);
        } catch (_) { setMessage(translate('voiceIdentity.requestFailed', '操作失败，请稍后重试。'), true); }
        finally { state.busy = false; render(); }
    }
    document.addEventListener('DOMContentLoaded', initialize);
})();
