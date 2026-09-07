import json
import re
import shutil

import pytest
from pathlib import Path
from tests.node_harness import run_node_script
from tests.static_app_parts import read_js_parts


class JavaScriptParts:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def read_text(self, *, encoding: str) -> str:
        return read_js_parts(self.directory, encoding=encoding)


APP_REACT_CHAT_WINDOW_PATH = JavaScriptParts(
    Path(__file__).resolve().parents[2] / "static" / "app" / "app-react-chat-window"
)
APP_JS_PATH = Path(__file__).resolve().parents[2] / "static" / "app" / "app.js"
APP_BUTTONS_PATH = Path(__file__).resolve().parents[2] / "static" / "app" / "app-buttons.js"
APP_CHAT_EXPORT_PATH = Path(__file__).resolve().parents[2] / "static" / "app" / "app-chat-export.js"
APP_INTERPAGE_PATH = JavaScriptParts(
    Path(__file__).resolve().parents[2] / "static" / "app" / "app-interpage"
)
AVATAR_UI_POPUP_PATH = Path(__file__).resolve().parents[2] / "static" / "avatar" / "avatar-ui-popup.js"
AVATAR_POPUP_COMMON_PATH = Path(__file__).resolve().parents[2] / "static" / "avatar" / "avatar-popup-common.js"
STATIC_LOCALES_DIR = Path(__file__).resolve().parents[2] / "static" / "locales"
MUSIC_UI_PATH = Path(__file__).resolve().parents[2] / "static" / "jukebox" / "music_ui.js"
MUSIC_UI_CSS_PATH = Path(__file__).resolve().parents[2] / "static" / "css" / "music_ui.css"
STATIC_INDEX_CSS_PATH = Path(__file__).resolve().parents[2] / "static" / "css" / "index.css"
STATIC_DARK_MODE_CSS_PATH = Path(__file__).resolve().parents[2] / "static" / "css" / "dark-mode.css"
STATIC_INDEX_JS_PATH = Path(__file__).resolve().parents[2] / "static" / "js" / "index.js"
REACT_CHAT_STYLES_PATH = Path(__file__).resolve().parents[2] / "frontend" / "react-neko-chat" / "src" / "styles.css"
REACT_CHAT_APP_PATH = Path(__file__).resolve().parents[2] / "frontend" / "react-neko-chat" / "src" / "App.tsx"
REACT_CHAT_AVATAR_TOOL_MANAGER_PATH = (
    Path(__file__).resolve().parents[2] / "frontend" / "react-neko-chat" / "src" / "AvatarToolItemManager.tsx"
)
REACT_CHAT_FULL_SURFACE_PATH = Path(__file__).resolve().parents[2] / "frontend" / "react-neko-chat" / "src" / "FullChatSurface.tsx"
REACT_CHAT_MESSAGE_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "frontend" / "react-neko-chat" / "src" / "message-schema.ts"
)
REACT_CHAT_IIFE_PATH = Path(__file__).resolve().parents[2] / "static" / "react" / "neko-chat" / "neko-chat-window.iife.js"
CHAT_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "chat.html"
INDEX_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "index.html"
SUBTITLE_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "subtitle.html"
PAGES_ROUTER_PATH = Path(__file__).resolve().parents[2] / "main_routers" / "pages_router.py"
MAIN_SERVER_PATH = Path(__file__).resolve().parents[2] / "app" / "main_server" / "__init__.py"
COMPACT_EXPORT_HISTORY_PANEL_PATH = (
    Path(__file__).resolve().parents[2] / "frontend" / "react-neko-chat" / "src" / "CompactExportHistoryPanel.tsx"
)


def css_block(styles: str, selector: str, next_selector: str) -> str:
    start = styles.find(selector)
    if start < 0:
        snippet = styles[:240].replace("\n", "\\n")
        raise AssertionError(
            f"css_block could not find selector {selector!r}; "
            f"next_selector={next_selector!r}; styles snippet={snippet!r}"
        )
    content_start = start + len(selector)
    end = styles.find(next_selector, content_start)
    if end < 0:
        snippet = styles[content_start : content_start + 240].replace("\n", "\\n")
        raise AssertionError(
            f"css_block could not find next_selector {next_selector!r} "
            f"after selector {selector!r}; styles snippet={snippet!r}"
        )
    return styles[content_start:end]


def assert_no_layout_transition(block: str) -> None:
    transition_section = block.split("transition:", 1)[1].split(";", 1)[0] if "transition:" in block else ""
    for prop in ("width", "height", "max-height", "min-height", "padding", "margin", "top", "right", "bottom", "left"):
        assert prop not in transition_section


def test_rps_result_hands_separate_smoothly_and_winner_is_already_above_on_approach():
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")

    assert ".avatar-tool-round-reveal.is-result .avatar-tool-round-reveal-hand.is-user" in styles
    assert "animation: avatar-tool-rps-user-separate" in styles
    assert "animation: avatar-tool-rps-avatar-separate" in styles
    assert "@keyframes avatar-tool-rps-user-separate" in styles
    assert "@keyframes avatar-tool-rps-avatar-separate" in styles
    assert ".avatar-tool-round-reveal.is-user_win.is-approach .avatar-tool-round-reveal-hand.is-user" in styles
    assert ".avatar-tool-round-reveal.is-avatar_win.is-approach .avatar-tool-round-reveal-hand.is-avatar" in styles


def test_chat_settings_auto_cat_and_cat_audio_toggles_are_independent():
    source = AVATAR_UI_POPUP_PATH.read_text(encoding="utf-8")
    chat_settings_block = source.split("const chatToggles = [", 1)[1].split("];", 1)[0]

    assert "id: 'auto-cat'" in chat_settings_block
    assert "id: 'cat-audio'" in chat_settings_block
    assert chat_settings_block.index("id: 'auto-cat'") < chat_settings_block.index("id: 'cat-audio'")
    assert "labelKey: 'settings.toggles.catAudio'" in chat_settings_block
    cat_audio_config = chat_settings_block.split("{ id: 'cat-audio'", 1)[1].split("}", 1)[0]
    assert "dependsOnToggleId" not in cat_audio_config
    assert "neko:auto-cat-setting-changed" not in source

    cat_audio_init_block = source.split("} else if (toggle.id === 'cat-audio'", 1)[1].split(
        "const indicator = document.createElement('div');",
        1,
    )[0]
    assert "window.nekoIdleCatAudio.isEnabled()" in cat_audio_init_block

    dependency_block = source.split("const updateDependentToggleState = () => {", 1)[1].split(
        "const updateStyle = () => {",
        1,
    )[0]
    assert "toggle.dependsOnToggleId" in dependency_block
    assert "const parent = toggleItem.parentElement;" in dependency_block
    assert "parent.querySelector(`#${dependencyId}`)" in dependency_block
    assert "document.getElementById(dependencyId)" in dependency_block
    assert "checkbox.disabled = !dependencyChecked;" in dependency_block
    assert "aria-disabled" in dependency_block
    assert "toggleItem.setAttribute('tabIndex', dependencyChecked ? '0' : '-1');" in dependency_block
    assert "toggleItem.style.opacity = dependencyChecked ? '1' : '0.5';" in dependency_block
    assert "const cursor = dependencyChecked ? 'pointer' : 'default';" in dependency_block
    assert "[toggleItem, indicator, label].forEach(el => { el.style.cursor = cursor; });" in dependency_block
    assert "if (typeof toggleItem._nekoUpdateSettingsToggleStyle === 'function')" in source
    assert "toggleItem._nekoUpdateSettingsToggleStyle();" in source
    assert "const refreshDependentToggles = () => {" in source
    assert "candidate._nekoUpdateSettingsToggleStyle();" in source

    cat_audio_change_block = source.split("} else if (toggle.id === 'cat-audio')", 1)[1].split(
        "}",
        1,
    )[0]
    assert "window.nekoIdleCatAudio.setEnabled(isChecked)" in cat_audio_change_block

    en_locale = json.loads((STATIC_LOCALES_DIR / "en.json").read_text(encoding="utf-8"))
    ja_locale = json.loads((STATIC_LOCALES_DIR / "ja.json").read_text(encoding="utf-8"))
    ko_locale = json.loads((STATIC_LOCALES_DIR / "ko.json").read_text(encoding="utf-8"))
    ru_locale = json.loads((STATIC_LOCALES_DIR / "ru.json").read_text(encoding="utf-8"))
    zh_cn_locale = json.loads((STATIC_LOCALES_DIR / "zh-CN.json").read_text(encoding="utf-8"))
    zh_tw_locale = json.loads((STATIC_LOCALES_DIR / "zh-TW.json").read_text(encoding="utf-8"))

    assert en_locale["settings"]["toggles"]["catAudio"] == "Cat Audio"
    assert "Volume" not in en_locale["settings"]["toggles"]["catAudio"]
    assert "音量" not in ja_locale["settings"]["toggles"]["catAudio"]
    assert "볼륨" not in ko_locale["settings"]["toggles"]["catAudio"]
    assert "Громкость" not in ru_locale["settings"]["toggles"]["catAudio"]
    assert zh_cn_locale["settings"]["toggles"]["catAudio"] == "猫猫音效"
    assert zh_tw_locale["settings"]["toggles"]["catAudio"] == "貓貓音效"


def test_model_settings_proactive_controls_use_right_aligned_sliders():
    source = AVATAR_UI_POPUP_PATH.read_text(encoding="utf-8")
    settings_toggles_block = source.split("const settingsToggles = [", 1)[1].split("];", 1)[0]

    for toggle_id in ("proactive-chat", "proactive-vision"):
        toggle_object = re.search(
            rf"\{{[^{{}}]*id:\s*'{re.escape(toggle_id)}'[^{{}}]*\}}",
            settings_toggles_block,
        )
        assert toggle_object, f"missing settings toggle object for {toggle_id}"
        assert "controlStyle: 'slider'" in toggle_object.group(0)

    assert ".${prefix}-toggle-item.${prefix}-toggle-item-slider" in source
    slider_label_style = source.split(
        ".${prefix}-toggle-item-slider .${prefix}-toggle-label {", 1
    )[1].split("}", 1)[0]
    assert "flex: 1 1 auto;" in slider_label_style
    assert ".${prefix}-toggle-indicator.${prefix}-toggle-slider" in source
    assert "width: 36px;" in source
    assert "height: 20px;" in source
    assert ".${prefix}-toggle-slider[aria-checked=\"true\"] .${prefix}-toggle-thumb" in source
    assert "transform: translateX(16px);" in source

    settings_item_block = source.split("function createSettingsToggleItem", 1)[1].split(
        "function createMenuItem", 1
    )[0]
    slider_order_block = settings_item_block.split("toggleItem.appendChild(checkbox);", 1)[1].split(
        "toggleItem.addEventListener('mouseenter'", 1
    )[0]
    assert "if (usesSliderControl)" in slider_order_block
    assert slider_order_block.index("toggleItem.appendChild(label);") < slider_order_block.index(
        "toggleItem.appendChild(indicator);"
    )


def test_index_game_window_state_pauses_hidden_avatar_rendering():
    source = INDEX_TEMPLATE_PATH.read_text(encoding="utf-8")
    block = source.split("var pngtuberHiddenForGameWindow = false;", 1)[1].split(
        "window.addEventListener('neko-game-window-state-change'",
        1,
    )[0]

    assert "var pngtuberHiddenForGameWindow = false;" in source
    assert "function applyGameWindowRenderingState(paused) {" in block
    assert "window.live2dManager" in block
    assert "window.vrmManager" in block
    assert "window.mmdManager" in block
    assert "var manager = window.pngtuberManager;" in block
    assert "manager.hide();" in block
    assert "manager.show();" in block
    assert "var method = paused ? 'pauseRendering' : 'resumeRendering';" in block
    assert "manager[method]();" in block
    assert "applyGameWindowRenderingState(true);" in block
    assert "document.body.classList.add('neko-game-active');" in block
    assert block.index("applyGameWindowRenderingState(true);") < block.index("document.body.classList.add('neko-game-active');")
    assert "document.body.classList.remove('neko-game-active');" in block
    assert "if (!window.__nekoPetInteracting__) {" in block
    assert "applyGameWindowRenderingState(false);" in block


def css_z_index(block: str) -> int:
    match = re.search(r"\bz-index:\s*(\d+)\s*;", block)
    if not match:
        raise AssertionError(f"missing CSS z-index in block: {block[:240]!r}")
    return int(match.group(1))


def inline_z_index(block: str) -> int:
    match = re.search(r"\bzIndex:\s*['\"](\d+)['\"]", block)
    if not match:
        raise AssertionError(f"missing inline zIndex in block: {block[:240]!r}")
    return int(match.group(1))


def test_choice_prompt_sources_have_distinct_accessibility_labels():
    for path in (REACT_CHAT_APP_PATH, REACT_CHAT_FULL_SURFACE_PATH):
        source = path.read_text(encoding="utf-8")
        choice_prompt_block = source.split("data-choice-source={choicePrompt.source}", 1)[1].split(
            "{choicePrompt.options.slice",
            1,
        )[0]
        assert "choicePrompt.source === 'mini_game_invite'" in choice_prompt_block
        assert "chat.miniGameInviteOptionsAriaLabel" in choice_prompt_block
        assert "choicePrompt.source === 'new_user_icebreaker'" in choice_prompt_block
        assert "chat.newUserIcebreakerOptionsAriaLabel" in choice_prompt_block


def test_react_chat_host_can_clear_only_yui_guide_messages():
    source = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    clear_block = source.split("function clearGuideMessages()", 1)[1].split(
        "function clearMessages()",
        1,
    )[0]

    assert "function isYuiGuideChatMessage(message)" in source
    assert "message.id.indexOf('yui-guide-') === 0" in source
    assert "return !isYuiGuideChatMessage(message);" in clear_block
    assert "clearGuideMessages: clearGuideMessages" in source
    assert "state.messages = []" not in clear_block


def test_subtitle_window_dark_mode_keeps_transparent_background():
    source = STATIC_DARK_MODE_CSS_PATH.read_text(encoding="utf-8")
    selector = (
        'html[data-theme="dark"].subtitle-window-host,\n'
        'html[data-theme="dark"].subtitle-window-host body.subtitle-window-host {'
    )
    assert selector in source
    block = source.split(selector, 1)[1].split("}", 1)[0]

    assert "background: transparent !important;" in block
    assert source.index(selector) > source.index("background: #000 !important;")


def test_standalone_subtitle_page_initializes_theme_before_subtitle_scripts():
    source = SUBTITLE_TEMPLATE_PATH.read_text(encoding="utf-8")

    assert '<script src="/static/theme-manager.js"></script>' in source
    assert source.index('/static/theme-manager.js') < source.index('/static/subtitle/subtitle-shared.js')


def test_chat_surface_mode_preference_is_shared_with_electron():
    source = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    gate_block = source.split("function shouldPersistChatSurfaceModePreference()", 1)[1].split(
        "function readChatSurfaceModePreference()",
        1,
    )[0]
    read_block = source.split("function readChatSurfaceModePreference()", 1)[1].split(
        "function persistChatSurfaceModePreference(mode)",
        1,
    )[0]
    persist_block = source.split("function persistChatSurfaceModePreference(mode)", 1)[1].split(
        "function readGalgameModePreference()",
        1,
    )[0]

    assert "electron-chat-window" not in gate_block
    assert "return true;" in gate_block
    assert "localStorage.getItem(CHAT_SURFACE_MODE_STORAGE_KEY)" in read_block
    # Restorable surfaces (compact + the revived legacy full) persist; minimized
    # never does — it restores via lastRestorableChatSurfaceMode.
    assert "if (mode !== 'compact' && mode !== 'full') return;" in persist_block
    assert "localStorage.setItem(CHAT_SURFACE_MODE_STORAGE_KEY, mode)" in persist_block


def test_avatar_tool_result_names_track_the_current_participants():
    geometry_path = (
        Path(__file__).resolve().parents[2]
        / "static"
        / "app"
        / "app-react-chat-window"
        / "geometry-and-messages.js"
    )
    source = geometry_path.read_text(encoding="utf-8")

    name_block = source.split("function getConfiguredAssistantName()", 1)[1].split(
        "function getCurrentAssistantName()",
        1,
    )[0]
    build_render_block = source.split("function buildRenderProps()", 1)[1].split(
        "function showToast",
        1,
    )[0]

    assert name_block.index("window.appState && window.appState.lanlan_name") < name_block.index(
        "window.lanlan_config && window.lanlan_config.lanlan_name"
    )
    assert "userName: getConfiguredUserName() || undefined" in build_render_block
    assert "assistantName: getConfiguredAssistantName() || undefined" in build_render_block


def test_goodbye_composer_hidden_survives_surface_mode_switches():
    source = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    build_render_block = source.split("function buildRenderProps()", 1)[1].split(
        "function showToast",
        1,
    )[0]
    submit_block = source.split("function handleComposerSubmit(payload)", 1)[1].split(
        "function prepareCompactHistoryDropSubmit",
        1,
    )[0]
    set_mode_block = source.split("function setChatSurfaceMode(nextMode)", 1)[1].split(
        "function cycleChatSurfaceMode()",
        1,
    )[0]
    goodbye_set_block = source.split("function setGoodbyeComposerHidden(hidden, reason)", 1)[1].split(
        "function syncGoodbyeComposerHidden",
        1,
    )[0]
    attachments_set_block = source.split("function setComposerAttachments(attachments)", 1)[1].split(
        "var MAX_MESSAGES",
        1,
    )[0]
    desktop_min_height_block = source.split("function getDesktopMinHeight()", 1)[1].split(
        "function createResizeEdges",
        1,
    )[0]
    effective_composer_hidden_block = source.split("function getEffectiveComposerHidden()", 1)[1].split(
        "function getNekoGoodbyeModeActive",
        1,
    )[0]

    assert "goodbyeComposerHidden: false" in source
    assert "function getEffectiveComposerHidden()" in source
    assert "function hasLocalGoodbyeModeSource()" in source
    assert "Standalone chat pages inherit window.isNekoGoodbyeModeActive" in source
    assert "typeof window.isNekoGoodbyeModeActive === 'function'" in source
    assert "&& window.isNekoGoodbyeModeActive()" in source
    assert "function getEffectiveComposerAttachmentsVisible()" in source
    assert "function syncComposerAttachmentsVisibility(previousVisible)" in source
    assert "!state.homeTutorialInputLocked" in effective_composer_hidden_block
    assert "state.composerHidden || state.goodbyeComposerHidden" in effective_composer_hidden_block
    assert "isCatLocalChatActive" in effective_composer_hidden_block
    assert "composerHidden: getEffectiveComposerHidden()" in build_render_block
    assert "state.homeTutorialInteractionLocked" in submit_block
    assert "state.homeTutorialInputLocked" in submit_block
    assert "getEffectiveComposerHidden()" in submit_block
    assert "syncGoodbyeComposerHidden('chat-surface-mode-change', { localOnly: true });" in set_mode_block
    assert "requestGoodbyeComposerHiddenState('chat-surface-mode-change');" in set_mode_block
    assert "options && options.localOnly && !hasLocalGoodbyeModeSource()" in source
    assert "function requestGoodbyeComposerHiddenState(reason)" in source
    assert "window.requestGoodbyeChatComposerHiddenState(resolvedReason)" in source
    assert "neko:request-goodbye-chat-composer-hidden-state" in source
    assert "requestGoodbyeComposerHiddenState('initial-goodbye-state');" in source
    assert "syncComposerAttachmentsVisibility(previousAttachmentsVisible);" in goodbye_set_block
    assert "restoredEffectiveComposer && getEffectiveGalgameEnabled()" in goodbye_set_block
    assert "fetchGalgameOptionsForLatestTurn();" in goodbye_set_block
    assert "var previousVisible = getEffectiveComposerAttachmentsVisible();" in attachments_set_block
    assert "syncComposerAttachmentsVisibility(previousVisible);" in attachments_set_block
    assert "if (!getEffectiveGalgameEnabled()) return MIN_HEIGHT;" in desktop_min_height_block
    assert "EVENT_PREFIX + 'set-goodbye-composer-hidden'" in source
    assert "window.addEventListener('live2d-goodbye-click'" in source
    assert "setGoodbyeComposerHidden(true, 'live2d-goodbye-click')" in source
    assert "window.addEventListener('neko:cat-return-complete'" in source
    assert "source !== 'pngtuber-return-click'" in source
    assert "source !== 'live2d-return-click'" in source
    assert "setGoodbyeComposerHidden(false, 'return-complete')" in source
    assert "setGoodbyeComposerHidden(false, 'live2d-return-click')" not in source


def test_chat_full_endpoint_uses_chat_template_with_initial_full_surface():
    router_source = PAGES_ROUTER_PATH.read_text(encoding="utf-8")
    template_source = CHAT_TEMPLATE_PATH.read_text(encoding="utf-8")

    assert '@router.get("/chat_full", response_class=HTMLResponse)' in router_source
    assert '"initial_chat_surface_mode": "full"' in router_source
    assert '"initial_chat_surface_mode": "compact"' in router_source
    assert '"initial_chat_host_kind": "full"' in router_source
    assert '"initial_chat_host_kind": "compact"' in router_source
    assert 'data-chat-host-kind="{{ initial_chat_host_kind|default(\'compact\', true) }}"' in template_source
    assert 'data-initial-chat-surface-mode="{{ initial_chat_surface_mode|default(\'compact\', true) }}"' in template_source

    chat_route_block = router_source.split('async def get_chat_page', 1)[1].split(
        '@router.get("/chat_full"',
        1,
    )[0]
    assert '"initial_chat_surface_mode": "compact"' in chat_route_block
    assert '"initial_chat_host_kind": "compact"' in chat_route_block
    assert '"initial_chat_surface_mode": "full"' not in chat_route_block
    assert '"initial_chat_host_kind": "full"' not in chat_route_block


def test_full_inset_layout_gated_by_electron_runtime_marker():
    """full 30px inset 布局是「仅 Electron 独立窗口」样式。chat.html 同时服务 Electron 独立窗口
    和 web /chat_full（两者共用本模板，body.electron-chat-window 是静态写入，浏览器访问 web
    /chat_full 时也会带它），故 inset 门禁必须用 preload 注入的运行时标记 neko-electron-runtime，
    否则 web /chat_full 会被误改成 30px inset。"""
    source = CHAT_TEMPLATE_PATH.read_text(encoding="utf-8")

    # inset 规则用运行时标记门禁，不再用静态 electron-chat-window（web /chat_full 也会命中它）。
    assert (
        'body.neko-electron-runtime #react-chat-window-shell[data-chat-surface-mode="full"]'
        in source
    )
    assert (
        'body.electron-chat-window #react-chat-window-shell[data-chat-surface-mode="full"]'
        not in source
    )

    # 运行时标记由 body 内联脚本基于 preload 信号（window.nekoChatWindow）/ Electron UA 注入；
    # web 浏览器无这些信号 → 不打标记 → full 退回 base 居中。
    assert "document.body.classList.add('neko-electron-runtime')" in source
    assert "w.nekoChatWindow" in source


def test_chat_templates_version_react_chat_bundle_from_react_assets():
    chat_template = CHAT_TEMPLATE_PATH.read_text(encoding="utf-8")
    index_template = INDEX_TEMPLATE_PATH.read_text(encoding="utf-8")

    assert 'neko-chat-window.css?v={{ react_chat_asset_version }}' in chat_template
    assert 'neko-chat-window.iife.js?v={{ react_chat_asset_version }}' in chat_template
    assert 'neko-chat-window.css?v={{ react_chat_asset_version }}' in index_template
    assert 'neko-chat-window.iife.js?v={{ react_chat_asset_version }}' in index_template
    interpage_parts = (
        'bootstrap-resources-and-model-reload.js',
        'composer-voice-sync.js',
        'cross-window-broadcast-and-bridge.js',
        'guide-message-relay.js',
        'guide-overlay.js',
        'guide-targets.js',
        'listeners-and-api.js',
    )
    for part_name in interpage_parts:
        versioned_path = f'app-interpage/{part_name}?v={{{{ static_asset_version }}}}'
        assert versioned_path in chat_template
        assert versioned_path in index_template


def test_static_app_part_filenames_are_semantic_not_numbered():
    app_root = Path(__file__).resolve().parents[2] / "static" / "app"

    for directory_name in ("app-react-chat-window", "app-ui", "app-interpage"):
        part_names = [path.name for path in sorted((app_root / directory_name).glob("*.js"))]
        assert part_names, f"no JavaScript parts found under {directory_name}"
        assert all(not re.match(r"^\d", name) for name in part_names), part_names


def test_web_chat_compact_endpoint_uses_index_template_with_initial_compact_surface():
    router_source = PAGES_ROUTER_PATH.read_text(encoding="utf-8")
    index_template = INDEX_TEMPLATE_PATH.read_text(encoding="utf-8")

    assert '@router.get("/web_chat_compact", response_class=HTMLResponse)' in router_source
    assert "{% if initial_chat_surface_mode is defined and initial_chat_surface_mode %}" in index_template
    assert 'data-initial-chat-surface-mode="{{ initial_chat_surface_mode }}"' in index_template

    route_block = router_source.split("async def get_web_chat_compact_page", 1)[1].split(
        '@router.get("/subtitle"',
        1,
    )[0]
    assert 'TemplateResponse("templates/index.html"' in route_block
    assert 'TemplateResponse("templates/chat.html"' not in route_block
    assert '"initial_chat_surface_mode": "compact"' in route_block
    assert '"initial_chat_surface_mode": "full"' not in route_block

    root_route_block = router_source.split('async def get_default_index', 1)[1].split(
        'def _render_model_manager',
        1,
    )[0]
    assert '"initial_chat_surface_mode"' not in root_route_block


def test_chat_full_is_reserved_from_character_page_config_routing():
    source = STATIC_INDEX_JS_PATH.read_text(encoding="utf-8")

    assert "const RESERVED_PAGE_PATHS = new Set([" in source
    assert "'chat'" in source
    assert "'chat_full'" in source
    assert "'web_chat_compact'" in source
    assert "RESERVED_PAGE_PATHS.has(pathParts[0])" in source
    assert "isReservedPagePath(window.location.pathname)" in source


def test_chat_full_skips_startup_prominent_notice_queue():
    source = APP_JS_PATH.read_text(encoding="utf-8")
    load_block = source.split("window.addEventListener('load'", 1)[1].split(
        "// 监听 voice_id",
        1,
    )[0]

    assert "window.location.pathname === '/chat'" in load_block
    assert "window.location.pathname === '/chat/'" in load_block
    assert "window.location.pathname === '/chat_full'" in load_block
    assert "window.location.pathname === '/chat_full/'" in load_block
    assert "if (_isChatPage) return;" in load_block


def test_web_chat_compact_is_allowed_during_main_limited_mode():
    source = MAIN_SERVER_PATH.read_text(encoding="utf-8")

    allowed_page_paths_block = source.split("_MAIN_LIMITED_MODE_ALLOWED_PAGE_PATHS = {", 1)[1].split(
        "_MAIN_LIMITED_MODE_ALLOWED_PREFIXES",
        1,
    )[0]
    assert '"/web_chat_compact"' in allowed_page_paths_block


def test_chat_host_initial_surface_mode_prefers_template_override_before_storage():
    source = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    assert "function readInitialChatSurfaceMode()" in source
    initial_reader_block = source.split("function readInitialChatSurfaceMode()", 1)[1].split(
        "function persistChatSurfaceModePreference",
        1,
    )[0]
    assert "var declaredModeAttr = body" in initial_reader_block
    assert "body.getAttribute('data-initial-chat-surface-mode')" in initial_reader_block
    assert "declaredModeAttr ? normalizeChatSurfaceMode(declaredModeAttr) : ''" in initial_reader_block
    assert "normalizeChatSurfaceMode(body.getAttribute('data-initial-chat-surface-mode'))" not in initial_reader_block
    assert "if (declaredMode === 'compact' || declaredMode === 'full')" in initial_reader_block
    assert "if (declaredMode === 'full' && isCompactOnlyElectronChatHost())" in initial_reader_block
    assert "return 'compact';" in initial_reader_block
    assert "return declaredMode;" in initial_reader_block
    assert initial_reader_block.index("return declaredMode;") < initial_reader_block.index("return readChatSurfaceModePreference();")

    assert "function isCompactOnlyElectronChatHost()" in source
    assert "body.getAttribute('data-chat-host-kind') === 'compact'" in source
    assert "body.getAttribute('data-initial-chat-surface-mode') === 'compact'" not in source
    assert "function coerceChatSurfaceModeForHost(mode)" in source
    assert "normalized === 'full' && isCompactOnlyElectronChatHost()" in source

    init_block = source.split("function init()", 1)[1].split(
        "function initAfterStorageBarrier()",
        1,
    )[0]
    initial_assignment = "state.chatSurfaceMode = readInitialChatSurfaceMode();"
    storage_read = "readChatSurfaceModePreference()"

    assert initial_assignment in init_block
    assert init_block.index(initial_assignment) < init_block.index("lastRestorableChatSurfaceMode = state.chatSurfaceMode;")
    assert storage_read not in init_block.split(initial_assignment, 1)[0]


def test_close_from_minimized_preserves_compact_surface_mode():
    source = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    close_block = source.split("function closeWindow()", 1)[1].split(
        "window.addEventListener('neko:main-ui-hidden-by-model-manager-changed'",
        1,
    )[0]
    minimized_block = close_block.split("if (minimized)", 1)[1].split("overlay.hidden = true;", 1)[0]

    assert "state.chatSurfaceMode = 'full'" not in minimized_block
    assert "minimized = false;" in minimized_block
    # closeWindow clears the minimized shell without routing through
    # setChatSurfaceMode, so it must restore the logical surface to the last
    # restorable mode. Otherwise the next openWindow() rebuilds the React props
    # with chatSurfaceMode:'minimized' and renders a blank body.
    assert (
        "state.chatSurfaceMode = coerceChatSurfaceModeForHost(lastRestorableChatSurfaceMode);"
        in minimized_block
    )
    assert "syncChatSurfaceModeUI();" in minimized_block
    assert "overlay.hidden = true;" in close_block
    assert "resetCompactChatState();" in close_block


def test_open_from_minimized_restores_surface_mode_before_mounting():
    source = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    open_block = source.split("function openWindow()", 1)[1].split(
        "function closeWindow()",
        1,
    )[0]
    # The minimized restore branch must reset the logical surface BEFORE
    # mountWindow() so React rebuilds the compact body instead of the blank
    # minimized surface — symmetric with closeWindow's reset.
    restore_assignment = (
        "state.chatSurfaceMode = coerceChatSurfaceModeForHost(lastRestorableChatSurfaceMode);"
    )
    assert restore_assignment in open_block
    assert open_block.index(restore_assignment) < open_block.index("if (!mountWindow())")


def test_minimized_restore_uses_previous_real_surface_mode():
    source = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    assert "lastRestorableChatSurfaceMode = 'compact';" in source
    assert "var CHAT_SURFACE_MODE_SEQUENCE = ['compact', 'minimized'];" in source
    assert "var COMPACT_CHAT_STATES = ['default', 'options', 'input'];" in source
    assert "compactChatState: 'default'," in source
    assert "return COMPACT_CHAT_STATES.indexOf(mode) >= 0 ? mode : 'default';" in source
    assert "state.compactChatState = 'default';" in source

    next_mode_block = source.split("function getNextChatSurfaceMode(mode)", 1)[1].split(
        "function resetCompactChatState()",
        1,
    )[0]
    set_mode_block = source.split("function setChatSurfaceMode(nextMode)", 1)[1].split(
        "function cycleChatSurfaceMode()",
        1,
    )[0]
    set_view_props_block = source.split("function setViewProps(nextViewProps)", 1)[1].split(
        "function ensureBundleLoaded()",
        1,
    )[0]
    init_block = source.split("function init()", 1)[1].split(
        "function initAfterStorageBarrier()",
        1,
    )[0]

    assert "if (normalized === 'minimized')" in next_mode_block
    assert "lastRestorableChatSurfaceMode" in next_mode_block
    assert "return coerceChatSurfaceModeForHost(lastRestorableChatSurfaceMode);" in next_mode_block
    assert "lastRestorableChatSurfaceMode = normalized;" in set_mode_block
    assert "lastRestorableChatSurfaceMode = previousMode;" in set_mode_block
    assert "lastRestorableChatSurfaceMode = normalizedChatSurfaceMode;" in set_view_props_block
    assert "lastRestorableChatSurfaceMode = previousChatSurfaceMode;" in set_view_props_block
    assert "lastRestorableChatSurfaceMode = state.chatSurfaceMode;" in init_block


def test_home_tutorial_input_lock_blocks_compact_capsule_input_state():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    react_source = REACT_CHAT_APP_PATH.read_text(encoding="utf-8")

    handler_block = script.split("function handleCompactChatStateChange(nextCompactChatState)", 1)[1].split(
        "function handleMiniGameInviteChoice(option)",
        1,
    )[0]
    set_state_block = script.split("function setCompactChatState(nextCompactChatState", 1)[1].split(
        "function setChatSurfaceMode(nextMode)",
        1,
    )[0]
    capsule_block = react_source.split('className="compact-chat-capsule-button"', 1)[1].split(
        "requestCompactChatState('input');",
        1,
    )[0]

    assert "state.homeTutorialInputLocked || isHomeTutorialInteractionLocked()" in handler_block
    assert "normalized === 'input'" in set_state_block
    assert "state.homeTutorialInputLocked || isHomeTutorialInteractionLocked()" in set_state_block
    input_lock_block = script.split("function setHomeTutorialInputLocked(locked, reason)", 1)[1].split(
        "function setAvatarToolMenuOpen(open, reason)",
        1,
    )[0]
    assert "compactInputLocked: next" in input_lock_block
    assert "var previousAttachmentsVisible = getEffectiveComposerAttachmentsVisible();" in input_lock_block
    assert "syncComposerAttachmentsVisibility(previousAttachmentsVisible);" in input_lock_block
    assert "setHomeTutorialInteractionLocked(next" not in input_lock_block
    assert "disabled={compactCapsuleEntryLocked}" in capsule_block
    assert "if (compactCapsuleEntryLocked) return;" in capsule_block


def test_home_tutorial_events_lock_chat_buttons_and_collapse_compact_input():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    react_source = REACT_CHAT_APP_PATH.read_text(encoding="utf-8")

    interaction_lock_block = script.split("function setHomeTutorialInteractionLocked(locked, reason)", 1)[1].split(
        "function setHomeTutorialInputLocked(locked, reason)",
        1,
    )[0]
    started_block = script.split("window.addEventListener('neko:tutorial-started'", 1)[1].split(
        "window.addEventListener('neko:tutorial-completed'",
        1,
    )[0]
    completed_block = script.split("window.addEventListener('neko:tutorial-completed'", 1)[1].split(
        "window.addEventListener('neko:tutorial-skipped'",
        1,
    )[0]
    skipped_block = script.split("window.addEventListener('neko:tutorial-skipped'", 1)[1].split(
        "window.addEventListener('neko:tutorial-ended-without-completion'",
        1,
    )[0]
    ended_block = script.split("window.addEventListener('neko:tutorial-ended-without-completion'", 1)[1].split(
        "// Refresh option list whenever an assistant turn finishes streaming.",
        1,
    )[0]
    history_handle_block = react_source.split('className={`compact-history-visibility-handle', 1)[1].split(
        "onClick={handleCompactHistoryVisibilityClick}",
        1,
    )[0]

    assert "if (next && getCurrentCompactChatState() === 'input')" in interaction_lock_block
    assert "resetCompactChatState();" in interaction_lock_block
    assert "compactChatState: getCurrentCompactChatState()" in interaction_lock_block
    assert "setHomeTutorialInteractionLocked(true, 'tutorial-started');" in started_block
    assert "setHomeTutorialInteractionLocked(false, 'tutorial-completed');" in completed_block
    assert "setHomeTutorialInteractionLocked(false, 'tutorial-skipped');" in skipped_block
    assert "setHomeTutorialInteractionLocked(false, 'tutorial-ended-without-completion');" in ended_block
    assert "disabled={composerDisabled}" in history_handle_block


def test_home_tutorial_host_wires_avatar_tool_requests():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    interpage_source = APP_INTERPAGE_PATH.read_text(encoding="utf-8")

    assert "setHomeTutorialInputLocked: setHomeTutorialInputLocked" in script
    assert "setAvatarToolMenuOpen: setAvatarToolMenuOpen" in script
    assert "setCompactToolFanOpen: setCompactToolFanOpen" in script
    assert "rotateCompactToolWheel: rotateCompactToolWheel" in script
    assert "setCompactToolWheelIndex: setCompactToolWheelIndex" in script
    build_render_block = script.split("function buildRenderProps()", 1)[1].split(
        "function showToast",
        1,
    )[0]
    assert "avatarToolMenuOpenRequest: state.viewProps.avatarToolMenuOpenRequest || null" in build_render_block
    assert "compactToolFanOpenRequest: state.viewProps.compactToolFanOpenRequest || null" in build_render_block
    assert "compactToolWheelRotateRequest" in script
    assert "compactToolWheelIndexRequest" in script

    assert "case 'yui_guide_set_chat_input_locked':" in interpage_source
    assert "case 'yui_guide_set_avatar_tool_menu_open':" in interpage_source
    assert "case 'yui_guide_set_compact_tool_fan_open':" in interpage_source
    assert "case 'yui_guide_rotate_compact_tool_wheel':" in interpage_source
    assert "case 'yui_guide_set_compact_tool_wheel_index':" in interpage_source
    assert "host.setHomeTutorialInputLocked(locked === true" in interpage_source
    assert "host.setAvatarToolMenuOpen(open === true" in interpage_source
    assert "host.rotateCompactToolWheel(payload && payload.direction" in interpage_source


def test_icebreaker_home_template_loads_delivered_daily_guide_scripts():
    source = INDEX_TEMPLATE_PATH.read_text(encoding="utf-8")

    for guide_script in [
        "tutorial/yui-guide/days/day1-home-guide.js",
        "tutorial/yui-guide/days/day2-screen-voice-guide.js",
        "tutorial/yui-guide/days/day3-interaction-guide.js",
        "tutorial/yui-guide/days/day4-companion-guide.js",
        "tutorial/yui-guide/days/day5-personalization-guide.js",
        "tutorial/yui-guide/days/day6-agent-guide.js",
        "tutorial/yui-guide/days/day7-graduation-guide.js",
    ]:
        assert f'<script src="/static/{guide_script}' in source
        assert (Path(__file__).resolve().parents[2] / "static" / guide_script).exists()

    assert '<script src="/static/tutorial/icebreaker/new-user-icebreaker.js' in source


def test_idle_cat1_compact_mirror_ignores_pet_window_local_events():
    source = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    assert "function shouldIgnoreIdleCat1CompactMirrorState(detail)" in source
    ignore_block = source.split("function shouldIgnoreIdleCat1CompactMirrorState(detail)", 1)[1].split(
        "function handleIdleCat1CompactMirrorState(event)",
        1,
    )[0]
    assert "window.__LANLAN_IS_ELECTRON_PET__" in ignore_block
    assert "detail.via === 'local'" in ignore_block
    assert "detail.source === 'pet-window'" in ignore_block

    handler_block = source.split("function handleIdleCat1CompactMirrorState(event)", 1)[1].split(
        "function dispatchCompactSurfaceLayoutChange(rect)",
        1,
    )[0]
    ignore_call = "if (shouldIgnoreIdleCat1CompactMirrorState(detail)) return;"
    inactive_branch = "if (!detail || !detail.active)"
    assert ignore_call in handler_block
    assert handler_block.index(ignore_call) < handler_block.index(inactive_branch)


def test_desktop_compact_history_uses_workarea_not_browserwindow_viewport():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")

    assert "normalizeCompactDesktopWorkArea" in script
    assert "--compact-desktop-workarea-width" in script
    assert "--compact-desktop-workarea-height" in script

    desktop_history_block = styles.split(
        "body.electron-chat-window.subtitle-web-host .compact-export-history-anchor",
        1,
    )[1].split(".compact-export-history-panel", 1)[0]

    assert "--compact-desktop-workarea-width" in desktop_history_block
    assert "--compact-desktop-workarea-height" in desktop_history_block
    assert "vh" not in desktop_history_block
    assert "vw" not in desktop_history_block


def test_compact_history_size_tokens_are_ratio_based_for_ui_optimization():
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")

    anchor_block = css_block(
        styles,
        ".compact-export-history-anchor {",
        "body.electron-chat-window.subtitle-web-host .compact-export-history-anchor",
    )
    desktop_history_block = styles.split(
        "body.electron-chat-window.subtitle-web-host .compact-export-history-anchor",
        1,
    )[1].split(".compact-export-history-panel", 1)[0]
    bubble_block = css_block(styles, ".compact-export-history-bubble {", ".compact-export-history-message.is-disabled")
    system_bubble_block = css_block(
        styles,
        ".compact-export-history-message.is-system .compact-export-history-bubble {",
        ".compact-export-history-message.is-selected",
    )
    preview_bubble_block = css_block(
        styles,
        ".compact-export-preview-bubble {",
        ".compact-export-preview-message.is-user",
    )
    preview_system_bubble_block = css_block(
        styles,
        ".compact-export-preview-message.is-system .compact-export-preview-bubble {",
        ".compact-export-preview-meta",
    )
    link_anchor_block = css_block(
        styles,
        ".compact-export-history-anchor:has(.compact-export-history-content > .message-block-link) {",
        ".compact-export-history-bubble:has(> .compact-export-history-content > .message-block-link)",
    )
    active_inline_size_marker = "--compact-export-history-active-inline-size:"
    assert active_inline_size_marker in link_anchor_block
    active_inline_size = link_anchor_block.split(active_inline_size_marker, 1)[1].split(";", 1)[0]

    assert "--compact-export-history-width-ratio:" in anchor_block
    assert "--compact-export-surface-width: var(--compact-surface-resize-width, var(--desktop-compact-surface-width, var(--compact-surface-width, 430px)));" in anchor_block
    assert "--compact-export-history-inline-size: min(" in anchor_block
    assert "calc(var(--compact-export-surface-width) * var(--compact-export-history-width-ratio))" in anchor_block
    assert "--compact-export-history-active-inline-size: var(--compact-export-history-inline-size);" in anchor_block
    assert "width: var(--compact-export-history-active-inline-size);" in anchor_block
    assert "min(" in active_inline_size
    assert (
        "max(var(--compact-export-history-inline-size), var(--compact-export-link-history-min-inline-size))"
        in active_inline_size
    )
    assert "var(--compact-export-history-max-inline-size)" in active_inline_size
    assert "--compact-export-history-max-inline-size: calc(100vw - var(--compact-export-history-viewport-gutter));" in anchor_block
    assert "--compact-export-preview-min-height: 360px;" in anchor_block
    assert "--compact-export-preview-max-height: 78vh;" in anchor_block
    assert "--compact-export-preview-region-height: min(" in anchor_block
    assert "max(var(--compact-export-history-region-height), var(--compact-export-preview-min-height))" in anchor_block
    assert "var(--compact-export-preview-max-height)" in anchor_block
    assert "max-height: var(--compact-export-preview-max-height);" in anchor_block
    assert "\n  max-height: 78vh;" not in anchor_block
    assert "--compact-export-history-max-inline-size: calc(" in desktop_history_block
    assert "var(--compact-desktop-workarea-width, 1440px) - var(--compact-export-history-viewport-gutter)" in desktop_history_block
    assert "--compact-export-preview-max-height: min(" in desktop_history_block
    assert "calc(var(--compact-export-surface-width) * 1.46)" in desktop_history_block
    assert "calc(var(--compact-desktop-workarea-height, 900px) * 0.78)" in desktop_history_block
    assert "max-height: var(--compact-export-preview-max-height);" in desktop_history_block
    assert "max-width: var(--compact-history-bubble-max-ratio, var(--compact-export-history-bubble-max-ratio));" in bubble_block
    assert "max-width: var(--compact-history-bubble-max-ratio, var(--compact-export-history-system-bubble-max-ratio));" in system_bubble_block
    assert "max-width: var(--compact-export-preview-bubble-max-ratio);" in preview_bubble_block
    assert "max-width: var(--compact-export-preview-system-bubble-max-ratio);" in preview_system_bubble_block


def test_compact_surface_resize_handles_keep_width_in_dom_geometry_contract():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")
    app_source = REACT_CHAT_APP_PATH.read_text(encoding="utf-8")

    metrics_block = script.split("function getCompactSurfaceMetrics()", 1)[1].split(
        "function clampCompactSurfacePosition(left, top, metrics)",
        1,
    )[0]
    include_block = script.split("function shouldIncludeCompactGeometryElement(element)", 1)[1].split(
        "function getCompactGeometryElementRect(element)",
        1,
    )[0]
    resize_shell_block = css_block(styles, ".compact-chat-surface-shell {", ".compact-chat-surface-frame")
    frame_block = css_block(styles, ".compact-chat-surface-frame {", ".compact-chat-surface-frame[data-compact-chat-state")
    handle_block = css_block(styles, ".compact-chat-resize-handle {", ".compact-chat-resize-handle-left")
    resize_clamp_block = script.split("function clampCompactSurfaceResizeWidthForSide", 1)[1].split(
        "function applyCompactSurfaceResizeRequest",
        1,
    )[0]

    assert "var measuredWidth = rect && rect.width > 0 ? rect.width : 0;" in metrics_block
    assert "var storedWidth = loadCompactSurfaceStoredWidth();" in metrics_block
    assert "getCompactSurfaceResizeMaxWidth()" in metrics_block
    assert "item !== 'resizeHandle'" in include_block
    assert "function applyCompactSurfaceResizeRequest(detail)" in script
    assert "function isDesktopHomeCompactSurfaceRoute()" in script
    assert "if (!isHomeCompactSurfaceRoute() && !isDesktopHomeCompactSurfaceRoute()) return;" in script
    assert "compactSurfaceDesktopResizeActive = false;" in script
    assert "if (isElectronChatWindow() && detail && detail.screenRect)" in script
    assert "compactSurfaceDesktopResizeActive = phase !== 'end';" in script
    assert "if (compactSurfaceDesktopResizeActive && isElectronChatWindow())" in script
    assert "function handleDesktopCompactLayoutChange(layout)" in script
    assert "if (baseAnchorChanged && !compactSurfaceDesktopResizeActive)" in script
    assert "compactSurfaceResizeSession = null;" in script
    assert "surfaceScreenRect: surfaceScreenRect" in script
    assert "function getCompactSurfaceDesktopWindowX()" in script
    assert "function getCompactSurfaceDesktopScreenRect()" in script
    assert "function getCompactDesktopWorkAreaEdge(workArea, edge)" in script
    assert "if (edge === 'right' && Number.isFinite(x) && Number.isFinite(width)) return x + width;" in script
    assert ": currentRect.left + windowX" in script
    assert ": currentRect.left + currentRect.width + windowX" in script
    assert "var desktopSurfaceRect = getCompactSurfaceDesktopScreenRect();" in script
    assert "anchorTopScreen: desktopSurfaceRect" in script
    assert "var workAreaLeft = getCompactDesktopWorkAreaEdge(workArea, 'left');" in script
    assert "var workAreaRight = getCompactDesktopWorkAreaEdge(workArea, 'right');" in script
    assert "workArea.left + COMPACT_SURFACE_VIEWPORT_PAD_X" not in resize_clamp_block
    assert "workArea.right - COMPACT_SURFACE_VIEWPORT_PAD_X" not in resize_clamp_block
    assert "if (!Number.isFinite(sideMax) || sideMax <= 0)" in resize_clamp_block
    assert "? compactSurfaceResizeSession.anchorRightScreen - windowX - width" in script
    assert ": compactSurfaceResizeSession.anchorLeftScreen - windowX;" in script
    assert "persist: phase === 'end'" in script
    assert "phase === 'end' && isElectronChatWindow()" in script
    assert "compactSurfaceResizeSession = null;" in script
    assert "neko:compact-surface-resize-request" in script
    assert "neko:compact-surface-layout-change" in script
    assert "neko:compact-surface-resize-width-change" in script
    assert "function isDesktopCompactSurfaceLayoutActive()" in app_source
    assert "document.documentElement.style.removeProperty('--compact-surface-resize-width');" in app_source
    assert "&& !isDesktopCompactSurfaceLayoutActive()" in app_source
    assert "const getClampedCompactSurfaceResizeWidthForSide = useCallback" in app_source
    assert "resizeState.anchorRightScreen - areaLeft" in app_source
    assert "areaRight - resizeState.anchorLeftScreen" in app_source
    assert "if (!isDesktopCompactSurfaceLayoutActive()) {\n      setCompactSurfaceResizeWidth(startWidth);" in app_source
    assert "if (!isDesktopCompactSurfaceLayoutActive()) {\n      setCompactSurfaceResizeWidth(nextWidth);" in app_source
    assert "if (isDesktopCompactSurfaceLayoutActive()) {\n        setCompactSurfaceResizeWidth(null);" in app_source
    assert "applyCompactSurfaceResizeWidthVar(null);\n        return;\n      }\n      const resizeState = compactSurfaceResizeStateRef.current;" in app_source

    assert "--compact-surface-active-width: var(--compact-surface-resize-width, var(--compact-surface-width, 430px));" in resize_shell_block
    assert "width: var(--compact-surface-active-width);" in resize_shell_block
    assert "width: 100%;" in frame_block
    assert "width: 12px;" in handle_block
    assert "cursor: ew-resize;" in handle_block
    assert "pointer-events: auto;" in handle_block
    assert "touch-action: none;" in handle_block
    assert "z-index: 100004;" in handle_block
    assert ".compact-chat-resize-handle::before" not in styles
    assert "left: -4px;" in styles
    assert "right: -4px;" in styles


def test_mobile_web_compact_surface_respects_width_bounds_and_position_vars():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    styles = STATIC_INDEX_CSS_PATH.read_text(encoding="utf-8")
    app_source = REACT_CHAT_APP_PATH.read_text(encoding="utf-8")

    mobile_compact_block = css_block(
        styles,
        'body:not(.electron-chat-window):where(:not(.lanlan-pet-mode)) #react-chat-window-shell[data-chat-surface-mode="compact"]:not(.is-minimized):not(.is-collapsing):not(.is-expanding) {',
        'body:not(.electron-chat-window):where(:not(.lanlan-pet-mode)) #react-chat-window-shell #react-chat-window-root',
    )
    mobile_compact_overflow_block = css_block(
        styles,
        'body:not(.electron-chat-window):where(:not(.lanlan-pet-mode)) #react-chat-window-shell[data-chat-surface-mode="compact"]:not(.is-minimized):not(.is-collapsing):not(.is-expanding) #react-chat-window-root,',
        'body:not(.electron-chat-window):where(:not(.lanlan-pet-mode)) #react-chat-window-shell .app-shell',
    )
    metrics_block = script.split("function getCompactSurfaceMetrics()", 1)[1].split(
        "function clampCompactSurfacePosition(left, top, metrics)",
        1,
    )[0]
    stored_width_block = script.split("function loadCompactSurfaceStoredWidth()", 1)[1].split(
        "function saveCompactSurfacePosition",
        1,
    )[0]
    resize_clamp_block = script.split("function clampCompactSurfaceResizeWidthForSide", 1)[1].split(
        "function applyCompactSurfaceResizeRequest",
        1,
    )[0]
    mobile_width_bounds_block = script.split("function getCompactSurfaceMobileWidthBounds()", 1)[1].split(
        "function getCompactSurfaceResizeMaxWidth()",
        1,
    )[0]

    assert "COMPACT_SURFACE_RESIZE_MOBILE_MIN_WIDTH = 180" in app_source
    assert "COMPACT_SURFACE_RESIZE_MOBILE_VIEWPORT_GUTTER = 16" in app_source
    assert "getCompactSurfaceResizeMinAvailableWidth" in app_source
    assert "getCompactSurfaceResizeViewportGutter" in app_source
    assert "maxAvailableWidth - viewportGutter" in app_source
    assert "window.innerWidth <= 768" in app_source
    assert "lanlan-pet-mode" in app_source
    assert "__LANLAN_IS_ELECTRON_PET__" in app_source
    assert "COMPACT_SURFACE_MOBILE_MIN_WIDTH = 180" in script
    assert "COMPACT_SURFACE_MOBILE_VIEWPORT_GUTTER" in script
    assert "COMPACT_SURFACE_RESIZE_MAX_WIDTH" in mobile_width_bounds_block
    assert "viewportWidth - COMPACT_SURFACE_MOBILE_VIEWPORT_GUTTER" in mobile_width_bounds_block
    assert "isMobileWidth() && storedWidth" in metrics_block
    assert "getCompactSurfaceMobileWidthBounds().minWidth" in stored_width_block
    assert "getCompactSurfaceMobileWidthBounds().minWidth" in resize_clamp_block
    assert "width: var(--compact-surface-width, calc(100vw - 16px)) !important;" in mobile_compact_block
    assert "left: var(--compact-surface-left, 8px) !important;" in mobile_compact_block
    assert "right: auto;" in mobile_compact_block
    assert ".chat-window.chat-surface-mode-compact" in mobile_compact_overflow_block
    assert "overflow: visible;" in mobile_compact_overflow_block


def test_yui_guide_fixed_compact_chat_uses_400_width_and_left_middle_lower_position():
    styles = STATIC_INDEX_CSS_PATH.read_text(encoding="utf-8")

    fixed_block = css_block(
        styles,
        'body.yui-guide-compact-chat-fixed.subtitle-web-host:not(.neko-electron-runtime) #react-chat-window-shell[data-chat-surface-mode="compact"]:not(.is-minimized):not(.is-collapsing):not(.is-expanding) {',
        'body.yui-guide-compact-chat-fixed.subtitle-web-host:not(.neko-electron-runtime) > .compact-chat-choice-anchor[data-chat-surface-mode="compact"] {',
    )
    choice_anchor_block = css_block(
        styles,
        'body.yui-guide-compact-chat-fixed.subtitle-web-host:not(.neko-electron-runtime) > .compact-chat-choice-anchor[data-chat-surface-mode="compact"] {',
        '/* Agent HUD 空状态折叠按钮样式 */',
    )

    assert "body.yui-guide-compact-chat-fixed.subtitle-web-host:not(.electron-chat-window)" not in styles
    assert "left: clamp(24px, 6vw, 72px) !important;" in fixed_block
    assert "top: min(62vh, calc(100vh - var(--compact-surface-height, 58px) - 24px)) !important;" in fixed_block
    assert "width: min(400px, calc(100vw - 32px)) !important;" in fixed_block
    assert "bottom: auto !important;" in fixed_block
    assert "left: calc(clamp(24px, 6vw, 72px) + 200px);" in choice_anchor_block
    assert "top: calc(min(62vh, calc(100vh - var(--compact-surface-height, 58px) - 24px)) + var(--compact-surface-height, 58px) + 16px);" in choice_anchor_block
    assert "bottom: calc(100vh - min(62vh, calc(100vh - var(--compact-surface-height, 58px) - 24px)) + 16px);" in choice_anchor_block


def test_compact_tool_fan_uses_shell_local_anchor_not_fixed_viewport_position():
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    app_source = REACT_CHAT_APP_PATH.read_text(encoding="utf-8")

    fan_block = css_block(styles, ".compact-input-tool-fan {", ".compact-chat-surface-shell *")
    compact_input_block = css_block(
        styles,
        '.compact-chat-surface-frame[data-compact-chat-state="input"] .composer-input {',
        '.compact-chat-surface-frame[data-compact-chat-state="input"] .composer-input::placeholder',
    )
    compact_mobile_block = css_block(
        styles,
        "@media (max-width: 820px) {",
        "/* ===================================================================",
    )
    wheel_block = styles.split(".compact-input-tool-fan .compact-input-tool-item {", 1)[1].split(
        ".compact-input-tool-fan .composer-tool-btn img",
        1,
    )[0]
    collector_block = script.split("function collectCompactToolFanGeometryItems(element)", 1)[1].split(
        "function collectCompactCompositeGeometryItems(element, kind)",
        1,
    )[0]
    native_hit_block = collector_block.split("nativeRects.forEach", 1)[1].split("return items.concat", 1)[0]
    geometry_sync_block = app_source.split(
        "window.dispatchEvent(new CustomEvent('neko:compact-interaction-geometry-change'));",
        1,
    )[1].split(
        "const openCompactInputToolFan",
        1,
    )[0]

    assert "position: absolute;" in fan_block
    assert "--compact-tool-wheel-hover-radius: 116px;" in fan_block
    assert "--compact-tool-wheel-music-control-cutout-width: 160px;" in fan_block
    assert "--compact-tool-wheel-music-control-cutout-height: 96px;" in fan_block
    assert "--compact-tool-wheel-orbit-radius: 80px;" in fan_block
    assert "--compact-tool-fan-focus-x: var(--compact-tool-wheel-hover-radius);" in fan_block
    assert "--compact-tool-fan-focus-y: var(--compact-tool-wheel-hover-radius);" in fan_block
    assert "--compact-tool-wheel-center-x: var(--compact-tool-wheel-hover-radius);" in fan_block
    assert "--compact-tool-wheel-center-y: var(--compact-tool-wheel-hover-radius);" in fan_block
    assert "--compact-tool-wheel-transform-duration: 0.22s;" in fan_block
    assert "--compact-tool-wheel-transform-easing: cubic-bezier(0.2, 0.9, 0.22, 1.12);" in fan_block
    assert "--compact-tool-wheel-charge-first-angle: 0deg;" in fan_block
    assert "--compact-tool-wheel-charge-second-angle: 0deg;" in fan_block
    assert "--compact-tool-toggle-center-x: calc(100% - 31px);" in fan_block
    assert "--compact-tool-toggle-center-y: 31px;" in fan_block
    assert "left: calc(var(--compact-tool-toggle-center-x) - var(--compact-tool-fan-focus-x));" in fan_block
    assert "top: calc(var(--compact-tool-toggle-center-y) - var(--compact-tool-fan-focus-y));" in fan_block
    assert "width: calc(var(--compact-tool-wheel-hover-radius) * 2);" in fan_block
    assert "height: calc(var(--compact-tool-wheel-hover-radius) * 2);" in fan_block
    assert "--compact-tool-wheel-orbit-radius: 80px;" in fan_block
    assert "touch-action: none;" in fan_block
    assert "position: fixed;" not in fan_block
    assert "--compact-input-tool-fan-origin-left" not in fan_block
    assert ".compact-input-tool-fan-hit-region" in styles
    assert ".compact-input-tool-wheel-charge" in styles
    assert "width: calc(var(--compact-tool-wheel-hover-radius) * 2);" in styles
    assert '.compact-input-tool-fan[data-compact-input-tool-fan-open="true"] .compact-input-tool-fan-hit-region' in styles
    assert '.app-shell:has(> #music-player-mount.compact-music-player-mount > .music-player-bar:not([hidden]))' in styles
    assert "var(--compact-tool-wheel-music-control-cutout-width)" in styles
    assert "var(--compact-tool-wheel-music-control-cutout-height)" in styles
    assert "function getCompactToolFanMusicControlCutoutRect(element, parentRect)" in script
    assert "#music-player-mount.compact-music-player-mount > .music-player-bar:not([hidden])" in script
    assert "function subtractCompactRect(rect, cutoutRect)" in script
    assert "COMPACT_TOOL_FAN_MUSIC_CONTROL_CUTOUT_WIDTH = 160;" in script
    assert "COMPACT_TOOL_FAN_MUSIC_CONTROL_CUTOUT_HEIGHT = 96;" in script
    assert "--compact-tool-wheel-music-control-cutout-width" in script
    assert "--compact-tool-wheel-music-control-cutout-height" in script
    assert "nativeRects = nativeRects.reduce" in collector_block
    assert "subtractCompactRect(nativeRect, musicControlCutoutRect)" in collector_block
    assert '.compact-input-tool-fan[data-compact-tool-wheel-charge-active="true"] .compact-input-tool-wheel-charge' in styles
    assert "conic-gradient(" in styles
    assert "--compact-tool-wheel-charge-first-angle" in styles
    assert "--compact-tool-wheel-charge-second-angle" in styles
    assert ".compact-input-tool-wheel-charge::after" in styles
    assert '.compact-input-tool-fan[data-compact-tool-wheel-charge-direction="backward"] .compact-input-tool-wheel-charge' in styles
    assert "calc(360deg - var(--compact-tool-wheel-charge-first-angle))" in styles
    assert "calc(360deg - var(--compact-tool-wheel-charge-second-angle))" in styles
    assert '.compact-input-tool-fan[data-compact-input-tool-fan-open="false"]' in styles
    assert "visibility: hidden;" in styles
    assert "pointer-events: none !important;" in styles
    assert '.compact-input-tool-fan[data-compact-input-tool-fan-open="true"]' in styles
    assert "visibility: visible;" in styles
    assert (
        '.compact-chat-surface-frame[data-compact-tool-toggle-visible="true"] '
        '.compact-input-tool-toggle:hover'
    ) in styles
    assert "--compact-chat-minimize-ball-size: 46px;" in styles
    assert "--compact-chat-minimize-ball-inset: 2px;" in styles
    assert "--compact-chat-minimize-ball-gap: 8px;" in styles
    assert "--compact-chat-minimize-ball-slot: calc(" in styles
    fixed_ball_block = css_block(
        styles,
        ".compact-chat-surface-frame .compact-chat-minimize-ball {",
        ".compact-chat-capsule-button",
    )
    assert "position: absolute;" in fixed_ball_block
    assert "left: var(--compact-chat-minimize-ball-inset);" in fixed_ball_block
    assert "top: 50%;" in fixed_ball_block
    assert "transform: translateY(-50%);" in fixed_ball_block
    assert "width: auto;" in compact_input_block
    assert "min-width: 0;" in compact_input_block
    assert "padding: 10px 8px 10px 0;" in compact_input_block
    assert 'padding: 5px 62px 5px var(--compact-chat-minimize-ball-slot);' in styles
    assert '.compact-chat-surface-frame[data-compact-chat-state="input"]' in compact_mobile_block
    assert 'padding: 5px 62px 5px var(--compact-chat-minimize-ball-slot);' in compact_mobile_block
    assert 'padding: 5px 8px 5px 18px;' not in compact_mobile_block
    assert '.compact-chat-surface-frame[data-compact-tool-toggle-visible="true"]:not([data-compact-chat-state="input"])' in styles
    assert 'padding-right: 62px;' in styles
    assert 'right: 9px;' in styles
    assert "transform: none;" in styles
    assert '.compact-input-tool-item[data-compact-tool-wheel-slot="hidden"]' in styles
    assert '.compact-input-tool-item[data-compact-tool-wheel-slot="hidden-forward"]' in styles
    assert '.compact-input-tool-item[data-compact-tool-wheel-slot="hidden-backward"]' in styles
    assert "rotate(calc(107.35deg + var(--compact-tool-wheel-drag-angle))) translateX(var(--compact-tool-wheel-orbit-radius)) rotate(calc(-107.35deg + var(--compact-tool-wheel-drag-counter-angle)))" in styles
    assert "rotate(calc(-17.35deg + var(--compact-tool-wheel-drag-angle))) translateX(var(--compact-tool-wheel-orbit-radius)) rotate(calc(17.35deg + var(--compact-tool-wheel-drag-counter-angle)))" in styles
    assert "rotate(-48.51deg) translateX(var(--compact-tool-wheel-orbit-radius)) rotate(48.51deg)" in styles
    assert "rotate(138.51deg) translateX(var(--compact-tool-wheel-orbit-radius)) rotate(-138.51deg)" in styles
    assert "translateX(83.82px)" not in wheel_block
    assert "translateX(89.74px)" not in wheel_block
    assert "translateX(92.06px)" not in wheel_block
    assert "scale(0.56)" in wheel_block
    assert "scale(0.86)" in wheel_block
    assert "scale(0.98)" in wheel_block
    assert "scale(1.04)" in wheel_block
    assert "bubbleFloat 3.2s ease-in-out infinite" in styles
    assert '.compact-input-tool-fan[data-compact-tool-wheel-fast-animation="true"]' in styles
    assert "--compact-tool-wheel-transform-duration: 0.07s;" in styles
    assert "pointer-events: none;" in styles
    assert "activeAvatarToolId" in geometry_sync_block
    assert "toolMenuOpen" in geometry_sync_block
    assert "var COMPACT_TOOL_AVATAR_CHOICE_FLOAT_PADDING_X = 6;" in script
    assert "var COMPACT_TOOL_AVATAR_CHOICE_FLOAT_PADDING_Y = 12;" in script
    assert "function buildCompactAvatarToolChoiceHitRect(rect)" in script
    assert ".composer-icon-popover .composer-icon-button" in collector_block
    assert "toolFan:avatarToolChoice:" in collector_block
    assert "var hitRect = isAvatarToolChoice ? buildCompactAvatarToolChoiceHitRect(rect) : rect;" in collector_block
    assert "nativeRect: hitRect" in collector_block
    assert "slot.indexOf('hidden') === 0" in collector_block
    assert "style.pointerEvents !== 'none'" not in collector_block
    assert "var tooltip = child.querySelector && child.querySelector('.compact-input-tool-tooltip');" in collector_block
    assert "id: itemId + ':tooltip'" in collector_block
    assert "visualRect: tooltipRect" in collector_block
    assert "hitRect: null" in collector_block
    assert "nativeRect: tooltipRect" in collector_block
    assert "interactive: false" in collector_block
    assert "hitRect: nativeRect" in native_hit_block
    assert "interactive: true" in native_hit_block
    assert "hitRect: null" not in native_hit_block
    assert "var COMPACT_TOOL_FAN_CIRCLE_SLICE_COUNT = 18;" in script
    assert "function buildCompactToolFanCircleSliceRects(rect, element)" in script
    assert "readCompactToolFanPixelVar(style, '--compact-tool-wheel-center-x', 116)" in script
    assert "readCompactToolFanPixelVar(style, '--compact-tool-wheel-hover-radius', 116)" in script
    assert "Math.sqrt(Math.max(0" in script
    assert "id: index === 0 ? 'toolFan:native' : 'toolFan:native:' + index" in script


def test_compact_tool_fan_tooltips_stay_noninteractive_and_anchored():
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")

    tooltip_block = css_block(
        styles,
        ".compact-input-tool-fan .compact-input-tool-tooltip {",
        ".compact-input-tool-fan button.compact-input-tool-item:not(:disabled):focus-within,",
    )
    visible_block = css_block(
        styles,
        '.compact-input-tool-fan[data-compact-input-tool-fan-open="true"][data-compact-input-tool-fan-interactive="true"] button.compact-input-tool-item:not(:disabled)[data-compact-tool-pointer-hovered="true"] > .compact-input-tool-tooltip,\n'
        '.compact-input-tool-fan[data-compact-input-tool-fan-open="true"][data-compact-input-tool-fan-interactive="true"] button.compact-input-tool-item:not(:disabled):focus-visible > .compact-input-tool-tooltip,\n'
        '.compact-input-tool-fan[data-compact-input-tool-fan-open="true"][data-compact-input-tool-fan-interactive="true"] .compact-input-tool-item-avatar[data-compact-tool-pointer-hovered="true"]:has(> .composer-emoji-btn:not(:disabled)) > .compact-input-tool-tooltip,\n'
        '.compact-input-tool-fan[data-compact-input-tool-fan-open="true"][data-compact-input-tool-fan-interactive="true"] .compact-input-tool-item-avatar:has(> .composer-emoji-btn:not(:disabled):focus-visible) > .compact-input-tool-tooltip {',
        ".compact-input-tool-fan .compact-input-tool-item > img,",
    )
    dark_tooltip_block = css_block(
        styles,
        '[data-theme="dark"] .compact-input-tool-fan .compact-input-tool-tooltip {',
        # 终止标记必须是紧邻的下一条规则：原来那个 .avatar-tool-quickbar 隔了
        # 很远，切片会把 .neko-chat-tooltip::after 一起吞进来，对它做的断言其实
        # 在断别的规则。
        '[data-theme="dark"] .neko-chat-tooltip::after {',
    )

    # 只钉行为性质，不钉观感取值：#2447 把这个 tooltip 从「白底方角平面标签」
    # 重设计成了带圆角/渐变/backdrop-filter 的玻璃质感，原来那几条
    # border-radius: 0 / background: #ffffff / box-shadow: none / 暗色具体色值
    # 全是被有意改掉的外观，留着只会在每次视觉迭代时假报警。
    assert "pointer-events: none;" in tooltip_block, "tooltip 不能吃掉指针事件"
    assert "user-select: none;" in tooltip_block
    assert "left: calc(100% + 5px);" in tooltip_block, "tooltip 锚点定在按钮右侧"
    assert "top: calc(100% - 6px);" in tooltip_block
    assert "transform-origin: 0 0;" in tooltip_block
    # 显示态必须落在静止几何上（不残留位移/缩放），否则标签会停在偏移位置。
    assert "transform: translate(0, 0);" in visible_block
    assert "scale(" not in visible_block
    # 暗色必须自带一套，不能继承亮色的底与字色（不钉具体色值）。
    assert "background:" in dark_tooltip_block
    assert "color:" in dark_tooltip_block


def test_compact_tool_wheel_rotate_request_is_present_in_host_and_built_bundle():
    host_source = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    app_source = REACT_CHAT_APP_PATH.read_text(encoding="utf-8")

    assert "rotateCompactToolWheel: rotateCompactToolWheel" in host_source
    assert "compactToolWheelRotateRequest = null" in app_source
    assert "rotateCompactInputToolWheelSteps(request.direction, request.stepCount" in app_source

    # 产物断言放在源码断言之后，并且产物缺席时跳过而不是红：
    # static/react/neko-chat/ 是 .gitignore 的 vite 输出，只有本地构建过或
    # 打包流水线里才有。它还会「假绿」——一份陈旧产物同样能匹配到这个符号。
    # 真正每次重建并校验产物的是 build-desktop 流水线，那才是该盯它的地方。
    if not REACT_CHAT_IIFE_PATH.is_file():
        pytest.skip("react chat bundle not built (static/react/ is a gitignored vite output)")
    bundle_source = REACT_CHAT_IIFE_PATH.read_text(encoding="utf-8")
    assert "compactToolWheelRotateRequest:" in bundle_source


def test_compact_history_open_request_drives_export_panel():
    host_source = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    app_source = REACT_CHAT_APP_PATH.read_text(encoding="utf-8")
    schema_source = REACT_CHAT_MESSAGE_SCHEMA_PATH.read_text(encoding="utf-8")

    assert "compactHistoryOpenRequest: state.viewProps.compactHistoryOpenRequest || null" in host_source
    assert "setTutorialChatRequest('compactHistoryOpenRequest'" in host_source
    assert "compactChatState: open === true ? 'history' : 'idle'" not in host_source
    assert "setCompactHistoryOpen: setCompactHistoryOpen" in host_source

    assert "compactHistoryOpenRequest = null" in app_source
    assert "const lastCompactHistoryOpenRequestIdRef = useRef('');" in app_source
    assert "const request = compactHistoryOpenRequest;" in app_source
    assert "lastCompactHistoryOpenRequestIdRef.current = request.id;" in app_source
    assert "openCompactExportHistory();" in app_source
    assert "closeCompactExportHistory();" in app_source
    assert app_source.count("const lastCompactHistoryOpenRequestIdRef = useRef('');") == 1
    assert app_source.count("const request = compactHistoryOpenRequest;") == 1

    assert "const compactHistoryOpenRequestSchema = z.object({" in schema_source
    assert "compactHistoryOpenRequest: compactHistoryOpenRequestSchema.optional()" in schema_source


def test_compact_choice_hit_contract_uses_real_options_only():
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")
    host_styles = STATIC_INDEX_CSS_PATH.read_text(encoding="utf-8")
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    choice_selector = 'body > .compact-chat-choice-anchor[data-chat-surface-mode="compact"] {'
    slot_selector = (
        'body > .compact-chat-choice-anchor[data-chat-surface-mode="compact"] .composer-galgame-slot,\n'
        'body > .compact-chat-choice-anchor[data-chat-surface-mode="compact"] .composer-galgame-options {'
    )
    option_selector = (
        'body > .compact-chat-choice-anchor[data-chat-surface-mode="compact"] .composer-galgame-option {'
    )
    choice_block = css_block(styles, choice_selector, 'body > .compact-chat-choice-anchor[data-chat-surface-mode="compact"]::-webkit-scrollbar')
    host_choice_block = css_block(host_styles, choice_selector, 'body > .compact-chat-choice-anchor[data-chat-surface-mode="compact"]::-webkit-scrollbar')
    slot_block = css_block(styles, slot_selector, option_selector)
    host_slot_block = css_block(host_styles, slot_selector, option_selector)
    option_block = css_block(styles, option_selector, 'body > .compact-chat-choice-anchor[data-chat-surface-mode="compact"] .composer-galgame-options')
    host_option_block = css_block(host_styles, option_selector, 'body > .compact-chat-choice-anchor[data-chat-surface-mode="compact"][data-choice-layer-open="false"]')
    geometry_block = script.split("function getCompactGeometryElementRect(element)", 1)[1].split(
        "function getCompactHistoryScrollbarRect(element, parentRect)",
        1,
    )[0]

    assert "pointer-events: none;" in choice_block
    assert "pointer-events: none;" in host_choice_block
    assert "pointer-events: none;" in slot_block
    assert "pointer-events: none;" in host_slot_block
    assert "pointer-events: auto;" in option_block
    assert "pointer-events: auto;" in host_option_block
    assert "if (item === 'choice')" in geometry_block
    assert "querySelectorAll('.composer-galgame-option')" in geometry_block
    assert "var ownRect = normalizeCompactDomRect(element.getBoundingClientRect());" in geometry_block
    assert geometry_block.index("if (item === 'choice')") < geometry_block.index("var ownRect = normalizeCompactDomRect")


def test_desktop_compact_choice_placement_uses_surface_anchor_without_frame_polling():
    app_source = REACT_CHAT_APP_PATH.read_text(encoding="utf-8")

    placement_effect = app_source.split("if (!compactChoiceLayerOpen) return;", 1)[1].split(
        "const requestCompactChatState = useCallback",
        1,
    )[0]

    assert "const shellNode = compactInputShellRef.current;" in placement_effect
    assert "const nextShellNode = compactInputShellRef.current;" in placement_effect
    assert "appShellRef.current" not in placement_effect
    assert "window.addEventListener('neko:desktop-compact-layout-change', handleDesktopCompactLayoutChange);" in placement_effect
    assert "schedulePlacementUpdateWithDesktopLayout(layout);" in placement_effect
    assert "requestAnimationFrame(trackPlacement)" not in placement_effect
    assert "const trackPlacement = () =>" not in placement_effect


def test_desktop_compact_history_hit_regions_are_clipped_to_visible_parent():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    assert "function intersectCompactRects(a, b)" in script

    composite_block = script.split("function collectCompactCompositeGeometryItems(element, kind)", 1)[1].split(
        "function collectCompactSurfaceGeometryItems()",
        1,
    )[0]

    assert "var clippedRect = kind === 'musicPlayer'" in composite_block
    assert "? rect" in composite_block
    assert ": (parentRect ? intersectCompactRects(rect, parentRect) : rect);" in composite_block
    assert "if (!clippedRect) return null;" in composite_block
    assert "visualRect: clippedRect" in composite_block
    assert "hitRect: clippedRect" in composite_block
    assert "nativeRect: clippedRect" in composite_block
    assert "id: 'history:scrollbar'" in composite_block
    parent_native_block = composite_block.split("id: kind + ':native'", 1)[1].split("interactive: false", 1)[0]
    assert "hitRect: null" in parent_native_block
    assert "nativeRect: parentRect" in parent_native_block
    scrollbar_block = composite_block.split("id: 'history:scrollbar'", 1)[1].split("hitRegionKind: 'scrollbar'", 1)[0]
    assert "nativeRect: scrollbarRect" in scrollbar_block


def test_compact_geometry_snapshot_separates_base_surface_from_extra_islands():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    assert "function isCompactSurfaceBaseAnchorKind(kind)" in script
    assert "return kind === 'surfaceShell' || kind === 'capsule' || kind === 'input';" in script
    assert "function isCompactSurfaceBaseHitKind(kind)" in script
    assert "return kind === 'inputControl';" in script
    assert "function getCompactSurfaceGeometryRole(kind)" in script
    assert "if (kind === 'dragHandle') return 'baseHit';" not in script
    assert "if (isCompactSurfaceBaseHitKind(kind)) return 'baseHit';" in script
    assert "return 'extraIsland';" in script
    assert "item.geometryRole = getCompactSurfaceGeometryRole(item.kind);" in script

    snapshot_block = script.split("function getCompactInteractionGeometrySnapshot()", 1)[1].split(
        "function syncCompactInteractionGeometry()",
        1,
    )[0]

    assert "var baseSurfaceItems = surfaceItems.filter(function (item) {" in snapshot_block
    assert "return item && item.geometryRole === 'baseAnchor';" in snapshot_block
    assert "var extraIslandItems = surfaceItems.filter(function (item) {" in snapshot_block
    assert "return item && item.geometryRole === 'extraIsland';" in snapshot_block
    assert "var baseSurfaceNativeItems = surfaceItems.filter(function (item) {" in snapshot_block
    assert "item.geometryRole === 'baseAnchor' || item.geometryRole === 'baseHit'" in snapshot_block
    assert "function getCompactGeometryItemBoundsRects(item)" in script
    assert "if (item.visualRect) rects.push(item.visualRect);" in script
    assert "if (item.nativeRect) rects.push(item.nativeRect);" in script
    assert "var surfaceRects = surfaceItems.reduce(function (rects, item) {" in snapshot_block
    assert "var baseSurfaceRects = baseSurfaceItems.reduce(function (rects, item) {" in snapshot_block
    assert "return rects.concat(getCompactGeometryItemBoundsRects(item));" in snapshot_block
    assert "baseSurfaceItems: baseSurfaceItems" in snapshot_block
    assert "baseSurfaceRect: unionCompactRects(baseSurfaceRects)" in snapshot_block
    assert "baseSurfaceNativeRects: baseSurfaceNativeItems.map(function (item) { return item.nativeRect; }).filter(Boolean)" in snapshot_block
    assert "baseSurfaceHitRects:" in snapshot_block
    assert "extraIslandItems: extraIslandItems" in snapshot_block
    assert "extraIslandNativeRects:" in snapshot_block
    assert "extraIslandHitRects:" in snapshot_block


def test_externalized_chat_input_spotlight_uses_pc_overlay_rounded_rect_radius():
    script = read_js_parts(Path(__file__).resolve().parents[2] / "static" / "app" / "app-interpage")

    rect_block = script.split("function updateYuiGuideChatSpotlight(kind, pcOverlayRunId)", 1)[1].split(
        "function applyYuiGuideChatSpotlight",
        1,
    )[0]

    assert (
        "var radius = kind === 'window' ? 26 : Math.min(34, Math.max(18, "
        "Math.round((sourceRect.height + padding * 2) / 2)));"
    ) in rect_block


def test_externalized_chat_input_spotlight_uses_global_overlay_only():
    script = read_js_parts(Path(__file__).resolve().parents[2] / "static" / "app" / "app-interpage")

    update_block = script.split("function updateYuiGuideChatSpotlight(kind, pcOverlayRunId)", 1)[1].split(
        "function applyYuiGuideChatSpotlight(kind, options)",
        1,
    )[0]

    assert "var pcOverlayAvailable = isYuiGuidePcOverlayAvailable();" in update_block
    assert "if (pcOverlayAvailable) {" in update_block
    assert "var pcWindowMetrics = pcOverlayAvailable && typeof getYuiGuideWindowMetrics === 'function'" in update_block
    assert "getYuiGuideChatSpotlightSourceRect(kind, yuiGuideChatSpotlightVariant, rect, pcWindowMetrics)" in update_block
    assert "metrics.waylandWorkAreaCarrier === true" in script
    assert "metrics.niriWaylandRuntime !== true" in script
    assert "var sourceRect = sourceRectInfo ? sourceRectInfo.rect : rect;" in update_block
    assert "toYuiGuideScreenRect({" in update_block
    assert "}, kind, yuiGuideChatSpotlightVariant, pcWindowMetrics)" in update_block
    assert "kind !== 'input' && isYuiGuidePcOverlayAvailable()" not in update_block
    assert "hideYuiGuideChatSpotlightElement" not in script
    assert "hideYuiGuideChatSpotlightElements" not in script
    assert "is-plain-capsule" not in update_block
    assert "getYuiGuideChatSpotlightElement(!pcOverlayAvailable)" in update_block
    assert "renderYuiGuideChatSpotlight(" not in update_block
    assert "function renderYuiGuideChatSpotlight" not in script


def test_yui_guide_state_messages_bypass_cross_channel_dedup_but_cursor_deltas_do_not():
    script = read_js_parts(Path(__file__).resolve().parents[2] / "static" / "app" / "app-interpage")

    bypass_block = script.split("function shouldBypassYuiGuideMessageDedup(action, message)", 1)[1].split(
        "function isMainUIHiddenByModelManager()",
        1,
    )[0]

    assert "message && message.bypassDedup === true" in bypass_block
    assert "action === 'yui_guide_set_chat_spotlight'" in bypass_block
    assert "action === 'yui_guide_set_chat_cursor'" in bypass_block
    assert "action === 'yui_guide_drag_chat_cursor'" not in bypass_block
    assert "action === 'yui_guide_arc_chat_cursor'" not in bypass_block
    assert "action === 'yui_guide_set_compact_history_open'" in bypass_block
    assert "action === 'yui_guide_set_compact_chat_fixed_layout'" in bypass_block
    assert "action === 'yui_guide_rotate_compact_tool_wheel'" in bypass_block
    assert "action === 'yui_guide_set_chat_spotlight'" in bypass_block
    assert "action === 'yui_guide_set_chat_buttons_disabled'" in bypass_block
    assert "!shouldBypassYuiGuideMessageDedup(message.action, message)" in script
    assert "!shouldBypassYuiGuideMessageDedup(event.data.action, event.data)" in script
    assert "case 'yui_guide_set_chat_cursor':" in script
    assert "case 'yui_guide_drag_chat_cursor':" in script
    assert "case 'yui_guide_arc_chat_cursor':" in script
    assert "relayYuiGuideChatCommand(Object.assign({}, event.data," in script
    assert "neko:tutorial-overlay-relay" in script
    assert "__nekoTutorialOverlayRelay" in script


def test_yui_guide_external_compact_history_open_is_bridged_to_react_host():
    react_host = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    interpage = read_js_parts(Path(__file__).resolve().parents[2] / "static" / "app" / "app-interpage")

    assert "function setCompactHistoryOpen(open, reason)" in react_host
    assert "compactHistoryOpenRequest" in react_host
    assert "setCompactHistoryOpen: setCompactHistoryOpen" in react_host
    assert "case 'yui_guide_set_compact_history_open'" in interpage
    assert "function applyYuiGuideCompactHistoryOpen(open, reason)" in interpage
    assert "host.setCompactHistoryOpen(open === true, reason || 'external-yui-guide');" in interpage


def test_yui_guide_compact_chat_fixed_layout_is_bridged_to_standalone_chat_body():
    interpage = APP_INTERPAGE_PATH.read_text(encoding="utf-8")

    relayed_block = interpage.split("function handleYuiGuideRelayedMessage(message)", 1)[1].split(
        "function ensureYuiGuideStandaloneInteractionShield",
        1,
    )[0]
    broadcast_block = interpage.split("handleNekoBroadcastMessage = async function (event)", 1)[1].split(
        "console.log('[BroadcastChannel] 初始化失败",
        1,
    )[0]
    scoped_block = interpage.split("function isYuiGuideLifecycleScopedAction(action)", 1)[1].split(
        "function resetYuiGuidePcOverlayRunForRetry()",
        1,
    )[0]
    cleanup_block = interpage.split("function clearYuiGuidePcOverlayBridgeState(reason", 1)[1].split(
        "function cleanupAppInterpageTransientResources()",
        1,
    )[0]

    assert "function applyYuiGuideCompactChatFixedLayout(fixed)" in interpage
    assert "document.body.classList.toggle('yui-guide-compact-chat-fixed', fixed === true);" in interpage
    assert "case 'yui_guide_set_compact_chat_fixed_layout':" in relayed_block
    assert "applyYuiGuideCompactChatFixedLayout(message.fixed === true);" in relayed_block
    assert "case 'yui_guide_set_compact_chat_fixed_layout':" in broadcast_block
    assert "applyYuiGuideCompactChatFixedLayout(event.data.fixed === true);" in broadcast_block
    assert "case 'yui_guide_set_compact_chat_fixed_layout':" in scoped_block
    # prepare 会建立本轮教程的胶囊恢复快照，也必须拒绝旧 run 的迟到消息。
    assert "case 'yui_guide_prepare_compact_chat':" in scoped_block
    assert "applyYuiGuideCompactChatFixedLayout(false);" in cleanup_block


def test_interpage_defers_broadcast_binding_until_all_parts_are_loaded():
    root = Path(__file__).resolve().parents[2] / "static" / "app" / "app-interpage"
    relay_part = (root / "guide-message-relay.js").read_text(encoding="utf-8")
    final_part = (root / "listeners-and-api.js").read_text(encoding="utf-8")

    assert "I.handleNekoBroadcastMessage = async function (event)" in relay_part
    assert "I.nekoBroadcastChannel.onmessage =" not in relay_part
    assert "I.nekoBroadcastChannel.onmessage = I.handleNekoBroadcastMessage;" in final_part


def test_interpage_defers_tutorial_relay_listeners_until_all_parts_are_loaded():
    root = Path(__file__).resolve().parents[2] / "static" / "app" / "app-interpage"
    bridge_part = (root / "cross-window-broadcast-and-bridge.js").read_text(encoding="utf-8")
    final_part = (root / "listeners-and-api.js").read_text(encoding="utf-8")

    assert "I.handleYuiGuideRelayedCustomEvent = function" in bridge_part
    assert "I.handleYuiGuideRelayedWindowMessage = function" in bridge_part
    assert bridge_part.count("I.handleYuiGuideRelayedCustomEvent") == 1
    assert bridge_part.count("I.handleYuiGuideRelayedWindowMessage") == 1
    assert "I.handleYuiGuideRelayedCustomEvent" in final_part
    assert "I.handleYuiGuideRelayedWindowMessage" in final_part


def test_new_user_icebreaker_choice_prompt_dispatches_host_event():
    react_host = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    choice_block = react_host.split("function handleChoiceSelect(option, source)", 1)[1].split(
        "function handleCompactChatStateChange",
        1,
    )[0]

    assert "source === 'new_user_icebreaker'" in choice_block
    before_icebreaker_block = choice_block.split("if (source === 'new_user_icebreaker')", 1)[0]
    assert "isHomeTutorialInteractionLocked()" not in before_icebreaker_block
    assert "prompt.source !== 'new_user_icebreaker'" in choice_block
    assert "state.choicePrompt = null;" in choice_block
    assert "choice: option.choice" in choice_block
    assert "label: option.label || ''" in choice_block
    assert "window.dispatchEvent(new CustomEvent('neko:icebreaker-choice-selected'" in choice_block
    assert "dispatchHostEvent('icebreaker-choice-selected', detail);" in choice_block


def test_new_user_icebreaker_prompt_is_exposed_by_react_host():
    react_host = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    prompt_block = react_host.split("function setNewUserIcebreakerPrompt(payload)", 1)[1].split(
        "function dismissChoicePromptIfMatches",
        1,
    )[0]

    assert "source: 'new_user_icebreaker'" in prompt_block
    assert "gameType: String(payload.gameType || 'new_user_icebreaker')" in prompt_block
    assert "invalidatePendingGalgameRequest();" in prompt_block
    assert "function setChoicePrompt(payload)" in react_host
    assert "setChoicePrompt: setChoicePrompt" in react_host
    assert "setNewUserIcebreakerPrompt: setNewUserIcebreakerPrompt" in react_host
    # 揭示延迟「只扣视觉」：prompt 立刻入 state（绑定输入路由），按 revealDelayMs
    # 记下 revealAt 并交给计时器延后露出按钮。
    assert "var revealDelayMs = Number(payload.revealDelayMs) || 0;" in prompt_block
    assert "revealAt: revealDelayMs > 0 ? Date.now() + revealDelayMs : 0" in prompt_block
    assert "scheduleChoicePromptReveal();" in prompt_block


def test_icebreaker_choice_prompt_reveal_delay_hides_buttons_not_state():
    # 揭示延迟必须只藏按钮、不扣 state.choicePrompt——否则间隙内的自由文本会绕过
    # icebreaker free-text 路由落到普通聊天。
    react_host = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    # 渲染层走 getRevealedChoicePrompt（揭示未到点返回 null 藏按钮）；输入路由仍直接
    # 读 state.choicePrompt，所以间隙内打字会被判为 icebreaker free-text。
    assert "function getRevealedChoicePrompt()" in react_host
    assert "choicePrompt: getRevealedChoicePrompt()," in react_host
    reveal_block = react_host.split("function getRevealedChoicePrompt()", 1)[1].split(
        "function setNewUserIcebreakerPrompt",
        1,
    )[0]
    assert "if (prompt.revealAt && Date.now() < prompt.revealAt) return null;" in reveal_block

    submit_block = react_host.split("function handleComposerSubmit(payload)", 1)[1].split(
        "function prepareCompactHistoryDropSubmit",
        1,
    )[0]
    assert "if (state.choicePrompt && state.choicePrompt.source === 'new_user_icebreaker')" in submit_block

    # 计时器只在同一个 prompt 仍在台上时才揭示，避免延迟期间被覆盖/清空后揭示过期选项。
    schedule_block = react_host.split("function scheduleChoicePromptReveal()", 1)[1].split(
        "function getRevealedChoicePrompt",
        1,
    )[0]
    assert "if (state.choicePrompt === prompt)" in schedule_block

    # galgame 选项拉取必须在 icebreaker prompt 激活（含揭示延迟内已就位但未露出）时让位，
    # 否则 turn-end 会把 galgame A/B/C 挤进尚未露出 icebreaker 选项的同一槽位（Codex P2）。
    galgame_fetch_block = react_host.split("function fetchGalgameOptionsForLatestTurn()", 1)[1].split(
        "function ",
        1,
    )[0]
    assert "if (state.choicePrompt && state.choicePrompt.source === 'new_user_icebreaker') return;" in galgame_fetch_block


def test_galgame_history_excludes_tutorial_guide_messages():
    react_host = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    history_block = react_host.split("function getRecentGalgameMessageHistory()", 1)[1].split(
        "function pickAcceptLanguage",
        1,
    )[0]

    assert "if (isYuiGuideChatMessage(m)) continue;" in history_block
    assert "if (!m) continue;" in history_block
    assert history_block.index("if (!m) continue;") < history_block.index("if (isYuiGuideChatMessage(m)) continue;")


def test_galgame_history_excludes_new_user_icebreaker_messages():
    react_host = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    history_block = react_host.split("function getRecentGalgameMessageHistory()", 1)[1].split(
        "function pickAcceptLanguage",
        1,
    )[0]

    assert "function isNewUserIcebreakerChatMessage(message)" in react_host
    assert "if (isNewUserIcebreakerChatMessage(m))" in history_block
    assert "if (!collected.length) return [];" in history_block
    append_block = react_host.split("function appendMessage(message)", 1)[1].split(
        "function updateMessage",
        1,
    )[0]
    assert "normalized.role === 'user' || isNewUserIcebreakerChatMessage(normalized)" in append_block
    assert "invalidatePendingGalgameRequest();" in append_block


def test_galgame_turn_end_listener_ignores_new_user_icebreaker():
    react_host = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    guard_block = react_host.split("function isNewUserIcebreakerTurnEndEvent(event)", 1)[1].split(
        "window.addEventListener('neko-assistant-turn-end'",
        1,
    )[0]
    listener_block = react_host.split(
        "window.addEventListener('neko-assistant-turn-end', function (event)",
        1,
    )[1].split("\n        });", 1)[0]

    assert "detail.meta" in guard_block
    assert "meta.source === 'new_user_icebreaker'" in guard_block
    assert "meta.kind === 'new_user_icebreaker'" in guard_block
    assert "meta.event" in guard_block
    assert "source === 'new_user_icebreaker'" in guard_block
    assert "if (isNewUserIcebreakerTurnEndEvent(event)) return;" in listener_block


def test_galgame_option_template_follows_interface_language():
    react_host = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    language_tail = react_host.split("function pickAcceptLanguage()", 1)[1].split(
        "var GALGAME_FETCH_TIMEOUT_MS",
        1,
    )[0]
    language_block = "function pickAcceptLanguage()" + language_tail
    assert "getConversationLanguagePreference" not in language_block
    assert "window.getCurrentLocale" in language_block
    assert "window.i18next.language" in language_block
    assert "navigator.language" in language_block
    assert language_block.index("window.getCurrentLocale") < language_block.index(
        "window.i18next.language"
    )
    assert language_block.index("window.i18next.language") < language_block.index(
        "navigator.language"
    )

    fetch_block = react_host.split("function fetchGalgameOptionsForLatestTurn()", 1)[1].split(
        "I.handleGalgameModeToggle",
        1,
    )[0]
    assert "language: pickAcceptLanguage()" in fetch_block

    node_path = shutil.which("node")
    if not node_path:
        pytest.skip("node is required to verify GalGame language selection")
    script = f"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const functionSource = {json.dumps(language_block)};

function resolve(windowValue, navigatorLanguage) {{
    const context = {{ window: windowValue }};
    if (navigatorLanguage !== undefined) {{
        context.navigator = {{ language: navigatorLanguage }};
    }}
    vm.createContext(context);
    vm.runInContext(functionSource, context);
    return vm.runInContext('pickAcceptLanguage()', context);
}}

assert.equal(resolve({{
    getConversationLanguagePreference: () => 'pt',
    getCurrentLocale: () => 'ja',
    i18next: {{ language: 'en' }}
}}, 'ko'), 'ja');
assert.equal(resolve({{
    getCurrentLocale: () => '',
    i18next: {{ language: 'en' }}
}}, 'ko'), 'en');
assert.equal(resolve({{ i18next: {{}} }}, 'ko'), 'ko');
assert.equal(resolve({{ i18next: {{}} }}, undefined), '');
"""
    result = run_node_script(
        node_path,
        script,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_icebreaker_reset_clears_prompt_by_source_without_session_match():
    react_host = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    assert "function clearChoicePromptBySource(source, reason)" in react_host
    reset_block = react_host.split("function clearChoicePromptBySource(source, reason)", 1)[1].split(
        "function clearIcebreakerChoicePrompt",
        1,
    )[0]
    assert "if (normalizedSource !== 'new_user_icebreaker') return false;" in reset_block
    assert "state.choicePrompt.source !== normalizedSource" in reset_block
    assert "clearChoicePromptBySource:', normalizedSource, reason || ''" in reset_block
    assert "state.choicePrompt = null;" in reset_block
    assert "invalidatePendingGalgameRequest();" in reset_block

    listeners_block = react_host.split("function bindBridgeEvents()", 1)[1].split(
        "function init()",
        1,
    )[0]
    assert "window.addEventListener('neko:new-user-icebreaker-reset'" in listeners_block
    assert "clearChoicePromptBySource('new_user_icebreaker', 'new-user-icebreaker-reset')" in listeners_block


def test_externalized_tutorial_chat_ready_replays_input_lock():
    takeover = (Path(__file__).resolve().parents[2] / "static" / "tutorial" / "core" / "interaction-takeover.js").read_text(
        encoding="utf-8"
    )
    bridge_bus = (Path(__file__).resolve().parents[2] / "static" / "tutorial" / "core" / "bridge-command-bus.js").read_text(
        encoding="utf-8"
    )
    interpage = read_js_parts(Path(__file__).resolve().parents[2] / "static" / "app" / "app-interpage")

    assert "yui_guide_set_chat_input_locked: true" in bridge_bus
    bridge_replay_block = interpage.split("function handleYuiGuideChatBridgeData(data)", 1)[1].split(
        "function drainPendingYuiGuideChatBridgeQueue",
        1,
    )[0]
    assert "case 'yui_guide_set_chat_input_locked':" in bridge_replay_block
    assert "applyYuiGuideChatInputLocked(data.locked === true, data.reason || '')" in bridge_replay_block
    ready_block = takeover.split("onExternalChatReady()", 1)[1].split(
        "destroy()",
        1,
    )[0]
    assert "this.setExternalizedChatInputLocked(true, 'external-chat-ready')" in ready_block


def test_new_user_icebreaker_choice_listener_posts_context():
    script = (Path(__file__).resolve().parents[2] / "static" / "icebreaker/new-user-icebreaker.js").read_text(encoding="utf-8")
    choice_block = script.split("function completeFromChoice(detail)", 1)[1].split(
        "function handleTutorialEnded",
        1,
    )[0]

    assert "const option = detail && detail.option" in choice_block
    assert "const choice = String((detail && detail.choice) || option.choice || '')" in choice_block
    assert "const label = String((detail && detail.label) || option.label || '')" in choice_block
    assert "const contextSynced = await postContext('user', label || choice, session.id);" in choice_block
    assert "completed: contextSynced" in choice_block
    assert "contextSyncPending: !contextSynced" in choice_block
    assert "activeSession = null;" in choice_block
    assert "fetch('/api/icebreaker/context'" in script
    assert "return !!(body && body.ok === true);" in script


def test_interpage_bundle_uses_static_asset_version_on_home_and_chat():
    index_template = INDEX_TEMPLATE_PATH.read_text(encoding="utf-8")
    chat_template = CHAT_TEMPLATE_PATH.read_text(encoding="utf-8")

    for part_name in (
        'bootstrap-resources-and-model-reload.js',
        'composer-voice-sync.js',
        'cross-window-broadcast-and-bridge.js',
        'guide-message-relay.js',
        'guide-overlay.js',
        'guide-targets.js',
        'listeners-and-api.js',
    ):
        static_path = f'/static/app/app-interpage/{part_name}?v={{{{ static_asset_version }}}}'
        react_path = f'/static/app/app-interpage/{part_name}?v={{{{ react_chat_asset_version }}}}'
        assert static_path in index_template
        assert static_path in chat_template
        assert react_path not in index_template
        assert react_path not in chat_template


def test_externalized_chat_input_spotlight_retries_after_message_layout():
    script = read_js_parts(Path(__file__).resolve().parents[2] / "static" / "app" / "app-interpage")

    retry_block = script.split("function scheduleYuiGuideChatInputSpotlightRetry(kind, pcOverlayRunId)", 1)[1].split(
        "function updateYuiGuideChatSpotlight(kind, pcOverlayRunId)",
        1,
    )[0]

    assert "if (!isYuiGuideChatInputSpotlightKind(retryKind))" in retry_block
    assert "if (yuiGuideChatSpotlightKind === retryKind)" in retry_block
    assert "updateYuiGuideChatSpotlight(retryKind, retryRunId);" in retry_block
    assert "scheduleYuiGuideChatInputSpotlightRetry(normalizedKind, pcOverlayRunId);" in script


def test_compact_tutorial_guide_preview_scrolls_without_ellipsis():
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")

    scrollable_block = css_block(
        styles,
        '.compact-chat-capsule-text[data-compact-preview-scrollable="true"],',
        ".compact-chat-capsule-text::-webkit-scrollbar",
    )

    assert "text-overflow: clip;" in scrollable_block


def test_compact_input_geometry_preserves_drag_surface_native_region():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    app_source = REACT_CHAT_APP_PATH.read_text(encoding="utf-8")

    input_collector = script.split("function collectCompactInputSurfaceGeometryItems(element)", 1)[1].split(
        "function collectCompactCompositeGeometryItems(element, kind)",
        1,
    )[0]
    surface_collector = script.split("function collectCompactSurfaceGeometryItems()", 1)[1].split(
        "function getCompactInteractionGeometrySnapshot()",
        1,
    )[0]

    assert "id: element.id || 'input:surface'" in input_collector
    assert "kind: 'input'" in input_collector
    assert "var inputSurfaceIsDragSurface = element.getAttribute('data-compact-drag-surface') === 'true';" in input_collector
    assert "hitRect: inputSurfaceIsDragSurface ? parentRect : null" in input_collector
    assert "nativeRect: inputSurfaceIsDragSurface ? parentRect : null" in input_collector
    assert "interactive: inputSurfaceIsDragSurface" in input_collector
    assert "var hitRegionElements = [];" in input_collector
    assert 'element.getAttribute(\'data-compact-hit-region\') === \'true\'' in input_collector
    assert "hitRegionElements.push(element);" in input_collector
    assert 'element.querySelectorAll(\'[data-compact-hit-region="true"]\')' in input_collector
    assert "kind: 'inputControl'" in input_collector
    assert "hitRect: clippedRect" in input_collector
    assert "nativeRect: clippedRect" in input_collector
    assert "hitRegionKind: child.getAttribute('data-compact-hit-region-kind') || null" in input_collector
    assert "if (compactGeometryItem === 'input')" in surface_collector
    assert "return items.concat(collectCompactInputSurfaceGeometryItems(element));" in surface_collector
    assert "id: 'surface:shell'" in surface_collector
    shell_block = surface_collector.split("id: 'surface:shell'", 1)[1].split("interactive: false", 1)[0]
    assert "hitRect: null" in shell_block
    assert "nativeRect: null" in shell_block

    assert "data-compact-geometry-hit-scope={!composerHidden ? 'children' : undefined}" in app_source
    assert "if (!isCompactSurface || composerHidden)" in app_source
    assert "if (!isCompactSurface || effectiveCompactChatState !== 'input')" not in app_source
    assert 'data-compact-hit-region-id="input:text"' in app_source
    assert 'data-compact-hit-region-kind="input-text"' in app_source
    assert 'data-compact-hit-region-id="input:minimize"' in app_source
    assert 'data-compact-hit-region-kind="input-minimize"' in app_source
    assert 'data-compact-hit-region-id="input:tool-toggle"' in app_source
    assert 'data-compact-hit-region-kind="input-tool-toggle"' in app_source


def test_compact_avatar_tool_manager_uses_desktop_work_area_for_carrier_layout():
    manager_source = REACT_CHAT_AVATAR_TOOL_MANAGER_PATH.read_text(encoding="utf-8")
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")

    assert "__nekoDesktopCompactLayout" in manager_source
    assert "workAreaX - windowX" in manager_source
    assert "workAreaY - windowY" in manager_source
    assert "viewport.compactDesktop" in manager_source
    assert "getDesktopCompactDialogSize(viewport, preferredHeight)" in manager_source
    assert "neko:desktop-compact-layout-change" in manager_source
    assert "'--avatar-tool-manager-width'" in manager_source
    assert "'--avatar-tool-manager-height'" in manager_source
    assert "is-desktop-compact-layout" in manager_source

    desktop_compact_block = css_block(
        styles,
        ".avatar-tool-manager-dialog.is-desktop-compact-layout",
        ".avatar-tool-manager-dialog.is-dragging",
    )
    assert "width: var(--avatar-tool-manager-width, 460px);" in desktop_compact_block
    assert "height: var(--avatar-tool-manager-height, 680px);" in desktop_compact_block
    assert "max-height: var(--avatar-tool-manager-max-height, 680px);" in desktop_compact_block
    assert "100vw" not in desktop_compact_block
    assert "85vh" not in desktop_compact_block


def test_compact_surface_drag_uses_declared_surface_and_no_drag_exclusions():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    assert "function isCompactDragSurfaceTarget(target)" in script
    assert "target.closest('[data-compact-no-drag=\"true\"]')" in script
    assert "target.closest('[data-compact-drag-surface=\"true\"]')" in script
    assert "function isCompactDragHandleTarget(target)" not in script
    assert "target.closest('[data-compact-drag-handle=\"true\"]')" not in script
    assert "if (dragState.compactSurface && !dragState.moved) return;" in script

    drag_block = script.split("document.addEventListener('mousedown', function (event)", 1)[1].split(
        "document.addEventListener('touchstart', function (event)",
        1,
    )[0]
    assert "if (!isCompactDragSurfaceTarget(event.target)) return;" in drag_block
    assert "compactSurface: true" in drag_block

    touch_block = script.split("document.addEventListener('touchstart', function (event)", 1)[1].split(
        "document.addEventListener('mousemove', function (event)",
        1,
    )[0]
    assert "if (!isCompactDragSurfaceTarget(event.target)) return;" in touch_block
    assert "compactSurface: true" in touch_block
    assert "compact capsule must still synthesize click" in touch_block
    assert "event.preventDefault();" not in touch_block
    assert "event.stopPropagation();" not in touch_block
    assert "}, { capture: true, passive: true });" in touch_block


def test_moved_drag_suppresses_trailing_release_click():
    # 移动过的拖拽（含 compact surface 本体拖拽）松开后，浏览器会在 mouseup 落点
    # 补发一次 click；落点若是胶囊按钮会被误判为点击而展开输入框。守卫在 moved
    # 时 arm，并在 document capture 阶段吞掉紧随的那一次 click。
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    assert "suppressDragReleaseClick = false;" in script

    stop_block = script.split("function stopDrag(options)", 1)[1].split(
        "function bindDragging()",
        1,
    )[0]
    assert "if (wasMoved) {" in stop_block
    assert "armDragReleaseClickGuard();" in stop_block

    arm_block = script.split("function armDragReleaseClickGuard()", 1)[1].split(
        "function consumeDragReleaseClickGuard",
        1,
    )[0]
    assert "suppressDragReleaseClick = true;" in arm_block
    # setTimeout(…, 0) 兜底清旗：click 同任务先消费，无 click 时也不残留误吞后续点击。
    assert "window.setTimeout(function () {" in arm_block
    assert "suppressDragReleaseClick = false;" in arm_block

    consume_block = script.split("function consumeDragReleaseClickGuard(event)", 1)[1].split(
        "function getOverlay()",
        1,
    )[0]
    assert "if (!suppressDragReleaseClick) return;" in consume_block
    assert "suppressDragReleaseClick = false;" in consume_block
    assert "event.preventDefault();" in consume_block
    assert "event.stopPropagation();" in consume_block

    # capture 阶段挂载，且排在 mobile 展开守卫之后（保留既有行为优先级）。
    assert "document.addEventListener('click', consumeDragReleaseClickGuard, true);" in script
    listeners_block = script.split(
        "document.addEventListener('click', blockMobileExpandSyntheticPointerEvent, true);",
        1,
    )[1]
    assert "document.addEventListener('click', consumeDragReleaseClickGuard, true);" in listeners_block


def test_minimized_yarn_drag_reports_forced_release_as_cancel():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    dispatch_block = script.split("function dispatchMinimizedYarnDragPhase", 1)[1].split(
        "function isCompactDragSurfaceTarget",
        1,
    )[0]
    start_block = script.split("function startDrag", 1)[1].split(
        "function updateDrag",
        1,
    )[0]
    assert "lifecycleSequence: dragState.yarnLifecycleSequence" in dispatch_block
    assert "yarnLifecycleSequence: getCurrentIdleChatLifecycleSequence()" in start_block

    stop_block = script.split("function stopDrag(options)", 1)[1].split(
        "function bindDragging()",
        1,
    )[0]
    assert "dispatchMinimizedYarnDragPhase(opts.suppressClick ? 'cancel' : 'end'" in stop_block

    touch_cancel_block = script.split("document.addEventListener('touchcancel'", 1)[1].split(
        ");",
        1,
    )[0]
    assert "suppressClick: true" in touch_cancel_block


def test_react_chat_host_exports_compact_lifecycle_restore_api():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    host_block = script.rsplit("Object.assign(window.reactChatWindowHost", 1)[1].split(
        "delete window.__appReactChatWindowParts",
        1,
    )[0]

    assert "republishCompactSurfaceLayoutChange: republishCompactSurfaceLayoutChange" in host_block
    assert "scheduleCompactMinimizeBallTracking: scheduleCompactMinimizeBallTracking" in host_block


def test_compact_minimize_targets_inline_yarn_ball_button_center():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    assert "compactMinimizeBallTargetAnchor = null;" in script
    assert "function getCompactMinimizeBallTargetRect()" in script
    assert "root.querySelector('.compact-chat-minimize-ball')" in script
    assert "width: MINIMIZED_SIZE" in script
    assert "height: MINIMIZED_SIZE" in script
    assert "left: buttonRect.left + buttonRect.width / 2 - MINIMIZED_SIZE / 2" in script
    assert "top: buttonRect.top + buttonRect.height / 2 - MINIMIZED_SIZE / 2" in script
    target_rect_block = script.split("function getCompactMinimizeBallTargetRect()", 1)[1].split(
        "function rememberCompactMinimizeBallTargetAnchor()",
        1,
    )[0]
    assert "document.querySelector('.compact-chat-minimize-ball')" not in target_rect_block
    assert "function getMinimizedTargetFromCompactAnchor(anchorRect)" in script
    assert "Math.round(anchorRect.left)" in script
    assert "Math.round(anchorRect.top)" in script

    cancel_block = script.split("function clearCompactMinimizePressTimer()", 1)[1].split(
        "function handleCompactMinimizeRequest()",
        1,
    )[0]
    assert "compactMinimizeBallTargetAnchor = null;" in cancel_block

    minimize_block = script.split("function handleCompactMinimizeRequest()", 1)[1].split(
        "function handleMiniGameInviteChoice(option)",
        1,
    )[0]
    assert "rememberCompactMinimizeBallTargetAnchor();" in minimize_block

    set_mode_block = script.split("function setChatSurfaceMode(nextMode)", 1)[1].split(
        "function cycleChatSurfaceMode()",
        1,
    )[0]
    assert "if (!previousMinimized && nextMinimized && previousMode === 'compact')" in set_mode_block
    assert "rememberCompactMinimizeBallTargetAnchor();" in set_mode_block
    assert set_mode_block.index("clearCompactMinimizePressTimer();") < set_mode_block.index(
        "rememberCompactMinimizeBallTargetAnchor();"
    )
    assert set_mode_block.index("rememberCompactMinimizeBallTargetAnchor();") < set_mode_block.index(
        "state.chatSurfaceMode = normalized;"
    )

    target_block = script.split("function getMinimizedTarget(rect)", 1)[1].split(
        "function getExpandedTargetFromSavedState()",
        1,
    )[0]
    assert "getMinimizedTargetFromCompactAnchor(consumeCompactMinimizeBallTargetAnchor())" in target_block
    assert "if (compactTarget) return compactTarget;" in target_block


def test_compact_minimize_collapse_origin_matches_target():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    collapse_block = script.split("// ---- 折叠动画", 1)[1].split(
        "// ---- 展开动画",
        1,
    )[0]

    assert "var originDenomX = 1 - sx;" in collapse_block
    assert "originX = (targetLeft - rect.left) / originDenomX;" in collapse_block
    assert "originY = (targetTop - rect.top) / originDenomY;" in collapse_block
    assert "shell.style.transformOrigin = originX + 'px ' + originY + 'px';" in collapse_block
    assert "shell.style.removeProperty('transform-origin');" in collapse_block


def test_compact_minimize_does_not_replay_full_shell_collapse_animation():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    request_block = script.split("function handleCompactMinimizeRequest()", 1)[1].split(
        "function handleMiniGameInviteChoice(option)",
        1,
    )[0]
    assert request_block.count(
        "setChatSurfaceMode('minimized', { skipShellCollapseAnimation: true });"
    ) == 2

    set_mode_block = script.split("function setChatSurfaceMode(nextMode)", 1)[1].split(
        "function cycleChatSurfaceMode()",
        1,
    )[0]
    assert "if (transitionOptions.skipShellCollapseAnimation === true" in set_mode_block
    assert "&& previousMode === 'compact'" in set_mode_block
    assert "&& !isElectronChatWindow()" in set_mode_block
    assert "&& !window.__LANLAN_IS_ELECTRON_PET__" in set_mode_block
    assert "setMinimized(nextMinimized, { skipShellCollapseAnimation: true });" in set_mode_block
    assert "setMinimized(nextMinimized);" in set_mode_block

    minimize_block = script.split("function setMinimized(nextMinimized)", 1)[1].split(
        "// ---- 展开动画",
        1,
    )[0]
    instant_branch = minimize_block.split(
        "if (transitionOptions.skipShellCollapseAnimation === true)",
        1,
    )[1].split("// 3. 计算缩放比", 1)[0]
    assert "shell.classList.add('is-minimized');" in instant_branch
    assert "shell.classList.add('is-collapsing');" not in instant_branch
    assert "requestAnimationFrame" not in instant_branch


def test_web_compact_restore_does_not_replay_full_shell_expand_animation():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")

    set_mode_block = script.split("function setChatSurfaceMode(nextMode)", 1)[1].split(
        "function cycleChatSurfaceMode()",
        1,
    )[0]
    assert "previousMinimized" in set_mode_block
    assert "normalized === 'compact'" in set_mode_block
    assert "!isElectronChatWindow()" in set_mode_block
    assert "!window.__LANLAN_IS_ELECTRON_PET__" in set_mode_block
    assert "setMinimized(nextMinimized, { skipShellExpandAnimation: true });" in set_mode_block
    assert "setMinimized(nextMinimized);" in set_mode_block

    minimize_block = script.split("function setMinimized(nextMinimized)", 1)[1].split(
        "// ---- 展开动画",
        1,
    )[0]
    compact_restore = minimize_block.split(
        "if (!willMinimize && transitionOptions.skipShellExpandAnimation === true)",
        1,
    )[1].split("if (willMinimize)", 1)[0]
    assert "shell.style.visibility = 'hidden';" in compact_restore
    assert "shell.classList.remove('is-mobile-content-capped', 'is-minimized');" in compact_restore
    assert "syncCompactSurfaceAnchor();" in compact_restore
    assert "scheduleMobileContentLayout();" in compact_restore
    assert "COMPACT_EXPAND_WIPE_MS + COMPACT_EXPAND_TRANSITION_BUFFER_MS" in compact_restore
    assert "shell.classList.add('is-expanding');" not in compact_restore
    assert "scale(" not in compact_restore

    host_duration = re.search(r"var COMPACT_EXPAND_WIPE_MS = (\d+);", script)
    css_duration = re.search(
        r"\.compact-chat-surface-shell\.neko-compact-expanding\s*\{[^}]*"
        r"animation:\s*neko-compact-expand-wipe\s+(\d+)ms",
        styles,
        re.DOTALL,
    )
    assert host_duration is not None
    assert css_duration is not None
    assert host_duration.group(1) == css_duration.group(1)


def test_queued_surface_mode_preserves_compact_transition_options():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    set_mode_block = script.split("function setChatSurfaceMode(nextMode)", 1)[1].split(
        "function cycleChatSurfaceMode()",
        1,
    )[0]
    flush_block = script.split("function flushPendingChatSurfaceModeIfNeeded()", 1)[1].split(
        "function setMinimized(nextMinimized)",
        1,
    )[0]

    assert "pendingChatSurfaceMode = {" in set_mode_block
    assert "mode: normalized," in set_mode_block
    assert "transitionOptions: transitionOptions" in set_mode_block
    assert "var pendingSurfaceMode = pendingChatSurfaceMode;" in flush_block
    assert "var targetMode = pendingSurfaceMode.mode;" in flush_block
    assert "setChatSurfaceMode(targetMode, pendingSurfaceMode.transitionOptions);" in flush_block


def test_desktop_compact_layout_change_resets_anchor_only_when_base_surface_changes():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    assert "var compactDesktopSurfaceAnchorSnapshot = '';" in script
    assert "function serializeCompactSurfaceRectSnapshot(rect)" in script
    assert "function getCompactDesktopLayoutAnchorSnapshot(layout)" in script
    assert "return 'version:' + Math.round(anchorVersion);" in script
    assert "var screenSnapshot = serializeCompactSurfaceRectSnapshot(layout.surfaceScreenRect);" in script
    assert "if (screenSnapshot) return 'screen:' + screenSnapshot;" in script

    handler_block = script.split("function handleDesktopCompactLayoutChange(layout)", 1)[1].split(
        "function normalizeCompactDesktopWorkArea(raw)",
        1,
    )[0]
    resize_drag_script = (
        Path(__file__).resolve().parents[2]
        / "static"
        / "app"
        / "app-react-chat-window"
        / "resize-drag-and-api.js"
    ).read_text(encoding="utf-8")
    stop_drag_block = resize_drag_script.split("I.stopDrag = function stopDrag(options)", 1)[1].split(
        "function bindDragging()",
        1,
    )[0]
    listener_block = script.split("window.addEventListener('neko:desktop-compact-layout-change'", 1)[1].split(
        "window.addEventListener('neko:desktop-avatar-bounds-change'",
        1,
    )[0]

    assert "var nextAnchorSnapshot = getCompactDesktopLayoutAnchorSnapshot(layout);" in handler_block
    assert "nextAnchorSnapshot !== compactDesktopSurfaceAnchorSnapshot" in handler_block
    assert "compactDesktopSurfaceAnchorSnapshot = nextAnchorSnapshot;" in handler_block
    assert "if (baseAnchorChanged && !compactSurfaceDesktopResizeActive)" in handler_block
    assert "compactSurfaceAnchorLocked = false;" in handler_block
    assert "compactSurfaceAnchorSnapshot = '';" in handler_block
    assert "noteCompactSurfaceManualDragRelease" in script
    assert "isCompactSurfaceManualDragReleaseGuardActive" in handler_block
    assert "localCompactDragActive" in handler_block
    release_index = stop_drag_block.index(
        "I.noteCompactSurfaceManualDragRelease(compactRect);"
    )
    dispatch_index = stop_drag_block.index(
        "I.dispatchCompactSurfaceLayoutChange(compactRect);"
    )
    assert release_index < dispatch_index
    guard_start = handler_block.index(
        "if (!localCompactDragActive && !manualDragReleaseGuardActive) {"
    )
    guard_end = handler_block.index("\n            }", guard_start)
    guard_block = handler_block[guard_start:guard_end]
    assert "compactSurfaceAnchorLocked = false;" in guard_block
    assert "compactSurfaceAnchorSnapshot = '';" in guard_block
    assert "scheduleCompactMinimizeBallTracking();" in handler_block
    assert "var layout = event && event.detail ? event.detail : window.__nekoDesktopCompactLayout;" in listener_block
    assert "handleDesktopCompactLayoutChange(layout || null);" in listener_block
    assert "compactSurfaceAnchorSnapshot = '';" not in listener_block


def test_compact_surface_tracking_stops_idle_raf_but_keeps_active_sessions():
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    tracking_block = script.split("function scheduleCompactMinimizeBallTracking()", 1)[1].split(
        "function revealPendingCompactSurfaceOpen",
        1,
    )[0]
    drag_start_block = script.split("function startDrag(clientX, clientY, options)", 1)[1].split(
        "function updateDrag",
        1,
    )[0]

    assert "COMPACT_SURFACE_IDLE_SETTLE_FRAME_COUNT = 3;" in script
    assert "compactSurfaceTrackingSettleFramesRemaining = 0;" in script
    assert "function isCompactSurfaceTrackingActive()" in script
    assert "(dragState && dragState.compactSurface)" in script
    assert "compactSurfaceDesktopDragActive" in script
    assert "compactSurfaceResizeSession" in script
    assert "compactSurfaceDesktopResizeActive" in script
    assert "compactSurfaceTrackingSettleFramesRemaining = COMPACT_SURFACE_IDLE_SETTLE_FRAME_COUNT;" in tracking_block
    assert "if (!trackingActive && compactSurfaceTrackingSettleFramesRemaining <= 0) {\n                return;\n            }" in tracking_block
    assert "compactMinimizeBallFrame = window.requestAnimationFrame(loop);" in tracking_block
    assert "COMPACT_SURFACE_IDLE_SYNC_INTERVAL_MS" not in script
    assert "if (compactSurface) {\n            scheduleCompactMinimizeBallTracking();\n        }" in drag_start_block


def test_electron_compact_chat_retires_full_surface_chrome():
    template = CHAT_TEMPLATE_PATH.read_text(encoding="utf-8")

    # The standalone /chat window renders the shared compact capsule (driven by
    # static/css/index.css's `body.subtitle-web-host #react-chat-window-shell
    # [data-chat-surface-mode="compact"]` rule set, which chat.html's
    # subtitle-web-host body opts into) instead of a bespoke full-window glass
    # panel. The retired full form used to paint an empty glass panel for one
    # frame before React mounted and collapsed it to compact ("先 full 再
    # compact" 的那一帧). chat.html must NOT re-introduce that full surface.
    #
    # Retired *un-gated* full-surface artifacts that must stay gone — these are the
    # ones that painted before React mounted on the SHARED /chat route ("先 full 再
    # compact" 闪帧). 玻璃流光特效后来在独立 Electron full 窗口上带门槛复活（见
    # test_full_chat_glass_flow_revived_and_gated），但**无门槛**的旧形态必须保持移除，
    # 共享 /chat 这条路径才永远不会画出它。
    assert "@keyframes lightSweep" not in template            # 死代码关键帧，未恢复
    # 四边渐隐 mask 已带 full gate 复活（见 test_full_chat_glass_flow_revived_and_gated）。这里只守
    # 「无门槛」旧形态：每处 -webkit-mask-image 前必须紧邻 full gate —— 裸 #react-chat-window-root
    # 直接带 mask 才是 #1594 在共享 /chat 上挂载前先画的闪帧根因，必须保持不存在。
    _mask_token = "-webkit-mask-image"
    _gate_token = 'data-chat-surface-mode="full"'
    _scan = 0
    while True:
        _scan = template.find(_mask_token, _scan)
        if _scan == -1:
            break
        assert _gate_token in template[max(0, _scan - 400):_scan], "发现无门槛 mask（共享 /chat 闪帧根因）"
        _scan += len(_mask_token)
    assert "#react-chat-window-shell:not(.is-minimized)::before" not in template
    assert "#react-chat-window-shell:not(.is-minimized)::after" not in template
    assert "inset: 40px 25px 25px 20px" not in template       # 全窗口 shell 几何

    # The shell font tweak survives (compact still uses #react-chat-window-root).
    assert "#react-chat-window-root {" in template
    # The overlay must start hidden so nothing paints before React mounts the
    # compact surface — parity with templates/index.html, which is what kills the
    # pre-mount full-form flash.
    assert 'id="react-chat-window-overlay" hidden' in template


def test_full_chat_glass_flow_revived_and_gated():
    """独立 Electron full 窗口（part B）的玻璃流光特效复活，且严格带门槛。

    原 full 玻璃面板在 #1594 退「共享 /chat 的 full 玻璃壳」时随整套 full 形态删除，
    根因是规则不带门槛、在共享 /chat 上先画空玻璃再塌成 compact（闪帧）。现在 full 是
    独立窗口（/chat_full + data-chat-surface-mode="full" + 运行时标记 neko-electron-
    runtime），用与 inset 几何**完全相同的门槛**把玻璃视觉挂回——只命中 Electron full
    shell，compact 与 web /chat_full 都不命中，故不再有闪帧。
    """
    template = CHAT_TEMPLATE_PATH.read_text(encoding="utf-8")

    # 三色流光关键帧已复活（被下方 ::after 引用）。
    assert "@keyframes liquidFlow" in template
    # liquidFlow 只被那一个带门槛的 ::after 消费，别处不得再引用。
    assert template.count("animation: liquidFlow") == 1

    # ::before 静态高光 + ::after 三色流光都只挂在 Electron full shell 门槛上。
    glass_gate = (
        'body.neko-electron-runtime '
        '#react-chat-window-shell[data-chat-surface-mode="full"]'
        ':not(.is-minimized):not(.neko-e-collapsed)'
    )
    assert glass_gate + "::before" in template      # 静态边缘/顶部高光
    assert glass_gate + "::after" in template        # 三色流光层

    # 玻璃边缘渐隐 mask 复活，且必须挂在与 ::before/::after 完全相同的 full gate 下：把不透明聊天
    # 内容（.chat-body rgba 0.91）四边淡出、露出 ::before 高光层 —— 这才是「玻璃」本体。#1695 只复活
    # 了浮在最上层的 ::after 流光，高光层被不透明内容盖住，故当时只有流光没有玻璃。裸 mask 会在共享
    # /chat 挂载前画→闪帧（#1594 删它的根因），所以这里断言 mask 只出现在 gated root 选择器上。
    assert glass_gate + " #react-chat-window-root {" in template
    assert "-webkit-mask-image" in template

    # 尊重系统「减少动态」：流光在 prefers-reduced-motion 下停掉。按花括号配平取整段 media
    # 块（而非定长切片），块体增长也不会让断言失真。
    reduced_idx = template.find("@media (prefers-reduced-motion: reduce)")
    assert reduced_idx != -1
    brace_start = template.index("{", reduced_idx)
    depth = 0
    block_end = brace_start
    for i in range(brace_start, len(template)):
        if template[i] == "{":
            depth += 1
        elif template[i] == "}":
            depth -= 1
            if depth == 0:
                block_end = i
                break
    media_block = template[reduced_idx:block_end + 1]
    assert "animation: none;" in media_block


def test_compact_history_resize_bar_is_draggable_and_persisted():
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")
    panel_source = COMPACT_EXPORT_HISTORY_PANEL_PATH.read_text(encoding="utf-8")
    app_source = REACT_CHAT_APP_PATH.read_text(encoding="utf-8")

    # 顶部 resize bar 的样式：可纵向拖、可接收 pointer、平时透明、不触发原生窗口拖动。
    bar_block = css_block(
        styles,
        ".compact-export-history-resize-bar {",
        ".compact-export-history-resize-bar::after",
    )
    assert "cursor: ns-resize;" in bar_block
    assert "pointer-events: auto;" in bar_block
    assert "-webkit-app-region: no-drag;" in bar_block
    # bar 本体不能透明：宿主几何收集器按 opacity<=0.01 丢弃 hit-region，透明会让 Electron 下鼠标穿透、点不到。
    assert "opacity: 0;" not in bar_block
    # 只让视觉抓手（伪元素）平时透明、hover/拖动显现。
    after_block = css_block(
        styles,
        ".compact-export-history-resize-bar::after {",
        ".compact-export-history-anchor:hover .compact-export-history-resize-bar::after",
    )
    assert "opacity: 0;" in after_block
    assert ".compact-export-history-resize-bar.is-active::after {" in styles
    # hover 整个历史区即浮现顶部高度 bar（回滚 fd138cd 的「调尺寸热区联动浮现」收窄，
    # 让整个历史记录气泡区域都能唤出 resize bar）。
    assert ".compact-export-history-anchor:hover .compact-export-history-resize-bar::after" in styles

    # bar 的 DOM：带可命中且不穿透的 hit-region，拖拽不触发面板 / 整窗拖动。
    assert "className={clsx('compact-export-history-resize-bar'" in panel_source
    # hit-region 受 historyInteractive && !choiceLayerAbove 双重 gate：choice prompt 压在历史上方时，
    # bar 不再上报命中区（与 scroll/controls 一起惰性，Electron 下不留可拖 / 不穿透的活条）。
    assert "data-compact-hit-region-id={historyInteractive && !choiceLayerAbove ? 'history:resize' : undefined}" in panel_source
    assert "data-compact-hit-region-kind={historyInteractive && !choiceLayerAbove ? 'resize' : undefined}" in panel_source
    resize_bar_jsx = panel_source.split("compact-export-history-resize-bar", 1)[1].split("/>", 1)[0]
    assert 'data-compact-no-drag="true"' in resize_bar_jsx
    # under-choice 态把 bar 一并纳入 pointer-events:none / 淡化规则。
    assert ".compact-export-history-anchor.under-choice-prompt .compact-export-history-resize-bar," in styles
    # 纯点击不落库：finish 仅在 moved 时 persist。
    assert "if (phase !== 'cancel' && resizeState.heightChanged && resizeState.lastHeight !== resizeState.initialHeight) {" in app_source

    # 高度上限写预留覆盖变量 + 独立 localStorage key；变更后触发宿主几何重算（Electron 窗口联动）。
    assert "COMPACT_HISTORY_HEIGHT_STORAGE_KEY = 'neko.reactChatWindow.compactHistorySlotHeight'" in app_source
    assert "'--compact-history-slot-height'" in app_source
    assert "new CustomEvent('neko:compact-interaction-geometry-refresh')" in app_source


def test_compact_history_controls_collapse_gives_height_back_to_history_scroll():
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")

    anchor_block = css_block(
        styles,
        ".compact-export-history-anchor {",
        "body.electron-chat-window.subtitle-web-host .compact-export-history-anchor",
    )
    collapsed_anchor_block = css_block(
        styles,
        ".compact-export-history-anchor.controls-collapsed {",
        "body.electron-chat-window.subtitle-web-host .compact-export-history-anchor",
    )
    scroll_block = css_block(styles, ".compact-export-history-scroll {", ".compact-export-history-scroll-content")
    controls_block = css_block(
        styles,
        ".compact-export-history-controls {",
        ".compact-export-history-controls-content",
    )
    history_handle_block = css_block(
        styles,
        ".compact-history-visibility-handle {",
        ".compact-history-visibility-handle::before",
    )
    control_button_block = css_block(styles, ".compact-export-history-control {", ".compact-export-history-control:hover")

    assert "Geometry-critical" in anchor_block
    assert "--compact-export-controls-padding-y:" in anchor_block
    assert "--compact-export-controls-action-height:" in anchor_block
    assert "--compact-export-controls-collapsed-toggle-height:" in anchor_block
    assert "--compact-export-controls-expanded-block-size: calc(" in anchor_block
    assert "--compact-export-controls-collapsed-block-size: calc(" in anchor_block
    assert "--compact-export-controls-collapse-delta: 0px;" in anchor_block
    assert "--compact-export-controls-collapse-delta: var(--compact-export-controls-expanded-block-size);" in collapsed_anchor_block
    assert "24px" not in collapsed_anchor_block
    assert (
        "height: calc(var(--compact-export-history-region-height) + "
        "var(--compact-export-controls-collapse-delta));"
    ) in scroll_block
    assert (
        "max-height: calc(var(--compact-export-history-region-height) + "
        "var(--compact-export-controls-collapse-delta));"
    ) in scroll_block
    assert "height: 40px;" not in controls_block
    assert "min-height: 40px;" not in controls_block
    assert "padding: var(--compact-export-controls-padding-y) 6px;" in controls_block
    assert "padding 0.16s ease" not in controls_block
    assert ".compact-export-history-controls.is-collapsed" not in styles
    assert ".compact-export-history-controls-toggle" not in styles
    assert "position: fixed;" in history_handle_block
    assert "--compact-history-handle-line-width: clamp(38px, calc(var(--compact-history-handle-surface-width) * 0.102), 50px);" in history_handle_block
    assert ".compact-history-visibility-handle.is-open {" in history_handle_block
    assert "--compact-history-handle-line-width: 100%;" in history_handle_block
    assert "top: calc(var(--desktop-compact-surface-top, var(--compact-surface-top, 68vh)) + 8px);" in history_handle_block
    assert "bottom: auto;" in history_handle_block
    assert "bottom: calc(100vh - var(--desktop-compact-surface-top" not in history_handle_block
    assert "transform: translate(-50%, -50%);" in history_handle_block
    assert "z-index: 100002;" in history_handle_block
    assert "pointer-events: auto;" in history_handle_block
    assert "-webkit-app-region: no-drag;" in history_handle_block
    assert ".compact-history-visibility-handle-triangle {\n  display: none;\n}" in styles
    assert "height: var(--compact-export-controls-action-height);" in control_button_block


def test_compact_history_layout_contract_avoids_jitter_feedback():
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")

    anchor_block = css_block(
        styles,
        ".compact-export-history-anchor {",
        "body.electron-chat-window.subtitle-web-host .compact-export-history-anchor",
    )
    panel_block = css_block(styles, ".compact-export-history-panel {", ".compact-export-history-anchor.under-choice-prompt")
    scroll_block = css_block(styles, ".compact-export-history-scroll {", ".compact-export-history-scroll-content")
    controls_block = css_block(
        styles,
        ".compact-export-history-controls {",
        ".compact-export-history-controls-content",
    )
    preview_block = css_block(
        styles.split(".compact-export-preview-region[hidden]", 1)[1],
        ".compact-export-preview-region {",
        ".compact-export-preview-header",
    )

    assert "transition:" not in anchor_block
    assert "transition:" not in panel_block
    assert "transition:" not in scroll_block
    assert_no_layout_transition(controls_block)
    assert "transition:" not in preview_block

    assert "height: calc(var(--compact-export-history-region-height)" in scroll_block
    assert "max-height: calc(var(--compact-export-history-region-height)" in scroll_block
    assert "height: var(--compact-export-preview-region-height);" in preview_block
    assert "max-height: var(--compact-export-preview-region-height);" in preview_block
    assert "height: var(--compact-export-history-region-height);" not in preview_block
    assert "max-height: var(--compact-export-history-region-height);" not in preview_block
    assert "max-height: inherit;" in panel_block


def test_compact_history_reduced_motion_closing_hides_immediately():
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")

    reduced_motion_block = styles.rsplit("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert '.compact-input-tool-fan[data-compact-tool-wheel-charge-rattle="weak"]' in reduced_motion_block
    assert '.compact-input-tool-fan[data-compact-tool-wheel-charge-rattle="strong"]' in reduced_motion_block
    rattle_block = css_block(
        reduced_motion_block,
        '.compact-input-tool-fan[data-compact-tool-wheel-charge-rattle="weak"],',
        ".compact-input-tool-fan .compact-input-tool-tooltip",
    )
    assert "animation: none !important;" in rattle_block

    closing_block = css_block(
        reduced_motion_block,
        '.compact-export-history-anchor[data-compact-export-history-visibility="closing"] {',
        ".avatar-tool-visual-overlay-stage",
    )

    assert "opacity: 0 !important;" in closing_block
    assert "visibility: hidden !important;" in closing_block


def test_avatar_tool_visuals_stay_above_model_side_menus():
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")
    popup_source = AVATAR_UI_POPUP_PATH.read_text(encoding="utf-8")

    avatar_tool_visual_layer = css_z_index(css_block(
        styles,
        ".avatar-tool-visual-overlay {",
        ".avatar-tool-visual-overlay.is-compact",
    ))
    avatar_tool_impact_layer = css_z_index(css_block(
        styles,
        ".avatar-tool-impact-effect {",
        ".avatar-tool-impact-effect.is-compact",
    ))
    model_popup_layer = css_z_index(css_block(
        popup_source,
        ".${prefix}-popup {",
        ".${prefix}-popup.is-positioning",
    ))
    model_side_panel_layer = inline_z_index(
        popup_source.split("function createSidePanelContainer", 1)[1].split(
            "const stopEventPropagation",
            1,
        )[0]
    )
    interval_side_panel_layer = inline_z_index(
        popup_source.split("container.setAttribute('data-neko-sidepanel-type', `interval-${toggle.id}`);", 1)[1].split(
            "const stopEventPropagation",
            1,
        )[0]
    )
    max_model_menu_layer = max(model_popup_layer, model_side_panel_layer, interval_side_panel_layer)

    assert avatar_tool_visual_layer > max_model_menu_layer
    assert avatar_tool_impact_layer > max_model_menu_layer


def test_avatar_popup_actions_have_stable_input_region_markers():
    source = AVATAR_UI_POPUP_PATH.read_text(encoding="utf-8")

    assert "function markAvatarPopupActionElement(el, type)" in source
    assert "function setAvatarPopupActionDebugMetadata(el, item, source)" in source
    assert "data-neko-avatar-popup-action" in source
    assert "data-neko-avatar-popup-item-id" in source
    assert "data-neko-avatar-popup-url" in source
    assert "function dispatchAvatarPopupLifecycleEvent(eventName, buttonId, popup, prefix)" in source
    assert "function dispatchAvatarPopupNavigateEvent(item, finalUrl, windowName, source)" in source
    assert "neko-avatar-popup-opening" in source
    assert "neko-avatar-popup-opened" in source
    assert "neko-avatar-popup-closing" in source
    assert "neko-avatar-popup-closed" in source
    assert "neko-avatar-popup-navigate" in source
    assert "markAvatarPopupActionElement(btn, 'settings-menu');" in source
    assert "setAvatarPopupActionDebugMetadata(btn, config, 'settings-button');" in source
    assert "markAvatarPopupActionElement(menuItem, 'sidepanel-menu');" in source
    assert "setAvatarPopupActionDebugMetadata(menuItem, item, 'sidepanel-menu');" in source
    assert "markAvatarPopupActionElement(linkItem, 'settings-link');" in source
    assert "markAvatarPopupActionElement(toggleItem, 'settings-toggle');" in source
    assert "markAvatarPopupActionElement(menuItem, isSubmenuItem ? 'settings-submenu' : 'settings-menu');" in source
    assert "setAvatarPopupActionDebugMetadata(menuItem, item, isSubmenuItem ? 'settings-submenu' : 'settings-menu');" in source


def test_avatar_popup_positioning_uses_niri_physical_crop_coordinates_only_when_available():
    source = AVATAR_POPUP_COMMON_PATH.read_text(encoding="utf-8")
    position_popup_block = source.split("function positionPopup(popup, options = {})", 1)[1].split(
        "function getButtonZone",
        1,
    )[0]
    position_sidepanel_block = source.split("function positionSidePanel(container, anchor, options = {})", 1)[1].split(
        "window.AvatarPopupUI =",
        1,
    )[0]

    assert "function getNiriPetPhysicalCropPlacementApi()" in source
    assert "window.__nekoNiriPetPhysicalCrop" in source
    assert "return api.isActive() ? api : null;" in source
    assert "const placementApi = niriViewport ? niriCropApi : null;" in position_popup_block
    assert "toPlacementRect(popup.getBoundingClientRect(), placementApi)" in position_popup_block
    assert "const screenWidth = niriViewport ? niriViewport.width : window.innerWidth;" in position_popup_block
    assert "try {\n            const state = api.getState();" in source
    assert "try {\n            const virtualRect = api.toVirtualRect({" in source
    assert "catch (_) {\n            return normalized;" in source
    assert "container.dataset.niriPhysicalCropPositioned = 'true';" in position_sidepanel_block
    assert (
        "const goLeft = isNiriPetPhysicalCrop\n"
        "            ? false\n"
        "            : (popup ? (popup.dataset.opensLeft === 'true' || !popup.dataset.opensLeft) : true);"
    ) in position_sidepanel_block
    assert "const popupRect = toPlacementRect" in position_sidepanel_block
    assert "placementApi)" in position_sidepanel_block
    niri_early_return_block = position_sidepanel_block.split("if (isNiriPetPhysicalCrop) {", 1)[1].split("}", 1)[0]
    assert "return;" in niri_early_return_block

    ui_source = AVATAR_UI_POPUP_PATH.read_text(encoding="utf-8")
    assert "container.dataset.niriPhysicalCropPositioned === 'true' && hasPositionStyles" in ui_source


def test_compact_history_closing_bubbles_disable_pointer_events():
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")

    closing_bubble_block = css_block(
        styles,
        '.compact-export-history-anchor[data-compact-export-history-visibility="closing"] .compact-export-history-bubble {',
        ".compact-export-history-message.is-disabled",
    )

    assert "animation: compact-history-message-exit" in closing_bubble_block
    assert "pointer-events: none;" in closing_bubble_block


def test_compact_history_bubbles_are_text_selectable_and_drag_free():
    """拖到 avatar 子系统已删除：气泡不再捕获指针，文本可被鼠标选中复制。

    断言气泡 CSS 显式声明 user-select:text / cursor:text（覆盖 [role=\"button\"]
    的 user-select:none），且气泡 JSX 不再挂任何指针拖拽 handler（只留 onClick /
    onKeyDown 驱动导出勾选）。"""
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")
    panel_source = COMPACT_EXPORT_HISTORY_PANEL_PATH.read_text(encoding="utf-8")

    bubble_block = css_block(
        styles,
        ".compact-export-history-bubble {",
        ".compact-export-history-anchor[data-compact-export-history-visibility=\"open\"]",
    )
    assert "user-select: text;" in bubble_block
    assert "-webkit-user-select: text;" in bubble_block
    assert "cursor: text;" in bubble_block
    assert "cursor: pointer;" not in bubble_block

    # 拖拽 CSS 全部移除。
    assert "compact-history-drag-layer" not in styles
    assert "data-compact-history-drag" not in styles

    # 气泡 JSX 只保留 onClick / onKeyDown，不再有拖拽指针 handler。
    bubble_jsx = panel_source.split('className="compact-export-history-bubble"', 1)[1].split(
        "compact-export-history-check",
        1,
    )[0]
    assert "onClick={(event) => handleClick(event, message, selectable)}" in bubble_jsx
    assert "onKeyDown={(event) => handleKeyDown(event, message, selectable)}" in bubble_jsx
    assert "onPointerDown" not in bubble_jsx
    assert "onPointerMove" not in bubble_jsx
    assert "onPointerUp" not in bubble_jsx
    assert "handlePointerDown" not in panel_source
    assert "startCompactHistoryDrag" not in panel_source


def test_compact_history_hit_contract_keeps_transparent_wrappers_out_of_hit_regions():
    styles = REACT_CHAT_STYLES_PATH.read_text(encoding="utf-8")
    music_ui_styles = MUSIC_UI_CSS_PATH.read_text(encoding="utf-8")
    panel_source = COMPACT_EXPORT_HISTORY_PANEL_PATH.read_text(encoding="utf-8")
    app_source = REACT_CHAT_APP_PATH.read_text(encoding="utf-8")
    script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")
    music_ui_source = MUSIC_UI_PATH.read_text(encoding="utf-8")

    anchor_block = css_block(
        styles,
        ".compact-export-history-anchor {",
        "body.electron-chat-window.subtitle-web-host .compact-export-history-anchor",
    )
    panel_block = css_block(styles, ".compact-export-history-panel {", ".compact-export-history-anchor.under-choice-prompt")
    scroll_block = css_block(styles, ".compact-export-history-scroll {", ".compact-export-history-scroll-content")
    scrollbar_hit_block = css_block(styles, ".compact-export-history-scrollbar-hit {", ".compact-export-history-message")
    content_block = css_block(
        styles,
        ".compact-export-history-scroll-content {",
        ".compact-export-history-scroll-content > .compact-export-history-message:first-child",
    )
    message_block = css_block(styles, ".compact-export-history-message {", ".compact-export-history-message.is-user")
    bubble_block = css_block(styles, ".compact-export-history-bubble {", ".compact-export-history-message.is-disabled")
    shared_hit_block = css_block(
        styles,
        ".compact-export-history-controls,\n.compact-export-preview-region {",
        ".compact-export-history-controls {",
    )
    scroll_jsx_block = panel_source.split('className="compact-export-history-scroll"', 1)[1].split(
        'className="compact-export-history-scroll-content"',
        1,
    )[0]
    message_hit_block = panel_source.split('className="compact-export-history-bubble"', 1)[1].split(
        'compact-export-history-check',
        1,
    )[0]
    controls_hit_block = panel_source.split('className="compact-export-history-controls"', 1)[1].split(
        'compact-export-history-controls-content',
        1,
    )[0]
    assert "pointer-events: none;" in anchor_block
    assert "pointer-events: none;" in panel_block
    assert "overflow-y: auto;" in scroll_block
    assert "pointer-events: none;" in scroll_block
    assert "position: absolute;" in scrollbar_hit_block
    assert "width: 20px;" in scrollbar_hit_block
    assert "pointer-events: auto;" in scrollbar_hit_block
    assert "pointer-events: none;" in content_block
    assert "pointer-events: none;" in message_block
    assert "pointer-events: auto;" in bubble_block
    assert ".compact-export-history-controls,\n.compact-export-preview-region {" in styles
    assert "pointer-events: auto;" in shared_hit_block
    assert "function getCompactHistoryScrollbarRect(element, parentRect)" in script
    assert "id: 'history:scrollbar'" in script
    assert "scrollNode.getAttribute('data-compact-scrollbar-visible') !== 'true'" in script
    assert "element.querySelector('.compact-export-history-scrollbar-hit')" in script
    scrollbar_block = script.split("function getCompactHistoryScrollbarRect(element, parentRect)", 1)[1].split(
        "var COMPACT_TOOL_FAN_CIRCLE_SLICE_COUNT",
        1,
    )[0]
    assert "if (hitStyle && hitStyle.pointerEvents === 'none') return null;" in scrollbar_block
    assert "style.pointerEvents === 'none'" not in scrollbar_block
    assert "data-compact-hit-region={historyInteractive && !choiceLayerAbove ? 'true' : undefined}" in scroll_jsx_block
    assert "data-compact-hit-region-id={historyInteractive && !choiceLayerAbove ? 'history:scroll' : undefined}" in scroll_jsx_block
    assert "data-compact-hit-region-kind={historyInteractive && !choiceLayerAbove ? 'scroll' : undefined}" in scroll_jsx_block
    assert 'className="compact-export-history-scrollbar-hit"' in panel_source
    assert 'data-compact-scrollbar-hit="true"' in panel_source
    assert "hasPointerCapture?.(event.pointerId)" in panel_source
    assert 'data-compact-hit-region-id={historyInteractive ? `history:message:${message.id}` : undefined}' in panel_source
    assert "data-compact-hit-region={historyInteractive ? 'true' : undefined}" in message_hit_block
    assert "data-compact-hit-region-kind={historyInteractive ? 'message' : undefined}" in message_hit_block
    assert "data-compact-hit-region-id={historyInteractive ? 'history:controls' : undefined}" in panel_source
    assert "data-compact-hit-region={historyInteractive ? 'true' : undefined}" in controls_hit_block
    assert "data-compact-hit-region-kind={historyInteractive ? 'controls' : undefined}" in controls_hit_block
    assert "compact-export-history-music-mount" not in panel_source
    assert 'className="compact-music-player-mount"' in app_source
    assert 'data-music-player-mount="compact-surface"' in app_source
    assert 'data-compact-music-player-visibility={compactMusicPlayerVisibility}' in app_source
    assert "aria-hidden={compactMusicPlayerVisibility === 'open' ? undefined : true}" in app_source
    assert 'data-compact-geometry-item="musicPlayer"' in app_source
    assert 'data-compact-geometry-hit-scope="children"' in app_source
    assert "function getPreferredMusicMountTarget(" in music_ui_source
    assert "function getCompactMusicMountTarget()" in music_ui_source
    assert "document.querySelector('[data-music-player-mount=\"compact-surface\"]')" in music_ui_source
    assert "document.querySelectorAll('#music-player-mount')" in music_ui_source
    assert "document.getElementById(MUSIC_CONFIG.dom.containerId)" in music_ui_source
    assert "mutation.type === 'attributes' && isMusicMountMutationTarget(mutation.target)" in music_ui_source
    assert "function isCompactMusicGeometryMutationTarget(node)" in music_ui_source
    assert "node.closest('[data-music-player-mount=\"compact-surface\"]')" in music_ui_source
    assert "node.classList.contains('music-bar-volume-container')" in music_ui_source
    assert "node.classList.contains('music-bar-volume-slider-wrapper')" in music_ui_source
    assert "mutation.type === 'attributes' && isCompactMusicGeometryMutationTarget(mutation.target)" in music_ui_source
    assert "attributes: true" in music_ui_source
    assert "'data-music-player-mount'" in music_ui_source
    assert "mountMusicBar(musicBar)" in music_ui_source
    assert "musicBar.setAttribute('data-compact-hit-region-id', 'music-player')" in music_ui_source
    assert "neko:compact-interaction-geometry-refresh" in music_ui_source
    volume_toggle_segments = music_ui_source.split("volumeContainer.classList.toggle('expanded');")
    assert len(volume_toggle_segments) == 3
    for segment in volume_toggle_segments[1:]:
        assert "requestCompactMusicGeometrySync();" in segment[:96]
    volume_close_segments = music_ui_source.split("volumeContainer.classList.remove('expanded');")
    assert len(volume_close_segments) == 3
    for segment in volume_close_segments[1:]:
        assert "requestCompactMusicGeometrySync();" in segment[:96]
    assert "window.addEventListener('neko:compact-interaction-geometry-refresh'" in script
    assert "kind === 'musicPlayer'" in script
    assert music_ui_source.count('data-compact-hit-region-id="music-player:volume"') == 2
    assert music_ui_source.count('data-compact-hit-region-kind="music-volume"') == 2
    assert "#music-player-mount.compact-music-player-mount {" in styles
    assert "#music-player-mount.compact-music-player-mount {" in music_ui_styles
    assert '#music-player-mount.compact-music-player-mount[data-compact-music-player-visibility="closing"]' in styles
    assert '#music-player-mount.compact-music-player-mount[data-compact-music-player-visibility="closed"]' in styles
    assert "visibility: hidden;" in css_block(
        styles,
        '#music-player-mount.compact-music-player-mount[data-compact-music-player-visibility="closed"] {',
        '#music-player-mount.compact-music-player-mount[data-compact-music-player-visibility="closing"] *',
    )
    assert "width: min(calc(var(--compact-music-player-surface-width) - 16px), 360px, calc(100vw - 24px));" in styles
    assert "width: min(calc(var(--compact-music-player-surface-width) - 16px), 360px, calc(100vw - 24px));" in music_ui_styles
    assert "#music-player-mount.compact-music-player-mount > .music-player-bar" in styles
    assert "#music-player-mount.compact-music-player-mount > .music-player-bar" in music_ui_styles
    assert "max-inline-size: 100%;" in styles
    assert "max-inline-size: 100%;" in music_ui_styles
    assert "#music-player-mount.compact-music-player-mount .music-bar-info" in styles
    assert "#music-player-mount.compact-music-player-mount .music-bar-info" in music_ui_styles
    assert "--compact-music-player-reserved-block-size: 88px;" in styles
    assert "--compact-music-player-history-gap: 14px;" in styles
    assert "+ var(--compact-music-player-reserved-block-size, 88px)" in styles
    assert "+ var(--compact-music-player-history-gap, 14px)" in styles
    assert ".app-shell:has(> .compact-music-player-mount:not(:empty)) .compact-export-history-anchor" in styles
    # 音量弹层展开时滑块向上伸进 history 区域，播放器整体提到 history(100001) 之上，
    # 但仍低于工具轮盘态 surface-shell(100003)，避免抢点击又不破坏工具轮盘对表情按钮的让位。
    assert "z-index: 100002;" in css_block(
        styles,
        "#music-player-mount.compact-music-player-mount:has(.music-bar-volume-container.expanded) {",
        "#music-player-mount.compact-music-player-mount:empty",
    )
    assert 'data-compact-hit-region-id="history:preview"' in panel_source


def test_subtitle_web_host_keeps_compact_history_transparent_wrappers_click_through():
    styles = STATIC_INDEX_CSS_PATH.read_text(encoding="utf-8")

    compact_surface_prefix = (
        'body.subtitle-web-host #react-chat-window-shell[data-chat-surface-mode="compact"]:not(.is-minimized):'
        'not(.is-collapsing):not(.is-expanding)'
    )
    broad_surface_rule = (
        f'{compact_surface_prefix} [data-compact-geometry-owner="surface"],\n'
        f'{compact_surface_prefix} [data-compact-geometry-owner="surface"] *,\n'
        f'{compact_surface_prefix} #reactChatWindowMinimizeButton,\n'
        f'{compact_surface_prefix} #reactChatWindowMinimizeButton * {{\n'
        "    pointer-events: auto;\n"
        "}"
    )
    tool_fan_passthrough_rule = (
        f'{compact_surface_prefix} #react-chat-window-root .compact-input-tool-fan[data-compact-input-tool-fan-open="true"] {{\n'
        "    pointer-events: none !important;\n"
        "}"
    )
    visible_tool_fan_interactive_rule = (
        f'{compact_surface_prefix} #react-chat-window-root .compact-input-tool-fan[data-compact-input-tool-fan-open="true"] .compact-input-tool-fan-hit-region,\n'
        f'{compact_surface_prefix} #react-chat-window-root .compact-input-tool-fan[data-compact-input-tool-fan-open="true"] .compact-input-tool-item:not([data-compact-tool-wheel-slot="hidden"]):not([data-compact-tool-wheel-slot="hidden-forward"]):not([data-compact-tool-wheel-slot="hidden-backward"]),\n'
        f'{compact_surface_prefix} #react-chat-window-root .compact-input-tool-fan[data-compact-input-tool-fan-open="true"] .avatar-tool-quickbar,\n'
        f'{compact_surface_prefix} #react-chat-window-root .compact-input-tool-fan[data-compact-input-tool-fan-open="true"] .avatar-tool-quickbar * {{\n'
        "    pointer-events: auto !important;\n"
        "}"
    )
    hidden_tool_fan_slots_rule = (
        f'{compact_surface_prefix} #react-chat-window-root .compact-input-tool-fan[data-compact-input-tool-fan-open="true"] .compact-input-tool-item[data-compact-tool-wheel-slot="hidden"],\n'
        f'{compact_surface_prefix} #react-chat-window-root .compact-input-tool-fan[data-compact-input-tool-fan-open="true"] .compact-input-tool-item[data-compact-tool-wheel-slot="hidden-forward"],\n'
        f'{compact_surface_prefix} #react-chat-window-root .compact-input-tool-fan[data-compact-input-tool-fan-open="true"] .compact-input-tool-item[data-compact-tool-wheel-slot="hidden-backward"] {{\n'
        "    pointer-events: none !important;\n"
        "}"
    )
    visible_music_tool_fan_cutout_rule = (
        f'{compact_surface_prefix} #react-chat-window-root .app-shell:has(> #music-player-mount.compact-music-player-mount > .music-player-bar:not([hidden])) .compact-input-tool-fan[data-compact-input-tool-fan-open="true"] .compact-input-tool-fan-hit-region {{\n'
        "    clip-path: polygon(0 0, 100% 0, 100% 100%, 0 100%, 0 var(--compact-tool-wheel-music-control-cutout-height), var(--compact-tool-wheel-music-control-cutout-width) var(--compact-tool-wheel-music-control-cutout-height), var(--compact-tool-wheel-music-control-cutout-width) 0);\n"
        "}"
    )
    compact_music_interactive_rule = (
        f'{compact_surface_prefix} #music-player-mount,\n'
        f'{compact_surface_prefix} #music-player-mount * {{\n'
        "    pointer-events: auto;\n"
        "}"
    )
    compact_music_hidden_rule = (
        f'{compact_surface_prefix} #music-player-mount.compact-music-player-mount[data-compact-music-player-visibility="closing"],\n'
        f'{compact_surface_prefix} #music-player-mount.compact-music-player-mount[data-compact-music-player-visibility="closing"] *,\n'
        f'{compact_surface_prefix} #music-player-mount.compact-music-player-mount[data-compact-music-player-visibility="closed"],\n'
        f'{compact_surface_prefix} #music-player-mount.compact-music-player-mount[data-compact-music-player-visibility="closed"] * {{\n'
        "    pointer-events: none;\n"
        "}"
    )
    history_passthrough_rule = (
        f'{compact_surface_prefix} .compact-export-history-anchor,\n'
        f'{compact_surface_prefix} .compact-export-history-panel,\n'
        f'{compact_surface_prefix} .compact-export-history-scroll,\n'
        f'{compact_surface_prefix} .compact-export-history-scroll-content,\n'
        f'{compact_surface_prefix} .compact-export-history-message {{\n'
        "    pointer-events: none;\n"
        "}"
    )
    history_interactive_rule = (
        f'{compact_surface_prefix} .compact-export-history-bubble,\n'
        f'{compact_surface_prefix} .compact-export-history-controls,\n'
        f'{compact_surface_prefix} .compact-export-preview-region {{\n'
        "    pointer-events: auto;\n"
        "}"
    )

    assert broad_surface_rule in styles
    assert tool_fan_passthrough_rule in styles
    assert visible_tool_fan_interactive_rule in styles
    assert hidden_tool_fan_slots_rule in styles
    assert visible_music_tool_fan_cutout_rule in styles
    assert compact_music_interactive_rule in styles
    assert compact_music_hidden_rule in styles
    assert history_passthrough_rule in styles
    assert history_interactive_rule in styles
    assert styles.index(broad_surface_rule) < styles.index(compact_music_interactive_rule)
    assert styles.index(compact_music_interactive_rule) < styles.index(compact_music_hidden_rule)
    assert styles.index(compact_music_hidden_rule) < styles.index(history_passthrough_rule)
    assert styles.index(history_passthrough_rule) < styles.index(history_interactive_rule)
    assert styles.index(broad_surface_rule) < styles.index(tool_fan_passthrough_rule)
    assert styles.index(tool_fan_passthrough_rule) < styles.index(visible_tool_fan_interactive_rule)
    assert styles.index(visible_tool_fan_interactive_rule) < styles.index(hidden_tool_fan_slots_rule)
    assert ".compact-export-history-scroll,\n" in history_passthrough_rule


def test_compact_inline_export_uses_windowless_app_chat_export_api():
    script = APP_CHAT_EXPORT_PATH.read_text(encoding="utf-8")

    translate_block = script.split("function translateText(key, fallback, params)", 1)[1].split(
        "function translateLabel(key, fallback)",
        1,
    )[0]

    assert "typeof translated === 'string' || typeof translated === 'number'" in translate_block
    assert "return String(translated);" in translate_block

    assert "getCompactInlineOptions: getCompactInlineExportOptions" in script
    assert "buildCompactInlinePreview: buildCompactInlinePreview" in script
    assert "copyCompactInlineSelection: copyCompactInlineSelection" in script
    assert "downloadCompactInlineSelection: downloadCompactInlineSelection" in script

    compact_api_block = script.split("// ======================== Compact inline API ========================", 1)[1].split(
        "// ======================== Action handlers ========================",
        1,
    )[0]

    assert "state.allMessages = getReactMessages();" in compact_api_block
    assert "state.selectedIds = selectedIds;" in compact_api_block
    assert "selectedIds.size >= MAX_EXPORT_SELECTION" in compact_api_block
    assert "normalizeExportFormatId(opts.format)" in compact_api_block
    assert "normalizeImageExportStyleId(opts.imageStyle)" in compact_api_block
    assert "normalizeImageExportFormatId(opts.imageFormat)" in compact_api_block
    assert "function buildCompactInlinePreview(options)" in compact_api_block
    assert "buildExportDocument(entries, state.exportFormat)" in compact_api_block
    assert "URL.createObjectURL(exportData.previewBlob)" in compact_api_block
    assert "previewKind: 'document'," in compact_api_block
    assert "previewDocument: buildMarkdownPreviewDocument(exportData.content)" in compact_api_block
    assert "getOrBuildPreviewPayload" not in compact_api_block
    assert "clearPreviewCache()" not in compact_api_block
    assert "function runCompactInlineExportAction(options, action)" in compact_api_block
    assert "state.exportFormat = previous.exportFormat;" in compact_api_block
    assert "buildExportDocument(entries, 'image')" in compact_api_block
    assert "copyImageToClipboard(imgBlob)" in compact_api_block
    assert "buildExportDocument(entries, 'markdown')" in compact_api_block
    assert "copyTextToClipboard(markdownData.content)" in compact_api_block
    assert "downloadExportFile(data.fileName, data.content, data.contentType, window)" in compact_api_block
    assert "handleCopyClick" not in compact_api_block
    assert "handleDownloadClick" not in compact_api_block
    assert "openExportPreviewWindow" not in compact_api_block
    assert "window.open" not in compact_api_block


def test_chat_export_keeps_meme_and_music_as_media():
    script = APP_CHAT_EXPORT_PATH.read_text(encoding="utf-8")

    plain_text_block = script.split("function extractBlocksPlainText(message)", 1)[1].split(
        "function blocksToMarkdown(message)",
        1,
    )[0]
    assert "if (memeOnly && block.type !== 'image') return;" in plain_text_block
    assert "if (musicOnly && !isMusicExportBlock(message, block)) return;" in plain_text_block
    assert "if (block.title) parts.push(String(block.title));" in plain_text_block
    assert "if (block.description) parts.push(String(block.description));" in plain_text_block

    selection_list_block = script.split("function renderSelectionList()", 1)[1].split(
        "function renderControls()",
        1,
    )[0]
    assert "extractBlocksPlainText(message)" in selection_list_block
    assert "extractBlocksPlainText(message.blocks)" not in selection_list_block

    markdown_block = script.split("function blocksToMarkdown(message)", 1)[1].split(
        "function collectImageDescriptors(message)",
        1,
    )[0]
    assert "getMusicExportCover(block)" in markdown_block
    assert "String(block.url || '')" not in markdown_block.split("if (isMusicExportBlock(message, block))", 1)[1].split(
        "return;",
        1,
    )[0]

    media_block = script.split("function collectImageDescriptors(message)", 1)[1].split(
        "function buildExportEntry(message)",
        1,
    )[0]
    assert "fallbackSource: MUSIC_EXPORT_PLACEHOLDER_COVER" in media_block

    lyrics_block = script.split("async function renderLyricsStyleCanvas(resolvedEntries, now)", 1)[1].split(
        "async function renderImageCanvas(resolvedEntries, styleId, now)",
        1,
    )[0]
    assert "ctx.drawImage(image.image, textX, y, image.width, image.height);" in lyrics_block
    assert "translateLabel('chat.exportImageLabel'" not in lyrics_block


def test_compact_history_drop_payload_suppresses_real_send_in_voice_mode_only_at_host_send_boundary():
    script = APP_BUTTONS_PATH.read_text(encoding="utf-8")
    host_script = APP_REACT_CHAT_WINDOW_PATH.read_text(encoding="utf-8")

    assert "function shouldSuppressCompactHistoryDropSendForVoiceMode()" in script
    assert "window.shouldKeepVoiceComposerHidden()" in script
    assert "S.isRecording || S.voiceChatActive || S.voiceStartPending" in script
    assert "prepareCompactHistoryDropSubmit: prepareCompactHistoryDropSubmit" in host_script

    drop_block = script.split("async function sendCompactHistoryDropPayloadNow(payload)", 1)[1].split(
        "window.sendCompactHistoryDropPayload = mod.sendCompactHistoryDropPayload",
        1,
    )[0]
    guard = "if (shouldSuppressCompactHistoryDropSendForVoiceMode()) {\n            return true;\n        }"
    assert guard in drop_block
    assert drop_block.index(guard) < drop_block.index("var normalizedImages = [];")
    assert drop_block.index(guard) < drop_block.index("mod.sendTextPayload(text,")
    assert "compactHistoryDropPayloadQueue = Promise.resolve();" in script
    assert "sendCompactHistoryDropPayloadNow(payload)" in script
    assert "prepareCompactHistoryDropSubmit" in drop_block


def test_chat_image_file_drop_uses_import_pipeline_and_blocks_browser_navigation():
    script = APP_BUTTONS_PATH.read_text(encoding="utf-8")

    assert "function getImageFilesFromFileList(fileList)" in script
    assert "return file instanceof File && (file.type === '' || isLikelyImageFile(file));" in script
    assert "!file.type ||" not in script
    assert "return /^files$/i.test(String(type || ''));" in script
    assert "mod.importImageFilesToPendingList = function importImageFilesToPendingList(files, options)" in script
    assert "return Promise.resolve({ succeeded: 0, failed: inputFiles.length });" in script
    assert "window.t('app.importImagePartial', { success: succeeded, failed: failed })" in script

    change_anchor = "input.addEventListener('change', function (event) {"
    change_block = script.split(change_anchor, 1)[1].split(
        "mod._importImageInput = input;",
        1,
    )[0]
    assert script.index("function getImageFilesFromFileList(fileList)") < script.index(change_anchor)
    assert script.index(change_anchor) < script.index(
        "mod.importImageFilesToPendingList = function importImageFilesToPendingList(files, options)"
    )
    assert "mod.importImageFilesToPendingList(files, { logPrefix: '[导入图片]' })" in change_block

    drag_block = script.split("document.addEventListener('dragover'", 1)[1].split(
        "document.addEventListener('drop'",
        1,
    )[0]
    drop_block = script.split("document.addEventListener('drop'", 1)[1].split(
        "mod.ensureImportImageInput();",
        1,
    )[0]

    assert "shouldHandleChatFileDrop(e)" in drag_block
    assert "e.preventDefault();" in drag_block
    assert "e.stopPropagation();" in drag_block
    assert "e.dataTransfer.dropEffect = isHomeTutorialInteractionLocked() ? 'none' : 'copy';" in drag_block

    assert "shouldHandleChatFileDrop(e)" in drop_block
    assert "e.preventDefault();" in drop_block
    assert "e.stopPropagation();" in drop_block
    assert "showHomeTutorialLockedToast();" in drop_block
    assert "mod.importImageFilesToPendingList(imageFiles, { logPrefix: '[拖放图片]' });" in drop_block
    assert "window.NekoAvatarDropParser" in drop_block
    assert "parser.parseFiles(otherFiles)" in drop_block
    assert "mod.sendAvatarDropPayload" in drop_block


def test_chat_composer_user_images_use_text_attachment_input_type():
    script = APP_BUTTONS_PATH.read_text(encoding="utf-8")

    helper_block = script.split("function getPendingAttachmentInputType(item)", 1)[1].split(
        "mod.syncPendingComposerAttachments",
        1,
    )[0]
    assert "source === 'user-image'" in helper_block
    assert "source === 'clipboard-image'" in helper_block
    assert "source === 'compact-history'" in helper_block
    assert "return 'user_image';" in helper_block
    assert "return U.isMobile() ? 'camera' : 'screen';" in helper_block

    import_block = script.split(
        "mod.importImageFileToPendingList = function importImageFileToPendingList(file)",
        1,
    )[1].split(
        "mod.importImageFilesToPendingList = function importImageFilesToPendingList(files, options)",
        1,
    )[0]
    paste_block = script.split("document.addEventListener('paste'", 1)[1].split(
        "document.addEventListener('dragover'",
        1,
    )[0]
    send_block = script.split("// Send screenshots first", 1)[1].split(
        "if (!isReactWindowSource)",
        1,
    )[0]

    assert "mod.addScreenshotToList(dataUrl, null, { source: 'user-image' });" in import_block
    assert "mod.addScreenshotToList(dataUrl, null, { source: 'clipboard-image' });" in paste_block
    assert "input_type: getPendingAttachmentInputType(screenshotItems[i])" in send_block
    assert "input_type: U.isMobile() ? 'camera' : 'screen'" not in send_block


def test_text_mode_screenshot_payload_always_tags_interaction_request():
    script = APP_BUTTONS_PATH.read_text(encoding="utf-8")

    screenshot_block = script.split("// Send screenshots first", 1)[1].split(
        "if (!isReactWindowSource)",
        1,
    )[0]
    text_block = script.split("// Then send text (if any)", 1)[1].split(
        "if (!options.preserveInputValue)",
        1,
    )[0]

    assert "request_id: requestId" in screenshot_block
    assert "if (text)" not in screenshot_block
    assert "msg.request_id = requestId" not in screenshot_block
    assert "request_id: requestId" in text_block
