const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const zlib = require('node:zlib');

const fileRoot = path.resolve(__dirname, '..', '..');
const root = fs.existsSync(path.join(fileRoot, 'static')) ? fileRoot : process.cwd();
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'static/vrm/motion/manifest.json'), 'utf8'));
const packedSource = fs.readFileSync(path.join(root, manifest.assets[0].src[0]));
const decodedSource = zlib.gunzipSync(packedSource);
const sourceBuffer = packedSource.buffer.slice(
    packedSource.byteOffset,
    packedSource.byteOffset + packedSource.byteLength
);

global.window = global;
global.location = { hostname: 'localhost' };
global.document = { baseURI: 'http://localhost/' };
global.crypto = crypto.webcrypto;
global.CustomEvent = class CustomEvent {
    constructor(type, init) {
        this.type = type;
        this.detail = init && init.detail;
    }
};
global.dispatchEvent = function () {};
global.addEventListener = function () {};
global.removeEventListener = function () {};

let blobSequence = 0;
global.URL.createObjectURL = function () { blobSequence += 1; return 'blob:test-' + blobSequence; };
global.URL.revokeObjectURL = function () {};

vm.runInThisContext(
    fs.readFileSync(path.join(root, 'static/vrm/motion/player.js'), 'utf8'),
    { filename: 'static/vrm/motion/player.js' }
);
vm.runInThisContext(
    fs.readFileSync(path.join(root, 'static/vrm/vrm-animation.js'), 'utf8'),
    { filename: 'static/vrm/vrm-animation.js' }
);
vm.runInThisContext(
    fs.readFileSync(path.join(root, 'static/vrm/vrm-expression.js'), 'utf8'),
    { filename: 'static/vrm/vrm-expression.js' }
);
vm.runInThisContext(
    fs.readFileSync(path.join(root, 'static/vrm/vrm-interaction.js'), 'utf8'),
    { filename: 'static/vrm/vrm-interaction.js' }
);

function response(body, status) {
    return {
        ok: !status || status < 400,
        status: status || 200,
        async json() { return JSON.parse(JSON.stringify(body)); },
        async arrayBuffer() { return body; }
    };
}

function deferred() {
    let resolve;
    const promise = new Promise(function (done) { resolve = done; });
    return { promise, resolve };
}

async function waitForLoadStart(predicate, message) {
    for (let attempt = 0; attempt < 1000; attempt += 1) {
        if (predicate()) return;
        await new Promise(setImmediate);
    }
    assert.fail(message);
}

(async function () {
    global.fetch = async function (url) {
        assert.equal(String(url), '/static/vrm/motion/manifest.json');
        return response(manifest);
    };
    const player = await new global.NekoMotionPlayer().load();
    assert.equal(player.assets.length, 75);
    assert.equal(player.assets.every(function (asset) { return asset.compression === 'gzip'; }), true);

    const zhCatalog = player.catalog('zh-CN');
    const enCatalog = player.catalog('en-US');
    assert.equal(zhCatalog.length, 73);
    assert.equal(zhCatalog[0].name, '开心地回应喜欢和亲近');
    assert.equal(enCatalog[0].name, 'Happily respond with affection');
    assert.equal(enCatalog[0].path, '/static/vrm/animation/liked.vrma.gz');
    assert.equal(enCatalog[0].systemMotion, true);
    assert.equal(zhCatalog.some(function (asset) { return asset.id === 'cheer_01'; }), false);
    assert.equal(zhCatalog.some(function (asset) { return asset.id === 'recover_01'; }), false);

    let previewPlan = null;
    player.playPlan = async function (plan) {
        previewPlan = plan;
        return true;
    };
    await player.playAsset('official_liked', { scheduleNext: false });
    assert.equal(previewPlan.length, 1);
    assert.equal(previewPlan[0].intent, 'like');
    assert.equal(previewPlan[0].evidence.assetId, 'official_liked');
    assert.equal(previewPlan[0].scheduleNextRest, false);

    const noRotatePreviewPlayer = new global.NekoMotionPlayer();
    noRotatePreviewPlayer.select = function () { return { id: 'preview-gesture' }; };
    noRotatePreviewPlayer._playTransient = async function () { return true; };
    let resumedWithScheduleNext = null;
    noRotatePreviewPlayer.enterRest = async function (options) {
        resumedWithScheduleNext = options.scheduleNext;
        return true;
    };
    assert.equal(await noRotatePreviewPlayer._executeDecision({
        intent: 'wave',
        kind: 'social',
        evidence: {},
        scheduleNextRest: false
    }, noRotatePreviewPlayer.queueGeneration, 'preview', true), true);
    assert.equal(resumedWithScheduleNext, false);

    const unlicensed = JSON.parse(JSON.stringify(manifest));
    unlicensed.assets[0].license = '?';
    global.fetch = async function () { return response(unlicensed); };
    await assert.rejects(new global.NekoMotionPlayer().load(), /unapproved or unlicensed/);

    let requested = '';
    let requestedOptions = null;
    global.fetch = async function (url, options) {
        requested = String(url);
        requestedOptions = options;
        return response(sourceBuffer);
    };
    assert.match(await player._assetUrl(player.assets[0]), /^blob:test-/);
    assert.equal(requested, '/static/vrm/' + player.assets[0].f + '.gz');
    assert.equal(requestedOptions.cache, 'no-cache');
    assert.equal(requestedOptions.signal instanceof AbortSignal, true);

    const originalSetTimeout = global.setTimeout;
    const originalClearTimeout = global.clearTimeout;
    const originalFetch = global.fetch;
    try {
        global.setTimeout = function (handler) {
            queueMicrotask(handler);
            return 1;
        };
        global.clearTimeout = function () {};
        global.fetch = async function (url, options) {
            return {
                ok: true,
                status: 200,
                json: async function () {
                    return new Promise(function (resolve, reject) {
                        options.signal.addEventListener('abort', function () {
                            reject(options.signal.reason);
                        }, { once: true });
                    });
                }
            };
        };
        await assert.rejects(new global.NekoMotionPlayer().load(), /abort/i);
    } finally {
        global.fetch = originalFetch;
        global.setTimeout = originalSetTimeout;
        global.clearTimeout = originalClearTimeout;
    }

    const corrupted = Buffer.from(packedSource);
    corrupted[2] ^= 0xff;
    global.fetch = async function () {
        return response(corrupted.buffer.slice(corrupted.byteOffset, corrupted.byteOffset + corrupted.byteLength));
    };
    await assert.rejects(player._assetUrl(player.assets[0]), /packed SHA-256 mismatch/);

    const secureCryptoDescriptor = Object.getOwnPropertyDescriptor(global, 'crypto');
    Object.defineProperty(global, 'crypto', { configurable: true, value: undefined });
    global.fetch = async function () { return response(sourceBuffer); };
    assert.match(await player._assetUrl(player.assets[0]), /^blob:test-/);
    global.fetch = async function () {
        return response(corrupted.buffer.slice(corrupted.byteOffset, corrupted.byteOffset + corrupted.byteLength));
    };
    await assert.rejects(player._assetUrl(player.assets[0]), /packed SHA-256 mismatch/);
    Object.defineProperty(global, 'crypto', secureCryptoDescriptor);

    const packed = zlib.gzipSync(decodedSource);
    const gzipAsset = Object.assign({}, player.assets[0], {
        compression: 'gzip',
        f: 'motion-pack/example.vrma',
        packedSha: crypto.createHash('sha256').update(packed).digest('hex'),
        decodedSha: crypto.createHash('sha256').update(decodedSource).digest('hex')
    });
    // Install an explicit implementation here because the following fixtures
    // exercise both gzip bytes and already-decoded proxy responses.
    global.DecompressionStream = require('node:stream/web').DecompressionStream;
    global.fetch = async function (url) {
        requested = String(url);
        return response(packed.buffer.slice(packed.byteOffset, packed.byteOffset + packed.byteLength));
    };
    assert.match(await player._assetUrl(gzipAsset), /^blob:test-/);
    assert.equal(requested, '/static/vrm/motion-pack/example.vrma.gz');

    global.fetch = async function () {
        return response(decodedSource.buffer.slice(
            decodedSource.byteOffset,
            decodedSource.byteOffset + decodedSource.byteLength
        ));
    };
    assert.match(await player._assetUrl(gzipAsset), /^blob:test-/);

    const corruptedDecoded = Buffer.from(decodedSource);
    corruptedDecoded[0] ^= 0xff;
    global.fetch = async function () {
        return response(corruptedDecoded.buffer.slice(
            corruptedDecoded.byteOffset,
            corruptedDecoded.byteOffset + corruptedDecoded.byteLength
        ));
    };
    await assert.rejects(player._assetUrl(gzipAsset), /decoded SHA-256 mismatch/);

    let parsedBytes = null;
    let parsedResourcePath = '';
    const directLoader = {
        async parseAsync(bytes, resourcePath) {
            parsedBytes = Buffer.from(bytes);
            parsedResourcePath = resourcePath;
            return { userData: { vrmAnimations: [{}] } };
        }
    };
    global.fetch = async function () {
        return response(sourceBuffer);
    };
    const animation = Object.create(global.VRMAnimation.prototype);
    await animation._loadVRMAGltf(directLoader, '/static/vrm/animation/liked.vrma.gz');
    assert.deepEqual(parsedBytes, decodedSource);
    assert.equal(parsedResourcePath, 'http://localhost/static/vrm/animation/');

    const lowPosePlayer = new global.NekoMotionPlayer();
    lowPosePlayer.assets = [
        { id: 'sit', m: 'sit', in: 'stand', out: 'sit', i: 2, s: ['upright'], card: { styles: ['upright'] } },
        { id: 'lie-side', m: 'lie', in: 'stand', out: 'lie', i: 2, s: ['side'], card: { styles: ['side'] } },
        { id: 'lie-prone', m: 'lie', in: 'stand', out: 'lie', i: 2, s: ['prone'], card: { styles: ['prone'] } },
        { id: 'recover-side', m: 'recover', in: 'lie', out: 'stand', i: 2, s: ['side'], card: { styles: ['side'] } },
        { id: 'recover-prone', m: 'recover', in: 'lie', out: 'stand', i: 2, s: ['prone'], card: { styles: ['prone'] } }
    ];
    const playedLowPoseIds = [];
    lowPosePlayer._playAsset = async function (asset) {
        playedLowPoseIds.push(asset.id);
        return true;
    };
    lowPosePlayer._playTransient = async function (asset) {
        playedLowPoseIds.push(asset.id);
        return true;
    };
    await lowPosePlayer._enterLowPose({
        intent: 'lie', style: 'side', intensity: 2, evidence: { canonicalZh: '侧身躺下' }
    }, lowPosePlayer.queueGeneration, 'side');
    await lowPosePlayer._enterLowPose({
        intent: 'lie', style: 'prone', intensity: 2, evidence: { canonicalZh: '俯身趴下' }
    }, lowPosePlayer.queueGeneration, 'prone');
    assert.equal(lowPosePlayer.state.poseStyle, 'prone');
    assert.equal(lowPosePlayer.state.poseAsset.id, 'lie-prone');
    assert.deepEqual(playedLowPoseIds.slice(0, 2), ['lie-side', 'lie-prone']);

    lowPosePlayer.state.posture = 'lie';
    lowPosePlayer.state.poseAsset = lowPosePlayer.assets[2];
    lowPosePlayer.state.poseStyle = 'prone';
    await lowPosePlayer._enterLowPose({
        intent: 'sit', style: 'upright', intensity: 2, evidence: { canonicalZh: '坐起来' }
    }, lowPosePlayer.queueGeneration, 'sit-after-prone');
    assert.deepEqual(playedLowPoseIds.slice(-2), ['recover-prone', 'sit']);
    assert.equal(lowPosePlayer.state.posture, 'sit');

    const cancelPlayer = new global.NekoMotionPlayer();
    let resumedAfterCancel = 0;
    cancelPlayer._resumeBase = async function () { resumedAfterCancel += 1; return true; };
    cancelPlayer.state.posture = 'lie';
    cancelPlayer.state.phase = 'pose';
    cancelPlayer.state.currentAsset = { id: 'held-pose' };
    cancelPlayer.state.restAsset = { id: 'old-rest' };
    cancelPlayer.state.poseAsset = { id: 'held-pose' };
    cancelPlayer.state.poseStyle = 'side';
    cancelPlayer.cancel('manual-stop', { resume: false });
    await Promise.resolve();
    assert.equal(resumedAfterCancel, 0);
    assert.deepEqual({
        posture: cancelPlayer.state.posture,
        phase: cancelPlayer.state.phase,
        currentAsset: cancelPlayer.state.currentAsset,
        restAsset: cancelPlayer.state.restAsset,
        poseAsset: cancelPlayer.state.poseAsset,
        poseStyle: cancelPlayer.state.poseStyle
    }, {
        posture: 'stand',
        phase: 'boot',
        currentAsset: null,
        restAsset: null,
        poseAsset: null,
        poseStyle: null
    });

    const heldCancelPlayer = new global.NekoMotionPlayer();
    const heldCancelResumeAssets = [];
    heldCancelPlayer._playAsset = async function (asset) {
        heldCancelResumeAssets.push(asset.id);
        return true;
    };
    heldCancelPlayer.state.posture = 'lie';
    heldCancelPlayer.state.phase = 'pose';
    heldCancelPlayer.state.poseAsset = { id: 'held-pose', mode: 'loop' };
    heldCancelPlayer.state.poseStyle = 'side';
    heldCancelPlayer.holdExternalPlayback('jukebox', { token: 91 });
    heldCancelPlayer.cancel('assistant_speech_cancel', { resume: true });
    await Promise.resolve();
    assert.equal(heldCancelResumeAssets.length, 0, 'cancel recovery must not replace held external playback');
    assert.equal(heldCancelPlayer.state.phase, 'external');
    assert.equal(await heldCancelPlayer.releaseExternalPlayback('jukebox', {
        token: 91,
        resume: true,
        scheduleNext: false
    }), true);
    assert.deepEqual(
        heldCancelResumeAssets,
        ['held-pose'],
        'the saved pose may resume after the final external release'
    );

    const staleCatalogPlayer = new global.NekoMotionPlayer();
    const staleCatalogAsset = { id: 'stale-catalog', m: 'wave', i: 2 };
    let finishCatalogLoad;
    let catalogShouldApply;
    const catalogLoadStarted = deferred();
    staleCatalogPlayer._assetUrl = async function () { return 'blob:test-stale-catalog'; };
    staleCatalogPlayer._manager = function () {
        return {
            playVRMAAnimation(url, options) {
                assert.equal(url, 'blob:test-stale-catalog');
                catalogShouldApply = options.shouldApply;
                catalogLoadStarted.resolve();
                return new Promise(function (resolve) { finishCatalogLoad = resolve; });
            }
        };
    };
    const staleCatalogRequest = staleCatalogPlayer._playAsset(
        staleCatalogAsset,
        staleCatalogPlayer.queueGeneration,
        {}
    );
    await catalogLoadStarted.promise;
    staleCatalogPlayer.cancel('model_manager_pause', { resume: false });
    assert.equal(catalogShouldApply(), false);
    finishCatalogLoad(false);
    assert.equal(await staleCatalogRequest, false);
    assert.equal(staleCatalogPlayer.state.currentAsset, null);
    assert.equal(staleCatalogPlayer.metrics.played, 0);

    const failedPlanPlayer = new global.NekoMotionPlayer();
    failedPlanPlayer._executeDecision = async function () { return false; };
    assert.equal(await failedPlanPlayer.playPlan([{ intent: 'wave' }]), false);

    const loopPlayer = new global.NekoMotionPlayer();
    loopPlayer.manifest = {};
    loopPlayer.assets = [{
        id: 'manual-loop', m: 'dance', in: 'stand', out: 'stand', i: 2,
        mode: 'loop', card: { kind: 'show' }
    }];
    loopPlayer._assetUrl = async function () { return 'blob:test-manual-loop'; };
    let manualLoopOption = null;
    loopPlayer._manager = function () {
        return {
            async playVRMAAnimation(url, options) {
                assert.equal(url, 'blob:test-manual-loop');
                manualLoopOption = options.loop;
                return true;
            }
        };
    };
    loopPlayer._wait = async function () {
        throw new Error('manual loop previews must not wait for a transient tail');
    };
    assert.equal(await loopPlayer.playAsset('manual-loop'), true);
    assert.equal(manualLoopOption, true);

    const expression = new global.VRMExpression({});
    assert.deepEqual(expression._resolveMoodWeights('shy', ['relaxed', 'happy']), {
        relaxed: 0.55,
        happy: 0.18
    });
    expression.setMoodMap({ shy: ['custom_blush'] });
    assert.deepEqual(expression._resolveMoodWeights('shy', ['custom_blush', 'happy']), {
        custom_blush: 1
    });
    expression.setMoodMap({ happy: ['model_happy'] });
    assert.deepEqual(expression._resolveMoodWeights('happy', ['happy', 'model_happy']), {
        model_happy: 1
    });

    const switchingExpression = new global.VRMExpression({});
    const firstMoodResponse = deferred();
    const secondMoodResponse = deferred();
    global.fetch = function (url) {
        return String(url).includes('first-model')
            ? firstMoodResponse.promise : secondMoodResponse.promise;
    };
    const firstMoodLoad = switchingExpression.loadMoodMap('first-model');
    const secondMoodLoad = switchingExpression.loadMoodMap('second-model');
    secondMoodResponse.resolve(response({ success: true, config: { happy: ['second_happy'] } }));
    await secondMoodLoad;
    firstMoodResponse.resolve(response({ success: true, config: { happy: ['first_happy'] } }));
    await firstMoodLoad;
    assert.deepEqual(switchingExpression.getMoodMap().happy, ['second_happy']);
    assert.equal(switchingExpression.customMoodKeys.has('happy'), true);

    const savedRestPlayer = new global.NekoMotionPlayer();
    savedRestPlayer.assets = [];
    assert.equal(savedRestPlayer.setSavedRestAnimations([
        '/user_vrm/animation/custom-idle.vrma',
        '/static/vrm/animation/custom-idle.vrma',
        '/static/vrm/animation/wait01.vrma'
    ]), 3);
    assert.equal(savedRestPlayer.savedRestAssets[1].url, '/static/vrm/animation/custom-idle.vrma');
    assert.equal(savedRestPlayer.savedRestAssets[2].url, '/static/vrm/animation/wait01.vrma.gz');
    savedRestPlayer.state.restAsset = savedRestPlayer.savedRestAssets[0];
    assert.equal(savedRestPlayer.setSavedRestAnimations([]), 0);
    assert.equal(savedRestPlayer.state.restAsset, null);
    savedRestPlayer.setSavedRestAnimations([
        '/user_vrm/animation/custom-idle.vrma',
        '/static/vrm/animation/wait01.vrma'
    ]);
    let savedRestUrl = '';
    savedRestPlayer._manager = function () {
        return {
            async playVRMAAnimation(url) {
                savedRestUrl = url;
                return true;
            }
        };
    };
    await savedRestPlayer.enterRest({
        assetId: savedRestPlayer.savedRestAssets[0].id,
        force: true,
        scheduleNext: false
    });
    assert.equal(savedRestUrl, '/user_vrm/animation/custom-idle.vrma');

    const savedCatalogPlayer = new global.NekoMotionPlayer();
    savedCatalogPlayer.assets = [
        {
            id: 'saved-built-in',
            m: 'idle',
            f: 'animation/wait02.vrma',
            compression: 'gzip',
            card: {}
        },
        {
            id: 'unselected-system-rest',
            m: 'idle',
            f: 'animation/wait03.vrma',
            compression: 'gzip',
            card: { systemRestEligible: true }
        }
    ];
    savedCatalogPlayer.state.restAsset = savedCatalogPlayer.assets[1];
    assert.equal(savedCatalogPlayer.setSavedRestAnimations([
        '/static/vrm/animation/wait02.vrma'
    ]), 1);
    assert.equal(savedCatalogPlayer.savedRestAssets.length, 0);
    assert.equal(savedCatalogPlayer.state.restAsset, null);
    assert.equal(savedCatalogPlayer.select({
        intent: 'idle',
        systemRest: true
    }, 'saved-built-in', 'stand').id, 'saved-built-in');

    savedCatalogPlayer.assets = [{
        id: 'official_wait_04',
        m: 'shy',
        f: 'animation/wait04.vrma',
        compression: 'gzip',
        card: { systemRestEligible: false }
    }];
    savedCatalogPlayer.setSavedRestAnimations([
        '/static/vrm/animation/wait04.vrma'
    ]);
    assert.equal(savedCatalogPlayer.savedRestAssets.length, 1);
    assert.equal(
        savedCatalogPlayer.savedRestAssets[0].url,
        '/static/vrm/animation/wait04.vrma.gz'
    );

    const scheduledRestPlayer = new global.NekoMotionPlayer();
    scheduledRestPlayer.assets = [{
        id: 'queued-rest',
        m: 'idle',
        in: 'stand',
        out: 'stand',
        mode: 'loop',
        card: { systemRestEligible: true }
    }];
    scheduledRestPlayer._playAsset = async function () { return true; };
    scheduledRestPlayer.busy = true;
    assert.equal(
        await scheduledRestPlayer.enterRest({
            assetId: 'queued-rest',
            force: true,
            seed: 'queued-rest'
        }),
        true
    );
    assert.notEqual(scheduledRestPlayer.idleSwitchTimer, null);
    const generationBeforeExternalPlayback = scheduledRestPlayer.queueGeneration;
    assert.equal(
        scheduledRestPlayer.holdExternalPlayback('jukebox', { token: 41 }),
        true
    );
    assert.equal(scheduledRestPlayer.idleSwitchTimer, null);
    assert.equal(scheduledRestPlayer.state.phase, 'external');
    assert.equal(scheduledRestPlayer.queueGeneration, generationBeforeExternalPlayback + 1);
    assert.equal(scheduledRestPlayer.resumeIdleCountdown('while-dancing'), false);
    assert.equal(await scheduledRestPlayer.enterRest({ force: true }), false);
    assert.equal(await scheduledRestPlayer.playPlan([{ intent: 'wave' }]), false);

    // A newer song reuses the same owner but replaces its token. The stale
    // loader must not release the newer dance when it finally settles.
    scheduledRestPlayer.holdExternalPlayback('jukebox', { token: 42 });
    assert.equal(
        await scheduledRestPlayer.releaseExternalPlayback('jukebox', { resume: true }),
        false,
        'a tokenized hold must not be released without its token'
    );
    assert.equal(scheduledRestPlayer.externalPlaybackOwners.get('jukebox'), 42);
    assert.equal(
        await scheduledRestPlayer.releaseExternalPlayback('jukebox', {
            token: 41,
            resume: true
        }),
        false
    );
    let externalResume = null;
    scheduledRestPlayer._resumeBase = async function (generation, seed, scheduleNext) {
        externalResume = { generation, seed, scheduleNext };
        return true;
    };
    assert.equal(
        await scheduledRestPlayer.releaseExternalPlayback('jukebox', {
            token: 42,
            resume: true,
            scheduleNext: true
        }),
        true
    );
    assert.equal(scheduledRestPlayer.externalPlaybackOwners.size, 0);
    assert.equal(externalResume.seed, 'external-release:jukebox');
    assert.equal(externalResume.scheduleNext, true);

    const staleQueuePlayer = new global.NekoMotionPlayer();
    const staleFirst = staleQueuePlayer.enqueuePlan([{ intent: 'wave' }], { seed: 'first' });
    const staleSecond = staleQueuePlayer.enqueuePlan([{ intent: 'nod' }], { seed: 'second' });
    staleQueuePlayer.holdExternalPlayback('jukebox', { token: 51 });
    assert.deepEqual(await Promise.all([staleFirst, staleSecond]), [false, false]);
    assert.equal(staleQueuePlayer.busy, false);
    assert.equal(await staleQueuePlayer.releaseExternalPlayback('jukebox', {
        token: 51,
        resume: false
    }), true);
    assert.equal(staleQueuePlayer.busy, false);

    savedCatalogPlayer.assets = [{
        id: 'saved-motion-pack',
        m: 'idle',
        f: 'motion/01_base/natural-wait.vrma',
        compression: 'gzip',
        card: { systemRestEligible: true }
    }];
    savedCatalogPlayer.setSavedRestAnimations([
        '/static/vrm/motion/01_base/natural-wait.vrma?legacy=1'
    ]);
    assert.equal(
        savedCatalogPlayer.savedRestAssets[0].url,
        '/static/vrm/motion/01_base/natural-wait.vrma.gz?legacy=1'
    );

    const framing = global.NekoVRMSafeFraming;
    assert.equal(framing.calculateFramingRatio({
        minX: 100, maxX: 900, minY: 100, maxY: 900
    }, 1000, 1000, 50) < 1, true);
    assert.equal(framing.calculateExpandedFov(30, {
        minX: -100, maxX: 1100, minY: 0, maxY: 1000
    }, 1000, 1000, 50, 44) > 30, true);

    const failingAnimation = Object.create(global.VRMAnimation.prototype);
    const failingVrm = {
        scene: { uuid: 'failure-scene', traverse() {} },
        humanoid: { autoUpdateHumanBones: true }
    };
    failingAnimation.manager = { currentModel: { vrm: failingVrm } };
    failingAnimation._playRequestGeneration = 0;
    failingAnimation._skinnedMeshes = [];
    failingAnimation._cachedSceneUuid = null;
    failingAnimation._fadeTimer = null;
    failingAnimation.currentAction = null;
    failingAnimation._cleanupOldMixer = function () {};
    failingAnimation._initLoader = async function () { return {}; };
    failingAnimation._loadVRMAGltf = async function () { throw new Error('fixture load failure'); };
    await assert.rejects(
        failingAnimation.playVRMAAnimation('/broken.vrma'),
        /fixture load failure/
    );
    assert.equal(failingVrm.humanoid.autoUpdateHumanBones, true);

    let finishDelayedLoad;
    let delayedRequestCurrent = true;
    let staleRequestPlayed = false;
    const staleAnimation = Object.create(global.VRMAnimation.prototype);
    const staleVrm = {
        scene: { uuid: 'stale-scene', traverse() {} },
        humanoid: { autoUpdateHumanBones: true }
    };
    staleAnimation.manager = { currentModel: { vrm: staleVrm } };
    staleAnimation._playRequestGeneration = 0;
    staleAnimation._skinnedMeshes = [];
    staleAnimation._cachedSceneUuid = null;
    staleAnimation._fadeTimer = null;
    staleAnimation._springBoneRestoreTimer = null;
    staleAnimation.currentAction = null;
    staleAnimation.vrmaMixer = null;
    staleAnimation._restorePhysics = function () {
        staleVrm.humanoid.autoUpdateHumanBones = true;
    };
    staleAnimation._cleanupOldMixer = function () {};
    staleAnimation._initLoader = async function () { return {}; };
    staleAnimation._loadVRMAGltf = function () {
        return new Promise(function (resolve) { finishDelayedLoad = resolve; });
    };
    staleAnimation._playAction = function () { staleRequestPlayed = true; };
    const staleRequest = staleAnimation.playVRMAAnimation('/delayed.vrma', {
        shouldApply() { return delayedRequestCurrent; }
    });
    await waitForLoadStart(
        function () { return finishDelayedLoad; },
        'delayed VRMA load did not start'
    );
    delayedRequestCurrent = false;
    finishDelayedLoad({ userData: { vrmAnimations: [{}] } });
    assert.equal(await staleRequest, false);
    assert.equal(staleRequestPlayed, false);
    assert.equal(staleVrm.humanoid.autoUpdateHumanBones, true);

    let finishStoppedLoad;
    let stoppedRequestPlayed = false;
    const stoppedAnimation = Object.create(global.VRMAnimation.prototype);
    const stoppedVrm = {
        scene: { uuid: 'stopped-scene', traverse() {} },
        humanoid: { autoUpdateHumanBones: true }
    };
    stoppedAnimation.manager = { currentModel: { vrm: stoppedVrm } };
    stoppedAnimation._playRequestGeneration = 0;
    stoppedAnimation._skinnedMeshes = [];
    stoppedAnimation._cachedSceneUuid = null;
    stoppedAnimation._fadeTimer = null;
    stoppedAnimation._springBoneRestoreTimer = null;
    stoppedAnimation.currentAction = null;
    stoppedAnimation.vrmaMixer = null;
    stoppedAnimation._cleanupOldMixer = function () {};
    stoppedAnimation._restorePhysics = function () {
        stoppedVrm.humanoid.autoUpdateHumanBones = true;
    };
    stoppedAnimation._initLoader = async function () { return {}; };
    stoppedAnimation._loadVRMAGltf = function () {
        return new Promise(function (resolve) { finishStoppedLoad = resolve; });
    };
    stoppedAnimation._playAction = function () { stoppedRequestPlayed = true; };
    const stoppedRequest = stoppedAnimation.playVRMAAnimation('/stopped-during-load.vrma');
    await waitForLoadStart(
        function () { return finishStoppedLoad; },
        'stopped VRMA load did not start'
    );
    stoppedAnimation.stopVRMAAnimation();
    finishStoppedLoad({ userData: { vrmAnimations: [{}] } });
    assert.equal(await stoppedRequest, false);
    assert.equal(stoppedRequestPlayed, false);
    assert.equal(stoppedVrm.humanoid.autoUpdateHumanBones, true);

    console.log('VRM motion player: OK (integrity and low-pose transitions)');
})().catch(function (error) {
    console.error(error.stack || error);
    process.exitCode = 1;
});
