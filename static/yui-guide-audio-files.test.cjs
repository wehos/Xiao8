const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { readDirectorSource } = require('./yui-guide-director-test-parts.cjs');
const test = require('node:test');
const vm = require('node:vm');

const repoRoot = path.resolve(__dirname, '..');
const guideAudioRoot = path.join(__dirname, 'assets', 'tutorial', 'guide-audio');
const directorSource = readDirectorSource(__dirname);
const supportedRecordedLocales = ['zh', 'ja', 'en', 'ko', 'ru'];
const guideFiles = [
    'tutorial/yui-guide/days/day1-home-guide.js',
    'tutorial/yui-guide/days/day2-screen-voice-guide.js',
    'tutorial/yui-guide/days/day3-interaction-guide.js',
    'tutorial/yui-guide/days/day4-companion-guide.js',
    'tutorial/yui-guide/days/day5-personalization-guide.js',
    'tutorial/yui-guide/days/day6-agent-guide.js',
    'tutorial/yui-guide/days/day7-graduation-guide.js'
];

function loadGuides() {
    const context = vm.createContext({ window: {} });
    const helperPath = path.join(__dirname, 'tutorial/core/guide-helpers.js');
    const commonPath = path.join(__dirname, 'tutorial/yui-guide/common.js');
    vm.runInContext(fs.readFileSync(helperPath, 'utf8'), context, { filename: helperPath });
    vm.runInContext(fs.readFileSync(commonPath, 'utf8'), context, { filename: commonPath });
    for (const fileName of guideFiles) {
        const filePath = path.join(__dirname, fileName);
        vm.runInContext(fs.readFileSync(filePath, 'utf8'), context, { filename: filePath });
    }
    return context.window.YuiGuideDailyGuides || {};
}

function collectRoundVoiceKeys(guides) {
    const keys = [];
    for (const day of [1, 2, 3, 4, 5, 6, 7]) {
        const guide = guides[day] || {};
        const scenes = guide.round && Array.isArray(guide.round.scenes)
            ? guide.round.scenes
            : [];
        for (const scene of scenes) {
            if (typeof scene.voiceKey === 'string' && scene.voiceKey.trim()) {
                keys.push({ day, sceneId: scene.id, voiceKey: scene.voiceKey.trim() });
            }
        }
    }
    return keys;
}

function mergeAudioFilesByKey(guides) {
    const result = {};
    for (const day of [1, 2, 3, 4, 5, 6, 7]) {
        Object.assign(result, (guides[day] && guides[day].audioFilesByKey) || {});
    }
    return result;
}

function collectGuideAudioDurationConfig() {
    const start = directorSource.indexOf('const GUIDE_AUDIO_DURATIONS_BY_KEY = Object.freeze({');
    const end = directorSource.indexOf('\n    });', start);
    assert.ok(start >= 0 && end > start, 'expected GUIDE_AUDIO_DURATIONS_BY_KEY block');

    const result = {};
    const source = directorSource.slice(start, end);
    const entryPattern = /([A-Za-z0-9_]+): Object\.freeze\(\{([^}]+)\}\)/g;
    let entryMatch;
    while ((entryMatch = entryPattern.exec(source))) {
        const durations = {};
        const localePattern = /([a-z-]+):\s*(\d+)/g;
        let localeMatch;
        while ((localeMatch = localePattern.exec(entryMatch[2]))) {
            durations[localeMatch[1]] = Number(localeMatch[2]);
        }
        result[entryMatch[1]] = durations;
    }
    return result;
}

function readAudioDurationMs(audioPath, ffprobeCommand) {
    const output = execFileSync(
        ffprobeCommand,
        ['-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', audioPath],
        { encoding: 'utf8' }
    );
    return Math.round(Number(output.trim()) * 1000);
}

function resolveFfprobeCommand() {
    try {
        execFileSync('ffprobe', ['-version'], { stdio: 'ignore' });
        return 'ffprobe';
    } catch (_) {
        return '';
    }
}

test('daily tutorial round scenes have recorded audio files for supported locales', () => {
    const guides = loadGuides();
    const audioFilesByKey = mergeAudioFilesByKey(guides);
    const missing = [];

    for (const entry of collectRoundVoiceKeys(guides)) {
        const files = audioFilesByKey[entry.voiceKey];
        const missingKey = `${entry.day}:${entry.sceneId}:${entry.voiceKey}`;
        for (const locale of supportedRecordedLocales) {
            const audioFile = files && typeof files[locale] === 'string' ? files[locale] : '';
            if (!audioFile || !fs.existsSync(path.join(guideAudioRoot, locale, audioFile))) {
                missing.push(`${missingKey}:${locale}:${audioFile || '<no file>'}`);
            }
        }
    }

    assert.deepEqual(missing, []);
});

test('daily tutorial audio keys have measured duration config for supported locales', (t) => {
    const ffprobeCommand = resolveFfprobeCommand();
    if (!ffprobeCommand) {
        t.skip('ffprobe unavailable; skipping measured audio duration comparison');
        return;
    }

    const guides = loadGuides();
    const audioFilesByKey = mergeAudioFilesByKey(guides);
    const durationConfigByKey = collectGuideAudioDurationConfig();
    const missing = [];
    const mismatched = [];

    for (const key of Object.keys(audioFilesByKey).sort()) {
        for (const locale of supportedRecordedLocales) {
            const configuredDurationMs = durationConfigByKey[key] && durationConfigByKey[key][locale];
            if (!Number.isFinite(configuredDurationMs)) {
                missing.push(`${key}:${locale}`);
                continue;
            }

            const audioFile = audioFilesByKey[key] && audioFilesByKey[key][locale];
            if (!audioFile) {
                missing.push(`${key}:${locale}:audio-file`);
                continue;
            }
            const audioPath = path.join(guideAudioRoot, locale, audioFile);
            const measuredDurationMs = readAudioDurationMs(audioPath, ffprobeCommand);
            if (Math.abs(configuredDurationMs - measuredDurationMs) > 25) {
                mismatched.push(`${key}:${locale}:configured=${configuredDurationMs}:measured=${measuredDurationMs}`);
            }
        }
    }

    assert.deepEqual(missing, []);
    // 修改原因：教程转场和光标演出依赖这张表，必须跟实际 mp3 时长同步，不能只检查“有数字”。
    assert.deepEqual(mismatched, []);
});

test('day4 model lock replacement audio URL is versioned for immutable static caches', () => {
    assert.match(directorSource, /avatar_floating_day4_model_lock:\s*'20260701'/);
    assert.match(directorSource, /\?v=' \+ encodeURIComponent\(version\)/);
});

test('avatar floating narration duration does not estimate tutorial audio from text', () => {
    const methodMatch = directorSource.match(/getAvatarFloatingNarrationDurationMs\(voiceKey, text\)\s*\{([\s\S]*?)\n\s*\}/);

    assert.ok(methodMatch, 'expected getAvatarFloatingNarrationDurationMs');
    assert.match(methodMatch[1], /getGuideVoiceDurationMs/);
    assert.doesNotMatch(methodMatch[1], /estimateSpeechDurationMs/);
});

test('day3 proactive chat uses its own recorded line instead of the detail narration after day swap', () => {
    const guides = loadGuides();
    const day3Scenes = guides[3].round.scenes;
    const proactiveScene = day3Scenes.find(scene => scene.id === 'day3_proactive_chat');

    assert.equal(proactiveScene.voiceKey, 'avatar_floating_day3_proactive_chat');
});

test('day3 voice-used intro has recorded audio files for supported locales after day swap', () => {
    const guides = loadGuides();
    const audioFilesByKey = mergeAudioFilesByKey(guides);
    const files = audioFilesByKey.avatar_floating_day3_intro_voice_used || {};
    const missing = [];

    for (const locale of supportedRecordedLocales) {
        const audioFile = typeof files[locale] === 'string' ? files[locale] : '';
        if (!audioFile || !fs.existsSync(path.join(guideAudioRoot, locale, audioFile))) {
            missing.push(`${locale}:${audioFile || '<no file>'}`);
        }
    }

    assert.deepEqual(missing, []);
});

test('day1 round scenes use timeline playback while specialized behavior delegates to operations', () => {
    const guides = loadGuides();
    const day1Scenes = guides[1].round.scenes;
    const timelineSceneIds = day1Scenes
        .filter(scene => scene.timelinePlayback === true)
        .map(scene => scene.id);

    // 修改原因：Day1 问候阶段和 Day2/Day4 日常开场一样，必须走显式 timeline 才能把胶囊高亮目标传给 spotlight。
    assert.equal(timelineSceneIds.length, 10);
    assert.equal(timelineSceneIds[0], 'day1_intro_activation');
    assert.equal(timelineSceneIds[1], 'day1_intro_greeting');
    assert.equal(timelineSceneIds[2], 'day1_capsule_drag_hint');
    assert.equal(timelineSceneIds[3], 'day1_history_handle');
    assert.equal(timelineSceneIds[4], 'day1_intro_basic_voice');
    assert.equal(timelineSceneIds[5], 'day1_screen_entry');
    assert.equal(timelineSceneIds[6], 'day1_screen_entry_invite');
    assert.equal(timelineSceneIds[7], 'day1_takeover_capture_cursor');
    assert.equal(timelineSceneIds[8], 'day1_avatar_zoom_hint');
    assert.equal(timelineSceneIds[9], 'day1_takeover_return_control');
});

test('day1 activation delegates timing through timeline while greeting follows the daily capsule intro pattern', () => {
    const guides = loadGuides();
    const day1Scenes = guides[1].round.scenes;
    const activation = day1Scenes.find(scene => scene.id === 'day1_intro_activation');
    const greeting = day1Scenes.find(scene => scene.id === 'day1_intro_greeting');
    const activationOperation = activation.timeline.find(event => event.command === 'operation.run');
    const greetingSpotlight = greeting.timeline.find(event => event.command === 'spotlight.show');
    const greetingCursor = greeting.timeline.find(event => event.command === 'cursor.move');

    assert.equal(activation.timelinePlayback, true);
    assert.equal(activation.timelineAudio, false);
    assert.equal(activation.afterSceneDelayMs, 0);
    assert.equal(activationOperation.operation, 'day1-intro-activation-flow');
    assert.equal(activationOperation.blocking, true);

    // 修改原因：参照 Day2/Day4，胶囊输入框 spotlight 和 cursor 必须显式指向同一个 chat-capsule-input。
    assert.equal(greeting.timelinePlayback, true);
    assert.equal(greeting.afterSceneDelayMs, 0);
    assert.equal(greetingSpotlight.target, 'chat-capsule-input');
    assert.equal(greetingCursor.target, 'chat-capsule-input');
    assert.equal(greeting.target, 'chat-capsule-input');
    assert.equal(greeting.cursorTarget, 'chat-capsule-input');
    assert.equal(greeting.spotlightVariant, undefined);
    assert.equal(greeting.cursorAction, 'move');
    assert.equal(greeting.operation, 'day1-intro-greeting-performance');
});

test('day1 intro basic voice starts showcase from timeline after narration starts', () => {
    const guides = loadGuides();
    const scene = guides[1].round.scenes.find(item => item.id === 'day1_intro_basic_voice');
    const operationCommand = scene.timeline.find(event => event.command === 'operation.run');

    assert.equal(scene.timelinePlayback, true);
    assert.ok(operationCommand);
    assert.equal(operationCommand.at, 1);
    assert.equal(operationCommand.operation, 'day1-intro-basic-voice-showcase');
    assert.equal(operationCommand.blocking, true);
    assert.equal(scene.interruptible, undefined);
});

test('day1 takeover capture delegates keyboard-control sequence through timeline operation', () => {
    const guides = loadGuides();
    const scene = guides[1].round.scenes.find(item => item.id === 'day1_takeover_capture_cursor');
    const operationCommand = scene.timeline.find(event => event.command === 'operation.run');

    assert.equal(scene.timelinePlayback, true);
    assert.ok(operationCommand);
    assert.equal(operationCommand.at, 1);
    assert.equal(operationCommand.operation, 'day1-managed-scene:takeover_capture_cursor');
    assert.equal(operationCommand.blocking, true);
    assert.equal(scene.interruptible, undefined);
});

test('day2 round scenes use the interaction tools flow after day swap', () => {
    const guides = loadGuides();
    const day2Scenes = guides[2].round.scenes;
    const timelineSceneIds = day2Scenes
        .filter(scene => scene.timelinePlayback === true)
        .map(scene => scene.id);

    assert.equal(timelineSceneIds.length, 8);
    assert.equal(timelineSceneIds[0], 'day2_tool_toggle_intro');
    assert.equal(timelineSceneIds[1], 'day2_avatar_tools');
    assert.equal(timelineSceneIds[2], 'day2_avatar_tools_props');
    assert.equal(timelineSceneIds[3], 'day2_tool_wheel_rotation');
    assert.equal(timelineSceneIds[4], 'day2_galgame_entry');
    assert.equal(timelineSceneIds[5], 'day2_galgame_choices');
    assert.equal(timelineSceneIds[6], 'day2_wrap');
    assert.equal(timelineSceneIds[7], 'day2_wrap_ready');
});

test('day3 personalization detail delegates narration and panel tour to SettingsTourFlow from timeline after day swap', () => {
    const guides = loadGuides();
    const scene = guides[3].round.scenes.find(item => item.id === 'day3_personalization_detail');

    assert.equal(scene.timelinePlayback, true);
    assert.equal(scene.timelineAudio, false);
    assert.equal(scene.afterSceneDelayMs, 0);
    assert.equal(Array.isArray(scene.timeline), true);
    assert.equal(scene.timeline.length, 1);
    assert.equal(scene.timeline[0].at, 0);
    assert.equal(scene.timeline[0].command, 'settingsTour.play');
    assert.equal(scene.timeline[0].blocking, true);
});

test('day3 proactive chat closes settings panel only after narration ends from timeline after day swap', () => {
    const guides = loadGuides();
    const scene = guides[3].round.scenes.find(item => item.id === 'day3_proactive_chat');
    const closeCommand = scene.timeline.find(event => event.command === 'settingsPanel.close');

    assert.equal(scene.timelinePlayback, true);
    assert.ok(closeCommand);
    assert.equal(closeCommand.afterAudioEnd, true);
    assert.equal(closeCommand.panel, 'settings');
    assert.equal(closeCommand.collapseSidePanels, true);
    assert.equal(closeCommand.blocking, true);
});

test('day3 round scenes use the personalization voice flow after day swap', () => {
    const guides = loadGuides();
    const day3Scenes = guides[3].round.scenes;
    const timelineSceneIds = day3Scenes
        .filter(scene => scene.timelinePlayback === true)
        .map(scene => scene.id);

    assert.equal(timelineSceneIds.length, 7);
    assert.equal(timelineSceneIds[0], 'day3_intro_context');
    assert.equal(timelineSceneIds[1], 'day3_personalization_space');
    assert.equal(timelineSceneIds[2], 'day3_personalization_detail');
    assert.equal(timelineSceneIds[3], 'day3_proactive_chat');
    assert.equal(timelineSceneIds[4], 'day3_wrap_intro');
    assert.equal(timelineSceneIds[5], 'day3_wrap_companion');
    assert.equal(timelineSceneIds[6], 'day3_wrap');
});

test('day4 round scenes use timeline playback after settings tours delegate to SettingsTourFlow', () => {
    const guides = loadGuides();
    const day4Scenes = guides[4].round.scenes;
    const timelineSceneIds = day4Scenes
        .filter(scene => scene.timelinePlayback === true)
        .map(scene => scene.id);

    assert.equal(timelineSceneIds.length, 8);
    assert.equal(timelineSceneIds[0], 'day4_intro_companion');
    assert.equal(timelineSceneIds[1], 'day4_chat_settings');
    assert.equal(timelineSceneIds[2], 'day4_model_behavior');
    assert.equal(timelineSceneIds[3], 'day4_gaze_follow');
    assert.equal(timelineSceneIds[4], 'day4_privacy_mode');
    assert.equal(timelineSceneIds[5], 'day4_model_lock');
    assert.equal(timelineSceneIds[6], 'day4_return_home');
    assert.equal(timelineSceneIds[7], 'day4_wrap');

    const wrapScene = day4Scenes.find(scene => scene.id === 'day4_wrap');
    const wrapMove = wrapScene.timeline.find(event => event.command === 'cursor.move');
    const wrapHold = wrapScene.timeline.find(event => event.command === 'cursor.hold');
    assert.equal(wrapMove.target, 'chat-capsule-input');
    assert.equal(wrapMove.freezePoint, true);
    assert.equal(wrapHold.target, 'chat-capsule-input');
    assert.equal(wrapHold.freezePoint, true);
    assert.equal(wrapHold.at > wrapMove.at, true);
});

test('day4 migrated settings scenes delegate narration and panel tour to SettingsTourFlow from timeline', () => {
    const guides = loadGuides();
    const sceneIds = ['day4_chat_settings', 'day4_model_behavior', 'day4_gaze_follow', 'day4_privacy_mode'];

    for (const sceneId of sceneIds) {
        const scene = guides[4].round.scenes.find(item => item.id === sceneId);

        assert.equal(scene.timelinePlayback, true);
        assert.equal(scene.timelineAudio, false);
        assert.equal(scene.afterSceneDelayMs, 0);
        assert.equal(Array.isArray(scene.timeline), true);
        assert.equal(scene.timeline.length, 1);
        assert.equal(scene.timeline[0].at, 0);
        assert.equal(scene.timeline[0].command, 'settingsTour.play');
        assert.equal(scene.timeline[0].blocking, true);
    }
});

test('day5 round scenes use timeline playback after settings and panic delegate to SettingsTourFlow', () => {
    const guides = loadGuides();
    const day5Scenes = guides[5].round.scenes;
    const timelineSceneIds = day5Scenes
        .filter(scene => scene.timelinePlayback === true)
        .map(scene => scene.id);

    assert.equal(timelineSceneIds.length, 4);
    assert.equal(timelineSceneIds[0], 'day5_character_settings');
    assert.equal(timelineSceneIds[1], 'day5_character_panic');
    assert.equal(timelineSceneIds[2], 'day5_memory_entry');
    assert.equal(timelineSceneIds[3], 'day5_wrap');
});

test('day5 settings scenes delegate narration and panic performance to SettingsTourFlow from timeline', () => {
    const guides = loadGuides();
    const sceneIds = ['day5_character_settings', 'day5_character_panic'];

    for (const sceneId of sceneIds) {
        const scene = guides[5].round.scenes.find(item => item.id === sceneId);

        assert.equal(scene.timelinePlayback, true);
        assert.equal(scene.timelineAudio, false);
        assert.equal(scene.afterSceneDelayMs, 0);
        assert.equal(Array.isArray(scene.timeline), true);
        // 修改原因：Day5 角色设置段可能先播放开场模型动作，核心约束是设置引导仍由 timeline 阻塞接管。
        const settingsTourEvent = scene.timeline.find(event => event.command === 'settingsTour.play');
        assert.ok(settingsTourEvent);
        assert.equal(settingsTourEvent.at, 0);
        assert.equal(settingsTourEvent.blocking, true);
        assert.equal(Object.prototype.hasOwnProperty.call(scene, 'avatarStandIn'), false);
    }
});

test('day6 round scenes use timeline playback after plugin handoff delegates to existing operation flow', () => {
    const guides = loadGuides();
    const day6Scenes = guides[6].round.scenes;
    const timelineSceneIds = day6Scenes
        .filter(scene => scene.timelinePlayback === true)
        .map(scene => scene.id);

    assert.equal(timelineSceneIds.length, 8);
    assert.equal(timelineSceneIds[0], 'day6_intro_agent');
    assert.equal(timelineSceneIds[1], 'day6_agent_status_master');
    assert.equal(timelineSceneIds[2], 'day6_plugin_side_panel');
    assert.equal(timelineSceneIds[3], 'day6_plugin_dashboard');
    assert.equal(timelineSceneIds[4], 'day6_agent_task_hud');
    assert.equal(timelineSceneIds[5], 'day6_agent_task_hud_control');
    assert.equal(timelineSceneIds[6], 'day6_wrap_cleanup');
    assert.equal(timelineSceneIds[7], 'day6_wrap');
});

test('day6 agent status opens the Agent panel through timeline operation after narration starts', () => {
    const guides = loadGuides();
    const scene = guides[6].round.scenes.find(item => item.id === 'day6_agent_status_master');
    const operationCommand = scene.timeline.find(event => event.command === 'operation.run');

    assert.equal(scene.timelinePlayback, true);
    assert.ok(operationCommand);
    assert.equal(operationCommand.at, 1);
    assert.equal(operationCommand.operation, 'day6-plugin-open-agent-panel-flow');
    assert.equal(operationCommand.blocking, true);
});

test('day6 plugin side panel opens management preview through timeline operation after narration starts', () => {
    const guides = loadGuides();
    const scene = guides[6].round.scenes.find(item => item.id === 'day6_plugin_side_panel');
    const operationCommand = scene.timeline.find(event => event.command === 'operation.run');

    assert.equal(scene.timelinePlayback, true);
    assert.ok(operationCommand);
    assert.equal(operationCommand.at, 1);
    assert.equal(operationCommand.operation, 'day6-plugin-open-management-panel-flow');
    assert.equal(operationCommand.blocking, true);
    assert.equal(scene.afterSceneDelayMs, 0);
});

test('day6 plugin dashboard handoff runs through timeline operation after narration starts', () => {
    const guides = loadGuides();
    const scene = guides[6].round.scenes.find(item => item.id === 'day6_plugin_dashboard');
    const operationCommand = scene.timeline.find(event => event.command === 'operation.run');

    assert.equal(scene.timelinePlayback, true);
    assert.ok(operationCommand);
    assert.equal(operationCommand.at, 1);
    assert.equal(operationCommand.operation, 'day6-plugin-dashboard-handoff-flow');
    assert.equal(operationCommand.blocking, true);
    assert.equal(scene.afterSceneDelayMs, 0);
});

test('day6 task HUD starts without blocking cleanup and keeps real HUD preparation on the timeline path', () => {
    const guides = loadGuides();
    const scene = guides[6].round.scenes.find(item => item.id === 'day6_agent_task_hud');

    assert.equal(scene.timelinePlayback, true);
    assert.notEqual(scene.cleanupBefore, true);
    assert.equal(scene.operation, 'show-task-hud');
    assert.equal(scene.target, '#agent-task-hud');
    assert.equal(scene.cursorAction, 'move');
});

test('day7 round scenes are opted into timeline playback after the petal cue path is covered', () => {
    const guides = loadGuides();
    const day7Scenes = guides[7].round.scenes;
    const memoryReview = day7Scenes.find(scene => scene.id === 'day7_memory_review');
    const memoryControl = day7Scenes.find(scene => scene.id === 'day7_memory_control');
    const graduationWrap = day7Scenes.find(scene => scene.id === 'day7_graduation_wrap');

    assert.equal(memoryReview.timelinePlayback, true);
    assert.equal(memoryControl.timelinePlayback, true);
    assert.equal(graduationWrap.timelinePlayback, true);
});

test('director merges audio maps from all registered daily guides', () => {
    const directorSource = readDirectorSource(__dirname);

    assert.match(directorSource, /function collectGuideAudioFilesByKey\(\)/);
    assert.match(directorSource, /window\.YuiGuideDailyGuides/);
    assert.doesNotMatch(directorSource, /GUIDE_AUDIO_FILES_BY_KEY = Object\.freeze\(Object\.assign\(\{\}, DAY1_HOME_GUIDE\.audioFilesByKey/);
});
