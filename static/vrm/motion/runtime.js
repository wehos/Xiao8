(function () {
    'use strict';

    // A cold external hold acquires ownership before the catalog is ready so
    // the official idle scheduler cannot race the jukebox. Failed startup must
    // hand ownership back after the last external owner releases it.
    window.__nekoMotionOwnsVrmPlayback = false;

    const SEMANTICS_URL = '/static/vrm/motion/semantics.json';
    const FETCH_TIMEOUT_MS = 10000;
    const POLL_INTERVAL_MS = 120;
    const BUFFER_SEAL_GRACE_MS = 1200;
    const TURN_END_GRACE_MS = 240;
    const EMOTION_READY_WAIT_MS = 900;
    const HISTORY_LIMIT = 80;
    const FINISHED_TURN_LIMIT = 32;
    const EMOTION_CONTEXT_WINDOW_MS = 45000;
    const EMOTION_EVIDENCE_DEBOUNCE_MS = 700;
    const EMOTION_PULSE_BASE_MS = 3000;
    const EMOTION_PULSE_STEP_MS = 1200;
    const EMOTION_PULSE_MAX_MS = 7200;
    const EMOTION_BODY_INTENT = Object.freeze({
        happy: 'happy',
        excited: 'excited',
        surprised: 'surprise',
        sad: 'sad',
        cry: 'cry',
        angry: 'angry',
        shy: 'shy',
        fearful: 'overwhelm',
        disgusted: 'dismiss',
        tired: 'yawn'
    });
    const PERSONA_PROFILES = Object.freeze({
        classic_genki: Object.freeze({ energy: 0.82, restraint: 0.25, warmth: 0.88 }),
        tsundere_helper: Object.freeze({ energy: 0.62, restraint: 0.55, warmth: 0.55 }),
        elegant_butler: Object.freeze({ energy: 0.30, restraint: 0.82, warmth: 0.74 })
    });
    const PERSONA_NAME_TO_ID = Object.freeze({
        '经典元气猫娘': 'classic_genki',
        '傲娇毒舌小猫': 'tsundere_helper',
        '优雅全能管家': 'elegant_butler'
    });

    let core = null;
    let player = null;
    let activeTurn = null;
    let lastFinishedTurn = null;
    const finishedTurnIds = new Set();
    const finishedTurnOrder = [];
    let bridgedText = '';
    let latestOfficialEmotion = '';
    let casualRepliesSinceTalk = 0;
    let lastCasualTalkAt = 0;
    let selectedMode = configuredMode();
    let readyPromise = null;
    let runtimeReady = false;
    // 外部播放可能在冷启动初始化完成前到达。先同步记账，让调用方无需等待
    // semantics/persona/idle 加载；player 一创建就补记，并跳过初始化待机。
    const externalPlaybackOwners = new Map();
    let activeCharacterProfile = null;
    let ownsPlayback = false;
    const emotionState = {
        value: 'neutral',
        since: 0,
        lastEvidenceAt: 0,
        expiresAt: 0,
        neutralStreak: 0,
        continuationCount: 0,
        pulseUntil: 0,
        pulseDurationMs: 0,
        lastAppliedAt: 0,
        applyCount: 0,
        source: null,
        expression: null
    };
    const history = [];
    const metrics = {
        turns: 0, closedStages: 0, plans: 0, noMotion: 0,
        modelCalls: 0, motionTokens: 0, rawDialoguePersisted: false,
        nonVrmTurns: 0, deferredUntilVrmReady: 0,
        bufferRecoveredTurns: 0, conversationalFallbacks: 0,
        bridgeMessages: 0, duplicateStartsIgnored: 0,
        coalescedTurnEnds: 0, casualTalkSkipped: 0,
        casualTalkSuppressedByEmotion: 0, processingFailures: 0
    };

    async function fetchWithTimeout(url, options, consumeResponse) {
        const controller = typeof AbortController === 'function' ? new AbortController() : null;
        const requestOptions = Object.assign({}, options || {});
        let timeoutId = null;
        if (controller && !requestOptions.signal) {
            requestOptions.signal = controller.signal;
            timeoutId = setTimeout(function () { controller.abort(); }, FETCH_TIMEOUT_MS);
        }
        try {
            const response = await fetch(url, requestOptions);
            return consumeResponse ? await consumeResponse(response) : response;
        } finally {
            if (timeoutId !== null) clearTimeout(timeoutId);
        }
    }

    function configuredMode() {
        const config = window.lanlan_config || {};
        const modelType = String(config.model_type || '').toLowerCase();
        const subType = String(config.live3d_sub_type || '').toLowerCase();
        if (modelType === 'vrm') return 'vrm';
        if (modelType === 'live3d' && subType !== 'mmd') return 'vrm';
        return modelType || 'live2d';
    }

    function stopOfficialIdleRotation() {
        if (typeof window._stopVrmIdleRotation === 'function') {
            window._stopVrmIdleRotation();
            return true;
        }
        return false;
    }

    function acquirePlaybackOwnership() {
        if (ownsPlayback) {
            window.__nekoMotionOwnsVrmPlayback = true;
            return false;
        }
        ownsPlayback = true;
        window.__nekoMotionOwnsVrmPlayback = true;
        stopOfficialIdleRotation();
        return true;
    }

    function releasePlaybackOwnership() {
        const hadOwnership = ownsPlayback;
        ownsPlayback = false;
        window.__nekoMotionOwnsVrmPlayback = false;
        if (!hadOwnership) return false;
        const idleAnimations = configuredRestAnimations();
        if (vrmReady() && Array.isArray(idleAnimations) && idleAnimations.length
            && typeof window._startVrmIdleRotation === 'function') {
            window._startVrmIdleRotation(idleAnimations);
        }
        return true;
    }

    function configuredRestAnimations() {
        const configured = window.lanlan_config || {};
        const keys = [
            'vrmIdleAnimations', 'vrm_idle_animations', 'idleAnimations', 'idle_animation',
            'vrmIdleAnimation', 'vrm_idle_animation', 'idleAnimation'
        ];
        for (const key of keys) {
            if (!Object.prototype.hasOwnProperty.call(configured, key)) continue;
            const value = configured[key];
            if (Array.isArray(value)) return value;
            if (typeof value === 'string') return value ? [value] : [];
        }
        return [];
    }

    function syncSavedRestAnimations() {
        if (!player || typeof player.setSavedRestAnimations !== 'function') return 0;
        return player.setSavedRestAnimations(configuredRestAnimations());
    }

    function currentLocale() {
        return String(window.i18next && window.i18next.language
            || document.documentElement.lang || navigator.language || 'zh-CN');
    }

    function refreshMode() {
        const configured = configuredMode();
        if (configured) selectedMode = configured;
        return selectedMode;
    }

    function vrmReady() {
        refreshMode();
        return selectedMode === 'vrm' && window.vrmManager && window.vrmManager.currentModel
            && window.vrmManager.currentModel.vrm && typeof window.vrmManager.playVRMAAnimation === 'function';
    }

    function inferredCharacterProfile() {
        const config = window.lanlan_config || {};
        const source = [config.personality, config.personality_prompt, config.character_prompt, config.prompt]
            .filter(function (value) { return typeof value === 'string'; }).join(' ').slice(0, 12000);
        const energetic = /元气|活泼|热情|外向|俏皮|energetic|lively|playful|outgoing/iu.test(source);
        const restrained = /克制|沉稳|冷静|优雅|安静|内向|reserved|composed|calm|quiet/iu.test(source);
        const gentle = /温柔|体贴|柔和|害羞|gentle|tender|shy|soft/iu.test(source);
        return {
            energy: energetic ? 0.78 : restrained ? 0.32 : 0.5,
            restraint: restrained ? 0.76 : energetic ? 0.34 : 0.5,
            warmth: gentle ? 0.76 : 0.5,
            key: String(config.lanlan_name || config.name || 'default').slice(0, 80),
            rawProfileStored: false
        };
    }

    function characterProfile() {
        return activeCharacterProfile || inferredCharacterProfile();
    }

    async function resolveCharacterProfile() {
        const inferred = inferredCharacterProfile();
        const config = window.lanlan_config || {};
        const name = String(config.lanlan_name || config.name || '').trim();
        let preset = '';
        if (name) {
            try {
                const payload = await fetchWithTimeout('/api/characters/character/' + encodeURIComponent(name) + '/persona-selection', {
                    cache: 'no-store'
                }, async function (response) {
                    return response.ok ? response.json() : null;
                });
                if (payload) {
                    const selection = payload && payload.selection || {};
                    preset = String(selection.preset_id || '').trim();
                    if (!preset) {
                        const prototypeName = String(selection.profile && selection.profile['性格原型'] || '').trim();
                        preset = PERSONA_NAME_TO_ID[prototypeName] || '';
                    }
                }
            } catch (error) {
                console.warn('[NekoMotion] persona selection unavailable; using local profile hints:', error);
            }
        }
        const exact = PERSONA_PROFILES[preset];
        activeCharacterProfile = Object.assign({}, inferred, exact || {}, {
            preset: exact ? preset : '',
            key: inferred.key + ':' + (exact ? preset : 'custom'),
            rawProfileStored: false
        });
        if (player) player.setProfile(activeCharacterProfile);
        return activeCharacterProfile;
    }

    function normalizeEmotion(value) {
        const normalized = String(value || '').trim().toLowerCase();
        const aliases = {
            joy: 'happy', happiness: 'happy', content: 'relaxed', calm: 'relaxed',
            excitement: 'excited', delighted: 'excited',
            surprise: 'surprised', shock: 'surprised', sadness: 'sad', sorrow: 'sad',
            crying: 'cry', tearful: 'cry', embarrassment: 'shy', embarrassed: 'shy',
            anger: 'angry', mad: 'angry', fear: 'fearful', scared: 'fearful',
            disgust: 'disgusted', sleepy: 'tired', drowsy: 'tired',
            none: 'neutral', normal: 'neutral'
        };
        return aliases[normalized] || normalized || 'neutral';
    }

    function currentVrmExpression() {
        const manager = window.vrmManager;
        const expression = manager && manager.currentModel && manager.currentModel.vrm && manager.expression;
        return expression && typeof expression.setMood === 'function' ? expression : null;
    }

    function releaseExpressionControl(applyNeutral) {
        const expression = emotionState.expression;
        if (!expression) return;
        if (applyNeutral && typeof expression.setMood === 'function') expression.setMood('neutral');
        emotionState.expression = null;
        emotionState.pulseUntil = 0;
    }

    function applyPersistentEmotion(reason) {
        if (emotionState.value === 'neutral' || refreshMode() !== 'vrm') return false;
        const expression = currentVrmExpression();
        if (!expression) return false;
        if (expression.moodMap && !expression.moodMap[emotionState.value]) return false;
        if (emotionState.expression !== expression) {
            releaseExpressionControl(false);
            emotionState.expression = expression;
        }
        const duration = Math.max(500, Number(emotionState.pulseDurationMs) || EMOTION_PULSE_BASE_MS);
        const previousDelay = Number(expression.neutralReturnDelay) || EMOTION_PULSE_BASE_MS;
        expression.autoReturnToNeutral = true;
        expression.neutralReturnDelay = duration;
        expression.setMood(emotionState.value);
        expression.neutralReturnDelay = previousDelay;
        emotionState.lastAppliedAt = Date.now();
        emotionState.pulseUntil = emotionState.lastAppliedAt + duration;
        emotionState.applyCount += 1;
        console.info('[NekoMotion] emotion pulse:', emotionState.value, duration, reason || 'evidence');
        return true;
    }

    function clearPersistentEmotion(reason) {
        if (emotionState.value === 'neutral' && !emotionState.expression) return;
        emotionState.value = 'neutral';
        emotionState.since = 0;
        emotionState.lastEvidenceAt = Date.now();
        emotionState.expiresAt = 0;
        emotionState.neutralStreak = 0;
        emotionState.continuationCount = 0;
        emotionState.pulseDurationMs = 0;
        emotionState.source = reason || 'clear';
        releaseExpressionControl(true);
        console.info('[NekoMotion] persistent emotion cleared:', emotionState.source);
    }

    function resetCharacterMotionState() {
        activeCharacterProfile = null;
        latestOfficialEmotion = '';
        clearPersistentEmotion('model_switched');
        emotionState.value = 'neutral';
        emotionState.since = 0;
        emotionState.lastEvidenceAt = 0;
        emotionState.expiresAt = 0;
        emotionState.neutralStreak = 0;
        emotionState.continuationCount = 0;
        emotionState.pulseUntil = 0;
        emotionState.pulseDurationMs = 0;
        emotionState.lastAppliedAt = 0;
        emotionState.applyCount = 0;
        emotionState.source = 'model_switched';
    }

    function updatePersistentEmotion(value, source) {
        const emotion = normalizeEmotion(value);
        const now = Date.now();
        if (emotion === 'neutral') {
            if (emotionState.value === 'neutral') return { emotion: emotion, changed: false };
            emotionState.neutralStreak += 1;
            // A neutral result does not immediately erase recent emotional
            // context, but it must stop the action layer from reviving the old
            // face after the official expression has started returning.
            emotionState.pulseUntil = 0;
            if (emotionState.neutralStreak >= 2 || now >= emotionState.expiresAt) {
                clearPersistentEmotion('confirmed_neutral');
            }
            return { emotion: emotion, changed: false };
        }
        const changed = emotionState.value !== emotion;
        const sameEvidenceWindow = !changed
            && now - emotionState.lastEvidenceAt <= EMOTION_CONTEXT_WINDOW_MS;
        if (sameEvidenceWindow && now - emotionState.lastEvidenceAt < EMOTION_EVIDENCE_DEBOUNCE_MS) {
            return { emotion: emotion, changed: false, renewed: false, debounced: true };
        }
        if (changed) {
            releaseExpressionControl(true);
            emotionState.value = emotion;
            emotionState.since = now;
        }
        emotionState.continuationCount = sameEvidenceWindow
            ? Math.min(5, emotionState.continuationCount + 1) : 1;
        emotionState.pulseDurationMs = Math.min(
            EMOTION_PULSE_MAX_MS,
            EMOTION_PULSE_BASE_MS + (emotionState.continuationCount - 1) * EMOTION_PULSE_STEP_MS
        );
        emotionState.lastEvidenceAt = now;
        emotionState.expiresAt = now + EMOTION_CONTEXT_WINDOW_MS;
        emotionState.neutralStreak = 0;
        emotionState.source = source || 'official';
        applyPersistentEmotion(sameEvidenceWindow ? 'continued_evidence' : 'new_evidence');
        return {
            emotion: emotion,
            changed: changed,
            renewed: sameEvidenceWindow,
            pulseDurationMs: emotionState.pulseDurationMs
        };
    }

    function maintainPersistentEmotion() {
        if (emotionState.value === 'neutral') return;
        if (Date.now() >= emotionState.expiresAt) {
            clearPersistentEmotion('expired');
            return;
        }
        const expression = currentVrmExpression();
        const pulseActive = Date.now() < emotionState.pulseUntil;
        if (expression && pulseActive && (
            expression !== emotionState.expression
            || normalizeEmotion(expression.currentMood) !== emotionState.value
        )) {
            applyPersistentEmotion('restore_active_pulse');
        }
    }

    function decoratePlan(plan) {
        const profile = characterProfile();
        return plan.map(function (decision) {
            const item = Object.assign({}, decision);
            const emotion = normalizeEmotion(item.emotion || emotionState.value || latestOfficialEmotion);
            if (!item.intensityExplicit) {
                if (['shy', 'sad', 'fearful'].includes(emotion)
                    && ['talk', 'look', 'nod', 'wave', 'plead'].includes(item.intent)) {
                    item.intensity = 1;
                } else if (emotion === 'angry'
                    && ['argue', 'shake', 'dismiss', 'point'].includes(item.intent)) {
                    item.intensity = 3;
                } else if (emotion === 'happy'
                    && ['wave', 'clap', 'victory', 'spin', 'dance'].includes(item.intent)) {
                    item.intensity = profile.restraint > 0.7 ? 2 : 3;
                } else if (profile.restraint > 0.7
                    && ['talk', 'happy', 'look', 'nod', 'wave'].includes(item.intent)) {
                    item.intensity = 1;
                } else if (profile.energy > 0.7
                    && ['talk', 'happy', 'excited', 'dance', 'argue', 'spin'].includes(item.intent)) {
                    item.intensity = 3;
                }
            }
            item.preferredStyles = profile.restraint > 0.7
                ? ['thoughtful', 'neutral', 'breathing', 'composed', 'formal']
                : profile.energy > 0.7
                    ? ['positive', 'firm', 'stylized', 'lively']
                    : ['neutral', 'thoughtful', 'relaxed'];
            return item;
        });
    }

    async function initialize() {
        if (readyPromise) return readyPromise;
        readyPromise = (async function () {
            const semantics = await fetchWithTimeout(SEMANTICS_URL, { cache: 'no-store' }, async function (response) {
                if (!response.ok) throw new Error('Motion semantics HTTP ' + response.status);
                return response.json();
            });
            core = new window.NekoMotionCore(semantics);
            player = new window.NekoMotionPlayer();
            externalPlaybackOwners.forEach(function (token, owner) {
                player.holdExternalPlayback(owner, { token: token });
            });
            await player.load();
            syncSavedRestAnimations();
            core.registerActionCards(player.assets);
            await resolveCharacterProfile();
            if (refreshMode() !== 'vrm') {
                releasePlaybackOwnership();
                runtimeReady = true;
                return true;
            }
            acquirePlaybackOwnership();
            if (vrmReady() && externalPlaybackOwners.size === 0) {
                await player.enterRest({
                    profile: characterProfile(),
                    seed: 'initialize',
                    force: true,
                    reselect: true
                });
            }
            runtimeReady = true;
            console.info('[NekoMotion] runtime ready; one playback owner, no prompt injection and no model call');
            return true;
        })().catch(function (error) {
            console.error('[NekoMotion] initialization failed:', error);
            runtimeReady = false;
            if (player && typeof player.cancel === 'function') {
                player.cancel('initialization_failed', { resume: false });
            }
            core = null;
            player = null;
            if (externalPlaybackOwners.size === 0) releasePlaybackOwnership();
            readyPromise = null;
            return false;
        });
        return readyPromise;
    }

    function remember(result) {
        history.push({
            at: Date.now(), locale: result.locale,
            intents: result.plan.map(function (item) { return item.intent; }),
            clauseCount: result.clauses.length,
            modelUsed: false, tokenTotal: 0
        });
        if (history.length > HISTORY_LIMIT) history.splice(0, history.length - HISTORY_LIMIT);
    }

    function isCurrentTurn(turn) {
        return !!turn && activeTurn === turn;
    }

    function queueTurnTask(turn, label, task) {
        if (!turn) return Promise.resolve(false);
        turn.processing = Promise.resolve(turn.processing).catch(function (error) {
            metrics.processingFailures += 1;
            console.warn('[NekoMotion] recovered rejected turn task:', label, error);
        }).then(task).catch(function (error) {
            metrics.processingFailures += 1;
            console.warn('[NekoMotion] turn task failed:', label, error);
            return false;
        });
        return turn.processing;
    }

    async function processStage(stage, turn) {
        if (!core || !player || turn && (turn.structured || !isCurrentTurn(turn))) return false;
        if (!vrmReady()) {
            if (turn && isCurrentTurn(turn)) turn.deferredUntilVrmReady = true;
            return false;
        }
        const result = core.analyze(stage.raw, {
            locale: currentLocale(),
            officialEmotion: turn && turn.officialEmotion || '',
            profilePreset: characterProfile().preset || '',
            speechMode: true,
            stageDirection: true
        });
        const plan = decoratePlan(result.plan);
        if (turn) {
            turn.explicitPlanCount += plan.length;
            plan.forEach(function (item) {
                if (!turn.explicitIntents.includes(item.intent)) turn.explicitIntents.push(item.intent);
            });
        }
        metrics.closedStages += 1;
        remember(Object.assign({}, result, { plan: plan }));
        window.dispatchEvent(new CustomEvent('neko-motion-decision', {
            detail: {
                stageId: stage.id,
                canonicalZh: result.canonicalZh,
                plan: plan.map(function (item) {
                    return { intent: item.intent, intensity: item.intensity, style: item.style, evidence: item.evidence };
                }),
                modelUsed: false,
                tokenUsage: result.tokenUsage,
                rawDialoguePersisted: false
            }
        }));
        if (!plan.length) {
            metrics.noMotion += 1;
            console.info('[NekoMotion] closed stage produced no supported motion');
            return true;
        }
        const semanticEmotion = plan.map(function (item) { return item.emotion; })
            .find(function (value) { return value && normalizeEmotion(value) !== 'neutral'; });
        if (semanticEmotion) updatePersistentEmotion(semanticEmotion, 'stage_semantic');
        metrics.plans += 1;
        if (turn && !isCurrentTurn(turn)) return false;
        console.info('[NekoMotion] playing', plan.map(function (item) { return item.intent; }).join(','));
        const context = { seed: turn && turn.id + ':' + stage.id };
        let played;
        if (turn && !turn.playerStarted) {
            turn.playerStarted = true;
            played = await player.playPlan(plan, context);
            if (!played) turn.playerStarted = false;
        } else {
            played = await player.enqueuePlan(plan, context);
        }
        if (!played) {
            if (turn && !isCurrentTurn(turn)) return false;
            if (!vrmReady()) {
                if (turn) turn.deferredUntilVrmReady = true;
                return false;
            }
            console.info('[NekoMotion] supported stage was intentionally skipped for the current posture');
            return true;
        }
        return true;
    }

    async function processSpeechFallback(turn, casualOnly) {
        casualOnly = casualOnly === true;
        if (!turn || !core || !player || !isCurrentTurn(turn)) return;
        if (turn.structured) {
            turn.speechProcessed = true;
            turn.casualTalkPending = false;
            return;
        }
        if (!vrmReady()) {
            turn.deferredUntilVrmReady = true;
            return;
        }
        if (casualOnly && (!turn.casualTalkPending || turn.casualTalkFinalized)) return;
        if (!casualOnly && turn.speechProcessed) return;
        if (!casualOnly) turn.speechProcessed = true;
        // Closed stage directions are handled as soon as their bracket closes;
        // visible prose is still meaningful and is analyzed at turn end. Only an
        // intent already played from brackets is removed, so “(yawns) I will lie
        // down to sleep” can progress from yawn -> sleep without replaying either.
        const text = String(turn.capturedText || '');
        if (!text.trim()) return;
        const result = casualOnly ? {
            raw: text,
            locale: currentLocale(),
            canonicalZh: '',
            clauses: [],
            plan: [],
            tokenUsage: { input: 0, output: 0, cached: 0, total: 0 },
            modelUsed: false,
            source: 'assistant:late-conversation-fallback'
        } : core.analyzeSpeech(text, {
            locale: currentLocale(),
            officialEmotion: turn.officialEmotion || '',
            profilePreset: characterProfile().preset || '',
            userText: turn.userText || ''
        });
        let plan = decoratePlan(result.plan);
        const explicitIntents = new Set(turn.explicitIntents || []);
        plan = plan.filter(function (item) { return !explicitIntents.has(item.intent); });
        if (!plan.length && turn.explicitPlanCount === 0) {
            const posture = player.stats().posture;
            const officialEmotion = normalizeEmotion(turn.officialEmotion);
            if (!turn.emotionReady) {
                turn.casualTalkPending = true;
                return;
            }
            turn.casualTalkPending = false;
            turn.casualTalkFinalized = true;
            const talkEmotionAllowed = !['sad', 'cry', 'shy', 'fearful', 'tired', 'surprised', 'disgusted']
                .includes(officialEmotion);
            if (!talkEmotionAllowed) {
                metrics.casualTalkSuppressedByEmotion += 1;
            } else if (posture === 'stand' || posture === 'sit') {
                const visibleText = text
                    .replace(/（[^（）]*）|\([^()]*\)/gu, ' ')
                    .replace(/\s+/gu, ' ')
                    .trim();
                casualRepliesSinceTalk += 1;
                const now = Date.now();
                const expressive = /[？?！!]|(?:但是|不过|其实|所以|因为|我觉得|听我说|告诉你|你知道|总之|首先|然后|当然|真的)|(?:but|because|actually|listen|you know|I think)/iu.test(visibleText);
                const cooldownReady = now - lastCasualTalkAt >= 12000;
                const cadenceReady = casualRepliesSinceTalk >= 3;
                if (visibleText.length >= 4 && cooldownReady && (expressive || cadenceReady)) {
                    const energetic = /[!！]{2,}|太好了|真的|当然|一定|绝对|哈哈|嘿嘿/iu.test(visibleText);
                    const restrained = /抱歉|对不起|小声|轻轻|慢慢|可能|也许|嗯/u.test(visibleText);
                    plan = decoratePlan([{
                        intent: 'talk',
                        kind: 'conversation',
                        intensity: energetic ? 3 : restrained || visibleText.length < 18 ? 1 : 2,
                        intensityExplicit: false,
                        count: 1,
                        emotion: turn.officialEmotion || null,
                        style: restrained ? 'gentle' : energetic ? 'lively' : 'neutral',
                        evidence: { source: 'assistant:conversation-fallback' }
                    }]);
                    result.source = 'assistant:conversation-fallback';
                    metrics.conversationalFallbacks += 1;
                    casualRepliesSinceTalk = 0;
                    lastCasualTalkAt = now;
                } else {
                    metrics.casualTalkSkipped += 1;
                }
            }
        }
        remember(Object.assign({}, result, { plan: plan }));
        window.dispatchEvent(new CustomEvent('neko-motion-decision', {
            detail: {
                stageId: 'speech:' + turn.id,
                source: result.source || 'assistant-speech',
                canonicalZh: result.canonicalZh,
                plan: plan.map(function (item) {
                    return { intent: item.intent, intensity: item.intensity, style: item.style, evidence: item.evidence };
                }),
                modelUsed: false,
                tokenUsage: result.tokenUsage,
                rawDialoguePersisted: false
            }
        }));
        if (!plan.length) {
            metrics.noMotion += 1;
            return;
        }
        const semanticEmotion = plan.map(function (item) { return item.emotion; })
            .find(function (value) { return value && normalizeEmotion(value) !== 'neutral'; });
        if (semanticEmotion) updatePersistentEmotion(semanticEmotion, 'speech_semantic');
        metrics.plans += 1;
        if (!isCurrentTurn(turn)) return;
        console.info('[NekoMotion] playing inferred speech motion', plan.map(function (item) { return item.intent; }).join(','));
        const previouslyStarted = turn.playerStarted;
        turn.playerStarted = true;
        turn.speechIntents = Array.from(new Set(
            (turn.explicitIntents || []).concat(plan.map(function (item) { return item.intent; }))
        ));
        const played = await player.playPlan(plan, { seed: turn.id + ':speech' });
        if (!played && isCurrentTurn(turn) && !vrmReady()) {
            turn.playerStarted = previouslyStarted;
            turn.speechProcessed = false;
            turn.casualTalkFinalized = false;
            turn.deferredUntilVrmReady = true;
        }
    }

    async function processEmotionBodyFallback(turn, update) {
        if (!turn || !update || !update.changed || !player || !vrmReady() || !isCurrentTurn(turn)) return;
        const intent = EMOTION_BODY_INTENT[update.emotion];
        if (!intent || turn.explicitPlanCount > 0 || turn.bodyEmotionPlayed === update.emotion) return;
        if (Array.isArray(turn.speechIntents) && turn.speechIntents.includes(intent)) return;
        const intensity = update.emotion === 'angry' ? 3
            : ['shy', 'sad', 'cry', 'fearful', 'tired'].includes(update.emotion) ? 1 : 2;
        const plan = decoratePlan([{
            intent: intent,
            kind: 'emotion-body',
            intensity: intensity,
            intensityExplicit: false,
            count: 1,
            emotion: update.emotion,
            evidence: { source: 'official-emotion-change' }
        }]);
        const alreadyPlaying = turn.playerStarted;
        turn.playerStarted = true;
        turn.bodyEmotionPlayed = update.emotion;
        metrics.plans += 1;
        console.info('[NekoMotion] playing body emotion change', update.emotion, 'as', intent);
        const played = alreadyPlaying
            ? await player.enqueuePlan(plan, { seed: turn.id + ':emotion:' + update.emotion })
            : await player.playPlan(plan, { seed: turn.id + ':emotion:' + update.emotion });
        if (!played && isCurrentTurn(turn) && !vrmReady()) {
            turn.playerStarted = alreadyPlaying;
            turn.bodyEmotionPlayed = null;
            turn.deferredUntilVrmReady = true;
        }
    }

    function processUnseenStages(turn) {
        if (!turn || turn.structured || !core || !player || !vrmReady() || !isCurrentTurn(turn)) {
            return Promise.resolve(false);
        }
        const stages = window.NekoMotionText.extractClosedStages(turn.capturedText || '');
        stages.forEach(function (stage) {
            if (turn.seen.has(stage.id) || turn.pendingStages.has(stage.id)) return;
            turn.pendingStages.add(stage.id);
            queueTurnTask(turn, 'closed-stage:' + stage.id, async function () {
                try {
                    if (await processStage(stage, turn)) turn.seen.add(stage.id);
                } finally {
                    turn.pendingStages.delete(stage.id);
                }
            });
        });
        return turn.processing;
    }

    async function processUnseenStagesDirect(turn) {
        if (!turn || turn.structured || !core || !player || !vrmReady() || !isCurrentTurn(turn)) return false;
        const stages = window.NekoMotionText.extractClosedStages(turn.capturedText || '');
        for (const stage of stages) {
            if (turn.seen.has(stage.id) || turn.pendingStages.has(stage.id)) continue;
            turn.pendingStages.add(stage.id);
            try {
                if (await processStage(stage, turn)) turn.seen.add(stage.id);
            } finally {
                turn.pendingStages.delete(stage.id);
            }
            if (!isCurrentTurn(turn)) return false;
        }
        return true;
    }

    function scanTurnText() {
        if (!core || !player || !vrmReady()) return;
        const localText = typeof window._geminiTurnFullText === 'string' ? window._geminiTurnFullText : '';
        const useBridgeText = activeTurn && String(activeTurn.source || '').startsWith('bridge');
        const text = useBridgeText ? bridgedText : localText;
        if (!text) return;
        if (!activeTurn || activeTurn.ended && text !== activeTurn.capturedText) {
            beginTurn(
                window._nekoAssistantTurnId || 'buffer-' + Date.now(),
                useBridgeText ? 'bridge-buffer' : 'buffer'
            );
            metrics.bufferRecoveredTurns += 1;
            console.info('[NekoMotion] recovered assistant turn from reply buffer');
        }
        // 同 beginTurn：本页的 window._turnIsStructured 不能替桥接回合作数。
        if (!isBridgedTurn(activeTurn) && window._turnIsStructured === true) {
            activeTurn.structured = true;
        }
        if (activeTurn.structured) return;
        if (text === activeTurn.lastText) {
            if (activeTurn.source === 'buffer'
                && window._geminiTurnEndSealed === true
                && Date.now() - activeTurn.lastTextAt >= BUFFER_SEAL_GRACE_MS
                && !activeTurn.ended) {
                finishTurn(activeTurn, 'buffer-sealed');
            }
            return;
        }
        if (activeTurn.finishTimer) {
            clearTimeout(activeTurn.finishTimer);
            activeTurn.finishTimer = null;
            metrics.coalescedTurnEnds += 1;
        }
        activeTurn.lastText = text;
        activeTurn.lastTextAt = Date.now();
        activeTurn.capturedText = text;
        void processUnseenStages(activeTurn);
    }

    function isBridgedTurn(turn) {
        return !!turn && String(turn.source || '').startsWith('bridge');
    }

    function beginTurn(turnId, source, userText) {
        let resolveEmotionReady;
        const emotionReadyPromise = new Promise(function (resolve) {
            resolveEmotionReady = resolve;
        });
        const pendingUserText = String(userText || '');
        activeTurn = {
            id: String(turnId || 'local-' + Date.now()),
            source: source || 'lifecycle',
            seen: new Set(),
            pendingStages: new Set(),
            lastText: '',
            lastTextAt: 0,
            capturedText: '',
            userText: pendingUserText,
            officialEmotion: '',
            emotionReady: false,
            emotionReadyPromise: emotionReadyPromise,
            resolveEmotionReady: resolveEmotionReady,
            playerStarted: false,
            explicitPlanCount: 0,
            explicitIntents: [],
            speechProcessed: false,
            casualTalkPending: false,
            casualTalkFinalized: false,
            structured: !String(source || '').startsWith('bridge')
                && window._turnIsStructured === true,
            speechIntents: [],
            bodyEmotionPlayed: null,
            deferredUntilVrmReady: false,
            finishTimer: null,
            processing: Promise.resolve()
        };
        metrics.turns += 1;
    }

    function rememberFinishedTurnId(turnId) {
        const id = turnId === undefined || turnId === null ? '' : String(turnId);
        if (!id || finishedTurnIds.has(id)) return;
        finishedTurnIds.add(id);
        finishedTurnOrder.push(id);
        while (finishedTurnOrder.length > FINISHED_TURN_LIMIT) {
            finishedTurnIds.delete(finishedTurnOrder.shift());
        }
    }

    function waitForOfficialEmotion(turn) {
        if (!turn || turn.emotionReady) return Promise.resolve(true);
        return Promise.race([
            turn.emotionReadyPromise.then(function () { return true; }),
            new Promise(function (resolve) {
                setTimeout(function () { resolve(false); }, EMOTION_READY_WAIT_MS);
            })
        ]);
    }

    function settleMissingEmotion(turn, emotionReceived) {
        if (!turn || emotionReceived || turn.emotionReady) return;
        turn.emotionReady = true;
        turn.officialEmotion = turn.officialEmotion || 'neutral';
        if (turn.resolveEmotionReady) turn.resolveEmotionReady();
        turn.resolveEmotionReady = null;
    }

    function finishTurn(turn, source) {
        if (!turn || turn.ended) return;
        if (turn.finishTimer) {
            clearTimeout(turn.finishTimer);
            turn.finishTimer = null;
        }
        turn.ended = true;
        turn.endSource = source || 'lifecycle';
        if (isCurrentTurn(turn)) {
            rememberFinishedTurnId(turn.id);
            lastFinishedTurn = {
                id: String(turn.id || ''),
                text: String(turn.capturedText || ''),
                at: Date.now()
            };
        }
        queueTurnTask(turn, 'finish-turn:' + turn.id, async function () {
            const ready = await initialize();
            if (!ready || !isCurrentTurn(turn)) return false;
            if (!vrmReady()) {
                turn.deferredUntilVrmReady = true;
                return false;
            }
            await processUnseenStagesDirect(turn);
            const emotionReceived = await waitForOfficialEmotion(turn);
            if (!isCurrentTurn(turn)) return false;
            // The official event is best-effort. A missing event must not
            // strand the ordinary conversational fallback indefinitely.
            settleMissingEmotion(turn, emotionReceived);
            await processSpeechFallback(turn);
            if (isCurrentTurn(turn) && player && typeof player.resumeIdleCountdown === 'function') {
                player.resumeIdleCountdown('assistant-turn-finished:' + turn.id);
            }
            if (String(turn.source || '').startsWith('bridge')) bridgedText = '';
            return true;
        });
    }

    function scheduleFinishTurn(turn, source) {
        if (!turn || turn.ended) return;
        if (turn.finishTimer) {
            clearTimeout(turn.finishTimer);
            metrics.coalescedTurnEnds += 1;
        }
        turn.finishTimer = setTimeout(function () {
            turn.finishTimer = null;
            finishTurn(turn, source);
        }, TURN_END_GRACE_MS);
    }

    function startObservedTurn(event) {
        const detail = event && event.detail || {};
        const turnId = detail.turnId;
        const userText = detail.userText;
        const bridgeEvent = detail.via === 'motion-lifecycle-bridge';
        if (!bridgeEvent) bridgedText = '';
        const mode = refreshMode();
        if (mode !== 'vrm') {
            metrics.nonVrmTurns += 1;
            console.info('[NekoMotion] ignored assistant turn because avatar mode is', mode);
            return;
        }
        const localText = typeof window._geminiTurnFullText === 'string' ? window._geminiTurnFullText : '';
        const candidateText = bridgeEvent ? bridgedText : localText;
        const duplicateId = turnId && lastFinishedTurn && String(turnId) === lastFinishedTurn.id;
        const duplicateStaleBuffer = (!turnId || duplicateId) && candidateText && lastFinishedTurn
            && candidateText === lastFinishedTurn.text
            && Date.now() - lastFinishedTurn.at < 2500;
        if (activeTurn && activeTurn.ended && (duplicateId || duplicateStaleBuffer)) {
            metrics.duplicateStartsIgnored += 1;
            console.info('[NekoMotion] ignored duplicate assistant turn start');
            return;
        }
        if (bridgeEvent) bridgedText = '';
        const canReuseActiveTurn = activeTurn && !activeTurn.ended
            && (turnId && String(turnId) === activeTurn.id
                || activeTurn.capturedText && (!turnId
                    || /^(?:buffer|bridge-buffer)$/.test(String(activeTurn.source || ''))));
        if (canReuseActiveTurn) {
            if (turnId) activeTurn.id = String(turnId);
            activeTurn.source = bridgeEvent ? 'bridge' : 'lifecycle';
            if (userText && !activeTurn.userText) activeTurn.userText = String(userText);
        } else {
            if (activeTurn && !activeTurn.ended) discardActiveTurn();
            beginTurn(turnId, bridgeEvent ? 'bridge' : 'lifecycle', userText);
        }
        const turn = activeTurn;
        void initialize().then(function (ready) {
            if (!ready) return;
            if (!isCurrentTurn(turn)) return;
            if (typeof player.noteActivity === 'function') {
                player.noteActivity();
            }
            maintainPersistentEmotion();
            if (!vrmReady()) {
                turn.deferredUntilVrmReady = true;
                metrics.deferredUntilVrmReady += 1;
                console.info('[NekoMotion] assistant turn accepted; waiting for VRM model readiness');
                return;
            }
            turn.deferredUntilVrmReady = false;
            if (turn.ended) return;
            console.info('[NekoMotion] assistant turn accepted in VRM mode');
            scanTurnText();
        });
    }

    function endObservedTurn(detail, source) {
        const payload = detail && typeof detail === 'object' ? detail : {};
        const turnId = payload.turnId;
        if (turnId && finishedTurnIds.has(String(turnId))) {
            metrics.duplicateStartsIgnored += 1;
            console.info('[NekoMotion] ignored stale assistant turn end', turnId);
            return;
        }
        if (turnId && activeTurn && !activeTurn.ended && String(turnId) !== activeTurn.id) {
            const recoveredBuffer = /^(?:buffer|bridge-buffer)/.test(String(activeTurn.source || ''))
                && (!payload.text || String(payload.text) === String(activeTurn.capturedText || ''));
            if (recoveredBuffer) {
                activeTurn.id = String(turnId);
                activeTurn.source = source || 'lifecycle';
            } else {
                metrics.duplicateStartsIgnored += 1;
                console.info('[NekoMotion] ignored stale assistant turn end', turnId);
                return;
            }
        }
        if (activeTurn && activeTurn.ended
            && (!turnId || String(turnId) === activeTurn.id)) return;
        if (!activeTurn || activeTurn.ended && (!turnId || String(turnId) !== activeTurn.id)) {
            if (refreshMode() !== 'vrm') {
                metrics.nonVrmTurns += 1;
                console.info('[NekoMotion] ignored assistant turn end outside VRM mode');
                return;
            }
            beginTurn(turnId, source || 'lifecycle');
        }
        const turn = activeTurn;
        turn.structured = turn.structured || payload.structured === true;
        if (typeof payload.text === 'string') {
            turn.capturedText = payload.text;
            turn.lastText = payload.text;
            turn.lastTextAt = Date.now();
            if (String(source || '').startsWith('bridge')) bridgedText = payload.text;
        } else {
            const localText = typeof window._geminiTurnFullText === 'string'
                ? window._geminiTurnFullText : '';
            if (localText) {
                turn.capturedText = localText;
                turn.lastText = localText;
                turn.lastTextAt = Date.now();
            }
            scanTurnText();
        }
        scheduleFinishTurn(turn, source || 'lifecycle');
    }

    function emotionObserved(detail) {
        const payload = detail && typeof detail === 'object' ? detail : {};
        const value = payload.emotion;
        const turnId = payload.turnId;
        if (!value) return;
        if (turnId && (!activeTurn || String(turnId) !== activeTurn.id)) return;
        latestOfficialEmotion = String(value).toLowerCase();
        if (activeTurn && (!turnId || String(turnId) === activeTurn.id)) {
            activeTurn.officialEmotion = latestOfficialEmotion;
            activeTurn.emotionReady = true;
            if (activeTurn.resolveEmotionReady) {
                activeTurn.resolveEmotionReady();
                activeTurn.resolveEmotionReady = null;
            }
        }
        const update = updatePersistentEmotion(latestOfficialEmotion, 'official_emotion');
        if (activeTurn && (!turnId || String(turnId) === activeTurn.id)) {
            const turn = activeTurn;
            queueTurnTask(turn, 'official-emotion:' + latestOfficialEmotion, async function () {
                await processEmotionBodyFallback(turn, update);
                if (turn.ended && turn.casualTalkPending && !turn.casualTalkFinalized && isCurrentTurn(turn)) {
                    await processSpeechFallback(turn, true);
                }
            });
        }
    }

    function discardActiveTurn() {
        const turn = activeTurn;
        if (turn) {
            if (turn.finishTimer) clearTimeout(turn.finishTimer);
            turn.finishTimer = null;
            if (!turn.ended) turn.ended = true;
            turn.cancelled = true;
            turn.deferredUntilVrmReady = false;
            if (turn.resolveEmotionReady) turn.resolveEmotionReady();
            turn.resolveEmotionReady = null;
        }
        activeTurn = null;
        bridgedText = '';
    }

    function cancelObservedSpeech(detail) {
        const turnId = detail && detail.turnId;
        if (turnId && (!activeTurn || String(turnId) !== activeTurn.id)) return;
        if (player) player.cancel('assistant_speech_cancel', { resume: refreshMode() === 'vrm' });
        discardActiveTurn();
    }

    function handleMotionLifecycleBridge(event) {
        const message = event && event.detail;
        if (!message || message.action !== 'motion_lifecycle') return;
        const detail = message.detail && typeof message.detail === 'object' ? message.detail : {};
        const currentName = String(window.lanlan_config && window.lanlan_config.lanlan_name || '');
        if (detail.lanlan_name && (!currentName || String(detail.lanlan_name) !== currentName)) return;
        metrics.bridgeMessages += 1;
        if (message.eventName === 'neko-assistant-text-update') {
            if (refreshMode() !== 'vrm') {
                bridgedText = '';
                return;
            }
            const updateTurnId = detail.turnId === undefined || detail.turnId === null
                ? '' : String(detail.turnId);
            if (updateTurnId && (finishedTurnIds.has(updateTurnId)
                || activeTurn && !activeTurn.ended && updateTurnId !== activeTurn.id)) {
                metrics.duplicateStartsIgnored += 1;
                console.info('[NekoMotion] ignored stale assistant text update', updateTurnId);
                return;
            }
            bridgedText = String(detail.text || '');
            if (!bridgedText) return;
            if (!activeTurn || activeTurn.ended && bridgedText !== activeTurn.capturedText) {
                beginTurn(detail.turnId || 'bridge-' + Date.now(), 'bridge-buffer');
            }
            activeTurn.structured = activeTurn.structured || detail.structured === true;
            scanTurnText();
            return;
        }
        if (message.eventName === 'neko-assistant-turn-end' && typeof detail.text === 'string') {
            bridgedText = detail.text;
        }
        if (message.eventName === 'neko-assistant-turn-start') {
            startObservedTurn({ detail: Object.assign({}, detail, { via: 'motion-lifecycle-bridge' }) });
        } else if (message.eventName === 'neko-assistant-turn-end') {
            endObservedTurn(detail, 'bridge');
        } else if (message.eventName === 'neko-assistant-emotion-ready') {
            emotionObserved(detail);
        } else if (message.eventName === 'neko-assistant-speech-cancel') {
            cancelObservedSpeech(detail);
        }
    }

    let motionLifecycleBridgeBound = false;
    function bindMotionLifecycleBridge() {
        if (motionLifecycleBridgeBound) return false;
        window.addEventListener('neko:motion-lifecycle-relay', handleMotionLifecycleBridge);
        motionLifecycleBridgeBound = true;
        return true;
    }

    function unbindMotionLifecycleBridge() {
        if (!motionLifecycleBridgeBound) return;
        window.removeEventListener('neko:motion-lifecycle-relay', handleMotionLifecycleBridge);
        motionLifecycleBridgeBound = false;
    }

    bindMotionLifecycleBridge();

    async function handleVrmModelLoaded() {
        const loadedModel = window.vrmManager && window.vrmManager.currentModel;
        resetCharacterMotionState();
        try {
            const ready = await initialize();
            if (!ready || !vrmReady()
                || window.vrmManager.currentModel !== loadedModel) return;
            player.cancel('vrm_model_loaded', { resume: false });
            const profile = await resolveCharacterProfile();
            if (!vrmReady() || window.vrmManager.currentModel !== loadedModel) return;
            acquirePlaybackOwnership();
            stopOfficialIdleRotation();
            syncSavedRestAnimations();
            player.setProfile(profile);

            const turn = activeTurn;
            if (turn && isCurrentTurn(turn) && turn.deferredUntilVrmReady) {
                turn.deferredUntilVrmReady = false;
                if (turn.ended) {
                    await processUnseenStagesDirect(turn);
                    if (!isCurrentTurn(turn)
                        || window.vrmManager.currentModel !== loadedModel) return;
                    const emotionReceived = await waitForOfficialEmotion(turn);
                    if (!isCurrentTurn(turn)
                        || window.vrmManager.currentModel !== loadedModel) return;
                    settleMissingEmotion(turn, emotionReceived);
                    await processEmotionBodyFallback(turn, {
                        changed: true,
                        emotion: normalizeEmotion(turn.officialEmotion)
                    });
                    if (!isCurrentTurn(turn)
                        || window.vrmManager.currentModel !== loadedModel) return;
                    await processSpeechFallback(turn);
                } else {
                    scanTurnText();
                    await processUnseenStages(turn);
                }
                if (!isCurrentTurn(turn) || turn.playerStarted) return;
            }
            if (!vrmReady() || window.vrmManager.currentModel !== loadedModel) return;
            await player.enterRest({
                profile: profile,
                seed: 'model-loaded',
                force: true,
                reselect: false
            });
        } catch (error) {
            console.warn('[NekoMotion] failed to enter rest after model load:', error);
        }
    }

    window.addEventListener('vrm-model-loaded', function () {
        void handleVrmModelLoaded();
    });

    window.addEventListener('neko:character-personality-updated', function () {
        activeCharacterProfile = null;
        void resolveCharacterProfile().then(function (profile) {
            if (!player || !vrmReady()) return;
            player.setProfile(profile);
        });
    });

    window.addEventListener('neko-model-manager-mode-set', function (event) {
        selectedMode = String(event && event.detail && event.detail.mode || configuredMode()).toLowerCase();
        if (selectedMode !== 'vrm' && player) {
            player.cancel('model_mode_changed', { resume: false });
        }
        if (selectedMode === 'vrm') {
            if (!player) {
                void initialize();
            } else if (vrmReady()) {
                acquirePlaybackOwnership();
                syncSavedRestAnimations();
                player.setProfile(characterProfile());
                void player.enterRest({
                    profile: characterProfile(),
                    seed: 'mode-set',
                    force: true,
                    reselect: false
                });
            }
            scanTurnText();
            maintainPersistentEmotion();
        } else {
            discardActiveTurn();
            releasePlaybackOwnership();
            releaseExpressionControl(false);
        }
    });

    let scanTimer = null;
    let emotionTimer = null;
    function startMaintenanceTimers() {
        if (scanTimer === null) {
            scanTimer = setInterval(function () {
                if (activeTurn && !activeTurn.ended) scanTurnText();
            }, POLL_INTERVAL_MS);
        }
        if (emotionTimer === null) emotionTimer = setInterval(maintainPersistentEmotion, 1000);
    }
    function stopMaintenanceTimers() {
        if (scanTimer !== null) clearInterval(scanTimer);
        if (emotionTimer !== null) clearInterval(emotionTimer);
        scanTimer = null;
        emotionTimer = null;
    }
    window.addEventListener('pagehide', function () {
        stopMaintenanceTimers();
        unbindMotionLifecycleBridge();
    });
    window.addEventListener('pageshow', function (event) {
        if (event && event.persisted) {
            bindMotionLifecycleBridge();
            startMaintenanceTimers();
        }
    });
    startMaintenanceTimers();
    if (refreshMode() === 'vrm') void initialize();

    async function requireInitialized() {
        if (await initialize() && core && player) return;
        throw new Error('NekoMotion runtime initialization failed');
    }

    window.NekoMotion = Object.freeze({
        analyze: async function (text, options) {
            await requireInitialized();
            return core.analyze(text, Object.assign({
                locale: currentLocale(),
                officialEmotion: activeTurn && activeTurn.officialEmotion || '',
                profilePreset: characterProfile().preset || ''
            }, options || {}));
        },
        analyzeSpeech: async function (text, options) {
            await requireInitialized();
            return core.analyzeSpeech(text, Object.assign({
                locale: currentLocale(),
                officialEmotion: activeTurn && activeTurn.officialEmotion || '',
                profilePreset: characterProfile().preset || ''
            }, options || {}));
        },
        normalizeToChinese: async function (text, locale) {
            await requireInitialized();
            return core.toChineseFrame(text, locale || currentLocale());
        },
        extract: function (text) { return window.NekoMotionText.extractClosedStages(text); },
        play: async function (intent, options) {
            await requireInitialized();
            const decision = Object.assign({ intent: String(intent || ''), kind: 'manual', intensity: 2, count: 1 }, options || {});
            return player.playPlan([decision], { seed: 'manual:' + Date.now() });
        },
        rest: async function (options) {
            await requireInitialized();
            syncSavedRestAnimations();
            return player.enterRest(Object.assign({
                profile: characterProfile(),
                seed: 'manual-rest',
                force: true
            }, options || {}));
        },
        holdExternalPlayback: async function (owner, options) {
            const settings = options || {};
            const ownerKey = String(owner || 'external');
            const token = Object.prototype.hasOwnProperty.call(settings, 'token')
                ? settings.token : null;
            externalPlaybackOwners.set(ownerKey, token);
            if (refreshMode() === 'vrm') acquirePlaybackOwnership();
            if (player) player.holdExternalPlayback(ownerKey, settings);
            if (!runtimeReady) void initialize();
            return true;
        },
        releaseExternalPlayback: async function (owner, options) {
            const settings = options || {};
            const ownerKey = String(owner || 'external');
            if (!externalPlaybackOwners.has(ownerKey)) return false;
            const hasToken = Object.prototype.hasOwnProperty.call(settings, 'token');
            const heldToken = externalPlaybackOwners.get(ownerKey);
            if ((hasToken && heldToken !== settings.token)
                || (!hasToken && heldToken !== null)) {
                return false;
            }
            externalPlaybackOwners.delete(ownerKey);
            const pendingInitialization = !runtimeReady ? readyPromise : null;
            if (player) {
                syncSavedRestAnimations();
                const released = await player.releaseExternalPlayback(ownerKey, Object.assign({}, settings, {
                    // 初始化尚未完成时不从半成品 player 恢复；initialize 会在无 owner 时
                    // 统一进入待机，避免冷启动 release 和初始化 idle 互相抢占。
                    resume: runtimeReady ? settings.resume : false
                }));
                if (released !== true) return false;
            } else if (!pendingInitialization) {
                if (externalPlaybackOwners.size > 0) return true;
                releasePlaybackOwnership();
                return false;
            }
            if (pendingInitialization) {
                // hold 是乐观且立即成功的；若后台初始化最终失败，必须把失败传给调用方，
                // 最后一个 owner 释放时让点歌台回退到底层 VRM 待机恢复；其他 owner
                // 仍持有播放权时则保持成功，避免其中一个调用方提前恢复待机。
                const initialized = await pendingInitialization === true;
                if (externalPlaybackOwners.size > 0) return true;
                if (initialized) {
                    if (settings.resume === false) {
                        // 初始化可能在 owner 删除后短暂进入了语义待机；无恢复释放必须
                        // 清掉该状态并交还官方待机调度，不能遗留全局播放所有权。
                        if (player && typeof player.cancel === 'function') {
                            player.cancel('external_release_without_resume', { resume: false });
                        }
                        releasePlaybackOwnership();
                    }
                    return true;
                }
                releasePlaybackOwnership();
                return false;
            }
            if (!runtimeReady) {
                if (externalPlaybackOwners.size > 0) return true;
                releasePlaybackOwnership();
                return false;
            }
            if (externalPlaybackOwners.size === 0 && settings.resume === false) {
                releasePlaybackOwnership();
            }
            return true;
        },
        stats: function () {
            refreshMode();
            return {
                mode: selectedMode,
                ready: !!(core && player),
                officialEmotion: latestOfficialEmotion || null,
                persistentEmotion: {
                    value: emotionState.value,
                    since: emotionState.since || null,
                    expiresInMs: emotionState.expiresAt ? Math.max(0, emotionState.expiresAt - Date.now()) : 0,
                    neutralStreak: emotionState.neutralStreak,
                    continuationCount: emotionState.continuationCount,
                    pulseDurationMs: emotionState.pulseDurationMs,
                    pulseRemainingMs: Math.max(0, emotionState.pulseUntil - Date.now()),
                    applyCount: emotionState.applyCount,
                    source: emotionState.source,
                    mappedMoods: currentVrmExpression() && currentVrmExpression().moodMap
                        ? Object.keys(currentVrmExpression().moodMap) : [],
                    availableExpressionCount: currentVrmExpression()
                        && typeof currentVrmExpression().getExpressionList === 'function'
                        ? currentVrmExpression().getExpressionList().length : 0
                },
                persona: {
                    preset: characterProfile().preset || 'custom',
                    energy: characterProfile().energy,
                    restraint: characterProfile().restraint,
                    warmth: characterProfile().warmth
                },
                core: core && core.stats(),
                player: player && player.stats(),
                runtime: Object.assign({}, metrics),
                recent: history.slice(),
                design: {
                    authority: 'clause-keyword-composition',
                    model: 'none',
                    addedTokenPerMotion: 0,
                    rawDialoguePersisted: false,
                    adapters: ['vrm']
                }
            };
        }
    });

})();
