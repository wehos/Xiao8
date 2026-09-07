from pathlib import Path
from tests.static_app_parts import read_js_parts

import pytest

from tests.yui_guide_director_parts import read_director_source


ROOT = Path(__file__).resolve().parents[2]
DAY1_GUIDE_PATH = ROOT / "static" / "tutorial/yui-guide/days/day1-home-guide.js"
DAY2_GUIDE_PATH = ROOT / "static" / "tutorial/yui-guide/days/day2-screen-voice-guide.js"
DAY3_GUIDE_PATH = ROOT / "static" / "tutorial/yui-guide/days/day3-interaction-guide.js"
DAY4_GUIDE_PATH = ROOT / "static" / "tutorial/yui-guide/days/day4-companion-guide.js"
DAY5_GUIDE_PATH = ROOT / "static" / "tutorial/yui-guide/days/day5-personalization-guide.js"
DAY6_GUIDE_PATH = ROOT / "static" / "tutorial/yui-guide/days/day6-agent-guide.js"
DAY7_GUIDE_PATH = ROOT / "static" / "tutorial/yui-guide/days/day7-graduation-guide.js"
STEPS_PATH = ROOT / "static" / "tutorial/yui-guide/steps.js"
SCENE_ORCHESTRATOR_PATH = ROOT / "static" / "tutorial/core/scene-orchestrator.js"
INTERPAGE_PATH = ROOT / "static" / "app" / "app-interpage"
REACT_APP_PATH = ROOT / "frontend" / "react-neko-chat" / "src" / "App.tsx"
REACT_SCHEMA_PATH = ROOT / "frontend" / "react-neko-chat" / "src" / "message-schema.ts"
REACT_HOST_PATH = ROOT / "static" / "app" / "app-react-chat-window"
MANAGER_PATH = ROOT / "static" / "tutorial/core/universal-manager.js"
PAGE_TUTORIAL_MANAGER_PATH = ROOT / "static" / "tutorial/core/page-tutorial-manager.js"
OVERLAY_PATH = ROOT / "static" / "tutorial/yui-guide/overlay.js"
GHOST_CURSOR_PATH = ROOT / "static" / "tutorial/visual/ghost-cursor-controller.js"
RESISTANCE_CONTROLLER_PATH = ROOT / "static" / "tutorial/visual/resistance-controllers.js"
SKIP_CONTROLLER_PATH = ROOT / "static" / "tutorial/core/skip-controller.js"
YUI_GUIDE_CSS_PATH = ROOT / "static" / "css/yui-guide.css"
TUTORIAL_STYLES_CSS_PATH = ROOT / "static" / "css/tutorial-styles.css"
INDEX_CSS_PATH = ROOT / "static" / "css/index.css"


EXPECTED_DAY1_SCENES = [
    "day1_intro_activation",
    "day1_intro_greeting",
    "day1_capsule_drag_hint",
    "day1_history_handle",
    "day1_intro_basic_voice",
    "day1_screen_entry",
    "day1_screen_entry_invite",
    "day1_takeover_capture_cursor",
    "day1_avatar_zoom_hint",
    "day1_takeover_return_control",
]

EXPECTED_DAY2_SCENES = [
    "day2_tool_toggle_intro",
    "day2_avatar_tools",
    "day2_avatar_tools_props",
    "day2_tool_wheel_rotation",
    "day2_galgame_entry",
    "day2_galgame_choices",
    "day2_wrap",
    "day2_wrap_ready",
]

EXPECTED_DAY3_SCENES = [
    "day3_intro_context",
    "day3_personalization_space",
    "day3_personalization_detail",
    "day3_proactive_chat",
    "day3_wrap_intro",
    "day3_wrap_companion",
    "day3_wrap",
]


EXPECTED_DAY4_SCENES = [
    "day4_intro_companion",
    "day4_chat_settings",
    "day4_model_behavior",
    "day4_gaze_follow",
    "day4_privacy_mode",
    "day4_model_lock",
    "day4_return_home",
    "day4_wrap",
]


EXPECTED_DAY5_SCENES = [
    "day5_character_settings",
    "day5_character_panic",
    "day5_memory_entry",
    "day5_wrap",
]


EXPECTED_DAY6_SCENES = [
    "day6_intro_agent",
    "day6_agent_status_master",
    "day6_plugin_side_panel",
    "day6_plugin_dashboard",
    "day6_agent_task_hud",
    "day6_agent_task_hud_control",
    "day6_wrap_cleanup",
    "day6_wrap",
]


EXPECTED_DAY7_SCENES = [
    "day7_memory_review",
    "day7_memory_control",
    "day7_graduation_wrap",
]


def assert_scene_order(source, expected):
    first_scene = source.index(f"id: '{expected[0]}'")
    for scene_id in expected[1:]:
        current = source.index(f"id: '{scene_id}'")
        assert first_scene < current
        first_scene = current


def extract_day1_round_block(source):
    return source.split("round: {", 1)[1].split("audioFileNames:", 1)[0]


def test_day1_daily_guide_registers_round_scenes_in_day2_to_7_shape():
    source = DAY1_GUIDE_PATH.read_text(encoding="utf-8")

    assert "round: {" in source
    round_block = extract_day1_round_block(source)
    assert "scenes: [" in round_block
    for scene_id in EXPECTED_DAY1_SCENES:
        assert f"id: '{scene_id}'" in round_block
    for old_scene_id in [
        "day1_takeover_plugin_preview_home",
        "day1_takeover_plugin_dashboard",
        "day1_takeover_settings_peek_intro",
        "day1_takeover_settings_peek_detail",
        "day1_takeover_proactive_chat",
    ]:
        assert f"id: '{old_scene_id}'" not in round_block

    assert_scene_order(round_block, EXPECTED_DAY1_SCENES)


def test_steps_registry_registers_global_resistance_steps_for_all_rounds():
    source = STEPS_PATH.read_text(encoding="utf-8")

    assert "DEFAULT_RESISTANCE_STEP_PATCHES" in source
    resist_block = source.split("interrupt_resist_light: Object.freeze({", 1)[1].split(
        "interrupt_angry_exit: Object.freeze({",
        1,
    )[0]
    angry_block = source.split("interrupt_angry_exit: Object.freeze({", 1)[1]

    assert "bubbleText: '喵！现在是人家的教学时间，不可以乱动鼠标和键盘啦！乖乖看着人家，好不好嘛？'" in resist_block
    assert "voiceKey: 'interrupt_resist_light_1'" in resist_block
    assert "resistanceVoices: Object.freeze([" in resist_block
    assert "真是的，又在乱动鼠标和键盘！再不听话的话，人家可真的要生气了喵！" in resist_block
    assert "最后警告一次喵！你要是再乱动一下，人家就直接退出新手教程，不教你了！" in resist_block
    assert "tutorial.yuiGuide.lines.interruptResistLight2" in resist_block
    assert "tutorial.yuiGuide.lines.interruptResistLight3" in resist_block
    assert "threshold: 4" in resist_block
    assert "resetOnStepAdvance: false" in resist_block

    assert "bubbleText: '人家已经忍你很久了！既然你就是不肯乖乖听话，那新手教程到此结束，接下来你自己慢慢研究吧，哼！'" in angry_block
    assert "voiceKey: 'interrupt_angry_exit'" in angry_block
    assert "threshold: 4" in angry_block
    assert "resetOnStepAdvance: false" in angry_block
    assert "Object.keys(DEFAULT_RESISTANCE_STEP_PATCHES).forEach(function (id) {" in source
    assert "if (!steps[id]) {" in source


def test_resistance_audio_assets_use_new_ten_character_names():
    audio_root = ROOT / "static" / "assets" / "tutorial" / "guide-audio"
    new_files = [
        "喵！现在是人家的教学.mp3",
        "真是的，又在乱动鼠标.mp3",
        "最后警告一次喵！你要.mp3",
        "人家已经忍你很久了！.mp3",
    ]
    legacy_files = [
        "喂！不要拽我啦，现在.mp3",
        "等一下啦！还没结束呢.mp3",
        "人类！你真的很没礼貌.mp3",
    ]

    for locale in ["zh", "en", "ja", "ko", "ru"]:
        for file_name in new_files:
            assert (audio_root / locale / file_name).is_file()
        for file_name in legacy_files:
            assert not (audio_root / locale / file_name).exists()


def test_day2_round_targets_compact_tool_flow_after_day_swap():
    source = DAY2_GUIDE_PATH.read_text(encoding="utf-8")
    round_block = source.split("round: {", 1)[1]
    intro_block = round_block.split("id: 'day2_tool_toggle_intro'", 1)[1].split(
        "id: 'day2_avatar_tools'",
        1,
    )[0]
    avatar_tools_block = round_block.split("id: 'day2_avatar_tools'", 1)[1].split(
        "id: 'day2_avatar_tools_props'",
        1,
    )[0]
    avatar_tools_props_block = round_block.split("id: 'day2_avatar_tools_props'", 1)[1].split(
        "id: 'day2_tool_wheel_rotation'",
        1,
    )[0]
    tool_wheel_rotation_block = round_block.split("id: 'day2_tool_wheel_rotation'", 1)[1].split(
        "id: 'day2_galgame_entry'",
        1,
    )[0]
    galgame_entry_block = round_block.split("id: 'day2_galgame_entry'", 1)[1].split(
        "id: 'day2_galgame_choices'",
        1,
    )[0]

    for scene_id in EXPECTED_DAY2_SCENES:
        assert f"id: '{scene_id}'" in round_block
    assert_scene_order(round_block, EXPECTED_DAY2_SCENES)
    assert "day2_avatar_tools_more" not in round_block
    assert "avatarToolsMore" not in round_block
    assert "avatar_floating_day2_avatar_tools_more" not in round_block
    assert "show-galgame-in-compact-tool-fan" not in round_block
    assert "cursorAction: 'wobble'" not in round_block
    assert "target: 'chat-capsule-input'" in intro_block
    assert "cursorAction: 'move'" in intro_block
    assert "operation: 'open-compact-tool-fan'" not in intro_block
    assert "persistent: 'chat-tool-toggle'" in avatar_tools_block
    assert "target: 'chat-tool-toggle'" in avatar_tools_block
    assert "cursorAction: 'click'" in avatar_tools_block
    assert "cursorMoveDurationMs: 1480" in avatar_tools_block
    assert "operation: 'open-compact-tool-fan'" in avatar_tools_block
    assert "persistent: 'chat-tool-toggle'" in avatar_tools_props_block
    assert "target: 'chat-avatar-tools'" in avatar_tools_props_block
    assert "cursorAction: 'click'" in avatar_tools_props_block
    assert "operation: 'show-avatar-tools-then-hide-after-narration'" in avatar_tools_props_block
    assert "textKey: 'tutorial.avatarFloating.day2.toolWheelRotation'" in tool_wheel_rotation_block
    assert "voiceKey: 'avatar_floating_day2_tool_wheel_rotation'" in tool_wheel_rotation_block
    assert "persistent: 'chat-tool-toggle'" in tool_wheel_rotation_block
    assert "target: 'chat-galgame'" in tool_wheel_rotation_block
    assert "cursorAction: 'move'" in tool_wheel_rotation_block
    assert "operation: 'rotate-galgame-tool-into-center'" in tool_wheel_rotation_block
    assert "target: 'chat-galgame'" in galgame_entry_block
    assert "cursorAction: 'hold'" in galgame_entry_block
    assert "cursorHoldFreezePoint: true" in galgame_entry_block
    assert "operation: 'rotate-galgame-tool-into-center'" not in galgame_entry_block
    assert "target: 'chat-tool-toggle'" in round_block
    assert "target: 'chat-avatar-tools'" in round_block
    assert "target: 'chat-galgame'" in round_block
    assert "day2_chat_tools" not in round_block
    assert "day2_galgame_games" not in round_block


def test_day2_tool_wheel_rotation_voice_key_has_localized_audio_files():
    source = DAY2_GUIDE_PATH.read_text(encoding="utf-8")
    audio_file = "day2-tool-wheel-rotation.mp3"

    assert f"avatar_floating_day2_tool_wheel_rotation: zhAudio('{audio_file}')" in source
    for locale in ["zh", "ja", "ko", "en", "ru"]:
        assert (
            ROOT / "static" / "assets" / "tutorial" / "guide-audio" / locale / audio_file
        ).is_file()


def test_day3_round_keeps_intro_text_and_moves_personalization_after_day_swap():
    if not DAY3_GUIDE_PATH.exists():
        pytest.skip("Day 3 guide is not shipped in this PR")
    source = DAY3_GUIDE_PATH.read_text(encoding="utf-8")
    round_block = source.split("round: {", 1)[1]
    detail_block = round_block.split("id: 'day3_personalization_detail'", 1)[1].split(
        "id: 'day3_proactive_chat'",
        1,
    )[0]
    wrap_intro_block = round_block.split("id: 'day3_wrap_intro'", 1)[1].split(
        "id: 'day3_wrap_companion'",
        1,
    )[0]
    wrap_companion_block = round_block.split("id: 'day3_wrap_companion'", 1)[1].split(
        "id: 'day3_wrap'",
        1,
    )[0]
    wrap_block = round_block.split("id: 'day3_wrap'", 1)[1]

    for scene_id in EXPECTED_DAY3_SCENES:
        assert f"id: '{scene_id}'" in round_block
    assert_scene_order(round_block, EXPECTED_DAY3_SCENES)
    assert "前两天你一直在噼里啪啦打字，我还没听过你说话呢。" in round_block
    assert "voiceKey: 'avatar_floating_day3_intro'" in round_block
    assert "id: 'day3_screen_entry'" not in round_block
    assert "id: 'day3_screen_entry_invite'" not in round_block
    assert "cursorAction: 'wobble'" not in round_block
    assert "target: '#${p}-menu-character'" in detail_block
    assert "cursorAction: 'click'" in detail_block
    assert "target: '#${p}-popup-settings'" not in detail_block
    assert "target: 'chat-input'" in wrap_intro_block
    assert "target: 'chat-input'" in wrap_companion_block
    assert "target: 'chat-input'" in wrap_block


def test_day4_round_wrap_returns_to_capsule_input_like_day2_wrap():
    if not DAY4_GUIDE_PATH.exists():
        pytest.skip("Day 4 guide is not shipped in this PR")
    source = DAY4_GUIDE_PATH.read_text(encoding="utf-8")
    round_block = source.split("round: {", 1)[1]
    wrap_block = round_block.split("id: 'day4_wrap'", 1)[1]

    for scene_id in EXPECTED_DAY4_SCENES:
        assert f"id: '{scene_id}'" in round_block
    assert_scene_order(round_block, EXPECTED_DAY4_SCENES)
    assert "target: 'chat-capsule-input'" in wrap_block
    assert "cursorAction: 'move'" in wrap_block
    assert "operation: 'cleanup'" in wrap_block
    assert "petalTransition: true" in wrap_block


def test_day5_round_wrap_returns_to_capsule_input_like_day2_wrap():
    if not DAY5_GUIDE_PATH.exists():
        pytest.skip("Day 5 guide is not shipped in this PR")
    source = DAY5_GUIDE_PATH.read_text(encoding="utf-8")
    round_block = source.split("round: {", 1)[1]
    wrap_block = round_block.split("id: 'day5_wrap'", 1)[1]

    for scene_id in EXPECTED_DAY5_SCENES:
        assert f"id: '{scene_id}'" in round_block
    assert_scene_order(round_block, EXPECTED_DAY5_SCENES)
    assert "target: 'chat-input'" in wrap_block
    assert "cursorAction: 'move'" in wrap_block
    assert "operation: 'cleanup'" in wrap_block
    assert "petalTransition: true" in wrap_block


def test_day5_wrap_voice_key_has_audio_file():
    if not DAY5_GUIDE_PATH.exists():
        pytest.skip("Day 5 guide is not shipped in this PR")
    source = DAY5_GUIDE_PATH.read_text(encoding="utf-8")
    audio_file = "好啦好啦，快去试试这.mp3"

    assert f"avatar_floating_day5_wrap: zhAudio('{audio_file}')" in source
    assert (ROOT / "static" / "assets" / "tutorial" / "guide-audio" / "zh" / audio_file).is_file()


def test_day6_round_wrap_returns_to_capsule_input_like_day2_wrap():
    if not DAY6_GUIDE_PATH.exists():
        pytest.skip("Day 6 guide is not shipped in this PR")
    source = DAY6_GUIDE_PATH.read_text(encoding="utf-8")
    director_source = read_director_source(ROOT)
    round_block = source.split("round: {", 1)[1]
    plugin_side_panel_block = round_block.split("id: 'day6_plugin_side_panel'", 1)[1].split(
        "id: 'day6_plugin_dashboard'",
        1,
    )[0]
    task_hud_block = round_block.split("id: 'day6_agent_task_hud'", 1)[1].split(
        "id: 'day6_agent_task_hud_control'",
        1,
    )[0]
    task_hud_control_block = round_block.split("id: 'day6_agent_task_hud_control'", 1)[1].split(
        "id: 'day6_wrap_cleanup'",
        1,
    )[0]
    wrap_cleanup_block = round_block.split("id: 'day6_wrap_cleanup'", 1)[1].split(
        "id: 'day6_wrap'",
        1,
    )[0]
    wrap_block = round_block.split("id: 'day6_wrap'", 1)[1]

    for scene_id in EXPECTED_DAY6_SCENES:
        assert f"id: '{scene_id}'" in round_block
    assert_scene_order(round_block, EXPECTED_DAY6_SCENES)
    assert "除了之前介绍的功能，这里还有超多好玩的插件呢。" in plugin_side_panel_block
    assert "除了之前介绍的功能，这里还有超多好玩的插件呢'," not in plugin_side_panel_block
    assert "afterSceneDelayMs: 0" in plugin_side_panel_block
    assert "target: '#agent-task-hud'" in task_hud_block
    assert "cursorAction: 'move'" in task_hud_block
    assert "cursorAction: 'tour'" not in task_hud_block
    assert "target: '#agent-task-hud'" in task_hud_control_block
    assert "cursorAction: 'move'" in task_hud_control_block
    assert "cursorAction: 'ellipse'" not in task_hud_control_block
    assert "cursorAction: 'tour'" not in task_hud_control_block
    assert "target: 'chat-input'" in wrap_cleanup_block
    assert "target: 'chat-input'" in wrap_block
    assert "preserveExternalizedChatGuideTarget: true" in wrap_cleanup_block
    assert "cursorAction: 'hold'" in wrap_block
    assert "cursorAction: 'move'" not in wrap_block
    assert "petalTransition: true" in wrap_block
    assert "avatar_floating_day6_wrap: Object.freeze({" in director_source
    assert "zh: 11340" in director_source


def test_day7_round_wrap_returns_to_capsule_input_like_day2_wrap():
    if not DAY7_GUIDE_PATH.exists():
        pytest.skip("Day 7 guide is not shipped in this PR")
    source = DAY7_GUIDE_PATH.read_text(encoding="utf-8")
    round_block = source.split("round: {", 1)[1]
    wrap_block = round_block.split("id: 'day7_graduation_wrap'", 1)[1]

    for scene_id in EXPECTED_DAY7_SCENES:
        assert f"id: '{scene_id}'" in round_block
    assert_scene_order(round_block, EXPECTED_DAY7_SCENES)
    assert "target: 'chat-input'" in wrap_block
    assert "cursorAction: 'move'" in wrap_block
    assert "operation: 'cleanup'" in wrap_block
    assert "petalTransition: true" in wrap_block


def test_compact_chat_tutorial_bridge_exposes_new_targets_and_requests():
    director = read_director_source(ROOT)
    interpage = read_js_parts(INTERPAGE_PATH)
    react_app = REACT_APP_PATH.read_text(encoding="utf-8")
    react_schema = REACT_SCHEMA_PATH.read_text(encoding="utf-8")
    react_host = read_js_parts(REACT_HOST_PATH)

    for token in [
        "chat-history-handle",
        "chat-tool-toggle",
        ".compact-history-visibility-handle",
        ".send-button-circle.compact-input-tool-toggle",
        ".compact-input-tool-item-avatar",
        ".compact-input-tool-item-galgame",
        "setCompactToolFanOpen",
        "setExternalizedChatCompactHistoryOpen",
    ]:
        assert token in director

    assert "yui_guide_set_compact_history_open" in interpage
    assert "yui_guide_set_compact_tool_fan_open" in interpage
    assert "compactToolFanOpenRequest" in react_schema
    assert "compactToolFanOpenRequest" in react_app
    assert "setCompactToolFanOpen" in react_host


def test_external_chat_cursor_retry_cannot_replay_stale_wobble_after_clear():
    source = read_js_parts(INTERPAGE_PATH)

    assert "yuiGuideChatCursorRequestToken" in source
    assert "var cursorRequestToken = yuiGuideChatCursorRequestToken;" in source
    assert "if (cursorRequestToken !== yuiGuideChatCursorRequestToken) {" in source


def test_tutorial_exit_clears_externalized_guide_chat_messages():
    director = read_director_source(ROOT)
    takeover = (ROOT / "static" / "tutorial/core/interaction-takeover.js").read_text(encoding="utf-8")

    termination_block = director.split("beginTerminationVisualCleanup()", 1)[1].split(
        "async playAvatarFloatingScene",
        1,
    )[0]
    destroy_block = director.split("requestTermination(reason, tutorialReason)", 1)[1].split(
        "updatePlaybackState",
        1,
    )[0]
    fx_block = takeover.split("clearExternalizedChatFx()", 1)[1].split(
        "onExternalChatReady()",
        1,
    )[0]

    assert "clearGuideChatMessages()" in director
    assert "action: 'yui_guide_clear_chat_messages'" in director
    assert termination_block.index("this.clearGuideChatStreamTimers();") < termination_block.index(
        "this.clearGuideChatMessages();"
    )
    assert "this.clearGuideChatMessages();" in termination_block
    assert destroy_block.index("this.clearGuideChatStreamTimers();") < destroy_block.index(
        "this.clearGuideChatMessages();"
    )
    assert "this.clearGuideChatMessages();" in destroy_block
    assert "clearExternalizedChatGuideMessages()" in takeover
    assert "this.clearExternalizedChatGuideMessages();" not in fx_block
    assert "this.setExternalizedChatInputLocked(false, 'clear-externalized-chat-fx');" in fx_block


def test_pc_external_chat_ghost_cursor_uses_overlay_with_dom_fallback():
    source = read_js_parts(INTERPAGE_PATH)
    cursor_block = source.split("function applyYuiGuideChatCursor(kind, options)", 1)[1].split(
        "function clearYuiGuideChatSpotlightTracking()",
        1,
    )[0]

    assert "yui-guide-chat-cursor" in source
    assert "function ensureYuiGuideChatCursorElement()" in source
    assert "cancelYuiGuideChatCursorElementAnimations" not in source
    assert ".animate(" not in cursor_block
    assert "sendYuiGuidePcOverlayPatch({" in cursor_block
    assert "isYuiGuidePcCursorOnlyMode" in source
    assert "cursor: {" in cursor_block
    assert "visible: true" in cursor_block
    assert "effect: normalizedOptions.effect || ''" in cursor_block
    assert "cursor.hidden = false" in source
    assert "if (isYuiGuidePcCursorOnlyMode())" in cursor_block


def test_pc_external_chat_spotlight_uses_overlay_without_dom_fallback():
    source = read_js_parts(INTERPAGE_PATH)
    spotlight_block = source.split("function getYuiGuideChatSpotlightElement(createIfMissing)", 1)[1].split(
        "function getYuiGuidePcOverlayHost",
        1,
    )[0]
    update_block = source.split("function updateYuiGuideChatSpotlight(kind", 1)[1].split(
        "function applyYuiGuideChatSpotlight",
        1,
    )[0]

    assert "isYuiGuidePcOverlayAvailable()" in spotlight_block
    assert "var pcOverlayAvailable = isYuiGuidePcOverlayAvailable();" in update_block
    assert "getYuiGuideChatSpotlightElement(!pcOverlayAvailable)" in update_block
    assert "sendYuiGuidePcOverlayPatch({ spotlights: pcRects }, false, patchOptions);" in update_block


def test_pc_external_chat_spotlight_reuses_last_rect_during_transient_layout_gaps():
    source = read_js_parts(INTERPAGE_PATH)
    update_block = source.split("function updateYuiGuideChatSpotlight(kind", 1)[1].split(
        "function applyYuiGuideChatSpotlight",
        1,
    )[0]
    apply_block = source.split("function applyYuiGuideChatSpotlight(kind, options)", 1)[1].split(
        "function applyYuiGuideChatCursorRelay",
        1,
    )[0]

    missing_rect_block = update_block.split("if (!sourceRect || sourceRect.width <= 0 || sourceRect.height <= 0) {", 1)[1].split(
        "var padding = kind === 'window'",
        1,
    )[0]
    assert "yuiGuideChatSpotlightLastPcKind === kind" in missing_rect_block
    assert "yuiGuideChatSpotlightLastPcVariant === yuiGuideChatSpotlightVariant" in missing_rect_block
    assert "yuiGuideChatSpotlightLastPcRects.length > 0" in missing_rect_block
    assert "spotlights: yuiGuideChatSpotlightLastPcRects.map" in missing_rect_block
    assert "spotlights: []" not in missing_rect_block
    assert "rememberYuiGuideChatPcSpotlightRects(kind, pcRects, yuiGuideChatSpotlightVariant);" in update_block
    assert "clearYuiGuideChatPcSpotlightRects();" in apply_block
    assert "sendYuiGuidePcOverlayPatch({ spotlights: [] }, false, {" in apply_block


def test_pc_external_chat_spotlight_preserves_highlight_during_resistance_pause():
    interpage_source = read_js_parts(INTERPAGE_PATH)
    takeover_source = (ROOT / "static" / "tutorial/core/interaction-takeover.js").read_text(encoding="utf-8")
    director_source = read_director_source(ROOT)
    apply_block = interpage_source.split("function applyYuiGuideChatSpotlight(kind, options)", 1)[1].split(
        "function applyYuiGuideChatCursorRelay",
        1,
    )[0]
    takeover_block = takeover_source.split("setExternalizedChatSpotlight(kind) {", 1)[1].split(
        "setExternalizedChatCursor(kind, options) {",
        1,
    )[0]
    constructor_block = director_source.split("window.TutorialInteractionTakeover.createController({", 1)[1].split(
        "externalizedChatDetector:",
        1,
    )[0]

    assert "isResistancePaused: () => this.scenePausedForResistance === true" in constructor_block
    assert "safeInvoke(this.isResistancePaused, [], false) === true" in takeover_block
    assert "message.preserveDuringResistance = true;" in takeover_block
    assert "options.preserveDuringResistance === true" in apply_block
    assert "yuiGuideChatSpotlightKind" in apply_block
    assert "updateYuiGuideChatSpotlight(yuiGuideChatSpotlightKind, pcOverlayRunId);" in apply_block
    assert "clearYuiGuideChatSpotlightTracking();" in apply_block.split(
        "updateYuiGuideChatSpotlight(yuiGuideChatSpotlightKind, pcOverlayRunId);",
        1,
    )[1]


def test_externalized_chat_spotlight_keeps_variant_pipeline_but_day1_uses_capsule_target():
    interpage_source = read_js_parts(INTERPAGE_PATH)
    takeover_source = (ROOT / "static" / "tutorial/core/interaction-takeover.js").read_text(encoding="utf-8")
    scene_source = SCENE_ORCHESTRATOR_PATH.read_text(encoding="utf-8")
    director_source = read_director_source(ROOT)
    visual_runtime_source = (ROOT / "static" / "tutorial/core/visual-runtime.js").read_text(encoding="utf-8")
    day1_source = DAY1_GUIDE_PATH.read_text(encoding="utf-8")

    assert "target: 'chat-capsule-input'" in day1_source
    assert "spotlightVariant: 'plain-capsule'" not in day1_source
    assert "this.externalizedChatSpotlightVariant = '';" in takeover_source
    assert "const previousVariant = this.externalizedChatSpotlightVariant;" in takeover_source
    assert "variant: this.externalizedChatSpotlightVariant" in takeover_source
    assert "preserveDuringResistance: true" in takeover_source
    assert "variant: typeof message.variant === 'string' ? message.variant : ''" in interpage_source
    assert "variant: typeof event.data.variant === 'string' ? event.data.variant : ''" in interpage_source
    assert "yuiGuideChatSpotlightVariant = '';" in interpage_source
    assert "yuiGuideChatSpotlightLastPcVariant = '';" in interpage_source
    assert "toYuiGuideScreenRect({" in interpage_source
    assert "}, kind, yuiGuideChatSpotlightVariant, pcWindowMetrics)" in interpage_source
    assert "rememberYuiGuideChatPcSpotlightRects(kind, pcRects, yuiGuideChatSpotlightVariant);" in interpage_source
    assert "yuiGuideChatSpotlightLastPcVariant === yuiGuideChatSpotlightVariant" in interpage_source
    assert "const sceneSpotlightVariant = scene && typeof scene.spotlightVariant === 'string'" in scene_source
    assert "variant: sceneSpotlightVariant" in scene_source
    assert "spotlightVariant: sceneSpotlightVariant" in scene_source
    assert "const spotlightVariant = options && typeof options.spotlightVariant === 'string'" in director_source
    assert "variant: spotlightVariant" in director_source
    assert "const legacySpotlightVariant = legacyScene && typeof legacyScene.spotlightVariant === 'string'" in visual_runtime_source
    assert "variant: legacySpotlightVariant" in visual_runtime_source


def test_external_chat_ready_replays_compact_fixed_layout_when_tutorial_is_active():
    takeover_source = (ROOT / "static" / "tutorial/core/interaction-takeover.js").read_text(encoding="utf-8")
    ready_block = takeover_source.split("onExternalChatReady() {", 1)[1].split(
        "destroy()",
        1,
    )[0]
    fixed_method_block = takeover_source.split("setExternalizedChatCompactFixedLayout(fixed, reason) {", 1)[1].split(
        "clearExternalizedChatGuideMessages()",
        1,
    )[0]

    assert "this.document.body.classList.contains('yui-guide-compact-chat-fixed')" in ready_block
    assert "this.setExternalizedChatCompactFixedLayout(true, 'external-chat-ready')" in ready_block
    assert "yui_guide_set_compact_chat_fixed_layout" in fixed_method_block
    assert "fixed: fixed === true" in fixed_method_block
    assert "reason: typeof reason === 'string' ? reason : ''" in fixed_method_block


def test_pc_overlay_sequence_collision_retries_without_rotating_tutorial_run():
    interpage_source = read_js_parts(INTERPAGE_PATH)
    overlay_source = (ROOT / "static" / "tutorial/yui-guide/overlay.js").read_text(encoding="utf-8")

    assert "YUI_GUIDE_PC_OVERLAY_SEQUENCE_KEY = 'yuiGuidePcOverlaySequence'" in interpage_source
    assert "PC_OVERLAY_SEQUENCE_STORAGE_KEY = 'yuiGuidePcOverlaySequence'" in overlay_source
    assert "YUI_GUIDE_PC_OVERLAY_MAX_SAME_RUN_STALE_RETRIES = 3" in interpage_source
    assert "PC_OVERLAY_MAX_SAME_RUN_STALE_RETRIES = 3" in overlay_source
    assert "YUI_GUIDE_PC_OVERLAY_MAX_TOTAL_STALE_RETRIES = 6" in interpage_source
    assert "PC_OVERLAY_MAX_TOTAL_SAME_RUN_STALE_RETRIES = 6" in overlay_source
    assert "YUI_GUIDE_PC_OVERLAY_DEFERRED_RECONCILIATION_DELAY_MS = 48" in interpage_source
    assert "PC_OVERLAY_DEFERRED_RECONCILIATION_DELAY_MS = 48" in overlay_source
    assert "function nextYuiGuidePcOverlaySequence(minimumSequence)" in interpage_source
    assert "const nextSequence = (minimumSequence) => {" in overlay_source
    assert "window.localStorage.getItem(YUI_GUIDE_PC_OVERLAY_SEQUENCE_KEY)" in interpage_source
    assert "window.localStorage.setItem(YUI_GUIDE_PC_OVERLAY_SEQUENCE_KEY" in interpage_source
    assert "window.localStorage.getItem(PC_OVERLAY_SEQUENCE_STORAGE_KEY)" in overlay_source
    assert "window.localStorage.setItem(PC_OVERLAY_SEQUENCE_STORAGE_KEY" in overlay_source
    assert "yuiGuidePcOverlaySequence = nextYuiGuidePcOverlaySequence();" in interpage_source
    assert "sequence = nextSequence();" in overlay_source
    assert "yuiGuidePcOverlaySequence = Math.max(yuiGuidePcOverlaySequence + 1, Date.now() * 1000);" not in interpage_source
    assert "sequence = Math.max(sequence + 1, Date.now() * 1000);" not in overlay_source
    assert "result.reason === 'stale-sequence'" in interpage_source
    assert "result.reason === 'stale-sequence'" in overlay_source
    assert "result.activeTutorialRunId === attemptedRunId" in interpage_source
    assert "result.activeTutorialRunId === attemptedRunId" in overlay_source
    assert "nextYuiGuidePcOverlaySequence(result.activeSequence)" in interpage_source
    assert "nextSequence(result.activeSequence)" in overlay_source
    assert "retryCount < YUI_GUIDE_PC_OVERLAY_MAX_SAME_RUN_STALE_RETRIES" in interpage_source
    assert "retryCount < PC_OVERLAY_MAX_SAME_RUN_STALE_RETRIES" in overlay_source
    assert "retryCount < YUI_GUIDE_PC_OVERLAY_MAX_TOTAL_STALE_RETRIES" in interpage_source
    assert "retryCount < PC_OVERLAY_MAX_TOTAL_SAME_RUN_STALE_RETRIES" in overlay_source
    assert "scheduleYuiGuidePcOverlayDeferredReconciliation(" in interpage_source
    assert "scheduleDeferredReconciliation(retryCount, attemptedSequence)" in overlay_source
    assert "sendYuiGuidePcOverlayPatch({}, retryCount + 1, {" in interpage_source
    assert "send({}, true, retryCount + 1);" in overlay_source
    assert "readStoredYuiGuidePcOverlaySequence() > attemptedSequence" in interpage_source
    assert "readStoredSequence() > attemptedSequence" in overlay_source
    assert "isDifferentRunStale && retryCount > 0" in interpage_source
    assert "attemptedOwnedRun && retryCount < YUI_GUIDE_PC_OVERLAY_MAX_TOTAL_STALE_RETRIES" in interpage_source
    assert "attemptedSequence !== yuiGuidePcOverlaySequence" in interpage_source
    assert "attemptedSequence !== sequence" in overlay_source
    assert "activeSequence > attemptedSequence" in interpage_source
    assert "activeSequence > attemptedSequence" in overlay_source


def test_pc_overlay_screen_coordinates_use_niri_virtual_origin_and_crop_safe_area():
    interpage_source = read_js_parts(INTERPAGE_PATH)
    overlay_source = OVERLAY_PATH.read_text(encoding="utf-8")
    director_source = read_director_source(ROOT)
    skip_controller_source = SKIP_CONTROLLER_PATH.read_text(encoding="utf-8")
    page_tutorial_source = PAGE_TUTORIAL_MANAGER_PATH.read_text(encoding="utf-8")
    yui_guide_css = YUI_GUIDE_CSS_PATH.read_text(encoding="utf-8")
    tutorial_styles_css = TUTORIAL_STYLES_CSS_PATH.read_text(encoding="utf-8")
    index_css = INDEX_CSS_PATH.read_text(encoding="utf-8")

    assert "if (metrics && (metrics.contentBounds || metrics.bounds))" in interpage_source
    assert "function getYuiGuideNiriPetPhysicalCropState(metrics)" in interpage_source
    assert "function hasYuiGuideNiriPetPhysicalCropVirtualizedMetrics(metrics)" in interpage_source
    assert "metrics.niriPetPhysicalCropMetricsVirtualized === true" in interpage_source
    assert "metrics.niriPetPhysicalCropBounds || metrics.contentBounds || metrics.bounds" in interpage_source
    assert "var api = typeof window !== 'undefined' ? window.__nekoNiriPetPhysicalCrop : null;" in interpage_source
    assert "return !getYuiGuideNiriPetPhysicalCropState(metrics);" in interpage_source
    assert "var screenBounds = cropState.virtualBounds || cropState.cropBounds;" in interpage_source
    assert "x: Number(screenBounds.x || 0) + Number(x || 0)" in interpage_source
    assert "y: Number(screenBounds.y || 0) + Number(y || 0)" in interpage_source
    assert "typeof api.toLayoutVirtualPoint === 'function'" in interpage_source
    assert "? api.toLayoutVirtualPoint" in interpage_source
    assert ": api.toVirtualPoint" in interpage_source
    assert "typeof api.toLayoutVirtualRect === 'function'" in interpage_source
    assert "? api.toLayoutVirtualRect" in interpage_source
    assert ": api.toVirtualRect" in interpage_source
    assert "toYuiGuideNiriPetPhysicalCropVirtualPointWithState" in interpage_source
    assert "if (cropState) {" in interpage_source
    assert "Number(cropState.offsetY || 0)" in interpage_source
    assert "var viewport = shouldApplyYuiGuideVisualViewportOffset(metrics) ? (window.visualViewport || null) : null;" in interpage_source
    assert "if (metrics && (metrics.contentBounds || metrics.bounds))" in overlay_source
    assert "const getNiriPetPhysicalCropState = (metrics) => {" in overlay_source
    assert "const hasNiriPetPhysicalCropVirtualizedMetrics = (metrics) => {" in overlay_source
    assert "metrics.niriPetPhysicalCropMetricsVirtualized === true" in overlay_source
    assert "metrics.niriPetPhysicalCropBounds || metrics.contentBounds || metrics.bounds" in overlay_source
    assert "const api = typeof window !== 'undefined' ? window.__nekoNiriPetPhysicalCrop : null;" in overlay_source
    assert "const shouldApplyVisualViewportOffset = (metrics) => !getNiriPetPhysicalCropState(metrics);" in overlay_source
    assert "const screenBounds = cropState.virtualBounds || cropState.cropBounds;" in overlay_source
    assert "x: Number(screenBounds.x || 0) + Number(x || 0)" in overlay_source
    assert "y: Number(screenBounds.y || 0) + Number(y || 0)" in overlay_source
    assert "typeof api.toLayoutVirtualPoint === 'function'" in overlay_source
    assert "? api.toLayoutVirtualPoint" in overlay_source
    assert ": api.toVirtualPoint" in overlay_source
    assert "typeof api.toLayoutVirtualRect === 'function'" in overlay_source
    assert "? api.toLayoutVirtualRect" in overlay_source
    assert ": api.toVirtualRect" in overlay_source
    assert "toNiriPetPhysicalCropVirtualPointWithState" in overlay_source
    assert "cropState ? {" in overlay_source
    assert "Number(cropState.offsetY || 0)" in overlay_source
    assert "let lastLocalSpotlightEntries = [];" in overlay_source
    assert "window.addEventListener('neko:niri-pet-physical-crop-state-applied', refreshSpotlightsForCropState);" in overlay_source
    assert "const viewport = shouldApplyVisualViewportOffset(metrics) ? (window.visualViewport || null) : null;" in overlay_source
    assert "getNiriPetPhysicalCropState(metrics)" in director_source
    assert "hasNiriPetPhysicalCropVirtualizedMetrics(metrics)" in director_source
    assert "metrics.niriPetPhysicalCropMetricsVirtualized === true" in director_source
    assert "metrics.niriPetPhysicalCropBounds || metrics.contentBounds || metrics.bounds" in director_source
    assert "const api = typeof window !== 'undefined' ? window.__nekoNiriPetPhysicalCrop : null;" in director_source
    assert "typeof api.toLayoutVirtualPoint === 'function'" in director_source
    assert "? api.toLayoutVirtualPoint" in director_source
    assert ": api.toVirtualPoint" in director_source
    assert "typeof api.toLayoutLocalPoint === 'function'" in director_source
    assert "? api.toLayoutLocalPoint" in director_source
    assert ": api.toLocalPoint" in director_source
    assert "toNiriPetPhysicalCropVirtualPointWithState(point, cropState)" in director_source
    assert "toNiriPetPhysicalCropLocalPointWithState(virtualPoint, cropState)" in director_source
    assert "if (cropState) {" in director_source
    assert "- Number(cropState.offsetY || 0)" in director_source
    assert "x: point.x - Number(screenBounds.x || 0)" in director_source
    assert "y: point.y - Number(screenBounds.y || 0)" in director_source
    assert "x: Number(screenBounds.x || 0) + virtualPoint.x" in director_source
    assert "y: Number(screenBounds.y || 0) + virtualPoint.y" in director_source
    assert "--neko-tutorial-crop-safe-area-top: max(var(--neko-tutorial-safe-area-top, 0px), calc(var(--neko-niri-pet-crop-offset-y, 0) * 1px));" in skip_controller_source
    assert "top: calc(max(${baseTop}px, env(safe-area-inset-top)) + var(--neko-tutorial-crop-safe-area-top));" in skip_controller_source
    assert "getNiriPetPhysicalCropTopInset()" in skip_controller_source
    assert "const offset = Number(metrics.niriPetPhysicalCropOffsetY);" in skip_controller_source
    assert "const metricDesktopWorkAreaInset = Number(metrics.desktopWorkAreaTopInset);" in skip_controller_source
    assert "desktopWorkAreaInset = Math.max(desktopWorkAreaInset, Math.round(metricDesktopWorkAreaInset));" in skip_controller_source
    assert "const combinedInset = hasCropEvidence ? cropInset + desktopWorkAreaInset : nonCropDesktopInset;" in skip_controller_source
    assert "const api = window.__nekoNiriPetPhysicalCrop;" in skip_controller_source
    assert "getNiriPetPhysicalCropCssTopInset()" in skip_controller_source
    assert "portalId = normalizedOptions.portalId || 'neko-tutorial-fixed-ui-root';" in skip_controller_source
    assert "this.document.documentElement.appendChild(portal);" in skip_controller_source
    assert "getNiriPetVisibleTopSafeInset()" in skip_controller_source
    assert "recordVisibleInset(metrics.niriWindowTopInset);" in skip_controller_source
    assert "recordVisibleInset(metrics.niriPetPhysicalCropVisibleTopInset);" in skip_controller_source
    assert "getNiriFixedUiMinimumTopInset()" in skip_controller_source
    assert "hasNiriFixedUiEvidence(metrics)" in skip_controller_source
    assert "metrics.niriWaylandRuntime === true" in skip_controller_source
    assert "recordVisibleInset(this.getNiriFixedUiMinimumTopInset());" in skip_controller_source
    assert "getDesktopWorkAreaTopInset(options)" in skip_controller_source
    assert "getCropTopInsetFromBounds(cropBounds, virtualBounds)" in skip_controller_source
    assert "const heightReservedInset = Number.isFinite(screenHeight)" in skip_controller_source
    assert "screenHeight - availHeight - availTop" in skip_controller_source
    assert "const candidateInset = Math.max(" in skip_controller_source
    assert "if (includeWorkAreaTop || hasHostMetrics) {" in skip_controller_source
    assert "const threshold = Math.max(4, candidateInset / 2);" in skip_controller_source
    assert "screenY <= threshold ? candidateInset : 0" in skip_controller_source
    assert "includeWorkAreaTop: hasCropEvidence || combinedInset > 0" in skip_controller_source
    assert "window.addEventListener('neko:niri-pet-physical-crop-state-applied', refresh);" in skip_controller_source
    assert "root.style.setProperty('--neko-tutorial-safe-area-top', transformedInset + 'px');" in skip_controller_source
    assert "const fixedUiInset = Math.max(visibleInset, transformedInset);" in skip_controller_source
    assert "root.style.setProperty('--neko-tutorial-visible-safe-area-top', fixedUiInset + 'px');" in skip_controller_source
    assert "this.applyButtonSafeAreaFrame(buttonUsesPortal ? fixedUiInset : transformedInset);" in skip_controller_source
    assert "applyButtonSafeAreaFrame(inset)" in skip_controller_source
    assert "button.style.setProperty(" in skip_controller_source
    assert "'top'," in skip_controller_source
    assert "'important'" in skip_controller_source
    assert "applySafeAreaVariables: function (options)" in skip_controller_source
    assert "applySkipSafeAreaVariables()" in page_tutorial_source
    assert "this.applySkipSafeAreaVariables();" in page_tutorial_source
    assert "window.TutorialSkipController.applySafeAreaVariables({" in page_tutorial_source
    assert "ensureSkipSafeAreaController()" in page_tutorial_source
    assert "controller.getButtonHost()" in page_tutorial_source
    assert "this._skipSafeAreaController.removeEmptyFixedPortal();" in page_tutorial_source
    assert "installSkipSafeAreaRefreshHooks()" in page_tutorial_source
    assert "window.addEventListener('neko:niri-pet-physical-crop-state-applied', refresh);" in page_tutorial_source
    assert "html.neko-niri-pet-physical-crop .yui-guide-overlay" in yui_guide_css
    assert "calc(var(--neko-niri-pet-crop-offset-x, 0) * 1px)" in yui_guide_css
    assert "calc(var(--neko-niri-pet-crop-offset-y, 0) * 1px)" in yui_guide_css
    assert "--neko-status-toast-crop-safe-area-top: calc(var(--neko-niri-pet-crop-offset-y, 0) * 1px);" in index_css
    assert "top: calc(20px + var(--neko-status-toast-crop-safe-area-top));" in index_css
    assert "top: calc(10px + var(--neko-status-toast-crop-safe-area-top));" in index_css
    yui_skip_rule = yui_guide_css.split("#neko-tutorial-skip-btn", 1)[1].split("}", 1)[0]
    tutorial_skip_rule = tutorial_styles_css.split("#neko-tutorial-skip-btn", 1)[1].split("}", 1)[0]
    page_skip_rule = tutorial_styles_css.split(".neko-page-tutorial-skip-btn", 1)[1].split("}", 1)[0]
    assert "--neko-tutorial-crop-safe-area-top: max(var(--neko-tutorial-safe-area-top, 0px), calc(var(--neko-niri-pet-crop-offset-y, 0) * 1px));" in yui_skip_rule
    assert "--neko-tutorial-crop-safe-area-top: max(var(--neko-tutorial-safe-area-top, 0px), calc(var(--neko-niri-pet-crop-offset-y, 0) * 1px));" in tutorial_skip_rule
    assert "--neko-tutorial-crop-safe-area-top: max(var(--neko-tutorial-safe-area-top, 0px), calc(var(--neko-niri-pet-crop-offset-y, 0) * 1px));" in page_skip_rule
    assert "top: calc(max(14px, env(safe-area-inset-top)) + var(--neko-tutorial-crop-safe-area-top));" in yui_skip_rule
    assert "top: calc(max(14px, env(safe-area-inset-top)) + var(--neko-tutorial-crop-safe-area-top));" in tutorial_skip_rule
    assert "top: calc(max(18px, env(safe-area-inset-top)) + var(--neko-tutorial-crop-safe-area-top));" in page_skip_rule
    assert "top: max(14px, env(safe-area-inset-top));" not in yui_skip_rule
    assert "top: max(14px, env(safe-area-inset-top));" not in tutorial_skip_rule
    assert "top: 18px;" not in page_skip_rule
    assert "const getScreenCoordinateBounds = (metrics) => (" in overlay_source
    assert "const bounds = getScreenCoordinateBounds(metrics);" in overlay_source
    assert "function getYuiGuideScreenCoordinateBounds(metrics)" in interpage_source
    assert "var bounds = getYuiGuideScreenCoordinateBounds(metrics);" in interpage_source
    assert "getGuideScreenCoordinateBounds(metrics)" in director_source
    assert "let bounds = this.getGuideScreenCoordinateBounds(metrics);" in director_source
    assert "- topInset" not in interpage_source
    assert "- topInset" not in overlay_source
    assert "usesNiriPetPhysicalCrop" not in director_source


def test_timeline_scenes_clear_suppressed_spotlights_before_playback():
    source = SCENE_ORCHESTRATOR_PATH.read_text(encoding="utf-8")
    clear_block = source.split("clearSuppressedTimelineSpotlight(scene)", 1)[1].split(
        "async playTimelineScene",
        1,
    )[0]
    timeline_block = source.split("async playTimelineScene(scene, day, index, total, context)", 1)[1].split(
        "prepareSceneNarration(scene)",
        1,
    )[0]

    assert "scene.spotlight !== false" in clear_block
    assert "director.overlay.clearActionSpotlight()" in clear_block
    assert "director.overlay.clearPersistentSpotlight()" in clear_block
    assert "director.clearExternalizedChatSpotlightOnly()" in clear_block
    assert "director.interactionTakeover.setExternalizedChatSpotlight('')" in clear_block
    assert "this.clearSuppressedTimelineSpotlight(scene);" in timeline_block
    assert timeline_block.index("this.clearSuppressedTimelineSpotlight(scene);") < timeline_block.index(
        "const timelineScene = this.normalizeSceneToTimeline(scene);"
    )


def test_pc_overlay_cursor_effect_is_one_shot_not_persisted_on_home_bridge():
    source = OVERLAY_PATH.read_text(encoding="utf-8")
    bridge_block = source.split("function createPcOverlayBridge(doc)", 1)[1].split(
        "function createExtraSpotlightElement",
        1,
    )[0]
    send_block = source.split("const send = (patch, force, retried) => {", 1)[1].split(
        "const key = JSON.stringify(payload || {});",
        1,
    )[0]

    assert "createPcOverlayCompleteStateStore" in bridge_block
    assert "const payload = completeStateStore.applyPatch(patch || {});" in send_block


def test_pc_overlay_resistance_cursor_uses_cursor_only_patch_without_touching_spotlight():
    overlay_source = OVERLAY_PATH.read_text(encoding="utf-8")
    ghost_source = GHOST_CURSOR_PATH.read_text(encoding="utf-8")
    director_source = read_director_source(ROOT)
    resistance_source = RESISTANCE_CONTROLLER_PATH.read_text(encoding="utf-8")

    cursor_only_block = overlay_source.split("const sendCursorOnly = (cursor, retried) => {", 1)[1].split(
        "return {",
        1,
    )[0]
    move_cursor_block = overlay_source.split("moveCursorTo(x, y, options) {", 1)[1].split(
        "clickCursor(durationMs)",
        1,
    )[0]
    director_resistance_block = director_source.split("playCursorResistanceToUserMotion(x, y, distance, motionDx, motionDy)", 1)[1].split(
        "runInterruptResistPerformance",
        1,
    )[0]

    assert "const payload = completeStateStore.applyPatch({ cursor: cursor });" in cursor_only_block
    assert "const payload = { cursor: cursor };" not in cursor_only_block
    assert "handleCursorOnlyStaleResult(result, cursor, retryCount, updateRunId, updateSequence);" in cursor_only_block
    assert "handleCursorOnlyStaleResult(result, cursor, retried === true, beginRunId);" not in cursor_only_block
    assert "result && result.ok === false" in cursor_only_block
    assert "moveCursorOnlyTo(x, y, durationMs, effect, effectDurationMs)" in overlay_source
    assert "normalizedOptions.forcePcOverlay === true" in move_cursor_block
    assert "const cursorEffect = normalizedOptions.effect || '';" in move_cursor_block
    assert "const cursorEffectDurationMs = Math.max(0, Math.round(Number(normalizedOptions.effectDurationMs) || 0));" in move_cursor_block
    assert "this.overlayRenderer.pcOverlayBridge.moveCursorOnlyTo" in move_cursor_block
    assert "this.overlayRenderer.pcOverlayBridge.moveCursorOnlyTo(x, y, 0, cursorEffect, cursorEffectDurationMs);" in move_cursor_block
    assert "normalizedOptions.forcePcOverlay === true\n                && this.isPcOverlayActive()" not in move_cursor_block
    assert "this.cursorVisible = this.isPcOverlayActive();" in move_cursor_block
    assert "forcePcOverlay: normalizedOptions.forcePcOverlay === true" in ghost_source
    assert "forcePcOverlay: true" in director_resistance_block
    assert "forcePcOverlay: true" in resistance_source


def test_externalized_resistance_restores_home_cursor_visibility_before_animating():
    source = read_director_source(ROOT)
    resistance_block = source.split("playCursorResistanceToUserMotion(x, y, distance, motionDx, motionDy)", 1)[1].split(
        "isCursorTransientMotionActive()",
        1,
    )[0]

    assert "let hasVisibleCursor" in resistance_block
    assert "this.isHomeChatExternalized()" in resistance_block
    assert "this.overlay.getCursorPosition()" in resistance_block
    assert "this.cursor.showAt(currentPoint.x, currentPoint.y);" in resistance_block
    assert "this.restoreCursorFromExternalizedChatAnchor(30000)" in resistance_block
    assert "if (!hasVisibleCursor) {" in resistance_block
    assert resistance_block.index("this.restoreCursorFromExternalizedChatAnchor(30000)") < resistance_block.index(
        "if (!hasVisibleCursor) {"
    )


def test_pc_overlay_cursor_effect_is_one_shot_not_persisted_on_external_chat_bridge():
    source = read_js_parts(INTERPAGE_PATH)
    bridge_block = source.split("function sendYuiGuidePcOverlayPatch(patch, retried, options)", 1)[1].split(
        "function isYuiGuidePcCursorOnlyMode()",
        1,
    )[0]

    assert "function withoutTransientYuiGuideCursorEffect(cursor)" in source
    assert "yuiGuidePcOverlayCursor = withoutTransientYuiGuideCursorEffect(patch.cursor);" in bridge_block
    assert "payload.cursor = patch.cursor || null;" in bridge_block
    assert "payload.cursor = yuiGuidePcOverlayCursor;" in bridge_block


def test_day1_round_start_uses_avatar_floating_round_lifecycle():
    source = MANAGER_PATH.read_text(encoding="utf-8")
    start_block = source.split("async startAvatarFloatingGuideRound(day, options = {})", 1)[1].split(
        "async waitForTutorialTeardownSettled(reason = '')",
        1,
    )[0]

    assert "if (round === 1)" not in start_block
    assert "requestTutorialStart" not in start_block
    assert "director.playAvatarFloatingRound(round" in start_block


def test_avatar_floating_round_start_keeps_tutorial_model_reload_before_first_scene():
    source = MANAGER_PATH.read_text(encoding="utf-8")
    prelude_source = (ROOT / "static" / "tutorial/core/round-prelude-controller.js").read_text(encoding="utf-8")
    start_block = source.split("async startAvatarFloatingGuideRound(day, options = {})", 1)[1].split(
        "async waitForTutorialTeardownSettled(reason = '')",
        1,
    )[0]

    assert "this._tutorialModelPrefix = 'live2d';" in start_block
    assert "await this.playAvatarFloatingRoundPrelude(round, source, director, {" in start_block
    assert "this.beginAvatarOverride({" in prelude_source
    assert "deferRevealPrepared" in prelude_source
    assert "this.ensureVisible(sceneId, {" in prelude_source
    assert "deferRevealPrepared" in prelude_source
    assert "director.playAvatarFloatingRound(round" in start_block
    assert start_block.index("this.playAvatarFloatingRoundPrelude(round, source, director,") < start_block.index(
        "director.playAvatarFloatingRound(round"
    )


def test_avatar_floating_round_waits_after_tutorial_model_is_visible():
    source = MANAGER_PATH.read_text(encoding="utf-8")
    prelude_source = (ROOT / "static" / "tutorial/core/round-prelude-controller.js").read_text(encoding="utf-8")
    start_block = source.split("async startAvatarFloatingGuideRound(day, options = {})", 1)[1].split(
        "async waitForTutorialTeardownSettled(reason = '')",
        1,
    )[0]

    assert "await toPromise(() => this.sleep(delayMs));" in prelude_source
    assert "this.defaultDelayMs" in prelude_source
    assert "1500" in prelude_source
    assert prelude_source.index("this.ensureVisible(sceneId, {") < prelude_source.index(
        "await toPromise(() => this.sleep(delayMs));"
    )
    assert "deferRevealPrepared: true" in source
    assert start_block.index("this.playAvatarFloatingRoundPrelude(round, source, director,") < start_block.index(
        "director.playAvatarFloatingRound(round"
    )


def test_avatar_floating_round_does_not_preheat_surface_before_playback():
    source = MANAGER_PATH.read_text(encoding="utf-8")
    start_block = source.split("async startAvatarFloatingGuideRound(day, options = {})", 1)[1].split(
        "async waitForTutorialTeardownSettled(reason = '')",
        1,
    )[0]

    assert "surfaceReadyPromise" not in start_block
    assert "ensureAvatarFloatingGuideSurfaceReady(round)" not in start_block
    assert "surfaceReady: true" in start_block


def test_tutorial_avatar_override_does_not_capture_avatar_preview():
    source = (ROOT / "static" / "tutorial/avatar/reload-controller.js").read_text(encoding="utf-8")
    begin_block = source.split("beginOverride(", 1)[1].split("restoreOverride()", 1)[0]

    assert "this.sleep(350)" not in begin_block
    assert "captureAvatarPreview" not in source
    assert "startIdentityOverrideCapture" not in source
    assert "this.applyIdentityOverride({" in begin_block
    assert "deferRevealPrepared" in begin_block
    assert begin_block.index("this.applyIdentityOverride({") > begin_block.index(
        "await this.reloadModel(currentName, tutorialModelPayload,"
    )


def test_avatar_floating_round_does_not_start_idle_sway_before_first_scene():
    source = (ROOT / "static" / "tutorial/core/scene-orchestrator.js").read_text(encoding="utf-8")
    round_block = source.split("async playRound(round, options)", 1)[1].split(
        "return {",
        1,
    )[0]
    before_scene_loop = round_block.split("for (let index = 0; index < config.scenes.length; index += 1)", 1)[0]

    assert "ensureGuideIdleSwayPerformance()" not in before_scene_loop


def test_day1_round_defers_cursor_look_at_until_capsule_mouse_hint():
    source = (ROOT / "static" / "tutorial/core/scene-orchestrator.js").read_text(encoding="utf-8")
    round_block = source.split("async playRound(round, options)", 1)[1].split(
        "return {",
        1,
    )[0]
    scene_loop = round_block.split("for (let index = 0; index < config.scenes.length; index += 1)", 1)[1].split(
        "const keepGoing = await director.playAvatarFloatingScene",
        1,
    )[0]

    assert "return await playScenes();" in round_block
    assert "return await director.withLookAt({" in round_block
    assert "config.scenes[index].id === 'day1_capsule_drag_hint'" in scene_loop
    assert "startDay1LookAt();" in scene_loop


def test_day1_chat_input_round_rect_highlight_excludes_mid_flow_cursor_scenes():
    source = DAY1_GUIDE_PATH.read_text(encoding="utf-8")
    round_block = extract_day1_round_block(source)
    greeting_scene_block = round_block.split("id: 'day1_intro_greeting'", 1)[1].split("id: 'day1_capsule_drag_hint'", 1)[0]
    capsule_block = round_block.split("id: 'day1_capsule_drag_hint'", 1)[1].split("id: 'day1_history_handle'", 1)[0]
    history_block = round_block.split("id: 'day1_history_handle'", 1)[1].split("id: 'day1_intro_basic_voice'", 1)[0]
    screen_entry_block = round_block.split("id: 'day1_screen_entry'", 1)[1].split("id: 'day1_screen_entry_invite'", 1)[0]
    screen_invite_block = round_block.split("id: 'day1_screen_entry_invite'", 1)[1].split(
        "id: 'day1_takeover_capture_cursor'",
        1,
    )[0]
    avatar_zoom_hint_block = round_block.split("id: 'day1_avatar_zoom_hint'", 1)[1].split(
        "id: 'day1_takeover_return_control'",
        1,
    )[0]

    assert "id: 'day1_intro_greeting'" in round_block
    assert "id: 'day1_avatar_zoom_hint'" in round_block
    assert "id: 'day1_takeover_return_control'" in round_block
    assert "cursorAction: 'wobble'" not in greeting_scene_block
    assert "timelinePlayback: true" in greeting_scene_block
    assert "day1-intro-greeting-flow" not in greeting_scene_block
    assert "target: 'chat-capsule-input'" in greeting_scene_block
    assert "cursorTarget: 'chat-capsule-input'" in greeting_scene_block
    assert "cursorAction: 'move'" in greeting_scene_block
    assert "operation: 'day1-intro-greeting-performance'" in greeting_scene_block
    assert "target: 'chat-capsule-input'" in capsule_block
    assert "spotlight: false" in capsule_block
    assert "cursorWobbleDurationMs: 2000" in capsule_block
    assert "target: 'chat-input'" in history_block
    assert "cursorTarget: 'chat-history-handle'" in history_block
    assert "spotlight: false" in history_block
    assert "persistent: 'chat-input'" not in history_block
    assert "cursorAction: 'click'" in screen_entry_block
    assert "cursorAction: 'wobble'" not in screen_entry_block
    assert "target: '.${p}-trigger-btn'" in screen_entry_block
    assert "spotlight: false" in screen_entry_block
    assert "operation: 'day1-screen-share-entry-flow'" in screen_entry_block
    assert "target: '#${p}-btn-screen'" not in screen_entry_block
    assert "cursorAction: 'hold'" in screen_invite_block
    assert "cursorAction: 'wobble'" not in screen_invite_block
    assert "target: '#${p}-popup-mic [data-neko-mic-main-action-row=\"screen\"]'" in screen_invite_block
    assert "target: '#${p}-btn-screen'" not in screen_invite_block
    assert "textKey: 'tutorial.avatarFloating.day1.avatarZoomHint'" in avatar_zoom_hint_block
    assert "voiceKey: 'day1_avatar_zoom_hint'" in avatar_zoom_hint_block
    assert "spotlight: false" in avatar_zoom_hint_block
    assert "cursorAction: 'none'" in avatar_zoom_hint_block
    assert "{ at: 0, command: 'operation.run', operation: 'cleanup', blocking: true }" in avatar_zoom_hint_block
    assert "{ at: 0, command: 'chat.message' }" in avatar_zoom_hint_block
    assert avatar_zoom_hint_block.index("operation: 'cleanup'") < avatar_zoom_hint_block.index("command: 'chat.message'")

    return_control_scene = round_block.split("id: 'day1_takeover_return_control'", 1)[1]
    assert "{ at: 0, command: 'operation.run', operation: 'cleanup', blocking: true }" in return_control_scene
    assert "{ at: 0, command: 'chat.message' }" in return_control_scene
    assert return_control_scene.index("operation: 'cleanup'") < return_control_scene.index("command: 'chat.message'")
    assert "cursorAction: 'move'" in return_control_scene
    assert "cursorAction: 'wobble'" not in return_control_scene

    director_source = read_director_source(ROOT)
    assert "scene.cursorTarget || scene.target || ''" in director_source
    assert "scene.cursorTarget || scene.target" in director_source

    director = read_director_source(ROOT)
    activation_block = director.split("async playDay1IntroActivationRoundScene", 1)[1].split(
        "async playDay1IntroGreetingRoundScene",
        1,
    )[0]
    assert "focusAndHighlightChatInput" not in activation_block
    assert "setExternalizedChatSpotlight('input')" not in activation_block
    assert "setExternalizedChatCursor('input'" not in activation_block
    assert "effect: 'wobble'" not in activation_block
    assert "setExternalizedChatCursor('');" not in activation_block
    assert "this.hideHomeCursorForExternalizedChat();" in activation_block
    assert "await this.runWakeupPrelude();" in activation_block
    assert "await this.waitForIntroActivationTransition();" in activation_block
    assert "wait(360)" not in activation_block

    transition_block = director.split("waitForIntroActivationTransition() {", 1)[1].split(
        "\n        shouldReduceTutorialMotion() {",
        1,
    )[0]
    assert "INTRO_ACTIVATION_AUTO_ADVANCE_MS" in transition_block
    assert "INTRO_ACTIVATION_REDUCED_MOTION_AUTO_ADVANCE_MS" in transition_block
    assert "return wait(waitMs);" in transition_block


def test_day1_capsule_drag_hint_copy_uses_single_click_language():
    source = DAY1_GUIDE_PATH.read_text(encoding="utf-8")
    capsule_block = source.split("id: 'day1_capsule_drag_hint'", 1)[1].split(
        "id: 'day1_history_handle'",
        1,
    )[0]

    assert "点击一下就能随时发消息给我哦！" in capsule_block
    assert "双击两下" not in capsule_block


def test_day1_intro_basic_voice_moves_from_history_handle_anchor():
    director = read_director_source(ROOT)
    showcase_block = director.split("async runIntroVoiceControlButtonShowcase", 1)[1].split(
        "async runTakeoverKeyboardControlSequence",
        1,
    )[0]

    assert "getAvatarFloatingSceneCursorAnchor('day1_history_handle')" in showcase_block
    assert "this.cursor.showAt(historyHandleAnchor.x, historyHandleAnchor.y);" in showcase_block
    assert "await this.moveCursorToElement(voiceControlButton, moveDurationMs);" in showcase_block


def test_day1_takeover_restores_original_agent_switches():
    director = read_director_source(ROOT)
    operations = (ROOT / "static" / "tutorial/core/operation-registry.js").read_text(encoding="utf-8")
    restore_block = director.split("async restoreDay1TakeoverAgentSwitches(reason)", 1)[1].split(
        "async clickAgentSidePanelAction",
        1,
    )[0]
    capture_operation = operations.split("async runDay1TakeoverCaptureCursor(scene)", 1)[1].split(
        "async runCleanup(scene)",
        1,
    )[0]
    cleanup_operation = operations.split("async runCleanup(scene)", 1)[1].split(
        "async runDay6PluginOpenAgentPanelFlow",
        1,
    )[0]

    assert "this.takeoverOriginalAgentSwitches = null;" in director
    assert "async captureDay1TakeoverAgentSwitches()" in director
    assert "await director.captureDay1TakeoverAgentSwitches();" in capture_operation
    assert "sceneId === 'day1_avatar_zoom_hint'" in cleanup_operation
    assert "'day1-before-avatar-zoom-hint'" in cleanup_operation
    assert "sceneId === 'day1_takeover_return_control'" in cleanup_operation
    assert "'day1-return-control'" in cleanup_operation
    assert "this.setDay1AvatarZoomInteractionActive(false);" in cleanup_operation
    assert "this.setDay1AvatarZoomInteractionActive(true);" in cleanup_operation
    assert "await this.director.restoreDay1TakeoverAgentSwitches(day1RestoreReason);" in cleanup_operation
    interaction_block = operations.split("setDay1AvatarZoomInteractionActive(active)", 1)[1].split(
        "async runCleanup(scene)",
        1,
    )[0]
    assert "director.overlay.setInteractionShieldSuppressed(isActive);" in interaction_block
    assert "director.disableInterrupts();" in interaction_block
    assert "director.cursor.hide();" in interaction_block
    assert "this.setDay1AvatarZoomModelLocked(isActive);" in interaction_block
    assert "document.documentElement.classList.toggle('yui-user-cursor-revealed', isActive);" in interaction_block
    assert "document.body.classList.toggle('yui-user-cursor-revealed', isActive);" in interaction_block
    assert "director.syncSystemCursorHidden(" in interaction_block
    model_lock_block = operations.split("setDay1AvatarZoomModelLocked(active)", 1)[1].split(
        "async runCleanup(scene)",
        1,
    )[0]
    assert "this.day1AvatarZoomOriginalLive2dLocked = manager.isLocked === true;" in model_lock_block
    assert "manager.setLocked(false, { updateFloatingButtons: false });" in model_lock_block
    assert "manager.setLocked(this.day1AvatarZoomOriginalLive2dLocked, { updateFloatingButtons: false });" in model_lock_block
    assert "this.day1AvatarZoomOriginalLive2dLocked = null;" in model_lock_block
    assert "setAgentFlagEnabled('computer_use_enabled', originalKeyboardControl)" in restore_block
    assert "setAgentMasterEnabled(false)" in restore_block
    assert "restoreDay1TakeoverAgentSwitches('termination_cleanup')" in director
    assert "restoreDay1TakeoverAgentSwitches('destroy')" in director


def test_day1_intro_greeting_highlights_capsule_input_without_cursor_wobble():
    source = DAY1_GUIDE_PATH.read_text(encoding="utf-8")
    greeting_block = source.split("id: 'day1_intro_greeting'", 1)[1].split(
        "id: 'day1_capsule_drag_hint'",
        1,
    )[0]

    assert "setExternalizedChatCursor('');" not in greeting_block
    assert "target: 'chat-capsule-input'" in greeting_block
    assert "cursorTarget: 'chat-capsule-input'" in greeting_block
    assert "cursorAction: 'move'" in greeting_block
    assert "operation: 'day1-intro-greeting-performance'" in greeting_block
    assert "day1-intro-greeting-flow" not in greeting_block
    assert "timelinePlayback: true" in greeting_block
    assert "cursorAction: 'wobble'" not in greeting_block


def test_day1_intro_greeting_performance_operation_does_not_play_narration():
    source = (ROOT / "static" / "tutorial/core/operation-registry.js").read_text(encoding="utf-8")
    operation_block = source.split("async runDay1IntroGreetingPerformance(context)", 1)[1].split(
        "async runDay1IntroBasicVoiceShowcase",
        1,
    )[0]

    assert "runDailyIntroAvatarPerformance" in operation_block
    assert "preset: 'wave-zoom'" in operation_block
    assert "runIntroGiftHeartPerformance" in operation_block
    assert "speakGuideLine" not in operation_block
    assert "appendGuideChatMessage" not in operation_block
    assert "setExternalizedChatCursor" not in operation_block


def test_daily_intro_avatar_motion_presets_are_fixed_per_day():
    guide_specs = [
        (DAY2_GUIDE_PATH, "day2_tool_toggle_intro", "day2_avatar_tools", "corner-peek", "bottom-left"),
        (DAY3_GUIDE_PATH, "day3_intro_context", "day3_personalization_space", "bottom-rise", None),
        (DAY4_GUIDE_PATH, "day4_intro_companion", "day4_chat_settings", "soft-approach", None),
        (DAY6_GUIDE_PATH, "day6_intro_agent", "day6_agent_status_master", "corner-peek", "bottom-right"),
        (DAY7_GUIDE_PATH, "day7_memory_review", "day7_memory_control", "bottom-rise-slow", None),
    ]

    day1_source = DAY1_GUIDE_PATH.read_text(encoding="utf-8")
    day1_block = day1_source.split("id: 'day1_intro_greeting'", 1)[1].split(
        "id: 'day1_capsule_drag_hint'",
        1,
    )[0]
    assert "operation: 'day1-intro-greeting-performance'" in day1_block
    assert "introAvatarPerformance:" in day1_block
    assert "preset: 'wave-zoom'" in day1_block
    assert "timelinePlayback: true" in day1_block
    assert "{ at: 0, command: 'operation.run', operation: 'day1-intro-greeting-performance', blocking: false }" in day1_block
    assert "{ at: 0, command: 'chat.message' }" in day1_block
    assert day1_block.index("day1-intro-greeting-performance") < day1_block.index("chat.message")

    for guide_path, first_scene_id, next_scene_id, preset, position in guide_specs:
        source = guide_path.read_text(encoding="utf-8")
        scene_block = source.split(f"id: '{first_scene_id}'", 1)[1].split(
            f"id: '{next_scene_id}'",
            1,
        )[0]
        assert "timelinePlayback: true" in scene_block
        assert "introAvatarPerformance:" in scene_block
        assert f"preset: '{preset}'" in scene_block
        if position:
            assert f"position: '{position}'" in scene_block
        assert "operation: 'daily-intro-avatar-performance'" in scene_block
        assert "{ at: 0, command: 'operation.run', operation: 'daily-intro-avatar-performance', blocking: false }" in scene_block


def test_day3_intro_bottom_rise_uses_shared_two_second_opening_motion_after_day_swap():
    source = DAY3_GUIDE_PATH.read_text(encoding="utf-8")
    avatar_stage_source = (ROOT / "static" / "tutorial/avatar/yui-stage.js").read_text(encoding="utf-8")
    director_source = read_director_source(ROOT)
    scene_block = source.split("id: 'day3_intro_context'", 1)[1].split(
        "id: 'day3_personalization_space'",
        1,
    )[0]
    daily_intro_block = director_source.split("async runDailyIntroAvatarPerformance(scene, day, options)", 1)[1].split(
        "async runIntroGreetingHugPerformance()",
        1,
    )[0]
    probe_motion_block = avatar_stage_source.split(
        "async function playTutorialAvatarProbeFrameMotion(options, preset)",
        1,
    )[1].split("async function playAvatarMotion(options)", 1)[0]
    play_motion_block = avatar_stage_source.split("async function playAvatarMotion(options)", 1)[1].split(
        "async function playSettingsPeekPanic(options)",
        1,
    )[0]

    assert "preset: 'bottom-rise'" in scene_block
    assert "approachMs:" not in scene_block
    assert "narrationBudgeted: tutorialDay >= 2 && tutorialDay <= 7" in daily_intro_block
    assert "if (normalizedOptions.narrationBudgeted === true)" in play_motion_block
    assert "return playTutorialAvatarProbeFrameMotion(normalizedOptions, preset);" in play_motion_block
    assert "const TUTORIAL_AVATAR_PROBE_APPROACH_MS = 2000;" in avatar_stage_source
    assert "enterMs: TUTORIAL_AVATAR_PROBE_APPROACH_MS" in probe_motion_block
    assert "restore: 'half-body'" in scene_block


def test_day5_first_scene_runs_fixed_intro_avatar_motion_without_blocking_settings_tour():
    source = DAY5_GUIDE_PATH.read_text(encoding="utf-8")
    scene_block = source.split("id: 'day5_character_settings'", 1)[1].split(
        "id: 'day5_character_panic'",
        1,
    )[0]

    assert "introAvatarPerformance:" in scene_block
    assert "preset: 'top-peek'" in scene_block
    assert "{ at: 0, command: 'operation.run', operation: 'daily-intro-avatar-performance', blocking: false }" in scene_block
    assert "{ at: 0, command: 'settingsTour.play', blocking: true }" in scene_block
    assert scene_block.index("daily-intro-avatar-performance") < scene_block.index("settingsTour.play")


def test_peek_intro_avatar_motions_explicitly_restore_to_half_body():
    guide_specs = [
        (DAY2_GUIDE_PATH, "day2_tool_toggle_intro", "day2_avatar_tools"),
        (DAY5_GUIDE_PATH, "day5_character_settings", "day5_character_panic"),
        (DAY6_GUIDE_PATH, "day6_intro_agent", "day6_agent_status_master"),
    ]

    for guide_path, first_scene_id, next_scene_id in guide_specs:
        source = guide_path.read_text(encoding="utf-8")
        scene_block = source.split(f"id: '{first_scene_id}'", 1)[1].split(
            f"id: '{next_scene_id}'",
            1,
        )[0]
        assert "restore: 'half-body'" in scene_block


def test_peek_intro_avatar_motions_keep_floating_buttons_attached_only_for_intro():
    guide_specs = [
        (DAY2_GUIDE_PATH, "day2_tool_toggle_intro", "day2_avatar_tools"),
        (DAY5_GUIDE_PATH, "day5_character_settings", "day5_character_panic"),
        (DAY6_GUIDE_PATH, "day6_intro_agent", "day6_agent_status_master"),
    ]
    director_source = read_director_source(ROOT)

    for guide_path, first_scene_id, next_scene_id in guide_specs:
        source = guide_path.read_text(encoding="utf-8")
        scene_block = source.split(f"id: '{first_scene_id}'", 1)[1].split(
            f"id: '{next_scene_id}'",
            1,
        )[0]
        assert "freezeFloatingButtons: false" in scene_block

    assert "freezeFloatingButtons: performance.freezeFloatingButtons === false ? false : undefined" in director_source


def test_corner_intro_avatar_motions_rotate_floating_buttons_with_model_when_model_rotates():
    day2_source = DAY2_GUIDE_PATH.read_text(encoding="utf-8")
    day5_source = DAY5_GUIDE_PATH.read_text(encoding="utf-8")
    day6_source = DAY6_GUIDE_PATH.read_text(encoding="utf-8")
    director_source = read_director_source(ROOT)

    day2_block = day2_source.split("id: 'day2_tool_toggle_intro'", 1)[1].split(
        "id: 'day2_avatar_tools'",
        1,
    )[0]
    day6_block = day6_source.split("id: 'day6_intro_agent'", 1)[1].split(
        "id: 'day6_agent_status_master'",
        1,
    )[0]
    assert "rotateFloatingButtons: true" in day2_block
    assert "rotateFloatingButtons: true" in day6_block

    assert "rotateFloatingButtons: true" not in day5_source
    assert "rotateFloatingButtons: performance.rotateFloatingButtons === true" in director_source


def test_peek_intro_half_body_session_fades_out_and_restores_full_opacity():
    source = (ROOT / "static" / "tutorial/avatar/yui-stage.js").read_text(encoding="utf-8")
    corner_block = source.split("async function playTimedAvatarCornerPeek(options, position)", 1)[1].split(
        "async function playFrameAvatarMotion",
        1,
    )[0]
    corner_session_block = source.split("class Live2DAvatarCornerPeekSession", 1)[1].split(
        "class Live2DSettingsPeekPanicSession",
        1,
    )[0]

    assert "restoreMode: normalizedOptions.restore || normalizedOptions.restoreMode || 'half-body'" in corner_block
    assert "await handle.stop('avatar_motion_complete');" in corner_block
    assert "this.restoreAlpha = this.restoreMode === 'half-body' ? 1 : this.initialAlpha;" in corner_session_block
    assert "lerp(1, 0, progress)" in corner_session_block
    assert "lerp(0, this.restoreAlpha, progress)" in corner_session_block


def test_avatar_floating_intro_motion_reveals_prepared_tutorial_model():
    manager_source = MANAGER_PATH.read_text(encoding="utf-8")
    orchestrator_source = SCENE_ORCHESTRATOR_PATH.read_text(encoding="utf-8")
    director_source = read_director_source(ROOT)

    prelude_block = manager_source.split("async playAvatarFloatingRoundPrelude(round, source, director, options = {})", 1)[1].split(
        "async checkAndStartTutorial()",
        1,
    )[0]
    assert "deferRevealPrepared: true" in prelude_block
    assert "Number(round) === 1" not in prelude_block

    round_block = manager_source.split("const completed = await director.playAvatarFloatingRound(round,", 1)[1].split(
        "});",
        1,
    )[0]
    assert "revealPrepared: () => this.revealTutorialLive2dPrepared()" in round_block

    assert "async playScene(scene, day, index, total, roundContext = {})" in orchestrator_source
    assert "revealPrepared: roundContext.revealPrepared" in orchestrator_source
    assert "config.scenes.length,\n                            options || {}" in orchestrator_source

    assert "async runAvatarFloatingSceneOperation(scene, primaryTarget, narrationStartedAt, narrationPromise, operationContext)" in director_source
    assert "this.operationRegistry.run(scene, primaryTarget, narrationStartedAt, narrationPromise, operationContext)" in director_source


def test_day1_externalized_intro_greeting_uses_scene_orchestrator_without_cursor_wobble():
    source = SCENE_ORCHESTRATOR_PATH.read_text(encoding="utf-8")
    externalized_block = source.split("if (introExternalizedChatSpotlightKind) {", 1)[1].split(
        "} else if (introChatSpotlightTarget)",
        1,
    )[0]

    assert "director.interactionTakeover.setExternalizedChatSpotlight(" in externalized_block
    assert "introExternalizedChatSpotlightKind" in externalized_block
    assert "effect: 'wobble'" not in externalized_block


def test_day1_legacy_externalized_intro_greeting_does_not_send_cursor_wobble():
    source = read_director_source(ROOT)
    externalized_block = source.split("async playDay1IntroGreetingRoundScene(sceneRunId)", 1)[1].split(
        "await this.playIntroGreetingReply()",
        1,
    )[0]

    assert "setExternalizedChatGuideTarget('capsule-input'" in externalized_block
    assert "setExternalizedChatCursor('input'" not in externalized_block
    assert "setExternalizedChatCursor('');" not in externalized_block
    assert "effect: 'wobble'" not in externalized_block
    assert "this.cursor.hide();" not in externalized_block


def test_day2_intro_externalized_cursor_uses_scene_action_not_wobble():
    orchestrator_source = SCENE_ORCHESTRATOR_PATH.read_text(encoding="utf-8")
    director_source = read_director_source(ROOT)
    first_daily_externalized_block = orchestrator_source.split("if (introExternalizedChatSpotlightKind) {", 1)[1].split(
        "} else if (introChatSpotlightTarget)",
        1,
    )[0]
    cursor_options_block = director_source.split("getAvatarFloatingIntroExternalizedCursorOptions(scene)", 1)[1].split(
        "setHomePcCursorOutputSuppressedForExternalizedChat",
        1,
    )[0]

    assert "director.getAvatarFloatingIntroExternalizedCursorOptions(scene)" in first_daily_externalized_block
    assert "effect: this.getExternalizedChatCursorEffect(scene)" in cursor_options_block
    assert "effect: 'wobble'" not in first_daily_externalized_block


def test_day1_intro_externalized_chat_suppresses_home_pc_cursor_before_hiding_it():
    source = SCENE_ORCHESTRATOR_PATH.read_text(encoding="utf-8")
    prelude_block = source.split("applyFirstDailySceneIntroCursorPrelude(scene, context)", 1)[1].split(
        "const introTarget = typeof director.getAvatarFloatingIntroSpotlightTarget",
        1,
    )[0]
    spotlight_block = source.split("if (introExternalizedChatSpotlightKind) {", 1)[1].split(
        "} else if (introChatSpotlightTarget)",
        1,
    )[0]

    assert prelude_block.index("director.setHomePcCursorOutputSuppressedForExternalizedChat(true);") < prelude_block.index(
        "director.hideHomeCursorForExternalizedChat();"
    )
    assert spotlight_block.index("director.setHomePcCursorOutputSuppressedForExternalizedChat(true);") < spotlight_block.index(
        "director.hideHomeCursorForExternalizedChat();"
    )


def test_day1_return_control_declares_externalized_cursor_preservation():
    source = DAY1_GUIDE_PATH.read_text(encoding="utf-8")
    round_block = extract_day1_round_block(source)
    scene_id_index = round_block.index("id: 'day1_takeover_return_control'")
    scene_start = round_block.rfind("\n                {", 0, scene_id_index)
    scene_end = round_block.find("\n                }", scene_id_index)
    assert scene_start != -1
    assert scene_end != -1
    return_control_scene = round_block[scene_start:scene_end]

    assert "preserveExternalizedChatGuideTarget: true" in return_control_scene


def test_only_day1_tutorial_configs_use_cursor_wobble():
    guide_files = sorted(Path("static").glob("tutorial/yui-guide/days/day*-*.js"))
    for guide_file in guide_files:
        if guide_file.name.startswith("day1-"):
            continue
        source = guide_file.read_text(encoding="utf-8")
        assert "cursorAction: 'wobble'" not in source
