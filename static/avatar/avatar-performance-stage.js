(function () {
    'use strict';

    if (window.AvatarPerformanceStage && window.AvatarPerformance) {
        return;
    }

    const DEFAULT_FRAME = Object.freeze({
        x: 0,
        y: 0,
        scale: 1,
        rotate: 0,
        opacity: ''
    });

    const CONTRACTS = Object.freeze({
        stageVersion: 'avatar-performance-stage-v1',
        driverVersion: 'avatar-performance-driver-v1',
        coordinatorVersion: 'avatar-performance-coordinator-v1',
        sequenceVersion: 'avatar-performance-sequence-v1',
        driverMethods: Object.freeze([
            'isAvailable',
            'resolveAvatar',
            'capture',
            'restore',
            'commitCurrentFrameAsBaseline',
            'acquireSession',
            'releaseSession',
            'applyFrame',
            'hasMotion',
            'playMotion',
            'stopMotion',
            'captureExpression',
            'applyExpression',
            'applyEmotion',
            'clearExpression',
            'resolveParamId',
            'captureParams',
            'setParam',
            'restoreParams',
            'runPoseTimeline',
            'lookAt',
            'clearLookAt'
        ]),
        coordinatorMethods: Object.freeze([
            'acquire',
            'release',
            'destroy',
            'isCapabilityLocked',
            'getLockedCapabilities',
            'getActiveSession'
        ]),
        sequenceStepTypes: Object.freeze([
            'frame',
            'motion',
            'motionWithFallback',
            'optionalMotion',
            'expression',
            'emotion',
            'param',
            'poseTimeline',
            'lookAt',
            'clearLookAt',
            'clearExpression',
            'clearParams',
            'wait',
            'sequence',
            'speechCue'
        ]),
        capabilities: Object.freeze([
            'frame',
            'motion',
            'expression',
            'params',
            'lookAt'
        ])
    });

    function createNoopDriver() {
        return {
            kind: 'noop',
            isAvailable: function () { return false; },
            resolveAvatar: function () { return null; },
            capture: function () { return {}; },
            restore: function () { return false; },
            commitCurrentFrameAsBaseline: function () { return null; },
            acquireSession: function () {},
            releaseSession: function () {},
            applyFrame: function () {},
            hasMotion: function () { return false; },
            playMotion: function () { return Promise.resolve(false); },
            stopMotion: function () { return false; },
            captureExpression: function () { return {}; },
            applyExpression: function () { return false; },
            applyEmotion: function () { return Promise.resolve(false); },
            clearExpression: function () { return false; },
            resolveParamId: function () { return ''; },
            setParam: function () { return false; },
            captureParams: function () { return {}; },
            restoreParams: function () {},
            runPoseTimeline: function () { return Promise.resolve(false); },
            lookAt: function () { return false; },
            clearLookAt: function () {}
        };
    }

    function createNoopCoordinator() {
        return {
            kind: 'noop-coordinator',
            contractVersion: CONTRACTS.coordinatorVersion,
            acquire: function () { return null; },
            release: function () { return Promise.resolve(false); },
            destroy: function () {},
            isCapabilityLocked: function () { return false; },
            getLockedCapabilities: function () { return []; },
            getActiveSession: function () { return null; }
        };
    }

    function normalizeAvatarId(value) {
        const normalized = String(value || '').trim();
        return normalized || 'default';
    }

    function normalizeCapabilities(value) {
        const source = Array.isArray(value) && value.length > 0
            ? value
            : CONTRACTS.capabilities;
        const seen = {};
        const result = [];
        source.forEach((capability) => {
            const normalized = String(capability || '').trim();
            if (!normalized || seen[normalized]) {
                return;
            }
            seen[normalized] = true;
            result.push(normalized);
        });
        return result.length ? result : CONTRACTS.capabilities.slice();
    }

    function createLockKey(avatarId, capability) {
        return normalizeAvatarId(avatarId) + '\u0000' + String(capability || '').trim();
    }

    // 排一帧舞台动画（tween / 漂浮 / 呼吸 / 姿态时间线）：Electron Pet 里渲染后端切到定时器
    // 驱动时走同周期定时器（frame-pacing.requestPacedFrame），否则 rAF。返回取消函数。
    // 这些循环在演出期间持续排帧，裸 rAF 会单独把 Blink 主帧顶回显示器刷新率。
    function scheduleStageFrame(callback) {
        const pacing = window.nekoFramePacing;
        if (pacing && typeof pacing.requestPacedFrame === 'function') {
            return pacing.requestPacedFrame(callback);
        }
        const id = window.requestAnimationFrame(callback);
        return () => window.cancelAnimationFrame(id);
    }

    function now() {
        return (window.performance && typeof window.performance.now === 'function')
            ? window.performance.now()
            : Date.now();
    }

    function easeOutCubic(t) {
        const clamped = Math.max(0, Math.min(1, t));
        return 1 - Math.pow(1 - clamped, 3);
    }

    function mergeFrame(base, next) {
        return {
            x: Number.isFinite(Number(next.x)) ? Number(next.x) : base.x,
            y: Number.isFinite(Number(next.y)) ? Number(next.y) : base.y,
            scale: Number.isFinite(Number(next.scale)) ? Number(next.scale) : base.scale,
            rotate: Number.isFinite(Number(next.rotate)) ? Number(next.rotate) : base.rotate,
            opacity: next.opacity === '' || next.opacity == null ? base.opacity : next.opacity
        };
    }

    function prefersReducedMotion() {
        return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    }

    function cloneJsonCompatible(value) {
        if (value == null) {
            return value;
        }
        try {
            return JSON.parse(JSON.stringify(value));
        } catch (_) {
            return value;
        }
    }

    function cloneParamIdSnapshot(value) {
        if (value instanceof Set) {
            return Array.from(value);
        }
        if (Array.isArray(value)) {
            return value.slice();
        }
        return cloneJsonCompatible(value);
    }

    function restoreParamIdSnapshot(value) {
        if (value instanceof Set) {
            return new Set(Array.from(value));
        }
        if (Array.isArray(value)) {
            return new Set(value);
        }
        return cloneJsonCompatible(value);
    }

    function hasOwn(object, key) {
        return !!(object && Object.prototype.hasOwnProperty.call(object, key));
    }

    function toArray(value) {
        if (Array.isArray(value)) {
            return value;
        }
        return value == null ? [] : [value];
    }

    function pushUnique(array, value) {
        const normalized = String(value || '').trim();
        if (normalized && array.indexOf(normalized) < 0) {
            array.push(normalized);
        }
    }

    function normalizeResourceName(value) {
        return String(value || '').trim();
    }

    class AvatarPerformanceStage {
        constructor(options) {
            const normalized = options || {};
            this.driver = normalized.driver || createNoopDriver();
            this.profile = normalized.profile || {};
            this.presets = normalized.presets || {};
            this.sequences = normalized.sequences || this.profile.sequences || {};
            this.logger = normalized.logger || console;
            this.frameState = Object.assign({}, DEFAULT_FRAME, this.profile.defaultFrame || {});
            this.activeSession = null;
            this.sessionSeq = 0;
            this.tweens = new Map();
            this.temporaryParamSnapshots = new Map();
            this.destroyed = false;
            this.reducedMotion = prefersReducedMotion();
        }

        isAvailable() {
            return !!(this.driver && typeof this.driver.isAvailable === 'function' && this.driver.isAvailable());
        }

        acquire(owner, options) {
            if (this.destroyed) {
                return null;
            }

            const normalized = options || {};
            const priority = Number.isFinite(Number(normalized.priority)) ? Number(normalized.priority) : 0;
            if (this.activeSession && !this.activeSession.cancelled) {
                if (priority <= this.activeSession.priority && !normalized.force) {
                    return null;
                }
                this.release(this.activeSession.id, 'preempted');
            }

            const session = {
                id: 'avatar-performance-' + (++this.sessionSeq),
                owner: String(owner || 'anonymous'),
                priority: priority,
                capabilities: normalizeCapabilities(normalized.capabilities),
                cancelled: false,
                snapshot: null
            };
            if (this.driver && typeof this.driver.capture === 'function') {
                try {
                    session.snapshot = this.driver.capture(session, {
                        capabilities: session.capabilities.slice(),
                        paramIds: Array.isArray(normalized.paramIds) ? normalized.paramIds.slice() : [],
                        captureParamIds: Array.isArray(normalized.captureParamIds) ? normalized.captureParamIds.slice() : []
                    }) || null;
                } catch (error) {
                    this.warn('driver capture failed', error);
                }
            }
            this.activeSession = session;
            if (this.driver && typeof this.driver.acquireSession === 'function') {
                try {
                    this.driver.acquireSession(session);
                } catch (error) {
                    this.warn('driver acquire failed', error);
                }
            }
            return session;
        }

        release(sessionId, reason) {
            const session = this.activeSession;
            if (!session || session.id !== sessionId) {
                return false;
            }

            this.cancelTweens(sessionId);
            this.clearLookAt({
                sessionId: sessionId,
                reason: reason || 'release'
            });
            session.cancelled = true;
            this.clearTemporaryParams(reason || 'release');
            this.frameState = Object.assign({}, DEFAULT_FRAME, this.profile.defaultFrame || {});

            let restored = false;
            if (this.driver && typeof this.driver.restore === 'function' && session.snapshot) {
                try {
                    restored = this.driver.restore(session.snapshot, reason || 'release') !== false;
                } catch (error) {
                    this.warn('driver restore failed', error);
                }
            }
            if (!restored && this.driver && typeof this.driver.applyFrame === 'function') {
                try {
                    this.driver.applyFrame(this.frameState, session);
                } catch (error) {
                    this.warn('driver reset frame failed', error);
                }
            }
            if (this.driver && typeof this.driver.releaseSession === 'function') {
                try {
                    this.driver.releaseSession(session, reason || 'release');
                } catch (error) {
                    this.warn('driver release failed', error);
                }
            }
            this.activeSession = null;
            return true;
        }

        commitCurrentFrameAsBaseline(sessionId) {
            const session = this.activeSession;
            if (!session || session.id !== sessionId || !this.driver) {
                return false;
            }
            if (typeof this.driver.commitCurrentFrameAsBaseline !== 'function') {
                return false;
            }
            try {
                return this.driver.commitCurrentFrameAsBaseline(session) !== false;
            } catch (error) {
                this.warn('driver commit baseline failed', error);
                return false;
            }
        }

        getActiveSessionId() {
            return this.activeSession && !this.activeSession.cancelled ? this.activeSession.id : '';
        }

        isSessionActive(sessionId) {
            return !!(this.activeSession && this.activeSession.id === sessionId && !this.activeSession.cancelled && !this.destroyed);
        }

        resolveFrame(frameOrMode) {
            if (typeof frameOrMode === 'string') {
                const composition = this.profile.composition || {};
                return Object.assign({}, composition[frameOrMode] || {});
            }
            if (frameOrMode && typeof frameOrMode === 'object') {
                return Object.assign({}, frameOrMode);
            }
            return {};
        }

        frame(frameOrMode, options) {
            const normalized = options || {};
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (!this.isSessionActive(sessionId)) {
                return Promise.resolve(false);
            }

            this.cancelTweens(sessionId);
            const target = mergeFrame(this.frameState, this.resolveFrame(frameOrMode));
            const duration = Math.max(0, Number(normalized.durationMs || normalized.duration || 0));
            if (duration <= 0 || this.reducedMotion) {
                this.frameState = target;
                this.applyFrame(sessionId);
                return Promise.resolve(true);
            }

            return this.tweenFrame(sessionId, target, {
                durationMs: duration,
                easing: typeof normalized.easing === 'function' ? normalized.easing : easeOutCubic
            });
        }

        tweenFrame(sessionId, target, options) {
            const start = Object.assign({}, this.frameState);
            const durationMs = Math.max(1, Number(options.durationMs || 1));
            const easing = typeof options.easing === 'function' ? options.easing : easeOutCubic;

            return new Promise((resolve) => {
                if (!this.isSessionActive(sessionId)) {
                    resolve(false);
                    return;
                }

                const tween = { cancelFrame: null, done: false };
                const tweenKey = sessionId + ':' + now() + ':' + Math.random();
                this.tweens.set(tweenKey, tween);
                const startedAt = now();
                const step = () => {
                    if (tween.done || !this.isSessionActive(sessionId)) {
                        tween.done = true;
                        this.tweens.delete(tweenKey);
                        resolve(false);
                        return;
                    }

                    const progress = Math.min(1, (now() - startedAt) / durationMs);
                    const eased = easing(progress);
                    this.frameState = {
                        x: start.x + (target.x - start.x) * eased,
                        y: start.y + (target.y - start.y) * eased,
                        scale: start.scale + (target.scale - start.scale) * eased,
                        rotate: start.rotate + (target.rotate - start.rotate) * eased,
                        opacity: target.opacity === '' ? start.opacity : target.opacity
                    };
                    this.applyFrame(sessionId);

                    if (progress >= 1) {
                        tween.done = true;
                        this.tweens.delete(tweenKey);
                        resolve(true);
                        return;
                    }
                    tween.cancelFrame = scheduleStageFrame(step);
                };
                tween.cancelFrame = scheduleStageFrame(step);
            });
        }

        applyFrame(sessionId) {
            if (!this.isSessionActive(sessionId) || !this.driver || typeof this.driver.applyFrame !== 'function') {
                return;
            }
            try {
                this.driver.applyFrame(this.frameState, this.activeSession);
            } catch (error) {
                this.warn('driver apply frame failed', error);
            }
        }

        playMotion(group, options) {
            const normalized = options || {};
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (!this.isSessionActive(sessionId) || !group || !this.driver || typeof this.driver.playMotion !== 'function') {
                return Promise.resolve(false);
            }
            const motionOptions = Object.assign({}, normalized, {
                profile: normalized.profile || this.profile
            });
            return Promise.resolve(this.driver.playMotion(group, motionOptions)).catch((error) => {
                this.warn('driver motion failed', error);
                return false;
            });
        }

        applyExpression(expression, options) {
            const normalized = options || {};
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (!this.isSessionActive(sessionId) || !expression || !this.driver || typeof this.driver.applyExpression !== 'function') {
                return Promise.resolve(false);
            }
            const expressionOptions = Object.assign({}, normalized, {
                profile: normalized.profile || this.profile
            });
            return Promise.resolve(this.driver.applyExpression(expression, expressionOptions)).catch((error) => {
                this.warn('driver expression failed', error);
                return false;
            });
        }

        applyEmotion(emotion, options) {
            const normalized = options || {};
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (!this.isSessionActive(sessionId) || !emotion || !this.driver || typeof this.driver.applyEmotion !== 'function') {
                return Promise.resolve(false);
            }
            const emotionOptions = Object.assign({}, normalized, {
                profile: normalized.profile || this.profile
            });
            return Promise.resolve(this.driver.applyEmotion(emotion, emotionOptions)).catch((error) => {
                this.warn('driver emotion failed', error);
                return false;
            });
        }

        clearExpression(options) {
            const normalized = Object.assign({}, options || {}, {
                profile: (options && options.profile) || this.profile
            });
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (!this.isSessionActive(sessionId)) {
                return Promise.resolve(false);
            }
            if (!this.driver || typeof this.driver.clearExpression !== 'function') {
                return Promise.resolve(false);
            }
            return Promise.resolve(this.driver.clearExpression(normalized)).then((cleared) => {
                return cleared !== false;
            }).catch((error) => {
                this.warn('driver clear expression failed', error);
                return false;
            });
        }

        runPreset(name, options) {
            const preset = (this.presets && this.presets[name]) || this.getBuiltInPreset(name);
            if (typeof preset !== 'function') {
                return Promise.resolve(false);
            }
            return Promise.resolve(preset(this, options || {}));
        }

        resolveSequence(sequenceOrName) {
            if (Array.isArray(sequenceOrName)) {
                return sequenceOrName.slice();
            }
            if (sequenceOrName && Array.isArray(sequenceOrName.steps)) {
                return sequenceOrName.steps.slice();
            }
            if (typeof sequenceOrName === 'string') {
                const sequence = this.sequences && this.sequences[sequenceOrName];
                if (Array.isArray(sequence)) {
                    return sequence.slice();
                }
                if (sequence && Array.isArray(sequence.steps)) {
                    return sequence.steps.slice();
                }
            }
            return [];
        }

        async runSequence(sequenceOrName, options) {
            const normalized = options || {};
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (!this.isSessionActive(sessionId)) {
                return false;
            }

            const steps = this.resolveSequence(sequenceOrName);
            if (!steps.length) {
                return false;
            }

            for (let index = 0; index < steps.length; index += 1) {
                if (!this.isSessionActive(sessionId)) {
                    return false;
                }
                const step = steps[index];
                const completed = await this.runSequenceStep(step, sessionId, normalized);
                if (completed === false && step && step.required === true) {
                    return false;
                }
            }
            return this.isSessionActive(sessionId);
        }

        async runOwnedSequence(owner, sequenceOrName, options) {
            const normalized = options || {};
            const session = this.acquire(owner || 'sequence', {
                priority: normalized.priority,
                force: normalized.force,
                capabilities: Array.isArray(normalized.capabilities) ? normalized.capabilities.slice() : [],
                paramIds: Array.isArray(normalized.paramIds) ? normalized.paramIds.slice() : [],
                captureParamIds: Array.isArray(normalized.captureParamIds) ? normalized.captureParamIds.slice() : []
            });
            if (!session || !session.id) {
                return {
                    completed: false,
                    sessionId: ''
                };
            }

            let completed = false;
            try {
                completed = await this.runSequence(sequenceOrName, Object.assign({}, normalized, {
                    sessionId: session.id
                }));
            } finally {
                if (normalized.releaseOnComplete === true && this.isSessionActive(session.id)) {
                    this.release(session.id, normalized.releaseReason || 'sequence-complete');
                }
            }

            return {
                completed: completed,
                sessionId: session.id
            };
        }

        async runSequenceStep(step, sessionId, sequenceOptions) {
            if (!this.isSessionActive(sessionId)) {
                return false;
            }
            if (!step || typeof step !== 'object') {
                return true;
            }
            if (this.reducedMotion && step.skipWhenReducedMotion === true) {
                return true;
            }

            const type = String(step.type || step.action || '').trim();
            const stepOptions = Object.assign({}, sequenceOptions.stepOptions || {}, step.options || {}, {
                sessionId: sessionId
            });

            const durationScale = Number.isFinite(Number(sequenceOptions.durationScale))
                ? Number(sequenceOptions.durationScale)
                : 1;
            const explicitDurationMs = Number.isFinite(Number(step.durationMs))
                ? Number(step.durationMs)
                : (Number.isFinite(Number(step.duration)) ? Number(step.duration) : null);
            const fallbackDurationMs = Number.isFinite(Number(sequenceOptions.durationMs))
                ? Number(sequenceOptions.durationMs)
                : null;
            const resolvedDurationMs = explicitDurationMs !== null ? explicitDurationMs : fallbackDurationMs;
            if (resolvedDurationMs !== null) {
                stepOptions.durationMs = Math.max(0, Math.round(resolvedDurationMs * durationScale));
            }

            if (sequenceOptions.reducedMotion === true) {
                stepOptions.durationMs = 0;
            }

            if (type === 'frame') {
                return this.frame(step.mode || step.frame || step.target || step.to || {}, stepOptions);
            }
            if (type === 'motion' || type === 'playMotion') {
                return this.playMotion(step.group || step.motion || step.name || step, Object.assign({}, stepOptions, {
                    step: step
                }));
            }
            if (type === 'motionWithFallback' || type === 'optionalMotion') {
                const group = step.group || step.motion || step.name || step;
                const fallback = step.fallback || step.fallbackSequence || step.sequence;
                const hasMotion = group && this.driver && typeof this.driver.hasMotion === 'function'
                    ? this.driver.hasMotion(group, Object.assign({}, stepOptions, {
                        profile: this.profile,
                        step: step
                    }))
                    : false;
                if (hasMotion) {
                    const played = await this.playMotion(group, Object.assign({}, stepOptions, {
                        step: step
                    }));
                    if (stepOptions.durationMs > 0) {
                        await this.waitForSequenceDelay(sessionId, stepOptions.durationMs);
                    }
                    if (played !== false) {
                        return true;
                    }
                }
                if (fallback) {
                    return this.runSequence(fallback, Object.assign({}, sequenceOptions, {
                        sessionId: sessionId
                    }));
                }
                return false;
            }
            if (type === 'preset' || type === 'runPreset') {
                return this.runPreset(step.name || step.preset, Object.assign({}, stepOptions, step.params || {}));
            }
            if (type === 'expression' || type === 'applyExpression') {
                const expression = step.expression || step.name || step.key || step;
                const applied = await this.applyExpression(expression, Object.assign({}, stepOptions, {
                    step: step,
                    blend: step.blend,
                    durationMs: stepOptions.durationMs
                }));
                if (stepOptions.durationMs > 0) {
                    await this.waitForSequenceDelay(sessionId, stepOptions.durationMs);
                }
                return applied;
            }
            if (type === 'emotion' || type === 'setEmotion' || type === 'applyEmotion') {
                const emotion = step.emotion || step.name || step.key || step;
                const applied = await this.applyEmotion(emotion, Object.assign({}, stepOptions, {
                    step: step,
                    blend: step.blend,
                    parts: step.parts || step.apply || null,
                    durationMs: stepOptions.durationMs
                }));
                if (stepOptions.durationMs > 0) {
                    await this.waitForSequenceDelay(sessionId, stepOptions.durationMs);
                }
                return applied;
            }
            if (type === 'clearExpression') {
                return this.clearExpression(Object.assign({}, stepOptions, {
                    reason: step.reason || 'sequence'
                }));
            }
            if (type === 'param' || type === 'setParam' || type === 'setTemporaryParam') {
                return this.setTemporaryParam(step.key || step.param || step.id, step.value, stepOptions);
            }
            if (type === 'poseTimeline' || type === 'runPoseTimeline') {
                if (!this.driver || typeof this.driver.runPoseTimeline !== 'function') {
                    return false;
                }
                return this.driver.runPoseTimeline(step, Object.assign({}, stepOptions, {
                    reducedMotion: sequenceOptions.reducedMotion === true || stepOptions.reducedMotion === true
                }));
            }
            if (type === 'lookAt') {
                const target = this.resolveSequenceTarget(step.target || step.point || step.element || step.key, sequenceOptions);
                const looked = this.lookAt(target, Object.assign({}, stepOptions, step.params || {}));
                if (stepOptions.durationMs > 0) {
                    await this.waitForSequenceDelay(sessionId, stepOptions.durationMs);
                }
                return looked;
            }
            if (type === 'clearLookAt') {
                this.clearLookAt(Object.assign({}, stepOptions, {
                    reason: step.reason || 'sequence'
                }));
                return true;
            }
            if (type === 'clearParams' || type === 'clearTemporaryParams') {
                this.clearTemporaryParams(step.reason || 'sequence');
                return true;
            }
            if (type === 'wait' || type === 'delay') {
                return this.waitForSequenceDelay(sessionId, stepOptions.durationMs || step.ms);
            }
            if (type === 'sequence') {
                return this.runSequence(step.name || step.sequence || step.steps, Object.assign({}, sequenceOptions, {
                    sessionId: sessionId
                }));
            }
            if (type === 'speechCue') {
                if (typeof sequenceOptions.onSpeechCue === 'function') {
                    try {
                        const result = await Promise.resolve(sequenceOptions.onSpeechCue({
                            cue: step.cue || step.name || step.key || '',
                            step: step,
                            sessionId: sessionId,
                            options: stepOptions
                        }));
                        return result !== false;
                    } catch (error) {
                        this.warn('speech cue callback failed', error);
                        return false;
                    }
                }
                return true;
            }

            return true;
        }

        resolveSequenceTarget(target, sequenceOptions) {
            if (typeof target === 'function') {
                try {
                    return target(sequenceOptions || {});
                } catch (error) {
                    this.warn('sequence target resolver failed', error);
                    return null;
                }
            }
            if (typeof target === 'string') {
                const targets = sequenceOptions && sequenceOptions.targets ? sequenceOptions.targets : null;
                if (targets && Object.prototype.hasOwnProperty.call(targets, target)) {
                    return targets[target];
                }
            }
            return target || null;
        }

        waitForSequenceDelay(sessionId, durationMs) {
            const delayMs = Math.max(0, Number(durationMs || 0));
            if (delayMs <= 0 || this.reducedMotion) {
                return Promise.resolve(this.isSessionActive(sessionId));
            }
            return new Promise((resolve) => {
                window.setTimeout(() => {
                    resolve(this.isSessionActive(sessionId));
                }, delayMs);
            });
        }

        getBuiltInPreset(name) {
            if (name === 'idleFloat') {
                return (stage, options) => stage.startIdleFloat(options || {});
            }
            if (name === 'pulse') {
                return (stage, options) => stage.runPulse(options || {});
            }
            if (name === 'hop') {
                return (stage, options) => stage.runHop(options || {});
            }
            if (name === 'shake') {
                return (stage, options) => stage.runShake(options || {});
            }
            if (name === 'settle') {
                return (stage, options) => stage.runSettle(options || {});
            }
            if (name === 'breathe') {
                return (stage, options) => stage.startBreathe(options || {});
            }
            return null;
        }

        startIdleFloat(options) {
            const normalized = options || {};
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (!this.isSessionActive(sessionId) || this.reducedMotion) {
                return false;
            }

            this.cancelPresetTweens(sessionId, 'idleFloat');
            const base = Object.assign({}, this.frameState);
            const amplitudeY = Number.isFinite(Number(normalized.amplitudeY)) ? Number(normalized.amplitudeY) : 5;
            const amplitudeX = Number.isFinite(Number(normalized.amplitudeX)) ? Number(normalized.amplitudeX) : 0;
            const periodMs = Math.max(1200, Number(normalized.periodMs || 5200));
            const phase = Number.isFinite(Number(normalized.phase)) ? Number(normalized.phase) : 0;
            const startedAt = now();
            const tween = { cancelFrame: null, done: false };
            const tweenKey = sessionId + ':preset:idleFloat:' + startedAt + ':' + Math.random();
            this.tweens.set(tweenKey, tween);

            const step = () => {
                if (tween.done || !this.isSessionActive(sessionId)) {
                    tween.done = true;
                    this.tweens.delete(tweenKey);
                    return;
                }

                const elapsed = now() - startedAt;
                const wave = Math.sin((elapsed / periodMs) * Math.PI * 2 + phase);
                this.frameState = Object.assign({}, base, {
                    x: base.x + amplitudeX * wave,
                    y: base.y + amplitudeY * wave
                });
                this.applyFrame(sessionId);
                tween.cancelFrame = scheduleStageFrame(step);
            };
            tween.cancelFrame = scheduleStageFrame(step);
            return true;
        }

        async runPulse(options) {
            const normalized = options || {};
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (!this.isSessionActive(sessionId) || this.reducedMotion) {
                return false;
            }

            const base = Object.assign({}, this.frameState);
            const scaleAmount = Number.isFinite(Number(normalized.scaleAmount)) ? Number(normalized.scaleAmount) : 0.04;
            const yAmount = Number.isFinite(Number(normalized.yAmount)) ? Number(normalized.yAmount) : -6;
            const durationMs = Math.max(120, Number(normalized.durationMs || 420));
            await this.frame({
                x: base.x,
                y: base.y + yAmount,
                scale: base.scale + scaleAmount,
                rotate: base.rotate,
                opacity: base.opacity
            }, {
                sessionId: sessionId,
                durationMs: Math.round(durationMs * 0.45)
            });
            if (!this.isSessionActive(sessionId)) {
                return false;
            }
            return this.frame(base, {
                sessionId: sessionId,
                durationMs: Math.round(durationMs * 0.55)
            });
        }

        async runHop(options) {
            const normalized = options || {};
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (!this.isSessionActive(sessionId) || this.reducedMotion) {
                return false;
            }

            const base = Object.assign({}, this.frameState);
            const yAmount = Number.isFinite(Number(normalized.yAmount)) ? Number(normalized.yAmount) : -18;
            const scaleAmount = Number.isFinite(Number(normalized.scaleAmount)) ? Number(normalized.scaleAmount) : 0.055;
            const rotateAmount = Number.isFinite(Number(normalized.rotateAmount)) ? Number(normalized.rotateAmount) : -2.5;
            const durationMs = Math.max(180, Number(normalized.durationMs || 560));
            await this.frame({
                x: base.x,
                y: base.y + yAmount,
                scale: base.scale + scaleAmount,
                rotate: base.rotate + rotateAmount,
                opacity: base.opacity
            }, {
                sessionId: sessionId,
                durationMs: Math.round(durationMs * 0.38)
            });
            if (!this.isSessionActive(sessionId)) {
                return false;
            }
            await this.frame({
                x: base.x,
                y: base.y + Math.abs(yAmount) * 0.18,
                scale: Math.max(0.1, base.scale - scaleAmount * 0.35),
                rotate: base.rotate - rotateAmount * 0.4,
                opacity: base.opacity
            }, {
                sessionId: sessionId,
                durationMs: Math.round(durationMs * 0.22)
            });
            if (!this.isSessionActive(sessionId)) {
                return false;
            }
            return this.frame(base, {
                sessionId: sessionId,
                durationMs: Math.round(durationMs * 0.4)
            });
        }

        async runShake(options) {
            const normalized = options || {};
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (!this.isSessionActive(sessionId) || this.reducedMotion) {
                return false;
            }

            const base = Object.assign({}, this.frameState);
            const xAmount = Number.isFinite(Number(normalized.xAmount)) ? Number(normalized.xAmount) : 7;
            const yAmount = Number.isFinite(Number(normalized.yAmount)) ? Number(normalized.yAmount) : 0;
            const rotateAmount = Number.isFinite(Number(normalized.rotateAmount)) ? Number(normalized.rotateAmount) : 1.8;
            const scaleAmount = Number.isFinite(Number(normalized.scaleAmount)) ? Number(normalized.scaleAmount) : 0;
            const cycles = Math.max(1, Math.min(8, Math.round(Number(normalized.cycles || 3))));
            const durationMs = Math.max(120, Number(normalized.durationMs || 360));
            const stepDuration = Math.max(24, Math.round(durationMs / (cycles * 2 + 1)));
            for (let index = 0; index < cycles * 2; index += 1) {
                if (!this.isSessionActive(sessionId)) {
                    return false;
                }
                const direction = index % 2 === 0 ? 1 : -1;
                await this.frame({
                    x: base.x + xAmount * direction,
                    y: base.y + yAmount * direction,
                    scale: base.scale + scaleAmount,
                    rotate: base.rotate + rotateAmount * direction,
                    opacity: base.opacity
                }, {
                    sessionId: sessionId,
                    durationMs: stepDuration
                });
            }
            if (!this.isSessionActive(sessionId)) {
                return false;
            }
            return this.frame(base, {
                sessionId: sessionId,
                durationMs: stepDuration
            });
        }

        runSettle(options) {
            const normalized = options || {};
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (!this.isSessionActive(sessionId)) {
                return Promise.resolve(false);
            }

            const target = Object.assign({}, DEFAULT_FRAME, this.profile.defaultFrame || {}, normalized.frame || {});
            return this.frame(target, {
                sessionId: sessionId,
                durationMs: this.reducedMotion ? 0 : Math.max(0, Number(normalized.durationMs || 360))
            });
        }

        startBreathe(options) {
            const normalized = options || {};
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (!this.isSessionActive(sessionId) || this.reducedMotion) {
                return false;
            }

            this.cancelPresetTweens(sessionId, 'breathe');
            const base = Object.assign({}, this.frameState);
            const scaleAmount = Number.isFinite(Number(normalized.scaleAmount)) ? Number(normalized.scaleAmount) : 0.012;
            const yAmount = Number.isFinite(Number(normalized.yAmount)) ? Number(normalized.yAmount) : -2;
            const periodMs = Math.max(1600, Number(normalized.periodMs || 4200));
            const phase = Number.isFinite(Number(normalized.phase)) ? Number(normalized.phase) : 0;
            const startedAt = now();
            const tween = { cancelFrame: null, done: false };
            const tweenKey = sessionId + ':preset:breathe:' + startedAt + ':' + Math.random();
            this.tweens.set(tweenKey, tween);

            const step = () => {
                if (tween.done || !this.isSessionActive(sessionId)) {
                    tween.done = true;
                    this.tweens.delete(tweenKey);
                    return;
                }

                const elapsed = now() - startedAt;
                const wave = (Math.sin((elapsed / periodMs) * Math.PI * 2 + phase) + 1) / 2;
                this.frameState = Object.assign({}, base, {
                    y: base.y + yAmount * wave,
                    scale: base.scale + scaleAmount * wave
                });
                this.applyFrame(sessionId);
                tween.cancelFrame = scheduleStageFrame(step);
            };
            tween.cancelFrame = scheduleStageFrame(step);
            return true;
        }

        resolveParamId(keyOrId) {
            return this.resolveParamIds(keyOrId)[0] || '';
        }

        resolveParamIds(keyOrId) {
            const params = this.profile.params || {};
            const mapped = params[keyOrId];
            const result = [];
            toArray(mapped).forEach((value) => pushUnique(result, value));
            pushUnique(result, keyOrId);
            return result;
        }

        setTemporaryParam(keyOrId, value, options) {
            const normalized = options || {};
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (!this.isSessionActive(sessionId) || !this.driver || typeof this.driver.setParam !== 'function') {
                return false;
            }

            const paramIds = this.resolveParamIds(keyOrId);
            if (!paramIds.length) {
                return false;
            }
            for (let index = 0; index < paramIds.length; index += 1) {
                const paramId = paramIds[index];
                const snapshotKey = sessionId + ':' + paramId;
                if (!this.temporaryParamSnapshots.has(snapshotKey) && typeof this.driver.captureParams === 'function') {
                    this.temporaryParamSnapshots.set(snapshotKey, this.driver.captureParams([paramId]));
                }
                if (this.driver.setParam(paramId, value, normalized)) {
                    return true;
                }
            }
            return false;
        }

        clearTemporaryParams(reason) {
            if (!this.driver || typeof this.driver.restoreParams !== 'function') {
                this.temporaryParamSnapshots.clear();
                return;
            }

            this.temporaryParamSnapshots.forEach((snapshot) => {
                try {
                    this.driver.restoreParams(snapshot, reason || 'clear');
                } catch (error) {
                    this.warn('driver restore param failed', error);
                }
            });
            this.temporaryParamSnapshots.clear();
        }

        lookAt(target, options) {
            const normalized = options || {};
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (!this.isSessionActive(sessionId) || !this.driver || typeof this.driver.lookAt !== 'function') {
                return false;
            }
            return this.driver.lookAt(target, Object.assign({}, this.profile.lookAt || {}, normalized), this.activeSession);
        }

        clearLookAt(options) {
            const normalized = options || {};
            const sessionId = normalized.sessionId || this.getActiveSessionId();
            if (sessionId && !this.isSessionActive(sessionId)) {
                return false;
            }
            if (!this.driver || typeof this.driver.clearLookAt !== 'function') {
                return false;
            }
            this.driver.clearLookAt(normalized);
            return true;
        }

        cancelTweens(sessionId) {
            this.tweens.forEach((tween, key) => {
                if (!sessionId || key.indexOf(sessionId + ':') === 0) {
                    tween.done = true;
                    if (tween.cancelFrame) {
                        tween.cancelFrame();
                        tween.cancelFrame = null;
                    }
                    this.tweens.delete(key);
                }
            });
        }

        cancelPresetTweens(sessionId, name) {
            const prefix = sessionId + ':preset:' + String(name || '') + ':';
            this.tweens.forEach((tween, key) => {
                if (key.indexOf(prefix) === 0) {
                    tween.done = true;
                    if (tween.cancelFrame) {
                        tween.cancelFrame();
                        tween.cancelFrame = null;
                    }
                    this.tweens.delete(key);
                }
            });
        }

        destroy(reason) {
            if (this.destroyed) {
                return;
            }
            const sessionId = this.getActiveSessionId();
            if (sessionId) {
                this.release(sessionId, reason || 'destroy');
            }
            this.cancelTweens();
            this.clearTemporaryParams(reason || 'destroy');
            this.destroyed = true;
        }

        warn(message, error) {
            if (this.logger && typeof this.logger.warn === 'function') {
                this.logger.warn('[AvatarPerformanceStage] ' + message, error);
            }
        }
    }

    class AvatarPerformanceCoordinator {
        constructor(options) {
            const normalized = options || {};
            this.kind = 'coordinator';
            this.contractVersion = CONTRACTS.coordinatorVersion;
            this.logger = normalized.logger || console;
            this.sessionSeq = 0;
            this.sessions = new Map();
            this.locks = new Map();
        }

        acquire(request) {
            const normalized = request || {};
            const avatarId = normalizeAvatarId(normalized.avatarId || normalized.characterId);
            const capabilities = normalizeCapabilities(normalized.capabilities);
            const priority = Number.isFinite(Number(normalized.priority)) ? Number(normalized.priority) : 0;
            const force = normalized.force === true;
            let preemptAttempts = 0;

            while (true) {
                const blockers = this.findLockHolders(avatarId, capabilities);
                if (blockers.length === 0) {
                    break;
                }

                for (let index = 0; index < blockers.length; index += 1) {
                    const holder = blockers[index];
                    if (!force && priority <= holder.priority) {
                        return null;
                    }
                }

                preemptAttempts += 1;
                if (preemptAttempts > 16) {
                    this.warn('coordinator preempt loop exceeded safety limit');
                    return null;
                }

                blockers.forEach((holder) => {
                    this.release(holder.id, 'preempted');
                });
            }

            const session = {
                id: 'avatar-performance-coordinator-' + (++this.sessionSeq),
                owner: String(normalized.owner || 'anonymous'),
                avatarId: avatarId,
                characterId: String(normalized.characterId || '').trim(),
                priority: priority,
                capabilities: capabilities.slice(),
                cancelled: false,
                startedAt: now(),
                onRelease: typeof normalized.onRelease === 'function' ? normalized.onRelease : null
            };
            this.sessions.set(session.id, session);
            capabilities.forEach((capability) => {
                this.locks.set(createLockKey(avatarId, capability), session.id);
            });
            return session;
        }

        findLockHolders(avatarId, capabilities) {
            const seen = {};
            const holders = [];
            normalizeCapabilities(capabilities).forEach((capability) => {
                const sessionId = this.locks.get(createLockKey(avatarId, capability));
                if (!sessionId || seen[sessionId]) {
                    return;
                }
                const session = this.sessions.get(sessionId);
                if (!session || session.cancelled) {
                    return;
                }
                seen[sessionId] = true;
                holders.push(session);
            });
            return holders;
        }

        release(sessionOrId, reason) {
            const sessionId = typeof sessionOrId === 'string'
                ? sessionOrId
                : (sessionOrId && sessionOrId.id ? sessionOrId.id : '');
            const session = this.sessions.get(sessionId);
            if (!session) {
                return false;
            }

            session.cancelled = true;
            session.capabilities.forEach((capability) => {
                const key = createLockKey(session.avatarId, capability);
                if (this.locks.get(key) === session.id) {
                    this.locks.delete(key);
                }
            });
            this.sessions.delete(session.id);
            if (typeof session.onRelease === 'function') {
                try {
                    session.onRelease(session, reason || 'release');
                } catch (error) {
                    this.warn('coordinator release callback failed', error);
                }
            }
            return true;
        }

        destroy(reason) {
            Array.from(this.sessions.keys()).forEach((sessionId) => {
                this.release(sessionId, reason || 'destroy');
            });
            this.locks.clear();
        }

        isCapabilityLocked(avatarId, capability) {
            const normalizedAvatarId = normalizeAvatarId(avatarId);
            const normalizedCapability = String(capability || '').trim();
            if (normalizedCapability) {
                const sessionId = this.locks.get(createLockKey(normalizedAvatarId, normalizedCapability));
                return !!(sessionId && this.sessions.has(sessionId));
            }
            return this.getLockedCapabilities(normalizedAvatarId).length > 0;
        }

        getLockedCapabilities(avatarId) {
            const normalizedAvatarId = normalizeAvatarId(avatarId);
            const result = [];
            this.locks.forEach((sessionId, key) => {
                const separatorIndex = key.indexOf('\u0000');
                const keyAvatarId = separatorIndex >= 0 ? key.slice(0, separatorIndex) : '';
                const capability = separatorIndex >= 0 ? key.slice(separatorIndex + 1) : '';
                if (keyAvatarId === normalizedAvatarId && capability && this.sessions.has(sessionId)) {
                    result.push(capability);
                }
            });
            return result;
        }

        getActiveSession(query) {
            const normalized = query || {};
            if (normalized.sessionId && this.sessions.has(normalized.sessionId)) {
                return this.sessions.get(normalized.sessionId) || null;
            }
            if (normalized.avatarId || normalized.characterId) {
                const avatarId = normalizeAvatarId(normalized.avatarId || normalized.characterId);
                if (normalized.capability) {
                    const sessionId = this.locks.get(createLockKey(avatarId, normalized.capability));
                    return sessionId ? (this.sessions.get(sessionId) || null) : null;
                }
                const sessions = Array.from(this.sessions.values()).filter((session) => {
                    return session.avatarId === avatarId && !session.cancelled;
                });
                return sessions[0] || null;
            }
            return Array.from(this.sessions.values()).find((session) => !session.cancelled) || null;
        }

        warn(message, error) {
            if (this.logger && typeof this.logger.warn === 'function') {
                this.logger.warn('[AvatarPerformanceCoordinator] ' + message, error);
            }
        }
    }

    class Live2DAvatarPerformanceDriver {
        constructor(options) {
            const normalized = options || {};
            this.managerResolver = typeof normalized.managerResolver === 'function'
                ? normalized.managerResolver
                : function () { return window.live2dManager || null; };
            this.containerResolver = typeof normalized.containerResolver === 'function'
                ? normalized.containerResolver
                : function () { return document.getElementById('live2d-container'); };
            this.profile = normalized.profile || {};
            this.styleSnapshot = null;
            this.ownerSessionId = '';
            this.activeFrameTransformNonIdentity = false;
            this.activeFrameTransformContainer = null;
            this.committedFrameTransformActive = false;
            this.committedFrameTransformContainer = null;
            this.lookAtSnapshot = null;
            this.lookAtSource = '';
            this.lookAtSessionId = '';
            this.motionSuspendSource = '';
            this.expressionParamSnapshot = null;
            this.lookAtParams = Object.assign({
                angleX: 'ParamAngleX',
                angleY: 'ParamAngleY',
                eyeX: 'ParamEyeBallX',
                eyeY: 'ParamEyeBallY'
            }, normalized.lookAtParams || {});
        }

        getManager() {
            return this.managerResolver() || null;
        }

        getModel() {
            const manager = this.getManager();
            if (!manager) {
                return null;
            }
            if (typeof manager.getCurrentModel === 'function') {
                return manager.getCurrentModel();
            }
            return manager.currentModel || null;
        }

        getCoreModel() {
            const model = this.getModel();
            return model && !model.destroyed && model.internalModel
                ? model.internalModel.coreModel || null
                : null;
        }

        getLive2DContext() {
            const manager = this.getManager();
            const model = this.getModel();
            const coreModel = model && !model.destroyed && model.internalModel
                ? model.internalModel.coreModel || null
                : null;
            if (!manager || !model || !coreModel) {
                return null;
            }
            return {
                manager: manager,
                model: model,
                coreModel: coreModel,
                ticker: manager.pixi_app && manager.pixi_app.ticker
            };
        }

        waitForLive2DContext(timeoutMs) {
            const immediate = this.getLive2DContext();
            if (immediate) {
                return Promise.resolve(immediate);
            }
            const maxWait = Math.max(0, Math.round(Number(timeoutMs || 0)));
            if (maxWait <= 0) {
                return Promise.resolve(null);
            }
            return new Promise((resolve) => {
                const startedAt = now();
                const check = () => {
                    const context = this.getLive2DContext();
                    if (context) {
                        resolve(context);
                        return;
                    }
                    if (now() - startedAt >= maxWait) {
                        resolve(null);
                        return;
                    }
                    window.requestAnimationFrame(check);
                };
                window.requestAnimationFrame(check);
            });
        }

        getContainer() {
            return this.containerResolver() || null;
        }

        getProfile(options) {
            return (options && options.profile) || this.profile || {};
        }

        getMotionManager() {
            const model = this.getModel();
            return model && model.internalModel ? model.internalModel.motionManager || null : null;
        }

        sessionHasCapability(session, capability) {
            const capabilities = session && Array.isArray(session.capabilities)
                ? session.capabilities
                : CONTRACTS.capabilities;
            return capabilities.indexOf(capability) >= 0;
        }

        getProfileCandidates(sectionName, name, options) {
            const profile = this.getProfile(options);
            const section = profile && profile[sectionName] ? profile[sectionName] : {};
            const key = normalizeResourceName(name);
            return key && hasOwn(section, key) ? toArray(section[key]) : [];
        }

        getMotionFile(item) {
            return item && (item.File || item.file || item.motionFile || item.path) || '';
        }

        normalizeAssetKey(value) {
            return String(value || '').replace(/\\/g, '/').replace(/^[./]+/, '').toLowerCase();
        }

        findMotionRuntimeReference(groupName, motionFile) {
            const targetKey = this.normalizeAssetKey(motionFile);
            if (!targetKey) {
                return null;
            }
            const motionManager = this.getMotionManager();
            const sources = [
                motionManager && motionManager.definitions,
                motionManager && motionManager._definitions,
                motionManager && motionManager.motionGroups,
                motionManager && motionManager._motionGroups,
                this.getManager() && this.getManager().fileReferences && this.getManager().fileReferences.Motions
            ].filter(Boolean);
            const findInGroup = (source, group) => {
                const items = source && source[group];
                if (!Array.isArray(items)) {
                    return null;
                }
                for (let index = 0; index < items.length; index += 1) {
                    if (this.normalizeAssetKey(this.getMotionFile(items[index])) === targetKey) {
                        return { group: group, index: index, file: this.getMotionFile(items[index]) };
                    }
                }
                return null;
            };

            for (let index = 0; index < sources.length; index += 1) {
                const source = sources[index];
                if (groupName) {
                    const preferred = findInGroup(source, groupName);
                    if (preferred) {
                        return preferred;
                    }
                }
                const groupNames = Object.keys(source || {});
                for (let groupIndex = 0; groupIndex < groupNames.length; groupIndex += 1) {
                    const found = findInGroup(source, groupNames[groupIndex]);
                    if (found) {
                        return found;
                    }
                }
            }
            return null;
        }

        collectMotionCandidates(motion, options) {
            const candidates = [];
            const addCandidate = (candidate) => {
                if (candidate == null) {
                    return;
                }
                if (Array.isArray(candidate)) {
                    candidate.forEach(addCandidate);
                    return;
                }
                candidates.push(candidate);
            };
            const name = typeof motion === 'string'
                ? motion
                : normalizeResourceName(motion && (motion.name || motion.motion || motion.group || motion.key));
            this.getProfileCandidates('motions', name, options).forEach(addCandidate);
            addCandidate(motion);
            return candidates;
        }

        withPerformanceBypass(callback) {
            const manager = this.getManager();
            if (!manager) {
                return callback();
            }
            const previousDepth = Number.isFinite(Number(manager._avatarPerformanceBypassDepth))
                ? Math.max(0, Number(manager._avatarPerformanceBypassDepth))
                : 0;
            const restoreExternalBypass = previousDepth === 0 && manager._avatarPerformanceBypassLocks === true;
            manager._avatarPerformanceBypassDepth = previousDepth + 1;
            manager._avatarPerformanceBypassLocks = true;
            const restore = () => {
                const currentDepth = Number.isFinite(Number(manager._avatarPerformanceBypassDepth))
                    ? Math.max(0, Number(manager._avatarPerformanceBypassDepth))
                    : 1;
                const nextDepth = Math.max(0, currentDepth - 1);
                manager._avatarPerformanceBypassDepth = nextDepth;
                manager._avatarPerformanceBypassLocks = nextDepth > 0 || restoreExternalBypass;
            };
            try {
                return Promise.resolve(callback()).finally(() => {
                    restore();
                });
            } catch (error) {
                restore();
                throw error;
            }
        }

        captureContainerStyle(container) {
            if (!container || !container.style) {
                return null;
            }
            return {
                transform: container.style.transform || '',
                transition: container.style.transition || '',
                transformOrigin: container.style.transformOrigin || '',
                opacity: container.style.opacity || '',
                willChange: container.style.willChange || ''
            };
        }

        restoreContainerStyle(styleSnapshot) {
            const container = this.getContainer();
            if (!container || !container.style || !styleSnapshot) {
                return false;
            }
            container.style.transform = styleSnapshot.transform || '';
            container.style.transition = styleSnapshot.transition || '';
            container.style.transformOrigin = styleSnapshot.transformOrigin || '';
            container.style.opacity = styleSnapshot.opacity || '';
            container.style.willChange = styleSnapshot.willChange || '';
            return true;
        }

        hasContainerTransform(container) {
            const transform = container && container.style
                ? String(container.style.transform || '').trim().toLowerCase()
                : '';
            return transform !== '' && transform !== 'none';
        }

        isAvailable() {
            return !!(this.getManager() && this.getModel() && this.getContainer());
        }

        resolveAvatar() {
            return {
                kind: 'live2d',
                manager: this.getManager(),
                model: this.getModel(),
                container: this.getContainer()
            };
        }

        capture(session, options) {
            const normalized = options || {};
            const container = this.getContainer();
            if (this.committedFrameTransformContainer === container &&
                !this.hasContainerTransform(container)) {
                if (window._nekoAvatarPerformanceFrameContainer === container) {
                    window._nekoAvatarPerformanceFrameContainer = null;
                }
                this.committedFrameTransformActive = false;
                this.committedFrameTransformContainer = null;
            }
            const params = this.lookAtParams || {};
            const requestedParamIds = []
                .concat(Array.isArray(normalized.paramIds) ? normalized.paramIds : [])
                .concat(Array.isArray(normalized.captureParamIds) ? normalized.captureParamIds : [])
                .concat([params.angleX, params.angleY, params.eyeX, params.eyeY])
                .filter(Boolean);
            const paramIds = Array.from(new Set(requestedParamIds.map((id) => String(id || '').trim()).filter(Boolean)));
            return {
                kind: 'live2d',
                sessionId: session && session.id ? session.id : '',
                container: container,
                containerStyle: this.captureContainerStyle(container),
                committedFrameTransformActive: this.committedFrameTransformActive,
                committedFrameTransformContainer: this.committedFrameTransformContainer,
                params: this.captureParams(paramIds),
                expression: this.captureExpression(),
                lookAt: {
                    snapshot: this.lookAtSnapshot ? cloneJsonCompatible(this.lookAtSnapshot) : null,
                    source: this.lookAtSource || '',
                    sessionId: this.lookAtSessionId || ''
                }
            };
        }

        restore(snapshot) {
            if (!snapshot || snapshot.kind !== 'live2d') {
                return false;
            }

            const manager = this.getManager();
            if (manager && typeof manager.clearTemporaryPoseOverride === 'function') {
                const sources = [this.lookAtSource, snapshot.lookAt && snapshot.lookAt.source].filter(Boolean);
                Array.from(new Set(sources)).forEach((source) => {
                    try {
                        manager.clearTemporaryPoseOverride(source);
                    } catch (_) {}
                });
            }

            if (snapshot.params) {
                this.restoreParams(snapshot.params);
            }
            if (snapshot.lookAt && snapshot.lookAt.snapshot) {
                this.restoreParams(snapshot.lookAt.snapshot);
            }
            if (this.expressionParamSnapshot) {
                this.restoreParams(this.expressionParamSnapshot);
                this.expressionParamSnapshot = null;
            }
            const container = this.getContainer();
            const snapshotContainer = snapshot.container || null;
            const snapshotCommittedContainer = snapshot.committedFrameTransformContainer || null;
            const ownedMarkerContainers = [
                this.activeFrameTransformContainer,
                this.committedFrameTransformContainer,
                snapshotCommittedContainer
            ].filter(Boolean);
            if (ownedMarkerContainers.includes(window._nekoAvatarPerformanceFrameContainer)) {
                window._nekoAvatarPerformanceFrameContainer = null;
            }

            this.restoreExpression(snapshot.expression);
            if (container && snapshotContainer === container) {
                this.restoreContainerStyle(snapshot.containerStyle);
            }

            this.activeFrameTransformNonIdentity = false;
            this.activeFrameTransformContainer = null;
            this.committedFrameTransformActive =
                snapshot.committedFrameTransformActive === true &&
                snapshotCommittedContainer === container &&
                snapshotContainer === container &&
                this.hasContainerTransform(container);
            this.committedFrameTransformContainer = this.committedFrameTransformActive
                ? container
                : null;
            if (this.committedFrameTransformActive) {
                window._nekoAvatarPerformanceFrameContainer = container;
            }
            this.styleSnapshot = null;
            this.ownerSessionId = '';
            this.lookAtSnapshot = null;
            this.lookAtSource = '';
            this.lookAtSessionId = '';
            return true;
        }

        commitCurrentFrameAsBaseline(session) {
            if (session && this.ownerSessionId && session.id !== this.ownerSessionId) {
                return false;
            }
            const container = this.getContainer();
            const committedStyle = this.captureContainerStyle(container);
            if (!committedStyle ||
                (this.activeFrameTransformContainer &&
                    this.activeFrameTransformContainer !== container)) {
                return false;
            }
            if (this.activeFrameTransformNonIdentity) {
                this.committedFrameTransformActive = true;
                this.committedFrameTransformContainer = container;
            }
            this.styleSnapshot = cloneJsonCompatible(committedStyle);
            if (session && session.snapshot && typeof session.snapshot === 'object') {
                session.snapshot.container = container;
                session.snapshot.containerStyle = cloneJsonCompatible(committedStyle);
                session.snapshot.committedFrameTransformActive = this.committedFrameTransformActive;
                session.snapshot.committedFrameTransformContainer = this.committedFrameTransformContainer;
            }
            return true;
        }

        acquireSession(session) {
            const container = this.getContainer();
            this.ownerSessionId = session && session.id ? session.id : '';
            this.activeFrameTransformNonIdentity = false;
            this.activeFrameTransformContainer = null;
            const manager = this.getManager();
            if (this.sessionHasCapability(session, 'motion') && manager && typeof manager.suspendTemporaryMotions === 'function') {
                this.motionSuspendSource = 'avatar-performance-motion-' + (this.ownerSessionId || 'session');
                try {
                    manager.suspendTemporaryMotions(this.motionSuspendSource, this.getModel());
                } catch (_) {}
            }
            const hasFrameCapability = this.sessionHasCapability(session, 'frame');
            if (hasFrameCapability && container) {
                this.activeFrameTransformContainer = container;
                window._nekoAvatarPerformanceFrameContainer = container;
            }
            if (!hasFrameCapability || !container || this.styleSnapshot) {
                return;
            }
            this.styleSnapshot = this.captureContainerStyle(container);
            container.style.transition = 'none';
            container.style.transformOrigin = container.style.transformOrigin || 'center bottom';
            container.style.willChange = 'transform, opacity';
        }

        releaseSession(session) {
            if (session && this.ownerSessionId && session.id !== this.ownerSessionId) {
                return;
            }
            const container = this.getContainer();
            const activeContainer = this.activeFrameTransformContainer;
            const committedContainer = this.committedFrameTransformContainer;
            this.activeFrameTransformNonIdentity = false;
            this.activeFrameTransformContainer = null;
            if (window._nekoAvatarPerformanceFrameContainer === activeContainer ||
                window._nekoAvatarPerformanceFrameContainer === committedContainer) {
                window._nekoAvatarPerformanceFrameContainer = null;
            }
            if (this.styleSnapshot && activeContainer && activeContainer === container) {
                this.restoreContainerStyle(this.styleSnapshot);
            }
            const retainCommittedTransform =
                this.committedFrameTransformActive &&
                committedContainer &&
                committedContainer === container &&
                this.hasContainerTransform(container);
            if (retainCommittedTransform) {
                window._nekoAvatarPerformanceFrameContainer = committedContainer;
            } else {
                this.committedFrameTransformActive = false;
                this.committedFrameTransformContainer = null;
            }
            this.styleSnapshot = null;
            this.ownerSessionId = '';
            const manager = this.getManager();
            if (manager && typeof manager.resumeTemporaryMotions === 'function' && this.motionSuspendSource) {
                try {
                    manager.resumeTemporaryMotions(this.motionSuspendSource);
                } catch (_) {}
            }
            this.motionSuspendSource = '';
            this.clearLookAt({
                sessionId: session && session.id ? session.id : ''
            });
        }

        applyFrame(frame, session) {
            if (session && this.ownerSessionId && session.id !== this.ownerSessionId) {
                return;
            }

            const container = this.getContainer();
            if (!container) {
                return;
            }
            const baseTransform = this.styleSnapshot && this.styleSnapshot.transform
                ? this.styleSnapshot.transform
                : '';
            const x = Number(frame.x || 0);
            const y = Number(frame.y || 0);
            const scale = Number(frame.scale || 1);
            const rotate = Number(frame.rotate || 0);
            const serializedX = x.toFixed(2);
            const serializedY = y.toFixed(2);
            const serializedScale = scale.toFixed(4);
            const serializedRotate = rotate.toFixed(3);
            const effectiveX = Number(serializedX);
            const effectiveY = Number(serializedY);
            const effectiveScale = Number(serializedScale);
            const effectiveRotate = Number(serializedRotate);
            const normalizedEffectiveRotate = ((effectiveRotate % 360) + 360) % 360;
            this.activeFrameTransformNonIdentity =
                effectiveX !== 0 ||
                effectiveY !== 0 ||
                effectiveScale !== 1 ||
                normalizedEffectiveRotate !== 0;
            const transform = [
                baseTransform,
                'translate3d(' + serializedX + 'px, ' + serializedY + 'px, 0)',
                'scale(' + serializedScale + ')',
                'rotate(' + serializedRotate + 'deg)'
            ].filter(Boolean).join(' ');
            container.style.transform = transform;
            if (this.activeFrameTransformContainer === container) {
                window._nekoAvatarPerformanceFrameContainer = container;
            }
            if (frame.opacity !== '') {
                container.style.opacity = String(frame.opacity);
            }
        }

        async playMotion(motion, options) {
            const manager = this.getManager();
            const model = this.getModel();
            if (!manager || !model) {
                return false;
            }
            const candidates = this.collectMotionCandidates(motion, options);
            for (let index = 0; index < candidates.length; index += 1) {
                const candidate = candidates[index];
                const groupName = typeof candidate === 'string'
                    ? normalizeResourceName(candidate)
                    : normalizeResourceName(candidate && (candidate.group || candidate.name || candidate.motion || candidate.key));
                const file = typeof candidate === 'object' ? this.getMotionFile(candidate) : '';
                const explicitIndex = candidate && Number.isInteger(Number(candidate.index)) ? Number(candidate.index) : null;

                if (groupName && explicitIndex !== null && typeof manager.playActionMotion === 'function') {
                    try {
                        const played = await this.withPerformanceBypass(() => manager.playActionMotion(groupName, explicitIndex));
                        if (played !== false) {
                            return true;
                        }
                    } catch (_) {}
                }

                if (file && typeof manager.playActionMotion === 'function') {
                    const runtimeRef = this.findMotionRuntimeReference(groupName, file);
                    if (runtimeRef) {
                        try {
                            const played = await this.withPerformanceBypass(() => manager.playActionMotion(runtimeRef.group, runtimeRef.index));
                            if (played !== false) {
                                return true;
                            }
                        } catch (_) {}
                    }
                }

                if (groupName && this.hasMotion(groupName, options) && typeof manager.playMotion === 'function') {
                    const played = await this.withPerformanceBypass(() => manager.playMotion(groupName));
                    if (played !== false) {
                        return true;
                    }
                }
            }
            return false;
        }

        async applyEmotion(emotion, options) {
            const manager = this.getManager();
            if (!manager || !this.getModel()) {
                return false;
            }
            const emotionName = typeof emotion === 'string'
                ? normalizeResourceName(emotion)
                : normalizeResourceName(emotion && (emotion.emotion || emotion.name || emotion.key));
            if (!emotionName) {
                return false;
            }

            const normalized = options || {};
            const parts = normalized.parts;
            const wantsExpression = !parts
                || parts === 'all'
                || parts === 'expression'
                || (Array.isArray(parts) && parts.indexOf('expression') >= 0);
            const wantsMotion = !parts
                || parts === 'all'
                || parts === 'motion'
                || (Array.isArray(parts) && parts.indexOf('motion') >= 0);

            if (wantsExpression && wantsMotion && typeof manager.setEmotion === 'function') {
                const expressionCandidates = this.collectExpressionCandidates(emotionName, normalized);
                for (let index = 0; index < expressionCandidates.length; index += 1) {
                    const candidate = expressionCandidates[index];
                    const file = typeof candidate === 'object'
                        ? normalizeResourceName(candidate.file || candidate.File || candidate.path)
                        : '';
                    const params = candidate && typeof candidate === 'object' && candidate.params ? candidate.params : null;
                    if (params && typeof params === 'object') {
                        this.mergeExpressionParamSnapshot(this.captureParams(Object.keys(params)));
                    }
                    if (file) {
                        this.mergeExpressionParamSnapshot(await this.captureExpressionParamsFromFile(file));
                    }
                }
                const applied = await this.withPerformanceBypass(() => manager.setEmotion(emotionName));
                return applied !== false;
            }

            let appliedAny = false;
            if (wantsExpression && typeof manager.playExpression === 'function') {
                const appliedExpression = await this.applyExpression(emotionName, normalized);
                appliedAny = appliedExpression !== false || appliedAny;
            }
            if (wantsMotion && typeof manager.playMotion === 'function') {
                const appliedMotion = await this.playMotion(emotionName, normalized);
                appliedAny = appliedMotion !== false || appliedAny;
            }
            return appliedAny;
        }

        stopMotion() {
            const manager = this.getManager();
            const motionManager = this.getMotionManager();
            if (manager && typeof manager.clearEmotionEffects === 'function') {
                try {
                    manager.clearEmotionEffects();
                    return true;
                } catch (_) {}
            }
            if (motionManager && typeof motionManager.stopAllMotions === 'function') {
                try {
                    motionManager.stopAllMotions();
                    return true;
                } catch (_) {}
            }
            return false;
        }

        captureExpression() {
            const manager = this.getManager();
            if (!manager) {
                return {};
            }
            const snapshot = {};
            if (hasOwn(manager, 'savedModelParameters')) {
                snapshot.savedModelParameters = cloneJsonCompatible(manager.savedModelParameters);
            }
            if (hasOwn(manager, '_shouldApplySavedParams')) {
                snapshot.shouldApplySavedParams = !!manager._shouldApplySavedParams;
            }
            if (hasOwn(manager, 'persistentExpressionParamsByName')) {
                snapshot.persistentExpressionParamsByName = cloneJsonCompatible(manager.persistentExpressionParamsByName);
            }
            if (hasOwn(manager, '_activeExpressionParamIds')) {
                snapshot.activeExpressionParamIds = cloneParamIdSnapshot(manager._activeExpressionParamIds);
            }
            return snapshot;
        }

        restoreExpression(snapshot) {
            const manager = this.getManager();
            if (!manager || !snapshot || typeof snapshot !== 'object') {
                return false;
            }
            if (hasOwn(snapshot, 'savedModelParameters')) {
                manager.savedModelParameters = cloneJsonCompatible(snapshot.savedModelParameters);
            }
            if (hasOwn(snapshot, 'shouldApplySavedParams')) {
                manager._shouldApplySavedParams = !!snapshot.shouldApplySavedParams;
            }
            if (hasOwn(snapshot, 'persistentExpressionParamsByName')) {
                manager.persistentExpressionParamsByName = cloneJsonCompatible(snapshot.persistentExpressionParamsByName);
            }
            if (hasOwn(snapshot, 'activeExpressionParamIds')) {
                manager._activeExpressionParamIds = restoreParamIdSnapshot(snapshot.activeExpressionParamIds);
            }
            return true;
        }

        collectExpressionCandidates(expression, options) {
            const candidates = [];
            const addCandidate = (candidate) => {
                if (candidate == null) {
                    return;
                }
                if (Array.isArray(candidate)) {
                    candidate.forEach(addCandidate);
                    return;
                }
                candidates.push(candidate);
            };
            const name = typeof expression === 'string'
                ? expression
                : normalizeResourceName(expression && (expression.name || expression.expression || expression.key));
            this.getProfileCandidates('expressions', name, options).forEach(addCandidate);

            const manager = this.getManager();
            const expressionFiles = manager && manager.emotionMapping && manager.emotionMapping.expressions
                ? manager.emotionMapping.expressions[name]
                : null;
            if (Array.isArray(expressionFiles)) {
                expressionFiles.forEach((file) => addCandidate({ name: name, file: file }));
            }
            if (manager && manager.fileReferences && Array.isArray(manager.fileReferences.Expressions) && name) {
                manager.fileReferences.Expressions.forEach((item) => {
                    if (item && item.File && String(item.Name || '').startsWith(name)) {
                        addCandidate({ name: item.Name || name, file: item.File });
                    }
                });
            }

            addCandidate(expression);
            return candidates;
        }

        async captureExpressionParamsFromFile(file) {
            const manager = this.getManager();
            if (!file || !manager || typeof fetch !== 'function' || typeof manager.resolveAssetPath !== 'function') {
                return {};
            }
            const candidates = [];
            const pushCandidate = (value) => {
                const normalized = String(value || '').replace(/\\/g, '/');
                if (normalized && candidates.indexOf(normalized) < 0) {
                    candidates.push(normalized);
                }
            };
            pushCandidate(file);
            const baseName = String(file).replace(/\\/g, '/').split('/').pop() || '';
            if (baseName && baseName === file) {
                pushCandidate('expressions/' + baseName);
            }
            for (let index = 0; index < candidates.length; index += 1) {
                try {
                    const response = await fetch(manager.resolveAssetPath(candidates[index]));
                    if (!response.ok) {
                        continue;
                    }
                    const data = await response.json();
                    const paramIds = Array.isArray(data.Parameters)
                        ? data.Parameters.map((param) => param && param.Id).filter(Boolean)
                        : [];
                    return this.captureParams(paramIds);
                } catch (_) {}
            }
            return {};
        }

        hasExpressionName(name) {
            const expressionName = normalizeResourceName(name);
            if (!expressionName) {
                return false;
            }
            const manager = this.getManager();
            if (!manager) {
                return false;
            }
            const mapped = manager.emotionMapping && manager.emotionMapping.expressions
                ? manager.emotionMapping.expressions[expressionName]
                : null;
            if (Array.isArray(mapped) && mapped.length > 0) {
                return true;
            }
            if (manager.fileReferences && Array.isArray(manager.fileReferences.Expressions)) {
                return manager.fileReferences.Expressions.some((item) => {
                    return item && item.File && String(item.Name || '').startsWith(expressionName);
                });
            }
            return false;
        }

        hasExpressionFile(file) {
            const expressionFile = normalizeResourceName(file);
            if (!expressionFile) {
                return false;
            }
            const manager = this.getManager();
            if (!manager) {
                return false;
            }
            if (typeof manager.isExpressionFileMissing === 'function' && manager.isExpressionFileMissing(expressionFile)) {
                return false;
            }
            if (typeof manager.resolveExpressionReferenceByFile === 'function' && manager.resolveExpressionReferenceByFile(expressionFile)) {
                return true;
            }
            return /\.exp3\.json$/i.test(expressionFile);
        }

        mergeExpressionParamSnapshot(snapshot) {
            if (!snapshot || typeof snapshot !== 'object') {
                return;
            }
            if (!this.expressionParamSnapshot) {
                this.expressionParamSnapshot = {};
            }
            Object.keys(snapshot).forEach((paramId) => {
                if (!hasOwn(this.expressionParamSnapshot, paramId)) {
                    this.expressionParamSnapshot[paramId] = snapshot[paramId];
                }
            });
        }

        async applyExpression(expression, options) {
            const manager = this.getManager();
            if (!manager) {
                return false;
            }
            await this.clearExpression();
            const candidates = this.collectExpressionCandidates(expression, options);
            for (let index = 0; index < candidates.length; index += 1) {
                const candidate = candidates[index];
                const candidateName = typeof candidate === 'string'
                    ? normalizeResourceName(candidate)
                    : normalizeResourceName(candidate && (candidate.name || candidate.expression || candidate.key));
                const file = typeof candidate === 'object'
                    ? normalizeResourceName(candidate.file || candidate.File || candidate.path)
                    : '';
                const params = candidate && typeof candidate === 'object' && candidate.params ? candidate.params : null;

                if (params && typeof params === 'object') {
                    const paramIds = Object.keys(params);
                    this.mergeExpressionParamSnapshot(this.captureParams(paramIds));
                    let wrote = false;
                    paramIds.forEach((paramId) => {
                        if (this.setParam(paramId, params[paramId])) {
                            wrote = true;
                        }
                    });
                    if (wrote) {
                        return true;
                    }
                }

                if (file && this.hasExpressionFile(file) && typeof manager.playExpression === 'function') {
                    this.mergeExpressionParamSnapshot(await this.captureExpressionParamsFromFile(file));
                    const played = await this.withPerformanceBypass(() => manager.playExpression(candidateName || file, file));
                    if (played !== false) {
                        return true;
                    }
                }

                if (candidateName && this.hasExpressionName(candidateName) && typeof manager.playExpression === 'function') {
                    const played = await this.withPerformanceBypass(() => manager.playExpression(candidateName));
                    if (played !== false) {
                        return true;
                    }
                }
            }
            return false;
        }

        async clearExpression() {
            const manager = this.getManager();
            const shouldClearManagerExpression = manager?._activeTransientExpression === true;
            if (this.expressionParamSnapshot) {
                this.restoreParams(this.expressionParamSnapshot);
                this.expressionParamSnapshot = null;
            }
            if (shouldClearManagerExpression && typeof manager.clearExpression === 'function') {
                await Promise.resolve(manager.clearExpression()).catch(() => {});
            } else if (manager && typeof manager.applyPersistentExpressionsNative === 'function') {
                await Promise.resolve(manager.applyPersistentExpressionsNative(true)).catch(() => {});
            }
            return true;
        }

        hasMotion(group, options) {
            const manager = this.getManager();
            const model = this.getModel();
            const motionManager = model && model.internalModel
                ? model.internalModel.motionManager || null
                : null;
            const candidates = this.collectMotionCandidates(group, options);

            const sources = [
                manager && manager.fileReferences && manager.fileReferences.Motions,
                manager && manager.emotionMapping && manager.emotionMapping.motions,
                motionManager && motionManager.definitions,
                motionManager && motionManager._definitions,
                motionManager && motionManager.motionGroups,
                motionManager && motionManager._motionGroups
            ].filter(Boolean);

            for (let candidateIndex = 0; candidateIndex < candidates.length; candidateIndex += 1) {
                const candidate = candidates[candidateIndex];
                const groupName = typeof candidate === 'string'
                    ? normalizeResourceName(candidate)
                    : normalizeResourceName(candidate && (candidate.group || candidate.name || candidate.motion || candidate.key));
                const file = typeof candidate === 'object' ? this.getMotionFile(candidate) : '';
                if (file && this.findMotionRuntimeReference(groupName, file)) {
                    return true;
                }
                if (!groupName) {
                    continue;
                }
                for (let index = 0; index < sources.length; index += 1) {
                    const source = sources[index];
                    const motions = source && source[groupName];
                    if (Array.isArray(motions) && motions.length > 0) {
                        return true;
                    }
                }
            }
            return false;
        }

        readParamMeta(coreModel, id) {
            const paramId = String(id || '').trim();
            if (!coreModel || !paramId || typeof coreModel.getParameterIndex !== 'function') {
                return null;
            }
            try {
                const index = coreModel.getParameterIndex(paramId);
                if (index < 0) {
                    return null;
                }
                const current = typeof coreModel.getParameterValueByIndex === 'function'
                    ? coreModel.getParameterValueByIndex(index)
                    : 0;
                let min = Number.NEGATIVE_INFINITY;
                let max = Number.POSITIVE_INFINITY;
                let defaultValue = current;
                try {
                    if (typeof coreModel.getParameterMinimumValueByIndex === 'function') {
                        min = coreModel.getParameterMinimumValueByIndex(index);
                    }
                } catch (_) {}
                try {
                    if (typeof coreModel.getParameterMaximumValueByIndex === 'function') {
                        max = coreModel.getParameterMaximumValueByIndex(index);
                    }
                } catch (_) {}
                try {
                    if (typeof coreModel.getParameterDefaultValueByIndex === 'function') {
                        defaultValue = coreModel.getParameterDefaultValueByIndex(index);
                    }
                } catch (_) {}
                if (!Number.isFinite(min)) {
                    min = paramId.indexOf('EyeBall') >= 0 ? -1 : (paramId.indexOf('Eye') >= 0 ? 0 : -30);
                }
                if (!Number.isFinite(max)) {
                    max = paramId.indexOf('EyeBall') >= 0 ? 1 : (paramId.indexOf('Eye') >= 0 ? 1 : 30);
                }
                return {
                    id: paramId,
                    index: index,
                    initial: Number.isFinite(current) ? current : defaultValue,
                    defaultValue: Number.isFinite(defaultValue) ? defaultValue : 0,
                    min: min,
                    max: max
                };
            } catch (_) {
                return null;
            }
        }

        collectPoseTimelineParamIds(step) {
            const normalized = step || {};
            const explicit = Array.isArray(normalized.paramIds) ? normalized.paramIds : [];
            const mapped = normalized.params && typeof normalized.params === 'object'
                ? Object.keys(normalized.params).map((key) => normalized.params[key])
                : [];
            return Array.from(new Set(explicit.concat(mapped)
                .map((id) => String(id || '').trim())
                .filter(Boolean)));
        }

        writePoseParam(coreModel, meta, value) {
            if (!coreModel || !meta || typeof coreModel.setParameterValueByIndex !== 'function') {
                return false;
            }
            const number = Number(value);
            if (!Number.isFinite(number)) {
                return false;
            }
            const clamped = Math.min(meta.max, Math.max(meta.min, number));
            try {
                coreModel.setParameterValueByIndex(meta.index, clamped);
                return true;
            } catch (_) {
                return false;
            }
        }

        applyPoseValues(coreModel, metas, values, weight) {
            if (!coreModel || !metas || !values || typeof values !== 'object') {
                return false;
            }
            const w = Math.max(0, Math.min(1, Number.isFinite(Number(weight)) ? Number(weight) : 1));
            let wrote = false;
            Object.keys(values).forEach((key) => {
                const meta = metas[key] || metas[String(key)];
                const value = Number(values[key]);
                if (!meta || !Number.isFinite(value)) {
                    return;
                }
                let current = meta.initial;
                try {
                    if (typeof coreModel.getParameterValueByIndex === 'function') {
                        current = coreModel.getParameterValueByIndex(meta.index);
                    }
                } catch (_) {}
                const base = Number.isFinite(current) ? current : meta.initial;
                const blended = base + (value - base) * w;
                wrote = this.writePoseParam(coreModel, meta, blended) || wrote;
            });
            return wrote;
        }

        restorePoseParams(coreModel, metas) {
            if (!coreModel || !metas) {
                return;
            }
            Object.keys(metas).forEach((key) => {
                const meta = metas[key];
                this.writePoseParam(coreModel, meta, meta.initial);
            });
        }

        async runPoseTimeline(step, options) {
            const normalized = step || {};
            const computePose = typeof normalized.computePose === 'function' ? normalized.computePose : null;
            if (!computePose) {
                return false;
            }
            const reducedMotion = options && options.reducedMotion === true;
            const durationMs = reducedMotion && Number.isFinite(Number(normalized.reducedMotionDurationMs))
                ? Math.max(0, Math.round(Number(normalized.reducedMotionDurationMs)))
                : Math.max(0, Math.round(Number(normalized.durationMs || 0)));
            const handoffMs = reducedMotion && Number.isFinite(Number(normalized.reducedHandoffMs))
                ? Math.max(0, Math.round(Number(normalized.reducedHandoffMs)))
                : Math.max(0, Math.round(Number(normalized.handoffMs || 0)));
            const readyWaitMs = reducedMotion
                ? 0
                : Math.max(0, Math.round(Number(normalized.readyWaitMs || 0)));
            const context = await this.waitForLive2DContext(readyWaitMs);
            if (!context) {
                return false;
            }

            const paramIdsByKey = normalized.params && typeof normalized.params === 'object' ? normalized.params : {};
            const metas = {};
            Object.keys(paramIdsByKey).forEach((key) => {
                const meta = this.readParamMeta(context.coreModel, paramIdsByKey[key]);
                if (meta) {
                    metas[key] = meta;
                }
            });
            this.collectPoseTimelineParamIds(normalized).forEach((id) => {
                const key = String(id || '').trim();
                if (!key || metas[key]) {
                    return;
                }
                const meta = this.readParamMeta(context.coreModel, key);
                if (meta) {
                    metas[key] = meta;
                }
            });

            const paramKeys = Object.keys(metas);
            if (!paramKeys.length) {
                return false;
            }

            const manager = context.manager;
            const source = 'avatar-performance-pose-' + (options && options.sessionId ? options.sessionId : 'session');
            const previousEyeBlinkSuspended = !!manager._suspendEyeBlinkOverride;
            let cancelFrame = null;
            let settled = false;
            let settleLoop = null;
            let usesTemporaryPoseOverride = false;
            let overrideFrameCount = 0;
            const startedAt = now();
            const finish = (result, reason) => {
                if (settled) {
                    return;
                }
                settled = true;
                if (cancelFrame) {
                    cancelFrame();
                    cancelFrame = null;
                }
                if (manager && normalized.suspendEyeBlink !== false) {
                    manager._suspendEyeBlinkOverride = previousEyeBlinkSuspended;
                }
                if (manager && typeof manager.clearTemporaryPoseOverride === 'function') {
                    try {
                        manager.clearTemporaryPoseOverride(source);
                    } catch (_) {}
                }
                if (normalized.restoreOnComplete !== false) {
                    this.restorePoseParams(context.coreModel, metas);
                }
                if (typeof normalized.onResult === 'function') {
                    try {
                        normalized.onResult({
                            result: result || 'played',
                            reason: reason || '',
                            paramCount: paramKeys.length
                        });
                    } catch (_) {}
                }
                // finish 可能由 override 回调（模型 update 注入点）触发，此时上面已
                // cancel 掉待执行的 tick，必须在这里直接唤醒等待循环，否则 promise 悬挂
                if (settleLoop) {
                    settleLoop();
                }
            };
            const applyFrame = () => {
                if (settled) {
                    return;
                }
                if (context.model !== this.getModel() || context.model.destroyed) {
                    finish('cancelled', 'model_changed');
                    return;
                }
                const elapsed = Math.max(0, now() - startedAt);
                const handoffStart = Math.max(0, durationMs - handoffMs);
                let progress = handoffStart > 0 ? Math.min(1, elapsed / handoffStart) : 1;
                let weight = 1;
                if (elapsed >= handoffStart) {
                    const handoffProgress = handoffMs > 0 ? Math.min(1, (elapsed - handoffStart) / handoffMs) : 1;
                    weight = 1 - easeOutCubic(handoffProgress);
                }
                if (reducedMotion) {
                    progress = 1;
                }
                let values = null;
                try {
                    values = computePose(progress, {
                        elapsed: elapsed,
                        reducedMotion: reducedMotion,
                        durationMs: durationMs,
                        handoffMs: handoffMs,
                        paramIds: paramIdsByKey
                    });
                } catch (error) {
                    finish('failed', 'compute_exception');
                    throw error;
                }
                this.applyPoseValues(context.coreModel, metas, values, weight);
                if (elapsed >= durationMs) {
                    finish('played', '');
                }
            };

            try {
                if (manager && normalized.suspendEyeBlink !== false) {
                    manager._suspendEyeBlinkOverride = true;
                }
                if (manager && typeof manager.setTemporaryPoseOverride === 'function') {
                    usesTemporaryPoseOverride = manager.setTemporaryPoseOverride(source, (coreModel) => {
                        if (coreModel === context.coreModel) {
                            overrideFrameCount += 1;
                            applyFrame();
                        }
                    }) === true;
                }
                applyFrame();
                if (typeof normalized.onInitialPose === 'function') {
                    try {
                        normalized.onInitialPose({
                            paramCount: paramKeys.length
                        });
                    } catch (_) {}
                }
                if (durationMs <= 0) {
                    finish('played', '');
                    return true;
                }
                await new Promise((resolve) => {
                    settleLoop = resolve;
                    // override 注册成功不代表在被驱动：coreModel.update 包装器
                    // （installMouthOverride）可能尚未安装或已因异常自卸载。心跳按
                    // 时间判定：超过阈值未见 override 回调推进才退回 rAF 驱动——不能
                    // 数 tick，高刷屏（120Hz+）配 30fps ticker 治理时一个模型帧间隔
                    // 就有 4+ 个 rAF。150ms 覆盖任何合法 ticker 配置的帧间隔。
                    // 心跳起点视为已停滞：回调首次推进前由 rAF 驱动，否则 hook 注册
                    // 成功但失活时前 150ms 无人写参，durationMs<=150 的短时间线会
                    // 只播初始帧就被督导 finish('played')。
                    const OVERRIDE_STALL_FALLBACK_MS = 150;
                    let lastOverrideFrameCount = overrideFrameCount;
                    let lastOverrideAdvanceAt = now() - OVERRIDE_STALL_FALLBACK_MS;
                    const tick = () => {
                        if (settled) {
                            resolve();
                            return;
                        }
                        try {
                            let overrideDriving = false;
                            if (usesTemporaryPoseOverride) {
                                if (overrideFrameCount !== lastOverrideFrameCount) {
                                    lastOverrideFrameCount = overrideFrameCount;
                                    lastOverrideAdvanceAt = now();
                                    overrideDriving = true;
                                } else {
                                    overrideDriving = (now() - lastOverrideAdvanceAt) < OVERRIDE_STALL_FALLBACK_MS;
                                }
                            }
                            if (overrideDriving) {
                                // override 回调已在模型 update 注入点逐帧 applyFrame，
                                // rAF 只做完成/取消督导；重复 applyFrame 会让 handoff 期
                                // 同一帧双重混合（实际权重变成 1-(1-w)^2）偏离设计曲线。
                                if (context.model !== this.getModel() || context.model.destroyed) {
                                    finish('cancelled', 'model_changed');
                                } else if (Math.max(0, now() - startedAt) >= durationMs) {
                                    finish('played', '');
                                }
                            } else {
                                applyFrame();
                            }
                        } catch (error) {
                            this.warn('driver pose timeline frame failed', error);
                            resolve();
                            return;
                        }
                        if (!settled) {
                            cancelFrame = scheduleStageFrame(tick);
                        }
                    };
                    cancelFrame = scheduleStageFrame(tick);
                });
                return true;
            } catch (error) {
                finish('failed', 'exception');
                this.warn('driver pose timeline failed', error);
                return false;
            }
        }

        captureParams(ids) {
            const coreModel = this.getCoreModel();
            const snapshot = {};
            if (!coreModel || !Array.isArray(ids)) {
                return snapshot;
            }
            ids.forEach((id) => {
                const paramId = String(id || '');
                if (!paramId || typeof coreModel.getParameterIndex !== 'function') {
                    return;
                }
                try {
                    const index = coreModel.getParameterIndex(paramId);
                    if (index >= 0 && typeof coreModel.getParameterValueByIndex === 'function') {
                        snapshot[paramId] = coreModel.getParameterValueByIndex(index);
                    }
                } catch (_) {}
            });
            return snapshot;
        }

        restoreParams(snapshot) {
            if (!snapshot || typeof snapshot !== 'object') {
                return;
            }
            Object.keys(snapshot).forEach((paramId) => {
                this.setParam(paramId, snapshot[paramId]);
            });
        }

        resolvePoint(target) {
            if (!target) {
                return null;
            }
            if (typeof target.getBoundingClientRect === 'function') {
                const rect = target.getBoundingClientRect();
                if (!rect || rect.width <= 0 || rect.height <= 0) {
                    return null;
                }
                return {
                    x: rect.left + rect.width / 2,
                    y: rect.top + rect.height / 2
                };
            }
            if (
                Number.isFinite(Number(target.left))
                && Number.isFinite(Number(target.top))
                && Number.isFinite(Number(target.width))
                && Number.isFinite(Number(target.height))
            ) {
                return {
                    x: Number(target.left) + Number(target.width) / 2,
                    y: Number(target.top) + Number(target.height) / 2
                };
            }
            if (Number.isFinite(Number(target.x)) && Number.isFinite(Number(target.y))) {
                return {
                    x: Number(target.x),
                    y: Number(target.y)
                };
            }
            if (Number.isFinite(Number(target.clientX)) && Number.isFinite(Number(target.clientY))) {
                return {
                    x: Number(target.clientX),
                    y: Number(target.clientY)
                };
            }
            return null;
        }

        resolveLookAtValues(target, options) {
            const point = this.resolvePoint(target);
            if (!point) {
                return null;
            }
            const container = this.getContainer();
            const rect = container && typeof container.getBoundingClientRect === 'function'
                ? container.getBoundingClientRect()
                : null;
            const origin = options && options.origin && Number.isFinite(Number(options.origin.x)) && Number.isFinite(Number(options.origin.y))
                ? {
                    x: Number(options.origin.x),
                    y: Number(options.origin.y)
                }
                : (rect && rect.width > 0 && rect.height > 0
                    ? {
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2
                    }
                    : {
                        x: (window.innerWidth || 1) / 2,
                        y: (window.innerHeight || 1) / 2
                    });
            const normalizeX = Math.max(120, (window.innerWidth || 1) * 0.45);
            const normalizeY = Math.max(120, (window.innerHeight || 1) * 0.45);
            const clamp = function (value, min, max) {
                return Math.max(min, Math.min(max, value));
            };
            const normX = clamp((point.x - origin.x) / normalizeX, -1, 1);
            const normY = clamp((origin.y - point.y) / normalizeY, -1, 1);
            const maxAngleX = Number.isFinite(Number(options.maxAngleX)) ? Number(options.maxAngleX) : 12;
            const maxAngleY = Number.isFinite(Number(options.maxAngleY)) ? Number(options.maxAngleY) : 8;
            const maxEyeX = Number.isFinite(Number(options.maxEyeX)) ? Number(options.maxEyeX) : 0.42;
            const maxEyeY = Number.isFinite(Number(options.maxEyeY)) ? Number(options.maxEyeY) : 0.32;
            const headWeight = Number.isFinite(Number(options.headWeight)) ? Number(options.headWeight) : 1;
            const eyeWeight = Number.isFinite(Number(options.eyeWeight)) ? Number(options.eyeWeight) : 1;
            return {
                angleX: normX * maxAngleX * headWeight,
                angleY: normY * maxAngleY * headWeight,
                eyeX: normX * maxEyeX * eyeWeight,
                eyeY: normY * maxEyeY * eyeWeight
            };
        }

        applyLookAtValues(coreModel, values) {
            if (!coreModel || !values) {
                return false;
            }
            const params = this.lookAtParams || {};
            const writes = [
                [params.angleX, values.angleX],
                [params.angleY, values.angleY],
                [params.eyeX, values.eyeX],
                [params.eyeY, values.eyeY]
            ];
            let wrote = false;
            writes.forEach((entry) => {
                const paramId = entry[0];
                const value = entry[1];
                if (!paramId || !Number.isFinite(Number(value))) {
                    return;
                }
                try {
                    if (typeof coreModel.setParameterValueById === 'function') {
                        coreModel.setParameterValueById(paramId, Number(value));
                        wrote = true;
                        return;
                    }
                    if (typeof coreModel.getParameterIndex === 'function' && typeof coreModel.setParameterValueByIndex === 'function') {
                        const index = coreModel.getParameterIndex(paramId);
                        if (index >= 0) {
                            coreModel.setParameterValueByIndex(index, Number(value));
                            wrote = true;
                        }
                    }
                } catch (_) {}
            });
            return wrote;
        }

        resolveParamId(id) {
            return String(id || '');
        }

        setParam(id, value) {
            const coreModel = this.getCoreModel();
            const paramId = this.resolveParamId(id);
            if (!coreModel || !paramId) {
                return false;
            }
            try {
                if (typeof coreModel.getParameterIndex === 'function') {
                    const index = coreModel.getParameterIndex(paramId);
                    if (index < 0) {
                        return false;
                    }
                    if (typeof coreModel.setParameterValueByIndex === 'function') {
                        coreModel.setParameterValueByIndex(index, Number(value));
                        return true;
                    }
                }
                if (typeof coreModel.setParameterValueById === 'function') {
                    coreModel.setParameterValueById(paramId, Number(value));
                    return true;
                }
            } catch (_) {}
            return false;
        }

        lookAt(target, options, session) {
            const normalized = options || {};
            if (session && this.ownerSessionId && session.id !== this.ownerSessionId) {
                return false;
            }
            const values = this.resolveLookAtValues(target, normalized);
            if (!values) {
                return false;
            }

            const sessionId = session && session.id ? session.id : (normalized.sessionId || this.ownerSessionId || '');
            const params = this.lookAtParams || {};
            const paramIds = [params.angleX, params.angleY, params.eyeX, params.eyeY].filter(Boolean);
            if (!this.lookAtSnapshot) {
                this.lookAtSnapshot = this.captureParams(paramIds);
            }
            this.lookAtSessionId = sessionId;
            this.lookAtSource = 'avatar-performance-look-at-' + (sessionId || 'session');

            const manager = this.getManager();
            if (manager && typeof manager.setTemporaryPoseOverride === 'function') {
                const source = this.lookAtSource;
                manager.setTemporaryPoseOverride(source, (coreModel) => {
                    this.applyLookAtValues(coreModel, values);
                });
                return true;
            }

            return this.applyLookAtValues(this.getCoreModel(), values);
        }

        clearLookAt(options) {
            const normalized = options || {};
            if (normalized.sessionId && this.lookAtSessionId && normalized.sessionId !== this.lookAtSessionId) {
                return false;
            }
            const manager = this.getManager();
            if (manager && typeof manager.clearTemporaryPoseOverride === 'function' && this.lookAtSource) {
                try {
                    manager.clearTemporaryPoseOverride(this.lookAtSource);
                } catch (_) {}
            }
            if (this.lookAtSnapshot) {
                this.restoreParams(this.lookAtSnapshot);
            }
            this.lookAtSnapshot = null;
            this.lookAtSource = '';
            this.lookAtSessionId = '';
            return true;
        }
    }

    const legacyApi = {
        create: function (options) {
            return new AvatarPerformanceStage(options || {});
        },
        createLive2DDriver: function (options) {
            return new Live2DAvatarPerformanceDriver(options || {});
        },
        createLive2DStage: function (options) {
            const normalized = options || {};
            const driverOptions = Object.assign({}, normalized.driverOptions || {}, {
                profile: normalized.profile || (normalized.driverOptions && normalized.driverOptions.profile) || {}
            });
            const driver = normalized.driver || new Live2DAvatarPerformanceDriver(driverOptions);
            return new AvatarPerformanceStage(Object.assign({}, normalized, {
                driver: driver
            }));
        },
        AvatarPerformanceStage: AvatarPerformanceStage,
        Live2DAvatarPerformanceDriver: Live2DAvatarPerformanceDriver,
        AvatarPerformanceCoordinator: AvatarPerformanceCoordinator
    };

    window.AvatarPerformanceStage = legacyApi;

    const defaultCoordinator = new AvatarPerformanceCoordinator();

    window.AvatarPerformance = {
        createStage: legacyApi.create,
        createCoordinator: function (options) {
            return new AvatarPerformanceCoordinator(options || {});
        },
        createLive2DDriver: legacyApi.createLive2DDriver,
        createLive2DPerformance: function (options) {
            return legacyApi.createLive2DStage(options || {});
        },
        createNoopDriver: createNoopDriver,
        createNoopCoordinator: createNoopCoordinator,
        getDefaultCoordinator: function () {
            return defaultCoordinator;
        },
        isCapabilityLocked: function (avatarId, capability) {
            return defaultCoordinator.isCapabilityLocked(avatarId, capability);
        },
        getLockedCapabilities: function (avatarId) {
            return defaultCoordinator.getLockedCapabilities(avatarId);
        },
        contracts: CONTRACTS
    };
})();
