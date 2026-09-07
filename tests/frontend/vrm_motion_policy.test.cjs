const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const zlib = require('node:zlib');

const fileRoot = path.resolve(__dirname, '..', '..');
const root = fs.existsSync(path.join(fileRoot, 'static')) ? fileRoot : process.cwd();
const motionRoot = path.join(root, 'static/vrm/motion');
const manifest = JSON.parse(fs.readFileSync(path.join(motionRoot, 'manifest.json'), 'utf8'));
const requiredLocales = ['zh-CN', 'zh-TW', 'en', 'ja', 'ko', 'ru', 'es', 'pt'];

assert.equal(manifest.policy.distribution, 'public-release');
assert.equal(manifest.policy.localTestEnabled, false);
assert.equal(manifest.policy.previewUnrated, false);
assert.equal(manifest.assets.length, 75);
assert.equal(manifest.counts.files, 75);
assert.equal(manifest.counts.official, 13);
assert.equal(manifest.assets.find(function (asset) { return asset.id === 'sit_01'; }).label, '半躺');
assert.deepEqual(
    manifest.assets.find(function (asset) { return asset.id === 'overwhelm_01'; }).card.emotions,
    ['fearful']
);

manifest.assets.forEach(function (asset) {
    assert.equal(asset.ok, true, asset.id);
    assert.equal(asset.license, 'Apache-2.0', asset.id);
    assert.equal(asset.compression, 'gzip', asset.id);
    assert.equal(asset.card.descriptionStatus, 'human-verified', asset.id);
    assert.equal(['a', 'b', 'l', 'r'].includes(asset.h), true, asset.id + ':handedness');
    assert.equal(asset.src[0], 'static/vrm/' + asset.f + '.gz', asset.id);
    requiredLocales.forEach(function (locale) {
        assert.equal(typeof asset.names[locale], 'string', asset.id + ':' + locale);
        assert.notEqual(asset.names[locale].trim(), '', asset.id + ':' + locale);
    });

    const source = path.join(root, asset.src[0]);
    const packed = fs.readFileSync(source);
    const decoded = zlib.gunzipSync(packed);
    const packedDigest = crypto.createHash('sha256').update(packed).digest('hex');
    const decodedDigest = crypto.createHash('sha256').update(decoded).digest('hex');
    assert.equal(packed.length, asset.packedBytes, asset.id);
    assert.equal(decoded.length, asset.decodedBytes, asset.id);
    assert.equal(packedDigest, asset.packedSha, asset.id);
    assert.equal(decodedDigest, asset.decodedSha, asset.id);
});

const talkAsset = manifest.assets.find(function (asset) { return asset.id === 'talk_03'; });
const talkGlb = zlib.gunzipSync(fs.readFileSync(path.join(root, talkAsset.src[0])));
const talkJsonLength = talkGlb.readUInt32LE(12);
const talkJson = JSON.parse(talkGlb.subarray(20, 20 + talkJsonLength).toString('utf8'));
assert.equal(talkJsonLength % 4, 0, 'GLB JSON chunk must be 4-byte aligned');
const talkBinOffset = 20 + talkJsonLength + 8;
const hipsIndex = talkJson.nodes.findIndex(function (node) { return node.name === 'mixamorig:Hips'; });
const hipsRotationChannel = talkJson.animations[0].channels.find(function (channel) {
    return channel.target.node === hipsIndex && channel.target.path === 'rotation';
});
const hipsRotationAccessor = talkJson.accessors[
    talkJson.animations[0].samplers[hipsRotationChannel.sampler].output
];
const hipsRotationView = talkJson.bufferViews[hipsRotationAccessor.bufferView];
const hipsRotationOffset = talkBinOffset + (hipsRotationView.byteOffset || 0)
    + (hipsRotationAccessor.byteOffset || 0);
const hipsRotationStride = hipsRotationView.byteStride || 16;
let maxTalkYaw = 0;
for (let index = 0; index < hipsRotationAccessor.count; index += 1) {
    const offset = hipsRotationOffset + index * hipsRotationStride;
    const x = talkGlb.readFloatLE(offset);
    const y = talkGlb.readFloatLE(offset + 4);
    const z = talkGlb.readFloatLE(offset + 8);
    const w = talkGlb.readFloatLE(offset + 12);
    const yaw = Math.atan2(2 * (w * y + x * z), 1 - 2 * (y * y + z * z));
    maxTalkYaw = Math.max(maxTalkYaw, Math.abs(yaw));
}
assert.ok(maxTalkYaw < 5 * Math.PI / 180, 'talk_03 must remain front-facing');

function walk(directory) {
    return fs.readdirSync(directory, { withFileTypes: true }).flatMap(function (entry) {
        const fullPath = path.join(directory, entry.name);
        return entry.isDirectory() ? walk(fullPath) : [fullPath];
    });
}

function sliceBetween(text, startMarker, endMarker, label) {
    const startIndex = text.indexOf(startMarker);
    assert.notEqual(startIndex, -1, 'missing start marker: ' + (label || startMarker));
    const tail = text.slice(startIndex + startMarker.length);
    const endIndex = tail.indexOf(endMarker);
    assert.notEqual(endIndex, -1, 'missing end marker: ' + (label || endMarker));
    return tail.slice(0, endIndex);
}

const relativeFiles = walk(motionRoot).map(function (filename) {
    return path.relative(motionRoot, filename).split(path.sep).join('/');
}).sort();

assert.deepEqual(relativeFiles.filter(function (name) { return !name.endsWith('.vrma.gz'); }), [
    'bridge.js',
    'core.js',
    'manifest.json',
    'player.js',
    'runtime.js',
    'semantics.json'
]);
assert.equal(relativeFiles.filter(function (name) { return name.endsWith('.vrma.gz'); }).length, 62);

const allVrmFiles = walk(path.join(root, 'static/vrm'));
assert.equal(allVrmFiles.filter(function (name) { return name.endsWith('.vrma.gz'); }).length, 75);
assert.equal(allVrmFiles.some(function (name) { return name.endsWith('.vrma'); }), false);

const websocketSource = fs.readFileSync(path.join(root, 'static/app/app-websocket.js'), 'utf8');
const buttonsSource = fs.readFileSync(path.join(root, 'static/app/app-buttons.js'), 'utf8');
const bridgeSource = fs.readFileSync(path.join(motionRoot, 'bridge.js'), 'utf8');
const relaySource = fs.readFileSync(
    path.join(root, 'static/app/app-interpage/guide-message-relay.js'),
    'utf8'
);
const runtimeSource = fs.readFileSync(path.join(motionRoot, 'runtime.js'), 'utf8');
assert.equal(websocketSource.includes('_nekoMotionPendingUserText'), false);
assert.equal(buttonsSource.includes('_nekoMotionPendingUserText'), false);
assert.match(buttonsSource, /requestId: requestId,\s*text: text,\s*source:/);
assert.match(
    websocketSource,
    // The value is no longer pinned: the mini-game route propagates the real
    // transcript source instead of always claiming 'voice'. What this guard is
    // for -- the websocket path dispatching requestId/text/source itself rather
    // than stashing pending user text -- is unchanged, and the sibling
    // buttonsSource assertion above already leaves the value open the same way.
    /requestId: resolveAssistantRequestId\(response\.request_id, response\.meta\),\s*text: normalizedVoiceTranscript,\s*source: /
);
assert.equal(bridgeSource.includes('USER_TEXT_LIMIT'), false);
assert.equal(bridgeSource.includes("new BroadcastChannel('neko_motion_lifecycle')"), false);
assert.match(bridgeSource, /appInterpage\.nekoBroadcastChannel/);
assert.match(bridgeSource, /function relayClosedStage\(event\)/);
assert.match(bridgeSource, /function peekUserText\(requestIdValue\)/);
assert.match(bridgeSource, /relay\('neko-assistant-turn-end', detail\);\s*finishUserText\(event\);/);
assert.match(bridgeSource, /function relaySpeechCancel\(event\)/);
assert.match(bridgeSource, /\^user_activity\(\?:_delayed\)\?\$[\s\S]*dropConsumedContexts\(\)/);
assert.equal(
    bridgeSource.includes("relayDetail('neko-assistant-speech-cancel')"),
    false,
    'a user-activity interrupt must clear the consumed user text, not only relay the cancel'
);
assert.match(bridgeSource, /event\.detail\.text/);
assert.match(bridgeSource, /detail\.structured = detail\.structured === true \|\| window\._turnIsStructured === true/);
assert.match(bridgeSource, /text: closedText,\s*structured: window\._turnIsStructured === true/);
assert.equal(bridgeSource.includes('_lastSubmittedText'), false);
assert.match(relaySource, /case 'motion_lifecycle'/);
assert.match(relaySource, /neko:motion-lifecycle-relay/);
assert.match(relaySource, /!motionCurrentName \|\| motionDetail\.lanlan_name !== motionCurrentName/);
assert.equal(runtimeSource.includes("new BroadcastChannel('neko_motion_lifecycle')"), false);
assert.equal(
    runtimeSource.includes("window.vrmManager.currentModel.vrm) selectedMode = 'vrm'"),
    false,
    'a retained hidden VRM model must not override the configured active mode'
);
assert.match(runtimeSource, /neko:motion-lifecycle-relay/);
assert.match(runtimeSource, /window\.__nekoMotionOwnsVrmPlayback = false/);
assert.match(runtimeSource, /releasePlaybackOwnership\(\)/);
assert.match(runtimeSource, /player\.cancel\('model_mode_changed', \{ resume: false \}\)/);
assert.match(runtimeSource, /syncSavedRestAnimations\(\)/);
assert.match(runtimeSource, /if \(refreshMode\(\) === 'vrm'\) void initialize\(\)/);
assert.equal(runtimeSource.includes('\n    void initialize();\n'), false);
assert.match(runtimeSource, /activeTurn === turn/);
assert.match(runtimeSource, /ignored stale assistant turn end/);
assert.equal(runtimeSource.includes("window.dispatchEvent(new CustomEvent(message.eventName"), false);
assert.match(runtimeSource, /await initialize\(\)/);
assert.equal(runtimeSource.includes('.slice(0, 1000)'), false);
assert.match(runtimeSource, /fetchWithTimeout\(SEMANTICS_URL/);
assert.match(runtimeSource, /fetchWithTimeout\('\/api\/characters\/character\/'/);
assert.match(runtimeSource, /async function requireInitialized\(\)/);
assert.equal((runtimeSource.match(/await requireInitialized\(\)/g) || []).length, 5);
assert.match(runtimeSource, /processUnseenStagesDirect\(turn\)/);
assert.match(runtimeSource, /turn\.cancelled = true/);
assert.match(runtimeSource, /played = await player\.playPlan\(plan, context\)/);
assert.match(runtimeSource, /if \(!played\) \{[\s\S]*if \(!vrmReady\(\)\) \{[\s\S]*turn\.deferredUntilVrmReady = true;[\s\S]*return false;[\s\S]*return true;/);
assert.match(runtimeSource, /const played = await player\.playPlan\(plan, \{ seed: turn\.id \+ ':speech' \}\)/);
assert.match(runtimeSource, /turn\.speechProcessed = false;\s*turn\.casualTalkFinalized = false;\s*turn\.deferredUntilVrmReady = true/);
assert.match(runtimeSource, /if \(!played && isCurrentTurn\(turn\) && !vrmReady\(\)\)/);
assert.match(runtimeSource, /if \(turn\.structured\) \{[\s\S]*turn\.speechProcessed = true;/);
assert.match(runtimeSource, /if \(!turn \|\| turn\.structured \|\| !core \|\| !player/);
assert.match(runtimeSource, /activeTurn\.structured = activeTurn\.structured \|\| detail\.structured === true/);
assert.match(runtimeSource, /if \(!vrmReady\(\)\) \{\s*turn\.deferredUntilVrmReady = true;\s*return;/);
assert.match(runtimeSource, /player\.cancel\('assistant_speech_cancel', \{ resume: refreshMode\(\) === 'vrm' \}\)/);
assert.match(runtimeSource, /activeTurn = null;\s*bridgedText = ''/);
assert.match(runtimeSource, /pendingStages: new Set\(\)/);
assert.match(runtimeSource, /structured: !String\(source \|\| ''\)\.startsWith\('bridge'\)/);
assert.match(
    runtimeSource,
    /if \(!isBridgedTurn\(activeTurn\) && window\._turnIsStructured === true\) \{\s*activeTurn\.structured = true;/
);
assert.equal(
    ((runtimeSource.replace(/^\s*\/\/.*$/gm, '')).match(/window\._turnIsStructured/g) || []).length,
    2,
    'every local structured-flag read must be gated on a non-bridged turn'
);
assert.match(
    runtimeSource,
    /beginTurn\(\s*window\._nekoAssistantTurnId \|\| 'buffer-' \+ Date\.now\(\),\s*useBridgeText \? 'bridge-buffer' : 'buffer'/,
    'a turn recovered from bridged text must stay bridge-sourced'
);
assert.match(runtimeSource, /turn\.pendingStages\.has\(stage\.id\)/);
assert.match(runtimeSource, /if \(await processStage\(stage, turn\)\) turn\.seen\.add\(stage\.id\)/);
assert.match(runtimeSource, /turn\.pendingStages\.delete\(stage\.id\)/);
assert.match(runtimeSource, /if \(turn && isCurrentTurn\(turn\)\) turn\.deferredUntilVrmReady = true/);
assert.match(runtimeSource, /const duplicateStaleBuffer = \(!turnId \|\| duplicateId\)/);
const startObservedTurn = sliceBetween(
    runtimeSource, 'function startObservedTurn(event)', 'function endObservedTurn', 'startObservedTurn'
);
assert.ok(
    startObservedTurn.indexOf("if (activeTurn && activeTurn.ended && (duplicateId || duplicateStaleBuffer))")
        < startObservedTurn.indexOf("if (bridgeEvent) bridgedText = ''")
        && startObservedTurn.indexOf("if (bridgeEvent) bridgedText = ''")
        < startObservedTurn.indexOf('const canReuseActiveTurn =')
);
assert.match(
    startObservedTurn,
    /turnId && String\(turnId\) === activeTurn\.id[\s\S]*activeTurn\.capturedText[\s\S]*\^\(\?:buffer\|bridge-buffer\)\$[\s\S]*if \(canReuseActiveTurn\)/
);
assert.match(
    startObservedTurn,
    /else \{\s*if \(activeTurn && !activeTurn\.ended\) discardActiveTurn\(\);\s*beginTurn\(/
);
const bridgeTextUpdate = sliceBetween(
    runtimeSource,
    "if (message.eventName === 'neko-assistant-text-update')",
    "if (message.eventName === 'neko-assistant-turn-end'",
    'bridgeTextUpdate'
);
assert.match(bridgeTextUpdate, /if \(refreshMode\(\) !== 'vrm'\)/);
assert.ok(
    bridgeTextUpdate.indexOf("if (refreshMode() !== 'vrm')")
        < bridgeTextUpdate.indexOf('finishedTurnIds.has(updateTurnId)')
        && bridgeTextUpdate.indexOf('finishedTurnIds.has(updateTurnId)')
        < bridgeTextUpdate.indexOf('bridgedText = String(detail.text')
        && bridgeTextUpdate.indexOf('bridgedText = String(detail.text')
        < bridgeTextUpdate.indexOf("beginTurn(detail.turnId || 'bridge-' + Date.now(), 'bridge-buffer')")
);
const turnEndSource = sliceBetween(
    runtimeSource, 'function endObservedTurn', 'function emotionObserved', 'endObservedTurn'
);
assert.match(turnEndSource, /if \(refreshMode\(\) !== 'vrm'\)/);
assert.ok(
    turnEndSource.indexOf("if (refreshMode() !== 'vrm')")
        < turnEndSource.indexOf("beginTurn(turnId, source || 'lifecycle')"),
    'a turn end outside VRM mode must not create a deferred motion turn'
);
assert.ok(
    turnEndSource.indexOf('finishedTurnIds.has(String(turnId))')
        < turnEndSource.indexOf("beginTurn(turnId, source || 'lifecycle')"),
    'a stale completed turn end must not replace the current turn'
);
const emotionSource = sliceBetween(
    runtimeSource, 'function emotionObserved', 'function cancelObservedSpeech', 'emotionObserved'
);
assert.match(
    emotionSource,
    /if \(turnId && \(!activeTurn \|\| String\(turnId\) !== activeTurn\.id\)\) return;/
);
assert.match(runtimeSource, /async function handleVrmModelLoaded\(\)/);
assert.match(runtimeSource, /await processUnseenStagesDirect\(turn\)/);
assert.match(runtimeSource, /function resetCharacterMotionState\(\)/);
assert.match(runtimeSource, /turn\.deferredUntilVrmReady = true/);
assert.match(runtimeSource, /turn && isCurrentTurn\(turn\) && turn\.deferredUntilVrmReady/);
assert.match(runtimeSource, /window\.vrmManager\.currentModel !== loadedModel/);
assert.match(runtimeSource, /casualTalkPending/);
const waitForEmotionBlock = sliceBetween(
    runtimeSource, 'function waitForOfficialEmotion(turn)', 'function finishTurn', 'waitForOfficialEmotion'
);
assert.match(waitForEmotionBlock, /Promise\.resolve\(true\)/);
assert.match(waitForEmotionBlock, /resolve\(false\)/);
assert.match(runtimeSource, /function settleMissingEmotion\(turn, emotionReceived\)/);
// 每一条等待官方情绪事件的路径都必须自己处理超时，否则 emotionReady 永远是
// false，processSpeechFallback 只挂起 casualTalkPending 就返回，再没有事件来重试。
assert.equal(
    (runtimeSource.match(/await waitForOfficialEmotion\(turn\)/g) || []).length,
    (runtimeSource.match(/settleMissingEmotion\(turn, emotionReceived\);/g) || []).length,
    'every waitForOfficialEmotion() call site must settle the timeout'
);
const finishTurnBlock = sliceBetween(
    runtimeSource, 'function finishTurn(turn, source)', 'function scheduleFinishTurn', 'finishTurn'
);
assert.match(finishTurnBlock, /const emotionReceived = await waitForOfficialEmotion\(turn\)/);
assert.match(
    finishTurnBlock,
    /settleMissingEmotion\(turn, emotionReceived\);[\s\S]*await processSpeechFallback\(turn\)/
);
const cancelObservedSpeechBlock = sliceBetween(
    runtimeSource,
    'function cancelObservedSpeech(detail)',
    'function handleMotionLifecycleBridge',
    'cancelObservedSpeech'
);
assert.match(
    cancelObservedSpeechBlock,
    /const turnId = detail && detail\.turnId;[\s\S]*if \(turnId && \(!activeTurn \|\| String\(turnId\) !== activeTurn\.id\)\) return;/
);
assert.match(runtimeSource, /neko-assistant-speech-cancel'[\s\S]*cancelObservedSpeech\(detail\)/);
const modelLoadedBlock = sliceBetween(
    runtimeSource,
    'async function handleVrmModelLoaded()',
    "window.addEventListener('vrm-model-loaded'",
    'handleVrmModelLoaded'
);
assert.match(
    modelLoadedBlock,
    /const emotionReceived = await waitForOfficialEmotion\(turn\)[\s\S]*settleMissingEmotion\(turn, emotionReceived\)/
);
assert.ok(
    modelLoadedBlock.indexOf("player.cancel('vrm_model_loaded', { resume: false })")
        < modelLoadedBlock.indexOf('resolveCharacterProfile()'),
    'a newly loaded model must reset stale player posture and queue state first'
);
assert.ok(
    modelLoadedBlock.indexOf('acquirePlaybackOwnership()')
        < modelLoadedBlock.indexOf('player.enterRest('),
    'a deferred VRM model load must acquire playback ownership before semantic rest'
);
const nonVrmMarker = "if (mode !== 'vrm') {";
const nonVrmParts = runtimeSource.split(nonVrmMarker);
assert.equal(nonVrmParts.length, 2, 'non-VRM turn guard must remain unique');
assert.equal(runtimeSource.includes('_nekoMotionPendingUserText'), false);
assert.equal(runtimeSource.includes('_lastSubmittedText'), false);
assert.equal(runtimeSource.includes("window.addEventListener('neko-assistant-turn-start'"), false);
assert.equal(runtimeSource.includes("window.addEventListener('neko-assistant-turn-end'"), false);
const modeSetMarker = "window.addEventListener('neko-model-manager-mode-set'";
const modeSetParts = runtimeSource.split(modeSetMarker);
assert.equal(modeSetParts.length, 2, 'mode-set listener must remain unique');
const modeSetBlock = modeSetParts[1].slice(0, 1800);
assert.match(modeSetBlock, /else \{\s*discardActiveTurn\(\);\s*releasePlaybackOwnership\(\)/);
const initializeBlock = sliceBetween(
    runtimeSource, 'async function initialize()', 'function remember', 'initialize'
);
assert.ok(
    initializeBlock.indexOf("if (refreshMode() !== 'vrm')")
        < initializeBlock.indexOf('acquirePlaybackOwnership()'),
    'initialization must recheck the selected mode before acquiring playback ownership'
);
assert.match(runtimeSource, /function stopMaintenanceTimers\(\)/);
assert.match(runtimeSource, /window\.addEventListener\('pagehide'/);
assert.match(runtimeSource, /window\.addEventListener\('pageshow'/);
assert.match(runtimeSource, /bindMotionLifecycleBridge\(\);\s*startMaintenanceTimers\(\)/);
assert.match(
    runtimeSource,
    /holdExternalPlayback:\s*async function[\s\S]*player\.holdExternalPlayback/
);
assert.match(
    runtimeSource,
    /releaseExternalPlayback:\s*async function[\s\S]*player\.releaseExternalPlayback/
);
const externalHoldBlock = sliceBetween(
    runtimeSource,
    'holdExternalPlayback: async function',
    'releaseExternalPlayback: async function',
    'external hold API'
);
assert.ok(
    externalHoldBlock.indexOf('externalPlaybackOwners.set(')
        < externalHoldBlock.indexOf('void initialize()'),
    'a cold external hold must be recorded synchronously before initialization starts'
);
assert.equal(externalHoldBlock.includes('await requireInitialized()'), false);
assert.match(
    runtimeSource,
    /if \(vrmReady\(\) && externalPlaybackOwners\.size === 0\) \{\s*await player\.enterRest/
);

const modelManagerSource = fs.readFileSync(
    path.join(root, 'static/js/model_manager/page-controller.js'),
    'utf8'
);
const indexTemplate = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
const chatTemplate = fs.readFileSync(path.join(root, 'templates/chat.html'), 'utf8');
assert.match(indexTemplate, /static\/vrm\/motion\/bridge\.js/);
assert.match(chatTemplate, /static\/vrm\/motion\/bridge\.js/);
assert.ok(
    indexTemplate.indexOf('/static/app/app-websocket.js')
        < indexTemplate.indexOf('/static/vrm/motion/bridge.js')
        && indexTemplate.indexOf('/static/vrm/motion/bridge.js')
        < indexTemplate.indexOf('/static/vrm/motion/runtime.js')
);
assert.ok(
    chatTemplate.indexOf('/static/app/app-websocket.js')
        < chatTemplate.indexOf('/static/vrm/motion/bridge.js')
        && chatTemplate.indexOf('/static/vrm/motion/bridge.js')
        < chatTemplate.indexOf('/static/app/app-buttons.js')
);
const modelManagerTemplate = fs.readFileSync(path.join(root, 'templates/model_manager.html'), 'utf8');
assert.match(modelManagerSource, /new window\.NekoMotionPlayer\(\)/);
assert.match(modelManagerSource, /mergeVrmAnimationLists/);
assert.match(modelManagerSource, /data-motion-asset-id/);
assert.match(modelManagerSource, /playSelectedVrmAnimationOption/);
assert.match(modelManagerSource, /vrmMotionCatalogLoadPromise/);
assert.match(modelManagerSource, /cancel\('model_manager_stop', \{ resume: false \}\)/);
assert.match(modelManagerSource, /normalizeBundledVrmAnimationUrl/);
assert.match(modelManagerTemplate, /static\/vrm\/motion\/player\.js/);

function createRuntimeHarness(fetchImplementation, options = {}) {
    const calls = [];
    const players = [];
    const listeners = new Map();
    class FakeMotionCore {
        registerActionCards() {}
        stats() { return {}; }
    }
    class FakeMotionPlayer {
        constructor() {
            this.assets = [];
            this.owners = new Map();
            players.push(this);
        }
        holdExternalPlayback(owner, options) {
            this.owners.set(owner, options.token);
            calls.push(['hold', owner, options.token]);
            return true;
        }
        async releaseExternalPlayback(owner, options) {
            if (!this.owners.has(owner) || this.owners.get(owner) !== options.token) return false;
            this.owners.delete(owner);
            calls.push(['release', owner, options.token, options.resume]);
            return true;
        }
        async load() {
            if (options.failPlayerLoad === true) throw new Error('manifest unavailable');
            return this;
        }
        async enterRest() {
            calls.push(['rest']);
            return true;
        }
        setSavedRestAnimations() { return 0; }
        setProfile() {}
        cancel() { return true; }
        stats() { return {}; }
    }
    const context = {
        AbortController,
        clearInterval: function () {},
        clearTimeout,
        console: {
            debug: function () {},
            error: function () {},
            info: function () {},
            log: function () {},
            warn: function () {}
        },
        document: { documentElement: { lang: 'zh-CN' } },
        fetch: fetchImplementation,
        navigator: { language: 'zh-CN' },
        setInterval: function () { return 1; },
        setTimeout,
        CustomEvent: class CustomEvent {
            constructor(type, options) {
                this.type = type;
                this.detail = options && options.detail;
            }
        }
    };
    context.window = context;
    context.lanlan_config = { model_type: 'live2d' };
    context.NekoMotionCore = FakeMotionCore;
    context.NekoMotionPlayer = FakeMotionPlayer;
    context.NekoMotionText = { extractClosedStages: function () { return []; } };
    context._stopVrmIdleRotation = function () { calls.push(['official-stop']); };
    context._startVrmIdleRotation = function (urls) {
        calls.push(['official-start', Array.isArray(urls) ? urls.join('|') : '']);
    };
    context.vrmManager = {
        currentModel: { vrm: {} },
        playVRMAAnimation: async function () { return true; }
    };
    context.addEventListener = function (name, listener) { listeners.set(name, listener); };
    context.removeEventListener = function (name) { listeners.delete(name); };
    context.dispatchEvent = function () { return true; };
    vm.runInNewContext(runtimeSource, context, { filename: 'runtime.js' });
    return { calls, context, players };
}

async function flushMicrotasks(count = 12) {
    for (let index = 0; index < count; index += 1) await Promise.resolve();
}

async function verifyColdExternalPlaybackOwnership() {
    let resolveSemantics;
    const semanticsResponse = new Promise(function (resolve) {
        resolveSemantics = function () {
            resolve({ ok: true, json: async function () { return {}; } });
        };
    });
    const harness = createRuntimeHarness(function () { return semanticsResponse; });
    harness.context.lanlan_config.model_type = 'vrm';

    let holdSettled = false;
    const holdResult = harness.context.NekoMotion.holdExternalPlayback('jukebox', { token: 71 })
        .then(function (held) {
            holdSettled = true;
            return held;
        });
    await flushMicrotasks(2);
    assert.equal(holdSettled, true, 'a cold external hold must not wait for runtime initialization');
    assert.equal(await holdResult, true);
    assert.equal(harness.players.length, 0, 'the hold should resolve while semantics are still loading');
    assert.deepEqual(harness.calls, [['official-stop']]);
    assert.equal(harness.context.__nekoMotionOwnsVrmPlayback, true);

    resolveSemantics();
    await flushMicrotasks();
    assert.equal(harness.players.length, 1);
    assert.deepEqual(harness.calls, [
        ['official-stop'],
        ['hold', 'jukebox', 71]
    ]);
    const originalRelease = harness.players[0].releaseExternalPlayback.bind(harness.players[0]);
    let forwardedMissingTokenRelease = 0;
    harness.players[0].releaseExternalPlayback = async function (owner, options) {
        forwardedMissingTokenRelease += 1;
        return originalRelease(owner, options);
    };
    assert.equal(
        await harness.context.NekoMotion.releaseExternalPlayback('jukebox', { resume: true }),
        false,
        'the runtime must reject a tokenless release of a tokenized owner'
    );
    assert.equal(forwardedMissingTokenRelease, 0, 'a rejected release must not reach the player');
    harness.players[0].releaseExternalPlayback = originalRelease;
    assert.equal(
        await harness.context.NekoMotion.releaseExternalPlayback('jukebox', { token: 71, resume: true }),
        true
    );
    assert.deepEqual(harness.calls, [
        ['official-stop'],
        ['hold', 'jukebox', 71],
        ['release', 'jukebox', 71, true]
    ]);

    harness.context.lanlan_config.vrmIdleAnimations = ['/ready-idle.vrma'];
    assert.equal(
        await harness.context.NekoMotion.holdExternalPlayback('jukebox', { token: 74 }),
        true
    );
    assert.equal(
        await harness.context.NekoMotion.releaseExternalPlayback('jukebox', { token: 74, resume: false }),
        true
    );
    assert.equal(harness.context.__nekoMotionOwnsVrmPlayback, false);
    assert.deepEqual(harness.calls.slice(-3), [
        ['hold', 'jukebox', 74],
        ['release', 'jukebox', 74, false],
        ['official-start', '/ready-idle.vrma']
    ]);

    const failedHarness = createRuntimeHarness(async function () {
        return { ok: true, json: async function () { return {}; } };
    }, { failPlayerLoad: true });
    failedHarness.context.lanlan_config = {
        model_type: 'vrm',
        vrmIdleAnimations: ['/idle-a.vrma', '/idle-b.vrma']
    };
    const failedJukeboxHold = failedHarness.context.NekoMotion.holdExternalPlayback('jukebox', { token: 72 });
    const failedPreviewHold = failedHarness.context.NekoMotion.holdExternalPlayback('preview', { token: 73 });
    assert.equal(await failedJukeboxHold, true);
    assert.equal(await failedPreviewHold, true);
    await flushMicrotasks();
    assert.equal(failedHarness.players.length, 1, 'the failure must happen after player assignment');
    assert.equal(failedHarness.context.NekoMotion.stats().ready, false);
    assert.equal(failedHarness.context.__nekoMotionOwnsVrmPlayback, true);
    assert.equal(
        failedHarness.calls.some(function (entry) { return entry[0] === 'official-start'; }),
        false,
        'official idle must stay stopped while the external dance still owns playback'
    );
    assert.equal(
        await failedHarness.context.NekoMotion.releaseExternalPlayback('jukebox', { token: 72, resume: true }),
        true,
        'a failed runtime must stay held while another external owner remains'
    );
    assert.equal(failedHarness.context.__nekoMotionOwnsVrmPlayback, true);
    assert.equal(
        failedHarness.calls.some(function (entry) { return entry[0] === 'official-start'; }),
        false,
        'releasing one owner must not restart official idle for the remaining owner'
    );
    assert.equal(
        await failedHarness.context.NekoMotion.releaseExternalPlayback('preview', { token: 73, resume: true }),
        false,
        'a failed background initialization must leave idle restoration to the caller'
    );
    assert.equal(failedHarness.context.__nekoMotionOwnsVrmPlayback, false);
    assert.deepEqual(failedHarness.calls.slice(-1), [
        ['official-start', '/idle-a.vrma|/idle-b.vrma']
    ]);
}

verifyColdExternalPlaybackOwnership().then(function () {
    console.log('VRM motion policy and source integrity: OK (75 gzip assets)');
}).catch(function (error) {
    console.error(error);
    process.exitCode = 1;
});
