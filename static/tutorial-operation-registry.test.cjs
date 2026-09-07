const assert = require('node:assert/strict');
const test = require('node:test');

const { OperationRegistry } = require('./tutorial/core/operation-registry.js');

test('OperationRegistry exposes configurable exact, prefix and predicate operation handlers', async () => {
    const calls = [];
    const registry = new OperationRegistry({
        waitForSceneDelay() {
            calls.push('wait');
            return Promise.resolve();
        }
    });

    registry.registerOperation('exact-op', function (context) {
        calls.push(['exact', context.operation, context.scene.id]);
        return 'exact-result';
    });
    registry.registerOperation({ prefix: 'prefix-op:' }, function (context) {
        calls.push(['prefix', context.operation]);
        return 'prefix-result';
    });
    registry.registerOperation((context) => context.scene && context.scene.usePredicate === true, function (context) {
        calls.push(['predicate', context.operation]);
        return 'predicate-result';
    });

    assert.equal(await registry.run({ id: 'scene-a', operation: 'exact-op' }, null, 10), 'exact-result');
    assert.equal(await registry.run({ id: 'scene-b', operation: 'prefix-op:item' }, null, 10), 'prefix-result');
    assert.equal(await registry.run({ id: 'scene-c', operation: 'unknown', usePredicate: true }, null, 10), 'predicate-result');
    assert.deepEqual(calls, [
        ['exact', 'exact-op', 'scene-a'],
        ['prefix', 'prefix-op:item'],
        ['predicate', 'unknown']
    ]);
});

test('OperationRegistry built-ins are registered declaratively', async () => {
    const registry = new OperationRegistry({
        openSettingsPanel() {
            return 'settings-opened';
        },
        waitForSceneDelay() {
            return Promise.resolve();
        },
        tourMiniGameChoiceButtons() {
            return Promise.resolve();
        }
    });

    assert.ok(Array.isArray(registry.operationHandlers));
    assert.ok(registry.operationHandlers.length > 10);
    assert.equal(await registry.run({ operation: 'day3-open-settings-personalization' }), 'settings-opened');
    assert.equal(await registry.run({ operation: 'cleanup' }), true);
    assert.equal(await registry.run({ operation: 'day1-managed-scene-settled:done' }), true);
    assert.equal(await registry.run({ id: 'day2_galgame_games' }), true);
});

test('Day1 avatar zoom hint temporarily returns wheel interaction and the real cursor', async () => {
    const calls = [];
    const previousDocument = global.document;
    const previousWindow = global.window;
    const createClassList = (scope) => ({
        toggle(name, active) {
            calls.push(['class', scope, name, active]);
        }
    });
    global.document = {
        documentElement: { classList: createClassList('html') },
        body: { classList: createClassList('body') }
    };
    global.window = {
        live2dManager: {
            isLocked: true,
            setLocked(locked, options) {
                this.isLocked = locked;
                calls.push(['live2d-locked', locked, options.updateFloatingButtons]);
            }
        }
    };
    const registry = new OperationRegistry({
        overlay: {
            setInteractionShieldSuppressed(active) {
                calls.push(['shield', active]);
            }
        },
        clearUserCursorRevealSuppression(resetCursor) {
            calls.push(['clear-user-cursor-reveal', resetCursor]);
        },
        disableInterrupts() {
            calls.push('disable-interrupts');
        },
        cursor: {
            cancel() {
                calls.push('cancel-ghost-cursor');
            },
            hide() {
                calls.push('hide-ghost-cursor');
            }
        },
        syncSystemCursorHidden(hidden, reason) {
            calls.push(['system-cursor-hidden', hidden, reason]);
        },
        restoreDay1TakeoverAgentSwitches(reason) {
            calls.push(['restore-agent-switches', reason]);
            return Promise.resolve(true);
        }
    });

    try {
        assert.equal(await registry.run({ id: 'day1_avatar_zoom_hint', operation: 'cleanup' }), true);
        assert.equal(await registry.run({ id: 'day1_takeover_return_control', operation: 'cleanup' }), true);
    } finally {
        global.document = previousDocument;
        global.window = previousWindow;
    }

    assert.deepEqual(calls, [
        ['restore-agent-switches', 'day1-before-avatar-zoom-hint'],
        ['shield', true],
        ['clear-user-cursor-reveal', true],
        'disable-interrupts',
        'cancel-ghost-cursor',
        'hide-ghost-cursor',
        ['live2d-locked', false, false],
        ['class', 'html', 'yui-user-cursor-revealed', true],
        ['class', 'body', 'yui-user-cursor-revealed', true],
        ['system-cursor-hidden', false, 'day1-avatar-zoom-hint'],
        ['shield', false],
        ['clear-user-cursor-reveal', true],
        ['live2d-locked', true, false],
        ['class', 'html', 'yui-user-cursor-revealed', false],
        ['class', 'body', 'yui-user-cursor-revealed', false],
        ['system-cursor-hidden', true, 'day1-return-control'],
        ['restore-agent-switches', 'day1-return-control']
    ]);
});

test('Day1 screen share flow opens the mic panel before highlighting the row and moving to its button', async () => {
    const calls = [];
    const screenShareButton = {};
    const screenShareRow = {
        querySelector(selector) {
            calls.push(['row-query', selector]);
            return screenShareButton;
        }
    };
    const popup = {
        querySelector(selector) {
            calls.push(['query', selector]);
            return screenShareRow;
        }
    };
    const registry = new OperationRegistry({
        openMicPanel() {
            calls.push('openMicPanel');
            return Promise.resolve(true);
        },
        isStopping() {
            return false;
        },
        waitForElement(resolve) {
            calls.push('waitForScreenShareToggle');
            return Promise.resolve(resolve());
        },
        getManagedPanelElement(panelId) {
            calls.push(['panel', panelId]);
            return popup;
        },
        getElementRect(target) {
            calls.push(['rect', target === screenShareRow ? 'row' : target === screenShareButton ? 'button' : 'other']);
            return target === screenShareRow || target === screenShareButton
                ? { width: 48, height: 28 }
                : null;
        },
        setSpotlightGeometryHint(target, options) {
            calls.push(['geometry', target === screenShareRow, options.geometry]);
        },
        applyGuideHighlights(config) {
            calls.push(['highlight', config.primary === screenShareRow]);
        },
        moveCursorToElement(target, durationMs) {
            calls.push(['move', target === screenShareButton, durationMs]);
            return Promise.resolve(true);
        }
    });

    assert.equal(await registry.run({ id: 'day1_screen_entry', operation: 'day1-screen-share-entry-flow' }), true);
    assert.deepEqual(calls, [
        'openMicPanel',
        'waitForScreenShareToggle',
        ['panel', 'mic'],
        ['query', '[data-neko-mic-main-action-row="screen"]'],
        ['rect', 'row'],
        ['row-query', '[data-neko-mic-main-action="screen"]'],
        ['rect', 'button'],
        ['geometry', true, 'rounded-rect'],
        ['highlight', true],
        ['move', true, 760]
    ]);
});

test('OperationRegistry routes daily intro greeting to generic performance only', async () => {
    const calls = [];
    const registry = new OperationRegistry({
        runDailyIntroGreetingPerformance(scene) {
            calls.push(scene.id);
            return Promise.resolve('daily-intro-complete');
        },
        runIntroGiftHeartPerformance() {
            calls.push('gift-heart');
            return Promise.resolve();
        },
        waitForSceneDelay() {
            return Promise.resolve();
        }
    });

    const result = await registry.run({
        id: 'day2_intro_context',
        operation: 'daily-intro-greeting-performance'
    });

    assert.equal(result, 'daily-intro-complete');
    assert.deepEqual(calls, ['day2_intro_context']);
});

test('OperationRegistry routes daily intro avatar motion presets through the director', async () => {
    const calls = [];
    const revealPrepared = () => 'revealed';
    const registry = new OperationRegistry({
        runDailyIntroAvatarPerformance(scene, day, options) {
            calls.push([
                scene.id,
                scene.introAvatarPerformance && scene.introAvatarPerformance.preset,
                options && options.revealPrepared
            ]);
            return Promise.resolve('avatar-motion-complete');
        },
        waitForSceneDelay() {
            return Promise.resolve();
        }
    });

    const result = await registry.run({
        id: 'day5_character_settings',
        operation: 'daily-intro-avatar-performance',
        introAvatarPerformance: {
            preset: 'top-peek'
        }
    }, null, 0, null, { revealPrepared });

    assert.equal(result, 'avatar-motion-complete');
    assert.deepEqual(calls, [['day5_character_settings', 'top-peek', revealPrepared]]);
});
