'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'js/voice_identity.js'), 'utf8');
const stylesheet = fs.readFileSync(path.join(__dirname, 'css/voice_identity.css'), 'utf8');
const darkModeStylesheet = fs.readFileSync(path.join(__dirname, 'css/dark-mode.css'), 'utf8');
const template = fs.readFileSync(
    path.join(__dirname, '../templates/voice_identity.html'),
    'utf8',
);
const runtimeCaptureSource = fs.readFileSync(
    path.join(__dirname, 'app/app-audio-capture.js'),
    'utf8',
);

const API_ROOT = '/api/voice-identity';
const PCM_CONTENT_TYPE = 'audio/pcm;format=pcm_s16le;rate=48000;channels=1';
const AUDIO_CONTRACT_ID = 'owner-campplus-desktop-v1';
const PROFILE_HEADER = 'X-Voice-Identity-Profile';
const TARGET_SAMPLE_RATE = 48000;
const REFERENCE_RECORDING_MS = 3000;
const VERIFICATION_RECORDING_MS = 5000;
const STREAMING_RESAMPLE_MARGIN_MS = 100;
const REFERENCE_CAPTURE_MS = REFERENCE_RECORDING_MS + STREAMING_RESAMPLE_MARGIN_MS;
const REFERENCE_CAPTURE_TIMEOUT_MS = REFERENCE_CAPTURE_MS + 1000;
const VERIFICATION_CAPTURE_TIMEOUT_MS = VERIFICATION_RECORDING_MS + 1000;
const WINDOW_CLOSE_START_WAIT_MS = 500;
const SEGMENT_PREPARATION_MS = 2000;
const PREPARATION_TICK_MS = 1000;
const REFERENCE_TARGET_SAMPLES = TARGET_SAMPLE_RATE * REFERENCE_CAPTURE_MS / 1000;
const VERIFICATION_TARGET_SAMPLES = TARGET_SAMPLE_RATE * VERIFICATION_RECORDING_MS / 1000;
const CHUNK_SAMPLES = 480;

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
    });
    return { promise, resolve, reject };
}

function jsonResponse(payload, { ok = true, status = 200 } = {}) {
    return {
        ok,
        status,
        async json() {
            return payload;
        },
    };
}

class MockHeaders {
    constructor(initial = {}) {
        this.values = new Map();
        if (initial instanceof MockHeaders) {
            initial.values.forEach((value, key) => this.values.set(key, value));
            return;
        }
        Object.entries(initial).forEach(([key, value]) => this.set(key, value));
    }

    set(key, value) {
        this.values.set(String(key).toLowerCase(), String(value));
    }

    get(key) {
        return this.values.get(String(key).toLowerCase());
    }

    has(key) {
        return this.values.has(String(key).toLowerCase());
    }
}

function createElement() {
    const listeners = new Map();
    const classes = new Set();
    const attributes = new Map();
    const element = {
        textContent: '',
        hidden: false,
        disabled: false,
        checked: false,
        setAttribute(name, value) {
            attributes.set(name, String(value));
        },
        getAttribute(name) {
            return attributes.get(name);
        },
        removeAttribute(name) {
            attributes.delete(name);
        },
        addEventListener(type, listener) {
            listeners.set(type, listener);
        },
        emit(type) {
            return listeners.get(type)?.({ type, target: element });
        },
        classList: {
            add(...names) {
                names.forEach(name => classes.add(name));
            },
            toggle(name, force) {
                const enabled = force === undefined ? !classes.has(name) : Boolean(force);
                if (enabled) classes.add(name);
                else classes.delete(name);
                return enabled;
            },
            contains(name) {
                return classes.has(name);
            },
        },
    };
    Object.defineProperty(element, 'className', {
        get() {
            return Array.from(classes).join(' ');
        },
        set(value) {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach(name => classes.add(name));
        },
    });
    return element;
}

function createHarness({
    initialProfile = false,
    initialRequested = false,
    statusGate,
    startGate,
    startError,
    startTransportErrorAfterCreate = false,
    mediaGate,
    mediaError,
    audioChunks = null,
    manualAudio = false,
    manualPreparation = false,
    profileError,
    profileErrorSegment = 1,
    profileTransportErrorAfterCommit = false,
    segmentTransportErrorAfterAccept = null,
    verificationPassed = true,
    verificationMatchPercent = 72,
    verificationNextSegmentIndex = 4,
    verificationTransportErrorAfterResult = false,
    showConfirm,
    nativeConfirm = true,
    webCryptoAvailable = true,
    selectedMicrophoneId = 'selected-microphone',
    microphoneGainDb = '6',
    actualContextSampleRate = TARGET_SAMPLE_RATE,
    initialEffectiveReason = null,
    initialEnrollment = false,
    initialEnrollmentProfileId = null,
    initialNextSegmentIndex = 1,
    initialRemainingSeconds = 45,
    statusHandler,
    finalResponseTransform,
} = {}) {
    const elementIds = [
        'voice-identity-status-dot',
        'voice-identity-profile-status',
        'voice-identity-enrollment',
        'voice-identity-capture-status',
        'voice-identity-capture-label',
        'voice-identity-timer',
        'voice-identity-message',
        'voice-identity-start',
        'voice-identity-cancel',
        'voice-identity-profile-controls',
        'voice-identity-reenroll',
        'voice-identity-delete',
        'voice-identity-filter',
        'voice-identity-progress-label',
        'voice-identity-phase',
        'voice-identity-remaining',
        'voice-identity-reading-prompt',
        'voice-identity-reading-text',
        'voice-identity-verification-help',
    ];
    const elements = new Map(elementIds.map(id => [id, createElement()]));
    const stepElements = [1, 2, 3, 4].map(index => {
        const element = createElement();
        element.setAttribute('data-voice-segment', String(index));
        return element;
    });
    const documentListeners = new Map();
    const windowListeners = new Map();
    const fetchCalls = [];
    const mediaStreams = [];
    const mediaRequestConstraints = [];
    const workletModules = [];
    const workletNodes = [];
    const workletCreations = [];
    const audioMessages = [];
    const gainNodes = [];
    const audioContextOptions = [];
    let processor = null;
    let mediaRequests = 0;
    let serverProfile = initialProfile;
    let serverProfileGeneration = initialProfile ? 'profile-0' : null;
    let serverRequested = initialRequested;
    let enrollmentId = initialEnrollment ? 'enrollment-1' : null;
    let enrollmentProfileId = initialEnrollmentProfileId;
    let nextSegmentIndex = initialEnrollment ? initialNextSegmentIndex : 1;
    let lastCompletedEnrollmentId = null;
    let statusRequestCount = 0;
    let canonicalStatusOverride = null;
    let timerId = 0;
    let nowMs = 1000;
    let enrollmentExpiresAtMs = enrollmentId
        ? nowMs + initialRemainingSeconds * 1000
        : null;
    const intervals = new Map();
    const timeouts = new Map();
    const timeoutHistory = new Map();
    let audioContext = null;
    let currentStartGate = startGate;

    function nextDueTimer(targetMs) {
        let next = null;
        for (const [id, timer] of timeouts) {
            if (timer.dueAt > targetMs) continue;
            if (!next || timer.dueAt < next.dueAt || (timer.dueAt === next.dueAt && id < next.id)) {
                next = { id, type: 'timeout', ...timer };
            }
        }
        for (const [id, timer] of intervals) {
            if (timer.dueAt > targetMs) continue;
            if (!next || timer.dueAt < next.dueAt || (timer.dueAt === next.dueAt && id < next.id)) {
                next = { id, type: 'interval', ...timer };
            }
        }
        return next;
    }

    function advanceVirtualTime(milliseconds) {
        const targetMs = nowMs + milliseconds;
        for (;;) {
            const next = nextDueTimer(targetMs);
            if (!next) break;
            nowMs = next.dueAt;
            if (next.type === 'timeout') {
                if (!timeouts.delete(next.id)) continue;
            } else {
                const interval = intervals.get(next.id);
                if (!interval) continue;
                interval.dueAt += interval.delay;
            }
            next.callback();
        }
        nowMs = targetMs;
    }

    const statusPayload = () => {
        if (enrollmentId && enrollmentExpiresAtMs !== null && nowMs >= enrollmentExpiresAtMs) {
            enrollmentId = null;
            enrollmentProfileId = null;
            nextSegmentIndex = 1;
        }
        return ({
        requested_enabled: serverRequested,
        effective_enabled: serverProfile && serverRequested,
        effective_reason: serverProfile
            ? (serverRequested ? 'ready' : 'disabled')
            : (enrollmentId ? 'enrollment_active' : (initialEffectiveReason || 'no_profile')),
        has_profile: serverProfile,
        enrollment: enrollmentId
            ? {
                enrollment_id: enrollmentId,
                profile_id: enrollmentProfileId,
                expires_at: 123.5,
                remaining_seconds: Math.max(0, (enrollmentExpiresAtMs - nowMs) / 1000),
                accepted_segments: nextSegmentIndex - 1,
                required_segments: 4,
                next_segment_index: nextSegmentIndex,
                phase: nextSegmentIndex < 4 ? 'collecting_reference' : 'verifying',
            }
            : null,
        profile_generation: serverProfileGeneration,
        last_completed_enrollment_id: lastCompletedEnrollmentId,
        runtime_mode: 'enforce',
        ...(canonicalStatusOverride || {}),
        });
    };

    async function defaultRoute(call) {
        if (call.url === '/api/config/page_config') {
            return jsonResponse({ autostart_csrf_token: 'csrf-token' });
        }
        if (call.url === `${API_ROOT}/status`) {
            statusRequestCount += 1;
            if (statusGate && statusRequestCount === 1) return statusGate.promise;
            if (statusHandler) {
                return statusHandler({
                    requestNumber: statusRequestCount,
                    payload: statusPayload(),
                    call,
                });
            }
            return jsonResponse(statusPayload());
        }
        if (call.url === `${API_ROOT}/enrollment/start`) {
            if (currentStartGate) await currentStartGate.promise;
            if (startError) {
                return jsonResponse(
                    { error_code: startError },
                    { ok: false, status: 503 },
                );
            }
            enrollmentId = 'enrollment-1';
            enrollmentProfileId = null;
            nextSegmentIndex = 1;
            enrollmentExpiresAtMs = nowMs + initialRemainingSeconds * 1000;
            if (startTransportErrorAfterCreate) throw new Error('start_response_lost');
            return jsonResponse(statusPayload());
        }
        if (call.url === `${API_ROOT}/enrollment/segment`) {
            const submittedIndex = Number(call.options.headers.get('x-voice-identity-segment'));
            if (profileError && submittedIndex === profileErrorSegment) {
                if (profileError === 'voice_samples_inconsistent') nextSegmentIndex = 1;
                return jsonResponse(
                    { error_code: profileError },
                    { ok: false, status: 422 },
                );
            }
            assert.equal(submittedIndex, nextSegmentIndex);
            enrollmentProfileId = call.options.headers.get(PROFILE_HEADER);
            if (submittedIndex < 4) {
                nextSegmentIndex += 1;
                if (submittedIndex === segmentTransportErrorAfterAccept) {
                    throw new Error('segment_response_lost');
                }
                return jsonResponse(statusPayload());
            }
            if (!verificationPassed) {
                nextSegmentIndex = verificationNextSegmentIndex;
                if (verificationTransportErrorAfterResult) {
                    throw new Error('verification_response_lost');
                }
                return jsonResponse({
                    ...statusPayload(),
                    verification: { passed: false, match_percent: verificationMatchPercent },
                });
            }
            lastCompletedEnrollmentId = enrollmentId;
            enrollmentId = null;
            serverProfile = true;
            serverProfileGeneration = enrollmentProfileId;
            serverRequested = initialProfile ? serverRequested : true;
            if (profileTransportErrorAfterCommit) {
                throw new Error('profile_response_lost');
            }
            const completedPayload = {
                ...statusPayload(),
                verification: { passed: true, match_percent: verificationMatchPercent },
            };
            return jsonResponse(
                finalResponseTransform
                    ? finalResponseTransform(completedPayload)
                    : completedPayload,
            );
        }
        if (call.url === `${API_ROOT}/enrollment/cancel`) {
            enrollmentId = null;
            enrollmentProfileId = null;
            nextSegmentIndex = 1;
            return jsonResponse(statusPayload());
        }
        if (call.url === `${API_ROOT}/filter`) {
            serverRequested = JSON.parse(call.options.body).enabled;
            return jsonResponse(statusPayload());
        }
        if (call.url === `${API_ROOT}/profile`) {
            serverProfile = false;
            serverProfileGeneration = null;
            serverRequested = false;
            enrollmentId = null;
            return jsonResponse(statusPayload());
        }
        throw new Error(`unexpected request: ${call.options.method || 'GET'} ${call.url}`);
    }

    const document = {
        activeElement: null,
        visibilityState: 'visible',
        get hidden() {
            return this.visibilityState !== 'visible';
        },
        getElementById(id) {
            return elements.get(id);
        },
        querySelectorAll(selector) {
            return selector === '[data-voice-segment]' ? stepElements : [];
        },
        addEventListener(type, listener) {
            documentListeners.set(type, listener);
        },
        dispatchEvent(event) {
            return documentListeners.get(event.type)?.(event);
        },
    };
    elements.forEach(element => {
        element.focus = () => {
            document.activeElement = element;
        };
    });

    class MockAudioContext {
        constructor(options) {
            audioContext = this;
            audioContextOptions.push(options);
            this.sampleRate = actualContextSampleRate;
            this.destination = {};
            this.state = 'suspended';
            this.audioWorklet = {
                addModule: async url => {
                    workletModules.push(url);
                },
            };
        }

        createMediaStreamSource() {
            return { connect() {}, disconnect() {} };
        }

        createGain() {
            const node = {
                gain: { value: 1 },
                disconnected: false,
                connect() {},
                disconnect() { this.disconnected = true; },
            };
            gainNodes.push(node);
            return node;
        }

        async resume() {
            this.state = 'running';
        }

        async close() {
            this.state = 'closed';
        }
    }

    class MockAudioWorkletNode {
        constructor(context, name, options) {
            assert.equal(name, 'audio-processor');
            assert.equal(options.processorOptions.originalSampleRate, context.sampleRate);
            assert.equal(options.processorOptions.targetSampleRate, TARGET_SAMPLE_RATE);
            this.port = {
                onmessage: null,
                closed: false,
                close() { this.closed = true; },
            };
            this.disconnected = false;
            processor = this;
            workletNodes.push(this);
            workletCreations.push({
                atMs: nowMs,
                prompt: elements.get('voice-identity-reading-text').textContent,
                captureLabel: elements.get('voice-identity-capture-label').textContent,
            });
        }

        connect() {}

        disconnect() { this.disconnected = true; }
    }

    const window = {
        t(key, options) {
            if (key === 'voiceIdentity.recordingSeconds') {
                return `${options.seconds} s`;
            }
            const translations = {
                'voiceIdentity.profileMissing': 'No Owner voice profile enrolled',
                'voiceIdentity.profileReady': 'Owner voice profile is saved and enabled',
                'voiceIdentity.profileSavedDisabled': 'Owner voice profile is saved; filtering is off',
                'voiceIdentity.reasonRuntimeDegraded': 'Voice filtering is unavailable',
                'voiceIdentity.reasonModelUnavailable': 'Prepare the voice model assets or repair the installation',
                'voiceIdentity.reasonAudioContractMismatch': 'Restore the enrolled noise-reduction setting or re-enroll',
                'voiceIdentity.reasonSecureStorageUnavailable': 'Secure storage is unavailable',
                'voiceIdentity.preparingRecording': 'Preparing to record...',
                'voiceIdentity.recordingStartsInSeconds': `Recording starts in ${options?.seconds} s`,
                'voiceIdentity.recording': 'Recording...',
                'voiceIdentity.saving': 'Saving...',
                'voiceIdentity.enrollmentComplete': 'Enrollment complete.',
                'voiceIdentity.microphoneDenied': 'Microphone unavailable.',
                'voiceIdentity.requestFailed': 'Request failed.',
                'voiceIdentity.errorModelUnavailable': 'Voice model unavailable; prepare assets or repair the installation.',
                'voiceIdentity.errorAudioProcessingUnavailable': 'Microphone audio processing is temporarily unavailable. Restart the microphone and try again.',
                'voiceIdentity.errorInvalidPcm': 'Invalid recording format.',
                'voiceIdentity.errorAudioTooLong': 'Recording is too long.',
                'voiceIdentity.errorSpeechTooShort': 'Not enough speech.',
                'voiceIdentity.errorVoiceSamplesInconsistent': 'Recordings differ too much.',
                'voiceIdentity.errorOwnerVerificationFailed': 'Verification failed.',
                'voiceIdentity.errorSegmentInProgress': 'Still checking.',
                'voiceIdentity.errorStaleEnrollment': 'Enrollment expired.',
                'voiceIdentity.deleteConfirm': 'Delete the profile?',
                'voiceIdentity.delete': 'Delete voice profile',
                'voiceIdentity.readingPromptLabel': 'Please read',
                'voiceIdentity.verificationPrompt': 'Read the five-second verification prompt.',
                'voiceIdentity.verificationPassed': `Lowest voice similarity: ${options?.percent}%. Verification passed. ${options?.status}`,
                'voiceIdentity.verificationRetry': `Lowest voice similarity: ${options?.percent}%. Please keep your natural tone and verify again.`,
            };
            if (/^voiceIdentity\.readingPrompt\d+$/.test(key)) {
                return `Prompt ${key.match(/\d+$/)[0]}`;
            }
            return translations[key] || key;
        },
        addEventListener(type, listener) {
            windowListeners.set(type, listener);
        },
        dispatchEvent(event) {
            return windowListeners.get(event.type)?.(event);
        },
        setInterval(callback, delay) {
            timerId += 1;
            intervals.set(timerId, { callback, delay, dueAt: nowMs + delay });
            return timerId;
        },
        clearInterval(id) {
            intervals.delete(id);
        },
        setTimeout(callback, delay) {
            timerId += 1;
            const id = timerId;
            const timer = { callback, delay, dueAt: nowMs + delay };
            timeouts.set(id, timer);
            timeoutHistory.set(id, timer);
            if ([REFERENCE_CAPTURE_TIMEOUT_MS, VERIFICATION_CAPTURE_TIMEOUT_MS].includes(delay)) {
                if (!manualAudio) {
                    Promise.resolve().then(() => {
                        const targetSamples = delay === VERIFICATION_CAPTURE_TIMEOUT_MS
                            ? VERIFICATION_TARGET_SAMPLES : REFERENCE_TARGET_SAMPLES;
                        const fullAudioChunks = Math.ceil(targetSamples / CHUNK_SAMPLES);
                        const chunksToEmit = audioChunks === null ? fullAudioChunks : audioChunks;
                        for (let index = 0; index < chunksToEmit; index += 1) {
                            audioMessages.push({ atMs: nowMs, prompt: elements.get('voice-identity-reading-text').textContent });
                            processor?.port.onmessage?.({
                                data: new Int16Array(CHUNK_SAMPLES).fill(1024),
                            });
                        }
                        if (chunksToEmit < fullAudioChunks && timeouts.delete(id)) callback();
                    });
                }
            } else if (delay === WINDOW_CLOSE_START_WAIT_MS) {
                Promise.resolve().then(() => {
                    if (timeouts.delete(id)) callback();
                });
            } else if ([PREPARATION_TICK_MS, SEGMENT_PREPARATION_MS].includes(delay)) {
                if (!manualPreparation) {
                    Promise.resolve().then(() => {
                        if (timeouts.has(id)) advanceVirtualTime(delay);
                    });
                }
            } else if (delay <= 0) {
                Promise.resolve().then(() => {
                    if (timeouts.delete(id)) callback();
                });
            } else {
                throw new Error(`unmodeled setTimeout delay: ${delay}`);
            }
            return id;
        },
        clearTimeout(id) { timeouts.delete(id); },
        AudioContext: MockAudioContext,
        webkitAudioContext: undefined,
        showConfirm,
        confirm: () => nativeConfirm,
        crypto: webCryptoAvailable ? {
            randomUUID: () => 'profile-1',
            getRandomValues(values) {
                values.fill(1);
                return values;
            },
        } : undefined,
        localStorage: {
            values: new Map([
                ['neko_selected_microphone', selectedMicrophoneId],
                ['neko_mic_gain_db', microphoneGainDb],
            ].filter(([, value]) => value !== null)),
            getItem(key) { return this.values.has(key) ? this.values.get(key) : null; },
            removeItem(key) { this.values.delete(key); },
        },
    };

    const context = {
        window,
        document,
        navigator: {
            mediaDevices: {
                async getUserMedia(constraints) {
                    mediaRequests += 1;
                    mediaRequestConstraints.push(constraints);
                    if (mediaGate) await mediaGate.promise;
                    const currentMediaError = typeof mediaError === 'function'
                        ? mediaError(mediaRequests) : mediaError;
                    if (currentMediaError) throw currentMediaError;
                    const track = { stopped: false, stop() { this.stopped = true; } };
                    const stream = { getTracks: () => [track], track };
                    mediaStreams.push(stream);
                    return stream;
                },
            },
        },
        fetch: async (url, options = {}) => {
            const call = {
                url,
                atMs: nowMs,
                prompt: elements.get('voice-identity-reading-text').textContent,
                options: { ...options, headers: new MockHeaders(options.headers) },
            };
            fetchCalls.push(call);
            return defaultRoute(call);
        },
        Headers: MockHeaders,
        AudioWorkletNode: MockAudioWorkletNode,
        performance: { now: () => nowMs },
        console: { log() {}, warn() {}, error() {} },
        Uint8Array,
        Int16Array,
        ArrayBuffer,
        Promise,
        Error,
        JSON,
        Math,
    };
    window.window = window;
    window.document = document;
    window.navigator = context.navigator;
    window.fetch = context.fetch;
    window.Headers = MockHeaders;
    window.AudioWorkletNode = MockAudioWorkletNode;
    window.performance = context.performance;

    vm.runInNewContext(source, context, { filename: 'voice_identity.js' });

    return {
        elements,
        fetchCalls,
        mediaStreams,
        mediaRequestConstraints,
        workletModules,
        workletNodes,
        workletCreations,
        audioMessages,
        gainNodes,
        audioContextOptions,
        localStorage: window.localStorage,
        getAudioContext() {
            return audioContext;
        },
        get mediaRequests() {
            return mediaRequests;
        },
        get intervalCount() {
            return intervals.size;
        },
        get timeoutCount() {
            return timeouts.size;
        },
        get nowMs() {
            return nowMs;
        },
        get statusRequestCount() {
            return statusRequestCount;
        },
        pendingTimeoutIds() {
            return Array.from(timeouts.keys());
        },
        fireHistoricalTimeout(id) {
            const timer = timeoutHistory.get(id);
            if (!timer) throw new Error(`unknown timeout: ${id}`);
            timer.callback();
        },
        setCanonicalEnrollment({ id = enrollmentId, profileId = enrollmentProfileId, segmentIndex = nextSegmentIndex } = {}) {
            enrollmentId = id;
            enrollmentProfileId = profileId;
            nextSegmentIndex = segmentIndex;
            enrollmentExpiresAtMs = id ? nowMs + initialRemainingSeconds * 1000 : null;
        },
        setCanonicalStatusOverride(override) {
            canonicalStatusOverride = override;
        },
        setStartGate(gate) {
            currentStartGate = gate;
        },
        setDocumentVisibility(visibilityState) {
            document.visibilityState = visibilityState;
        },
        advanceTime(milliseconds) {
            advanceVirtualTime(milliseconds);
        },
        async initialize() {
            await documentListeners.get('DOMContentLoaded')();
        },
        startInitialization() {
            return documentListeners.get('DOMContentLoaded')();
        },
        emit(id, type = 'click') {
            return elements.get(id).emit(type);
        },
        dispatch(type, event = {}) {
            return window.dispatchEvent({ type, ...event });
        },
        dispatchDocument(type, event = {}) {
            return document.dispatchEvent({ type, ...event });
        },
        beforeClose() {
            return window.nekoBeforeWindowClose();
        },
    };
}

async function flush(turns = 8) {
    for (let index = 0; index < turns; index += 1) {
        await new Promise(resolve => setImmediate(resolve));
    }
}

test('mutation controls stay disabled until CSRF and canonical status resolve', async () => {
    const statusGate = deferred();
    const harness = createHarness({ statusGate });

    const initializing = harness.startInitialization();
    await flush(2);

    assert.equal(harness.elements.get('voice-identity-start').disabled, true);
    statusGate.resolve(jsonResponse({
        requested_enabled: false,
        effective_enabled: false,
        effective_reason: 'no_profile',
        has_profile: false,
        enrollment: null,
        runtime_mode: 'enforce',
    }));
    await initializing;

    assert.equal(harness.elements.get('voice-identity-start').disabled, false);
});

test('active enrollment shows deterministic reference prompts and a dedicated verification prompt', async () => {
    const prompts = [];
    for (let segment = 1; segment <= 4; segment += 1) {
        const harness = createHarness({ initialEnrollment: true, initialNextSegmentIndex: segment });
        await harness.initialize();
        assert.equal(harness.elements.get('voice-identity-reading-prompt').hidden, false);
        const prompt = harness.elements.get('voice-identity-reading-text').textContent;
        if (segment < 4) assert.match(prompt, /^Prompt \d+$/);
        else assert.equal(prompt, 'Read the five-second verification prompt.');
        assert.equal(
            harness.elements.get('voice-identity-verification-help').hidden,
            segment !== 4,
        );
        prompts.push(prompt);
    }
    assert.equal(new Set(prompts).size, 4);
});

test('all four prompts remain visible for a full two seconds before PCM capture starts', async () => {
    const harness = createHarness({ manualPreparation: true });
    await harness.initialize();

    const enrolling = harness.emit('voice-identity-start');
    await flush(12);

    for (let segment = 1; segment <= 4; segment += 1) {
        const preparationStartedAt = harness.nowMs;
        const previousWorklets = harness.workletNodes.length;
        const previousMessages = harness.audioMessages.length;
        const previousUploads = harness.fetchCalls.filter(
            call => call.url === `${API_ROOT}/enrollment/segment`,
        ).length;
        const prompt = harness.elements.get('voice-identity-reading-text').textContent;

        assert.notEqual(prompt, '', `segment ${segment} prompt`);
        assert.equal(
            harness.elements.get('voice-identity-capture-status').classList.contains('preparing'),
            true,
            `segment ${segment} preparing class`,
        );
        assert.equal(
            harness.elements.get('voice-identity-capture-status').classList.contains('saving'),
            false,
            `segment ${segment} is not saving`,
        );
        assert.equal(
            harness.elements.get('voice-identity-capture-label').textContent,
            'Preparing to record...',
            `segment ${segment} preparation label`,
        );

        harness.advanceTime(SEGMENT_PREPARATION_MS - 1);
        await flush(8);
        assert.equal(harness.workletNodes.length, previousWorklets, `segment ${segment} at 1999ms`);
        assert.equal(harness.audioMessages.length, previousMessages, `segment ${segment} has no PCM at 1999ms`);
        assert.equal(
            harness.fetchCalls.filter(call => call.url === `${API_ROOT}/enrollment/segment`).length,
            previousUploads,
            `segment ${segment} has no PUT at 1999ms`,
        );

        harness.advanceTime(1);
        await flush(16);
        assert.equal(harness.workletNodes.length, previousWorklets + 1, `segment ${segment} at 2000ms`);
        assert.deepEqual(harness.workletCreations.at(-1), {
            atMs: preparationStartedAt + SEGMENT_PREPARATION_MS,
            prompt,
            captureLabel: 'Recording...',
        });
        assert.equal(
            harness.fetchCalls.filter(call => call.url === `${API_ROOT}/enrollment/segment`).length,
            previousUploads + 1,
            `segment ${segment} uploads only after capture`,
        );
    }

    await enrolling;
    const uploads = harness.fetchCalls.filter(call => call.url === `${API_ROOT}/enrollment/segment`);
    assert.deepEqual(
        uploads.map(call => call.options.body.byteLength),
        [REFERENCE_TARGET_SAMPLES * 2, REFERENCE_TARGET_SAMPLES * 2,
            REFERENCE_TARGET_SAMPLES * 2, VERIFICATION_TARGET_SAMPLES * 2],
    );
    assert.equal(harness.mediaRequests, 1);
    assert.equal(harness.workletNodes.every(node => node.port.closed && node.disconnected), true);
    assert.equal(harness.timeoutCount, 0);
    assert.equal(harness.intervalCount, 0);
});

test('cancelling the first preparation leaves no late PCM, PUT, or timer', async () => {
    const harness = createHarness({ manualPreparation: true });
    await harness.initialize();

    const enrolling = harness.emit('voice-identity-start');
    await flush(12);
    harness.advanceTime(PREPARATION_TICK_MS);
    await flush(4);
    await harness.emit('voice-identity-cancel');
    await enrolling;

    harness.advanceTime(SEGMENT_PREPARATION_MS * 2);
    await flush(8);
    assert.equal(harness.workletNodes.length, 0);
    assert.equal(harness.audioMessages.length, 0);
    assert.equal(
        harness.fetchCalls.some(call => call.url === `${API_ROOT}/enrollment/segment`),
        false,
    );
    assert.equal(harness.timeoutCount, 0);
    assert.equal(harness.intervalCount, 0);
});

test('cancelling a later preparation cannot start the next segment', async () => {
    const harness = createHarness({ manualPreparation: true });
    await harness.initialize();

    const enrolling = harness.emit('voice-identity-start');
    await flush(12);
    harness.advanceTime(SEGMENT_PREPARATION_MS);
    await flush(16);
    assert.equal(harness.workletNodes.length, 1);
    assert.equal(
        harness.fetchCalls.filter(call => call.url === `${API_ROOT}/enrollment/segment`).length,
        1,
    );

    await harness.emit('voice-identity-cancel');
    await enrolling;
    harness.advanceTime(SEGMENT_PREPARATION_MS * 2);
    await flush(8);
    assert.equal(harness.workletNodes.length, 1);
    assert.equal(
        harness.fetchCalls.filter(call => call.url === `${API_ROOT}/enrollment/segment`).length,
        1,
    );
    assert.equal(harness.timeoutCount, 0);
    assert.equal(harness.intervalCount, 0);
});

for (const [eventName, stop] of [
    ['pagehide', async harness => { harness.dispatch('pagehide'); await flush(12); }],
    ['window close', async harness => { await harness.beforeClose(); await flush(12); }],
]) {
    test(`${eventName} during preparation prevents late capture and uses keepalive cancellation`, async () => {
        const harness = createHarness({ manualPreparation: true });
        await harness.initialize();

        const enrolling = harness.emit('voice-identity-start');
        await flush(12);
        await stop(harness);
        await enrolling;
        harness.advanceTime(SEGMENT_PREPARATION_MS * 2);
        await flush(8);

        assert.equal(harness.workletNodes.length, 0);
        assert.equal(harness.audioMessages.length, 0);
        assert.equal(
            harness.fetchCalls.some(call => call.url === `${API_ROOT}/enrollment/segment`),
            false,
        );
        assert.equal(
            harness.fetchCalls.some(call => (
                call.url === `${API_ROOT}/enrollment/cancel` && call.options.keepalive === true
            )),
            true,
        );
        assert.equal(harness.timeoutCount, 0);
        assert.equal(harness.intervalCount, 0);
    });
}

test('TTL expiry during preparation stops before AudioWorklet and PCM creation', async () => {
    const harness = createHarness({ manualPreparation: true, initialRemainingSeconds: 1 });
    await harness.initialize();

    const enrolling = harness.emit('voice-identity-start');
    await flush(12);
    harness.advanceTime(PREPARATION_TICK_MS);
    await flush(20);
    await enrolling;
    harness.advanceTime(SEGMENT_PREPARATION_MS * 2);
    await flush(8);

    assert.equal(harness.workletNodes.length, 0);
    assert.equal(harness.audioMessages.length, 0);
    assert.equal(
        harness.fetchCalls.some(call => call.url === `${API_ROOT}/enrollment/segment`),
        false,
    );
    assert.equal(harness.mediaStreams[0].track.stopped, true);
    assert.equal(harness.timeoutCount, 0);
    assert.equal(harness.intervalCount, 0);
});

for (const [identityName, canonical] of [
    ['enrollment id', { id: 'enrollment-2' }],
    ['profile id', { profileId: 'profile-2' }],
    ['segment index', { segmentIndex: 2 }],
    ['retired enrollment', { id: null }],
]) {
    test(`canonical ${identityName} drift makes the active preparation stale`, async () => {
        const harness = createHarness({ manualPreparation: true });
        await harness.initialize();

        const enrolling = harness.emit('voice-identity-start');
        await flush(12);
        harness.setCanonicalEnrollment(canonical);
        await harness.dispatch('pageshow', { persisted: true });
        await flush(16);
        await enrolling;
        harness.advanceTime(SEGMENT_PREPARATION_MS * 2);
        await flush(8);

        assert.equal(harness.workletNodes.length, 0);
        assert.equal(harness.audioMessages.length, 0);
        assert.equal(
            harness.fetchCalls.some(call => call.url === `${API_ROOT}/enrollment/segment`),
            false,
        );
        assert.equal(harness.timeoutCount, 0);
    });
}

test('a cancelled preparation timer cannot clear or start its successor operation', async () => {
    const harness = createHarness({ manualPreparation: true });
    await harness.initialize();

    const firstEnrollment = harness.emit('voice-identity-start');
    await flush(12);
    const staleTimerId = harness.pendingTimeoutIds()[0];
    assert.ok(staleTimerId);
    await harness.emit('voice-identity-cancel');
    await firstEnrollment;

    const secondEnrollment = harness.emit('voice-identity-start');
    await flush(12);
    assert.equal(harness.workletNodes.length, 0);
    assert.equal(harness.timeoutCount, 1);
    harness.fireHistoricalTimeout(staleTimerId);
    await flush(8);
    assert.equal(harness.workletNodes.length, 0);
    assert.equal(harness.timeoutCount, 1);

    harness.advanceTime(SEGMENT_PREPARATION_MS - 1);
    await flush(8);
    assert.equal(harness.workletNodes.length, 0);
    harness.advanceTime(1);
    await flush(16);
    assert.equal(harness.workletNodes.length, 1);

    await harness.emit('voice-identity-cancel');
    await secondEnrollment;
    assert.equal(harness.timeoutCount, 0);
    assert.equal(harness.intervalCount, 0);
});

test('one click PUTs three reference captures plus one exact five-second verification at 48 kHz', async () => {
    const harness = createHarness();
    await harness.initialize();

    await harness.emit('voice-identity-start');

    const paths = harness.fetchCalls.map(call => call.url);
    assert.deepEqual(paths, [
        '/api/config/page_config',
        `${API_ROOT}/status`,
        `${API_ROOT}/enrollment/start`,
        `${API_ROOT}/enrollment/segment`,
        `${API_ROOT}/enrollment/segment`,
        `${API_ROOT}/enrollment/segment`,
        `${API_ROOT}/enrollment/segment`,
        `${API_ROOT}/status`,
    ]);
    const uploads = harness.fetchCalls.filter(call => call.url === `${API_ROOT}/enrollment/segment`);
    assert.equal(uploads.length, 4);
    for (const [offset, upload] of uploads.entries()) {
        assert.equal(upload.options.method, 'PUT');
        const targetSamples = offset === 3 ? VERIFICATION_TARGET_SAMPLES : REFERENCE_TARGET_SAMPLES;
        assert.equal(upload.options.body.byteLength, targetSamples * 2);
        assert.equal(upload.options.headers.get('content-type'), PCM_CONTENT_TYPE);
        assert.equal(upload.options.headers.get('x-voice-audio-contract'), AUDIO_CONTRACT_ID);
        assert.equal(upload.options.headers.get('x-voice-identity-enrollment'), 'enrollment-1');
        assert.equal(upload.options.headers.get('x-voice-identity-profile'), 'profile-1');
        assert.equal(upload.options.headers.get('x-voice-identity-segment'), String(offset + 1));
    }
    assert.equal(harness.mediaRequests, 1);
    assert.deepEqual(JSON.parse(JSON.stringify(harness.mediaRequestConstraints)), [{
        audio: {
            noiseSuppression: false,
            echoCancellation: true,
            autoGainControl: true,
            channelCount: 1,
            deviceId: { exact: 'selected-microphone' },
        },
        video: false,
    }]);
    assert.deepEqual(
        JSON.parse(JSON.stringify(harness.audioContextOptions)),
        [{ sampleRate: TARGET_SAMPLE_RATE }],
    );
    assert.deepEqual(harness.workletModules, ['/static/audio-processor.js']);
    assert.equal(harness.workletNodes.length, 4);
    assert.equal(harness.workletNodes.every(node => node.port.closed && node.disconnected), true);
    assert.equal(harness.gainNodes.length, 8);
    for (let index = 0; index < harness.gainNodes.length; index += 2) {
        assert.ok(Math.abs(harness.gainNodes[index].gain.value - Math.pow(10, 6 / 20)) < 1e-12);
        assert.equal(harness.gainNodes[index + 1].gain.value, 0);
        assert.equal(harness.gainNodes[index].disconnected, true);
        assert.equal(harness.gainNodes[index + 1].disconnected, true);
    }
    assert.equal(harness.mediaStreams[0].track.stopped, true);
    assert.equal(
        harness.elements.get('voice-identity-message').textContent,
        'Lowest voice similarity: 72%. Verification passed. Enrollment complete.',
    );
    assert.equal(harness.elements.get('voice-identity-profile-controls').hidden, false);
});

test('successful final keeps its verification score visible once the profile is canonical', async () => {
    const harness = createHarness({ verificationMatchPercent: 83 });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    const message = harness.elements.get('voice-identity-message').textContent;
    assert.match(message, /83%/);
    assert.equal(harness.elements.get('voice-identity-profile-controls').hidden, false);
    assert.equal(harness.elements.get('voice-identity-message').classList.contains('error'), false);
});

test('completion refresh preserves the score while adopting a recovered status suffix', async () => {
    const refreshGate = deferred();
    const refreshStarted = deferred();
    let canonicalAfterCompletion = null;
    const harness = createHarness({
        verificationMatchPercent: 79,
        finalResponseTransform(payload) {
            return {
                ...payload,
                effective_enabled: false,
                effective_reason: 'model_unavailable',
            };
        },
        statusHandler({ requestNumber, payload }) {
            if (requestNumber === 1) return jsonResponse(payload);
            if (requestNumber === 2) {
                canonicalAfterCompletion = payload;
                refreshStarted.resolve();
                return refreshGate.promise;
            }
            return jsonResponse(payload);
        },
    });
    await harness.initialize();

    const enrolling = harness.emit('voice-identity-start');
    await refreshStarted.promise;
    await flush(2);
    const unavailableMessage = harness.elements.get('voice-identity-message').textContent;
    assert.match(unavailableMessage, /79%/);

    refreshGate.resolve(jsonResponse(canonicalAfterCompletion));
    await enrolling;
    await flush(2);

    const recoveredMessage = harness.elements.get('voice-identity-message').textContent;
    assert.match(recoveredMessage, /79%/);
    assert.notEqual(recoveredMessage, unavailableMessage);
    assert.equal(harness.elements.get('voice-identity-profile-controls').hidden, false);
});

test('focus and visible visibilitychange silently refresh canonical profile state', async () => {
    const harness = createHarness({ initialProfile: true, initialRequested: false });
    await harness.initialize();

    harness.setCanonicalStatusOverride({
        requested_enabled: true,
        effective_enabled: true,
        effective_reason: 'ready',
    });
    await harness.dispatch('focus');
    await flush(2);
    assert.equal(harness.statusRequestCount, 2);
    assert.equal(harness.elements.get('voice-identity-filter').checked, true);

    harness.setDocumentVisibility('hidden');
    await harness.dispatchDocument('visibilitychange');
    await flush(2);
    assert.equal(harness.statusRequestCount, 2);

    harness.setCanonicalStatusOverride({
        requested_enabled: false,
        effective_enabled: false,
        effective_reason: 'disabled',
    });
    harness.setDocumentVisibility('visible');
    await harness.dispatchDocument('visibilitychange');
    await flush(2);
    assert.equal(harness.statusRequestCount, 3);
    assert.equal(harness.elements.get('voice-identity-filter').checked, false);
});

test('simultaneous focus and visibility signals share one canonical refresh', async () => {
    const refreshGate = deferred();
    const harness = createHarness({
        initialProfile: true,
        statusHandler({ requestNumber, payload }) {
            return requestNumber === 2 ? refreshGate.promise : jsonResponse(payload);
        },
    });
    await harness.initialize();

    const focusRefresh = harness.dispatch('focus');
    const visibleRefresh = harness.dispatchDocument('visibilitychange');
    await flush(2);
    assert.equal(harness.statusRequestCount, 2);

    refreshGate.resolve(jsonResponse({
        requested_enabled: true,
        effective_enabled: true,
        effective_reason: 'ready',
        has_profile: true,
        enrollment: null,
        profile_generation: 'profile-0',
        last_completed_enrollment_id: null,
        runtime_mode: 'enforce',
    }));
    await Promise.all([focusRefresh, visibleRefresh]);
    assert.equal(harness.statusRequestCount, 2);
});

test('a refresh response captured before re-enrollment cannot overwrite its identity', async () => {
    const refreshGate = deferred();
    const startGate = deferred();
    const harness = createHarness({
        initialProfile: true,
        initialRequested: true,
        startGate,
        statusHandler({ requestNumber, payload }) {
            return requestNumber === 2 ? refreshGate.promise : jsonResponse(payload);
        },
    });
    await harness.initialize();

    const staleRefresh = harness.dispatch('focus');
    await flush(2);
    const reenrolling = harness.emit('voice-identity-reenroll');
    await flush(2);
    refreshGate.resolve(jsonResponse({
        requested_enabled: false,
        effective_enabled: false,
        effective_reason: 'disabled',
        has_profile: true,
        enrollment: null,
        profile_generation: 'profile-0',
        last_completed_enrollment_id: null,
        runtime_mode: 'enforce',
    }));
    await staleRefresh;
    await flush(2);

    assert.equal(harness.elements.get('voice-identity-filter').checked, true);
    assert.equal(harness.elements.get('voice-identity-enrollment').hidden, false);
    startGate.resolve();
    await reenrolling;
});

test('passive refresh failure is silent and preserves a completed verification result', async () => {
    const harness = createHarness({
        verificationMatchPercent: 77,
        statusHandler({ requestNumber, payload }) {
            if (requestNumber === 3) throw new Error('offline');
            return jsonResponse(payload);
        },
    });
    await harness.initialize();
    await harness.emit('voice-identity-start');
    const completedMessage = harness.elements.get('voice-identity-message').textContent;
    assert.match(completedMessage, /77%/);

    await harness.dispatch('focus');
    await flush(2);

    assert.equal(harness.elements.get('voice-identity-message').textContent, completedMessage);
    assert.equal(harness.elements.get('voice-identity-message').classList.contains('error'), false);
});

test('lost final response never invents a verification score', async () => {
    const harness = createHarness({
        profileTransportErrorAfterCommit: true,
        verificationMatchPercent: 91,
    });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    assert.doesNotMatch(harness.elements.get('voice-identity-message').textContent, /91%/);
    assert.equal(harness.elements.get('voice-identity-profile-controls').hidden, false);
});

test('starting a new recording clears the previous verification result', async () => {
    const harness = createHarness({ verificationMatchPercent: 88 });
    await harness.initialize();
    await harness.emit('voice-identity-start');
    assert.match(harness.elements.get('voice-identity-message').textContent, /88%/);

    const startGate = deferred();
    harness.setStartGate(startGate);
    const reenrolling = harness.emit('voice-identity-reenroll');
    await flush(2);

    assert.equal(harness.elements.get('voice-identity-message').textContent, '');
    startGate.resolve();
    await reenrolling;
});

test('a non-48k AudioContext is rejected before creating an enrollment', async () => {
    const harness = createHarness({ actualContextSampleRate: 44100 });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    assert.equal(
        harness.fetchCalls.some(call => call.url === `${API_ROOT}/enrollment/start`),
        false,
    );
    assert.equal(harness.getAudioContext().state, 'closed');
    assert.equal(harness.mediaStreams[0].track.stopped, true);
    assert.equal(harness.elements.get('voice-identity-message').textContent, 'Microphone unavailable.');
});

test('an unavailable saved device falls back to runtime default constraints', async () => {
    const unavailable = new Error('missing');
    unavailable.name = 'NotFoundError';
    const harness = createHarness({
        mediaError: requestNumber => requestNumber === 1 ? unavailable : null,
    });
    await harness.initialize();
    await harness.emit('voice-identity-start');

    assert.deepEqual(
        JSON.parse(JSON.stringify(harness.mediaRequestConstraints[0].audio.deviceId)),
        { exact: 'selected-microphone' },
    );
    assert.deepEqual(JSON.parse(JSON.stringify(harness.mediaRequestConstraints[1])), {
        audio: {
            noiseSuppression: false,
            echoCancellation: true,
            autoGainControl: true,
            channelCount: 1,
        },
        video: false,
    });
    assert.equal(harness.localStorage.getItem('neko_selected_microphone'), null);

    const defaults = createHarness({ selectedMicrophoneId: null, microphoneGainDb: 'invalid' });
    await defaults.initialize();
    await defaults.emit('voice-identity-start');
    assert.equal(defaults.gainNodes[0].gain.value, 1);
});

test('lost start response adopts the active server session and keeps one microphone lease', async () => {
    const harness = createHarness({ startTransportErrorAfterCreate: true });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    assert.equal(
        harness.fetchCalls.filter(call => call.url === `${API_ROOT}/enrollment/start`).length,
        1,
    );
    assert.equal(
        harness.fetchCalls.filter(call => call.url === `${API_ROOT}/enrollment/segment`).length,
        4,
    );
    assert.equal(harness.mediaRequests, 1);
    assert.equal(
        harness.elements.get('voice-identity-message').textContent,
        'Lowest voice similarity: 72%. Verification passed. Enrollment complete.',
    );
});

test('failed verification shows its transient percentage and follows the server retry step', async () => {
    const harness = createHarness({
        verificationPassed: false,
        verificationMatchPercent: 31,
        verificationNextSegmentIndex: 4,
    });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    assert.equal(
        harness.elements.get('voice-identity-message').textContent,
        'Lowest voice similarity: 31%. Please keep your natural tone and verify again.',
    );
    assert.equal(harness.elements.get('voice-identity-progress-label').textContent, '第 4/4 段');
    assert.equal(harness.elements.get('voice-identity-verification-help').hidden, false);
    assert.equal(harness.mediaStreams[0].track.stopped, false);
});

test('failed verification follows a server reset to segment one', async () => {
    const harness = createHarness({
        verificationPassed: false,
        verificationMatchPercent: 28,
        verificationNextSegmentIndex: 1,
    });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    assert.equal(harness.elements.get('voice-identity-progress-label').textContent, '第 1/4 段');
    assert.equal(harness.elements.get('voice-identity-verification-help').hidden, true);
    assert.match(harness.elements.get('voice-identity-message').textContent, /28%/);
});

test('lost failed-verification response recovers status without inventing a percentage', async () => {
    const harness = createHarness({
        verificationPassed: false,
        verificationMatchPercent: 31,
        verificationNextSegmentIndex: 4,
        verificationTransportErrorAfterResult: true,
    });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    assert.equal(harness.elements.get('voice-identity-progress-label').textContent, '第 4/4 段');
    assert.equal(harness.elements.get('voice-identity-message').textContent, 'Request failed.');
    assert.doesNotMatch(harness.elements.get('voice-identity-message').textContent, /\d+%/);
});

test('underfilled capture cancels the lease and never uploads partial PCM', async () => {
    const harness = createHarness({ audioChunks: 50 });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    assert.equal(
        harness.fetchCalls.some(call => call.url === `${API_ROOT}/enrollment/segment`),
        false,
    );
    assert.equal(
        harness.fetchCalls.some(call => call.url === `${API_ROOT}/enrollment/cancel`),
        true,
    );
    assert.equal(harness.elements.get('voice-identity-message').textContent, 'Request failed.');
});

test('recoverable speech rejection keeps the session and microphone for current-segment retry', async () => {
    const harness = createHarness({ profileError: 'speech_too_short' });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    assert.equal(
        harness.fetchCalls.some(call => call.url === `${API_ROOT}/enrollment/segment`),
        true,
    );
    assert.equal(harness.elements.get('voice-identity-profile-controls').hidden, true);
    assert.equal(harness.elements.get('voice-identity-message').textContent, 'Not enough speech.');
    assert.equal(harness.mediaStreams[0].track.stopped, false);
    assert.equal(harness.elements.get('voice-identity-start').hidden, false);
});

test('server consistency reset returns the client to segment one without reopening the microphone', async () => {
    const harness = createHarness({
        profileError: 'voice_samples_inconsistent',
        profileErrorSegment: 3,
    });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    const uploads = harness.fetchCalls.filter(call => call.url === `${API_ROOT}/enrollment/segment`);
    assert.deepEqual(
        uploads.map(call => call.options.headers.get('x-voice-identity-segment')),
        ['1', '2', '3'],
    );
    assert.equal(harness.elements.get('voice-identity-progress-label').textContent, '第 1/4 段');
    assert.equal(harness.mediaRequests, 1);
    assert.equal(harness.mediaStreams[0].track.stopped, false);
});

test('lost non-final response adopts server progress without resubmitting accepted audio', async () => {
    const harness = createHarness({ segmentTransportErrorAfterAccept: 2 });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    const uploads = harness.fetchCalls.filter(call => call.url === `${API_ROOT}/enrollment/segment`);
    assert.deepEqual(
        uploads.map(call => call.options.headers.get('x-voice-identity-segment')),
        ['1', '2'],
    );
    assert.equal(harness.elements.get('voice-identity-progress-label').textContent, '第 3/4 段');
    assert.equal(harness.mediaStreams[0].track.stopped, false);
});

test('active status reuses the server-bound opaque profile id after reload', async () => {
    const harness = createHarness({
        initialEnrollment: true,
        initialEnrollmentProfileId: 'bound-profile',
    });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    const uploads = harness.fetchCalls.filter(call => call.url === `${API_ROOT}/enrollment/segment`);
    assert.equal(
        harness.fetchCalls.some(call => call.url === `${API_ROOT}/enrollment/start`),
        false,
    );
    assert.equal(uploads.length, 4);
    assert.equal(
        uploads.every(call => call.options.headers.get('x-voice-identity-profile') === 'bound-profile'),
        true,
    );
});

test('active unbound status generates one opaque profile id before the first segment', async () => {
    const harness = createHarness({ initialEnrollment: true });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    const uploads = harness.fetchCalls.filter(call => call.url === `${API_ROOT}/enrollment/segment`);
    assert.equal(uploads.length, 4);
    assert.equal(
        uploads.every(call => call.options.headers.get('x-voice-identity-profile') === 'profile-1'),
        true,
    );
});

test('TTL expiry while awaiting a retry stops media resources and clears its interval', async () => {
    const harness = createHarness({
        profileError: 'speech_too_short',
        initialRemainingSeconds: 10,
    });
    await harness.initialize();
    await harness.emit('voice-identity-start');

    assert.equal(harness.mediaStreams[0].track.stopped, false);
    assert.equal(harness.intervalCount, 1);
    harness.advanceTime(8500);
    await flush();

    assert.equal(harness.mediaStreams[0].track.stopped, true);
    assert.equal(harness.getAudioContext().state, 'closed');
    assert.equal(harness.intervalCount, 0);
    assert.equal(harness.elements.get('voice-identity-message').textContent, 'Enrollment expired.');
});

test('canonical enrollment audio errors show localized messages', async () => {
    const invalid = createHarness({ profileError: 'invalid_pcm' });
    await invalid.initialize();
    await invalid.emit('voice-identity-start');
    assert.equal(
        invalid.elements.get('voice-identity-message').textContent,
        'Invalid recording format.',
    );

    const tooLong = createHarness({ profileError: 'audio_too_long' });
    await tooLong.initialize();
    await tooLong.emit('voice-identity-start');
    assert.equal(
        tooLong.elements.get('voice-identity-message').textContent,
        'Recording is too long.',
    );

    const unavailable = createHarness({ profileError: 'audio_processing_unavailable' });
    await unavailable.initialize();
    await unavailable.emit('voice-identity-start');
    assert.equal(
        unavailable.elements.get('voice-identity-message').textContent,
        'Microphone audio processing is temporarily unavailable. Restart the microphone and try again.',
    );
});

test('verification capture remains an exact five-second 480000-byte contract', () => {
    assert.match(source, /const VERIFICATION_RECORDING_MS = 5000;/);
    assert.equal(VERIFICATION_TARGET_SAMPLES, 240000);
    assert.equal(VERIFICATION_TARGET_SAMPLES * 2, 480000);
    assert.match(
        source,
        /recordingDurationMs === VERIFICATION_RECORDING_MS\s*\? recordingDurationMs\s*: recordingDurationMs \+ STREAMING_RESAMPLE_MARGIN_MS/,
    );
});

test('missing Web Crypto fails before creating a server enrollment or uploading', async () => {
    const harness = createHarness({ webCryptoAvailable: false });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    assert.equal(
        harness.fetchCalls.some(call => call.url === `${API_ROOT}/enrollment/segment`),
        false,
    );
    assert.equal(
        harness.fetchCalls.some(call => call.url === `${API_ROOT}/enrollment/start`),
        false,
    );
    assert.equal(harness.elements.get('voice-identity-message').textContent, 'Request failed.');
});

test('microphone denial prevents enrollment start and reports a useful error', async () => {
    const denied = new Error('denied');
    denied.name = 'NotAllowedError';
    const harness = createHarness({ mediaError: denied });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    assert.equal(
        harness.fetchCalls.some(call => call.url === `${API_ROOT}/enrollment/start`),
        false,
    );
    assert.equal(harness.elements.get('voice-identity-message').textContent, 'Microphone unavailable.');
});

test('canonical has_profile reveals only switch, re-enroll, and delete controls', async () => {
    const harness = createHarness({ initialProfile: true, initialRequested: true });
    await harness.initialize();

    assert.equal(harness.elements.get('voice-identity-enrollment').hidden, true);
    assert.equal(harness.elements.get('voice-identity-profile-controls').hidden, false);
    assert.equal(harness.elements.get('voice-identity-filter').checked, true);
    assert.equal(harness.elements.get('voice-identity-profile-status').textContent,
        'Owner voice profile is saved and enabled');
    assert.equal(template.includes('voice-identity-record'), false);
    assert.equal(template.includes('step-progress'), true);
});

test('backend degradation reason is preserved when no profile exists', async () => {
    const harness = createHarness({
        initialEffectiveReason: 'secure_storage_unavailable',
    });
    await harness.initialize();

    assert.equal(
        harness.elements.get('voice-identity-profile-status').textContent,
        'Secure storage is unavailable',
    );
    assert.equal(harness.elements.get('voice-identity-start').disabled, true);
});

test('pending activation preserves the saved request without presenting ready', async () => {
    const statusGate = deferred();
    const harness = createHarness({ initialProfile: true, initialRequested: true, statusGate });
    const initializing = harness.startInitialization();
    statusGate.resolve(jsonResponse({
        requested_enabled: true,
        effective_enabled: false,
        effective_reason: 'activation_pending',
        has_profile: true,
        enrollment: null,
        profile_generation: 'profile-0',
        runtime_mode: 'enforce',
    }));
    await initializing;

    // The fallback remains actionable even before locale loading settles.
    assert.equal(harness.elements.get('voice-identity-profile-status').textContent,
        '设置已保存，等待语音链路就绪');
    assert.equal(harness.elements.get('voice-identity-filter').checked, true);
    assert.equal(harness.elements.get('voice-identity-delete').disabled, false);
});

test('model unavailability disables enrollment and shows actionable status', async () => {
    const harness = createHarness({
        initialEffectiveReason: 'model_unavailable',
    });
    await harness.initialize();

    assert.equal(
        harness.elements.get('voice-identity-profile-status').textContent,
        'Prepare the voice model assets or repair the installation',
    );
    assert.equal(harness.elements.get('voice-identity-start').disabled, true);
});

test('model unavailability disables re-enrollment for an existing profile', async () => {
    const statusGate = deferred();
    const harness = createHarness({
        initialProfile: true,
        initialRequested: true,
        statusGate,
    });
    const initializing = harness.startInitialization();
    statusGate.resolve(jsonResponse({
        requested_enabled: true,
        effective_enabled: false,
        effective_reason: 'model_unavailable',
        has_profile: true,
        enrollment: null,
        profile_generation: 'profile-0',
        runtime_mode: 'enforce',
    }));
    await initializing;

    assert.equal(harness.elements.get('voice-identity-reenroll').disabled, true);
    assert.equal(harness.elements.get('voice-identity-delete').disabled, false);
});

test('audio contract mismatch explains how to restore filtering without producing LOW', async () => {
    const statusGate = deferred();
    const harness = createHarness({ initialProfile: true, initialRequested: true, statusGate });
    const initializing = harness.startInitialization();
    statusGate.resolve(jsonResponse({
        requested_enabled: true,
        effective_enabled: false,
        effective_reason: 'audio_contract_mismatch',
        has_profile: true,
        enrollment: null,
        profile_generation: 'profile-0',
        runtime_mode: 'enforce',
    }));
    await initializing;

    assert.equal(
        harness.elements.get('voice-identity-profile-status').textContent,
        'Restore the enrolled noise-reduction setting or re-enroll',
    );
    assert.equal(harness.elements.get('voice-identity-filter').checked, true);
});

test('late model rejection shows its dedicated enrollment error', async () => {
    const harness = createHarness({ startError: 'model_unavailable' });
    await harness.initialize();

    await harness.emit('voice-identity-start');

    assert.equal(
        harness.elements.get('voice-identity-message').textContent,
        'Voice model unavailable; prepare assets or repair the installation.',
    );
    assert.equal(
        harness.fetchCalls.some(call => call.url === `${API_ROOT}/enrollment/segment`),
        false,
    );
});

test('filter toggle sends the requested boolean and adopts canonical state', async () => {
    const harness = createHarness({ initialProfile: true });
    await harness.initialize();
    const filter = harness.elements.get('voice-identity-filter');
    filter.checked = true;

    await harness.emit('voice-identity-filter', 'change');

    const request = harness.fetchCalls.at(-1);
    assert.equal(request.url, `${API_ROOT}/filter`);
    assert.deepEqual(JSON.parse(request.options.body), { enabled: true });
    assert.equal(filter.checked, true);
});

test('re-enrollment hides profile mutations while the new session starts', async () => {
    const startGate = deferred();
    const harness = createHarness({
        initialProfile: true,
        initialRequested: false,
        startGate,
    });
    await harness.initialize();

    const reenrolling = harness.emit('voice-identity-reenroll');
    await flush(2);
    assert.equal(harness.elements.get('voice-identity-profile-controls').hidden, true);
    assert.equal(harness.elements.get('voice-identity-enrollment').hidden, false);
    assert.equal(harness.elements.get('voice-identity-cancel').hidden, false);
    startGate.resolve();
    await reenrolling;

    assert.equal(harness.elements.get('voice-identity-profile-controls').hidden, false);
    assert.equal(harness.elements.get('voice-identity-filter').checked, false);
});

test('re-enrollment recovers a lost response and preserves disabled preference', async () => {
    const harness = createHarness({
        initialProfile: true,
        initialRequested: false,
        profileTransportErrorAfterCommit: true,
    });
    await harness.initialize();

    await harness.emit('voice-identity-reenroll');

    assert.equal(harness.elements.get('voice-identity-profile-controls').hidden, false);
    assert.equal(harness.elements.get('voice-identity-filter').checked, false);
    assert.equal(
        harness.elements.get('voice-identity-message').textContent,
        'Owner voice profile is saved; filtering is off',
    );
});

test('delete confirms, removes the profile, and returns to one-click enrollment', async () => {
    const confirmations = [];
    const harness = createHarness({
        initialProfile: true,
        initialRequested: true,
        showConfirm: async (...args) => {
            confirmations.push(args);
            return true;
        },
    });
    await harness.initialize();

    await harness.emit('voice-identity-delete');

    assert.equal(confirmations.length, 1);
    assert.equal(harness.fetchCalls.at(-1).url, `${API_ROOT}/profile`);
    assert.equal(harness.fetchCalls.at(-1).options.method, 'DELETE');
    assert.equal(harness.elements.get('voice-identity-profile-controls').hidden, true);
    assert.equal(harness.elements.get('voice-identity-start').hidden, false);
});

test('explicit cancel aborts an active capture and releases the server session', async () => {
    const harness = createHarness({ manualAudio: true });
    await harness.initialize();

    const enrolling = harness.emit('voice-identity-start');
    await flush();
    assert.equal(harness.elements.get('voice-identity-cancel').hidden, false);
    await harness.emit('voice-identity-cancel');
    await enrolling;

    const cancel = harness.fetchCalls.find(call => (
        call.url === `${API_ROOT}/enrollment/cancel`
    ));
    assert.ok(cancel);
    assert.equal(cancel.options.headers.get('x-voice-identity-enrollment'), 'enrollment-1');
    assert.equal(harness.mediaStreams[0].track.stopped, true);
    assert.equal(harness.getAudioContext().state, 'closed');
    assert.equal(harness.workletNodes.length, 1);
    assert.equal(harness.workletNodes[0].port.closed, true);
    assert.equal(harness.workletNodes[0].disconnected, true);
    assert.equal(harness.elements.get('voice-identity-start').hidden, false);
});

test('pagehide sends keepalive cancellation and stops microphone resources', async () => {
    const harness = createHarness({ manualAudio: true });
    await harness.initialize();

    const enrolling = harness.emit('voice-identity-start');
    await flush();
    harness.dispatch('pagehide');
    await flush();
    await enrolling;

    const cancel = harness.fetchCalls.find(call => (
        call.url === `${API_ROOT}/enrollment/cancel`
        && call.options.keepalive === true
    ));
    assert.ok(cancel);
    assert.equal(harness.mediaStreams[0].track.stopped, true);
    assert.equal(harness.getAudioContext().state, 'closed');
});

test('slow enrollment start uses keepalive cancellation after close wait expires', async () => {
    const startGate = deferred();
    const harness = createHarness({ startGate, manualAudio: true });
    await harness.initialize();

    const enrolling = harness.emit('voice-identity-start');
    await flush();
    await harness.beforeClose();
    startGate.resolve();
    await enrolling;

    const cancel = harness.fetchCalls.find(call => (
        call.url === `${API_ROOT}/enrollment/cancel`
        && call.options.keepalive === true
    ));
    assert.ok(cancel);
});

test('the one-click page keeps complete dark-theme overrides', () => {
    for (const token of [
        '--voice-ink: #e8f5fb',
        '--voice-muted: #afc5d1',
        '--voice-blue-dark: #8edcff',
        '--voice-border: rgba(91, 215, 255, 0.28)',
        '--voice-panel: rgba(27, 39, 48, 0.96)',
        '--voice-danger: #ff8d9b',
        '--voice-focus: #8edcff',
    ]) {
        assert.match(stylesheet, new RegExp(token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
    }
    assert.match(stylesheet, /\[data-theme="dark"\] \.secondary-button/);
    assert.match(stylesheet, /\[data-theme="dark"\] \.danger-button/);
    assert.match(darkModeStylesheet, /html\[data-theme="dark"\]/);
    assert.match(template, /static\/css\/dark-mode\.css/);
});

test('retired bypass endpoints and prompt contracts do not return', () => {
    for (const retired of [
        '/enrollment/profile',
        '/enrollment/verify',
        '/enrollment/commit',
        'ready_to_commit',
        'fixedPrompts',
        'voice-identity-record',
    ]) {
        assert.equal(source.includes(retired) || template.includes(retired), false, retired);
    }
});

test('web enrollment and Electron runtime keep one desktop microphone contract', () => {
    for (const constraint of [
        'noiseSuppression: false',
        'echoCancellation: true',
        'autoGainControl: true',
        'channelCount: 1',
    ]) {
        assert.equal(source.includes(constraint), true, constraint);
        assert.equal(runtimeCaptureSource.includes(constraint), true, constraint);
    }
    assert.match(source, /new AudioContextClass\(\{ sampleRate: TARGET_SAMPLE_RATE \}\)/);
    assert.match(runtimeCaptureSource, /new AudioContext\(\{ sampleRate: 48000 \}\)/);
    assert.match(source, /targetSampleRate: TARGET_SAMPLE_RATE/);
    assert.match(runtimeCaptureSource, /const targetSampleRate = isMobile\(\) \? 16000 : 48000/);
    assert.match(source, /neko_selected_microphone/);
    assert.match(runtimeCaptureSource, /neko_selected_microphone/);
    assert.match(source, /neko_mic_gain_db/);
    assert.match(runtimeCaptureSource, /neko_mic_gain_db/);
});

test('all supported locales explain an audio-contract mismatch', () => {
    for (const locale of ['en', 'es', 'ja', 'ko', 'pt', 'ru', 'zh-CN', 'zh-TW']) {
        const messages = JSON.parse(fs.readFileSync(
            path.join(__dirname, `locales/${locale}.json`),
            'utf8',
        ));
        assert.equal(
            typeof messages.voiceIdentity.reasonAudioContractMismatch,
            'string',
            locale,
        );
        assert.notEqual(messages.voiceIdentity.reasonAudioContractMismatch, '', locale);
    }
});
