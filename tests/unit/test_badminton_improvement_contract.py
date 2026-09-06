import json
import re
import shutil
import textwrap
from pathlib import Path

import pytest

from config.prompts import prompts_badminton
from main_routers import game_router, pages_router
from main_routers.game_router import balance as gr_balance
from main_routers.game_router import pregame as gr_pregame
from main_routers.game_router import runtime as gr_runtime
from tests.node_harness import run_node_script


ROOT = Path(__file__).resolve().parents[2]
BADMINTON_TEMPLATE = ROOT / "templates" / "badminton_demo.html"
BADMINTON_RACKET_SPRITE = ROOT / "static" / "game" / "games" / "badminton" / "images" / "badminton-racket-sprite.svg"
BADMINTON_INK_OVERLAY = ROOT / "static" / "game" / "games" / "badminton" / "images" / "yui-octopus-ink-overlay.png"
BADMINTON_EMOTES_DIR = ROOT / "static" / "game" / "games" / "badminton" / "images" / "emotes"
LOCALES_DIR = ROOT / "static" / "locales"


def _badminton_html() -> str:
    return BADMINTON_TEMPLATE.read_text(encoding="utf-8")


class _FakePageRequest:
    def __init__(self, query_params: dict | None = None):
        self.query_params = query_params or {}


class _FakeTemplates:
    def TemplateResponse(self, template_name: str, context: dict):
        return {"template_name": template_name, "context": context}


def _get_nested(payload: dict, dotted_key: str):
    node = payload
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


@pytest.mark.unit
def test_badminton_improvement_static_contract():
    html = _badminton_html()
    racket_sprite = BADMINTON_RACKET_SPRITE.read_text(encoding="utf-8")

    for expected in (
        "/static/i18n-i18next.js",
        "/static/game/system/game-audio-system.js",
        "/static/game/games/badminton/badminton-audio-config.js",
        'id="game-audio-controls"',
        'id="badminton-loading"',
        'role="status" aria-live="polite" aria-atomic="true"',
        'data-i18n="badminton.loading.title"',
        'data-i18n="badminton.loading.subtitle"',
        "function scheduleBadmintonLoadingDismiss(",
        "function hideBadmintonLoading()",
        "function setBadmintonLoadingProgress(",
        "function startBadmintonFakeProgress(",
        "function completeBadmintonLoading()",
        "function settleBadmintonLoadingPromise(",
        "function afterInitialPaint(",
        "function isBadmintonLoadingActive()",
        'id="badminton-loading-bar"',
        'id="badminton-loading-percent" aria-hidden="true"',
        "badminton-loading-progress",
        "markFirstFrameRendered();",
        'id="game-bgm-volume"',
        'id="game-sfx-volume"',
        'function _i18n(',
        "addBadmintonEventListener(window, 'localechange'",
        'id="bd-debug-panel"',
        "data-debug-distance",
        "data-debug-event",
        "function updateDebugReadout()",
        "window.BadmintonDemo =",
        "onEvent: function",
        "offEvent: function",
        "DUEL_DIFFICULTY",
        "function setDuelDifficulty(",
        "MOODS =",
        "function setMood(",
        "function syncBgm(",
        "function _bdResolvePlaylist(",
        "function resetSyncKey(",
        "badmintonGameAudio.resetSyncKey()",
        "badmintonGameAudio.sync(",
        "function scheduleAudioPreload",
        "function autoAdjustMood(",
        "function getPressureLine(",
        "function applyPreGameContext(",
        "function _badmintonGameMemoryPolicyPayload(",
        "game_memory_enabled",
        "loadGeneratedQuickLines",
        "userReplyProtectedUntil",
        "function buildGameSummary()",
        "game_summary",
        'data-tab="duel"',
        "function getFilteredLeaderboard(",
        "function drawStreakEffect(",
        "function drawFireBorder(",
        "function drawBackspinBall(",
        "THEMES =",
        "function cycleTheme(",
        "function checkSeasonalEasterEggs(",
        "function recordShotHistory(",
        "function showStatsPanel(",
        "function emitCardEvent(",
        "card_eligible",
        "firstTutorialShotGuaranteed",
    ):
        assert expected in html

    assert 'data-mode="horse"' not in html
    assert 'data-i18n="badminton.mode.horse"' not in html
    assert "requestedMode === 'horse'" not in html
    assert "nextMode === 'horse'" not in html
    assert 'data-mode="timed"' not in html
    assert 'data-i18n="badminton.mode.timed"' not in html
    assert "requestedMode === 'timed'" not in html
    assert "nextMode === 'timed'" not in html
    assert 'data-mode="spectator"' not in html
    assert 'data-mode="shooter"' not in html
    assert "#mode-switcher" not in html
    assert "modeSwitcher" not in html
    assert "switchBadmintonMode" not in html
    assert "updateModeSwitcher" not in html
    assert "YUI_PASSIVE_LINES_SHOOTER" not in html
    assert "function shouldCallLLMShooter(" not in html
    assert "return !!(badmintonLoading && !badmintonLoading.hidden) || isBadmintonStartOverlayActive();" in html
    assert "badmintonLoading.classList.contains('hide')" not in html
    assert "badmintonLoading.hidden = true;\n      window.__badmintonInitialLoadingHidden = true;" in html
    assert "setTimeout(finish, 5000);" in html
    assert "return settleBadmintonLoadingPromise(promise, 5000);" in html
    assert "if (badmintonLoadingProgress >= 92) {\n        clearInterval(badmintonLoadingTimer);" in html
    assert "自由练习" not in html
    assert "挥拍挑战" not in html
    assert 'viewBox="0 0 78 168"' in racket_sprite
    assert '<clipPath id="headClip">' in racket_sprite
    assert 'clip-path="url(#headClip)"' in racket_sprite
    assert 'M39 8 C23 8 13 22 13 42' in racket_sprite
    assert 'M39 78 L39 129' in racket_sprite


@pytest.mark.unit
def test_badminton_timed_mode_is_removed_from_template_runtime():
    html = _badminton_html()

    for removed in (
        "TIME_ATTACK_DURATION",
        "function isTimeAttackMode()",
        "currentMode === 'timed'",
        "_i18n('result.timed'",
        "_i18n('hud.timedTitle'",
        "_i18n('leaderboard.mode.timed'",
        "timedRemaining",
        "timedDeadline",
        "timedTimerStarted",
    ):
        assert removed not in html


@pytest.mark.unit
def test_badminton_horse_mode_is_removed_from_template_runtime():
    html = _badminton_html()

    for removed in (
        "HORSE_WORD",
        "function isHorseMode()",
        "function buildHorseStatePayload()",
        "function buildHorseFinalScorePayload(",
        "function startHorseNekoChallenge()",
        "function finishHorseShot(",
        "function horseLetters(",
        "function endHorseIfNeeded()",
        "currentMode === 'horse'",
        "game.horse",
        "horse_phase",
        "_i18n('result.horse'",
        "_i18n('hud.horseTitle'",
        "_i18n('debug.readout.horse'",
        "_i18n('lines.horse.",
    ):
        assert removed not in html


@pytest.mark.unit
def test_badminton_invite_character_request_uses_invited_lanlan_name():
    html = _badminton_html()

    assert "window.__nekoBadmintonQueryLanlanName = queryLanlan || '';" in html
    assert "lanlan_name: queryLanlan || ''" in html
    assert "lanlan_name: queryLanlan || 'badminton_demo'" not in html
    assert "var requestedLanlanName = String(window.__nekoBadmintonQueryLanlanName || '').trim();" in html
    assert "characterPath += '?lanlan_name=' + encodeURIComponent(requestedLanlanName);" in html
    assert "var charResp = await fetch(characterPath);" in html
    assert "var live2dPath = charData.live2d_path || '/static/yui-lolita/yui-lolita.model3.json';" in html
    assert "window.lanlan_config.model_type = 'live2d';" in html
    assert "window.lanlan_config.live3d_sub_type = '';" in html
    assert "await initLive2DAvatar(live2dPath);" in html


@pytest.mark.unit
def test_badminton_i18n_placeholder_token_avoids_jinja_braces():
    html = _badminton_html()

    assert "{% raw %}" in html
    assert "{% endraw %}" in html
    assert "return s.replace('{{' + k + '}}', String(params[k]));" not in html
    assert "var token = '{' + '{' + k + '}' + '}';" in html
    assert "return s.split(token).join(String(params[k]));" in html


@pytest.mark.unit
def test_badminton_hidden_tab_keeps_route_alive():
    html = _badminton_html()

    beforeunload_start = html.index("window.addEventListener('beforeunload', function () {")
    beforeunload_section = html[beforeunload_start:html.index("addBadmintonEventListener(window, 'localechange'", beforeunload_start)]
    assert "closeSpeechPlaybackStateBridge();" not in beforeunload_section
    assert "disposeBadmintonGame('beforeunload');" in beforeunload_section
    assert "var pageVisible = !document.hidden;" in html
    assert "visible: pageVisible" in html
    assert "pageVisible: pageVisible" in html
    assert "visibilityState: document.visibilityState || (pageVisible ? 'visible' : 'hidden')" in html
    assert "if (document.hidden) endRoute(true);" not in html


@pytest.mark.unit
def test_badminton_pagehide_and_exit_share_dispose_lifecycle():
    html = _badminton_html()

    assert "var badmintonGameDisposed = false;" in html
    assert "function disposeBadmintonGame(reason) {" in html
    assert "var routeEndAfterStart = pendingRouteStart ? Promise.resolve(pendingRouteStart).catch(function () { return null; }).then(function (res) {" in html
    assert "Promise.resolve(routeEndAfterStart).catch(function () {});" in html
    assert "cancelAnimationFrame(badmintonFrameRequestId);" in html
    assert "clearTimeout(badmintonHiddenFrameTimer);" in html
    assert "badmintonGameAudio.destroy();" in html
    assert "window.addEventListener('pagehide', function () {" in html

    close_start = html.index("function closeBadmintonWindow() {")
    close_section = html[close_start:html.index("function formatLeaderboardDate", close_start)]
    assert "disposeBadmintonGame('exit_button');" in close_section

    pagehide_start = html.index("window.addEventListener('pagehide', function () {")
    pagehide_section = html[pagehide_start:html.index("addBadmintonEventListener(window, 'localechange'", pagehide_start)]
    assert "disposeBadmintonGame('pagehide');" in pagehide_section


@pytest.mark.unit
def test_badminton_invite_launches_duel_mode_and_marks_started():
    html = _badminton_html()

    assert "launchedFromInvite" not in html
    assert "badmintonInviteRequired" not in html
    assert "var currentMode = 'duel';" in html
    assert "launchedFromInvite ? 'duel' : 'shooter'" not in html
    assert "gameStarted: true, game_started: true" in html


@pytest.mark.unit
def test_badminton_rejected_route_start_does_not_activate_frontend_route():
    html = _badminton_html()
    start = html.index("function startRoute() {")
    end = html.index("function startRouteAfterCharacterReady() {", start)
    start_route = html[start:end]

    assert "if (!res || !res.ok) {" in start_route
    assert "routeActive = false;" in start_route
    assert start_route.index("if (!res || !res.ok) {") < start_route.index("routeActive = true;")
    assert start_route.index("routeActive = true;") < start_route.index("heartbeatTimer = setInterval")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_badminton_demo_page_only_renders_shell_without_invite_gate(monkeypatch):
    monkeypatch.setattr(pages_router, "get_templates", lambda: _FakeTemplates())

    result = await pages_router.badminton_demo(_FakePageRequest())

    assert result["template_name"] == "templates/badminton_demo.html"


@pytest.mark.unit
def test_badminton_restart_rotates_route_session():
    html = _badminton_html()

    assert "function createBadmintonSessionId() {" in html
    assert "var sessionId = window.__nekoMiniGameInviteSessionId || createBadmintonSessionId();" in html
    assert "sessionId = createBadmintonSessionId();" in html
    assert "resetVoiceArbiter();" in html
    assert "switchBadmintonMode" not in html
    assert "url.searchParams.delete('session_id');" not in html
    assert "startRoute();" in html


@pytest.mark.unit
def test_badminton_personal_stats_ignore_neko_shots():
    html = _badminton_html()

    assert "if (resultEntry && resultEntry.shooter && resultEntry.shooter !== 'player') return;" in html
    assert "recordShotHistory(game.attemptsResults[game.attemptsResults.length - 1]);" in html


@pytest.mark.unit
def test_badminton_duel_applies_llm_difficulty_before_neko_shot():
    html = _badminton_html()

    assert "var pendingControl = game.duel.pendingVoiceControl || null;" in html
    assert "if (pendingControl && pendingControl.difficulty) setDuelDifficulty(pendingControl.difficulty);" in html
    assert "var shot = getNekoDuelShot();" in html
    assert "var shot = game.duel.pendingShot || getNekoDuelShot();" not in html


@pytest.mark.unit
def test_badminton_duel_ramps_yui_difficulty_from_player_wins():
    html = _badminton_html()
    finish_duel = html[html.index("function finishDuelShot("):html.index("function finishShot(", html.index("function finishDuelShot("))]

    assert "var duelDifficultyIdx = 2;" in html
    assert "var storedDuelDifficulty = readJson('bd_duel_difficulty', 'lv3');" in html
    assert "function getPlayerWinDifficultyIndex(playerWins) {" in html
    assert "if (wins >= 8) return 0;" in html
    assert "if (wins >= 5) return 1;" in html
    assert "return 2;" in html
    assert "function getEffectiveDuelDifficultyIndex() {" in html
    assert "return Math.min(duelDifficultyIdx, rampIdx);" in html
    assert "function getEffectiveDuelDifficulty() {" in html
    assert "return getEffectiveDuelDifficulty().name;" in html
    assert "function speakDuelDifficultyRampLine(difficulty) {" in html
    assert "difficulty === 'max' ? 'lines.duel.difficultyMax' : 'lines.duel.difficultyLv2'" in html
    assert "var lines = _i18nArray(lineKey, fallbackLines);" in html
    assert "var rampEmote = difficulty === 'max' ? 'dominant' : 'surprised';" in html
    assert "var previousDifficulty = getDuelDifficultyName();" in finish_duel
    assert "var currentDifficulty = getDuelDifficultyName();" in finish_duel
    assert "if (currentDifficulty !== previousDifficulty && bgmEnabled) badmintonGameAudio.sync('difficulty-ramp');" in finish_duel
    assert "speakDuelDifficultyRampLine(currentDifficulty);" in html
    assert "'过家家时间结束了'" in html
    assert "'从现在起天上地下唯我独尊'" in html
    assert "'现在你才是挑战者'" in html
    assert "'如果未来是你的，证明给我看！'" in html
    assert "'今天手感不太好，下球我要认真了'" in html
    assert "{ name: 'max', skillBase: 0.985" in html
    assert "angleVar: 0.25" in html
    assert "powerVar: 0.55" in html
    assert "windupMs: 90" in html
    assert "reactionMs: 120" in html
    assert ", 0.40, 0.995)" in html


@pytest.mark.unit
def test_badminton_yui_rally_return_uses_pro_difficulty_table():
    html = _badminton_html()

    assert "shot.angle = clamp(43 + Math.random()" not in html
    assert "shot.power = clamp(50 + Math.random()" not in html
    assert "var diff = getEffectiveDuelDifficulty();" in html
    assert "var previewDiff = getEffectiveDuelDifficulty();" in html
    assert "var returnPressureBoost = Math.min(5, game.duel.rallyHits * 0.30);" in html
    assert "shot.angle = clamp(shot.angle - Math.min(2.5, game.duel.rallyHits * 0.16), 38, 52);" in html
    assert "shot.power = clamp(shot.power + returnPressureBoost, 48, 66);" in html


@pytest.mark.unit
def test_badminton_yui_bubble_anchors_to_avatar_and_supports_emote_marks():
    html = _badminton_html()

    assert "var BADMINTON_BUBBLE_EMOTE_IMAGES = {" in html
    assert 'font: 17px/1.45 "PingFang SC", "Segoe UI", sans-serif;' in html
    assert "max-width: min(410px, calc(100vw - 32px)); text-align: left;" in html
    assert "padding: 13px 17px; border-radius: 8px;" in html
    assert ".neko-bubble-body" in html
    assert ".neko-bubble-text" in html
    assert ".neko-bubble-emote" in html
    assert "display: flex; align-items: center; gap: 14px;" in html
    assert "flex: 0 0 92px; width: 92px; height: 92px;" in html
    assert "surprised: '/static/game/games/badminton/images/emotes/yui-surprised.png'" in html
    assert "dominant: '/static/game/games/badminton/images/emotes/yui-dominant.png'" in html
    assert "awkward: '/static/game/games/badminton/images/emotes/yui-awkward.png'" in html
    assert "joy: '/static/game/games/badminton/images/emotes/yui-joy.png'" in html
    assert "function positionBubbleNearYui() {" in html
    assert "var anchor = getActiveAvatarContainer();" in html
    assert "bubble.style.left = clamp(anchorX, 96, window.innerWidth - 96) + 'px';" in html
    assert "bubble.style.top = clamp(anchorY, 70, window.innerHeight - 120) + 'px';" in html
    assert "if (bubble.classList.contains('show')) positionBubbleNearYui();" in html
    assert "function renderBubbleContent(text, emote) {" in html
    assert "function showBubbleEmote(emote, control) {" in html
    assert "body.dataset.emoteOnly = text ? 'false' : 'true';" in html
    assert '.neko-bubble-body[data-emote-only="true"] .neko-bubble-text { display: none; }' in html
    assert "renderBubbleContent(text, getBubbleEmote(control || {}));" in html
    assert "var sticker = document.createElement('img');" in html
    assert "sticker.className = 'neko-bubble-emote';" in html
    assert "sticker.dataset.emote = emote;" in html
    assert "sticker.src = BADMINTON_BUBBLE_EMOTE_IMAGES[emote] || BADMINTON_BUBBLE_EMOTE_IMAGES.surprised;" in html
    assert "sticker.decoding = 'async';" in html
    assert "copy.className = 'neko-bubble-text';" in html
    assert "copy.textContent = text;" in html
    assert "if (expression === 'dominant' || expression === 'pro' || expression === 'serious') return 'dominant';" in html
    assert "if (expression === 'awkward' || expression === 'excuse' || expression === 'sweat') return 'awkward';" in html
    assert "if (expression === 'joy' || expression === 'happy' || expression === 'cheer' || expression === 'celebrate') return 'joy';" in html
    assert "if (mood === 'happy') return 'joy';" in html
    assert "{ expression: 'awkward', intensity: 'medium', mood: 'surprised', emote: 'awkward' }" in html
    for emote in ("surprised", "dominant", "awkward", "joy"):
        path = BADMINTON_EMOTES_DIR / f"yui-{emote}.png"
        assert path.exists()
        assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.unit
def test_badminton_yui_smash_result_uses_contextual_emotes():
    html = _badminton_html()
    finish_duel = html[html.index("function finishDuelShot("):html.index("function finishShot(", html.index("function finishDuelShot("))]
    emote_start = html.index("function showYuiDuelResultEmote(scored, shotType, ball, pointWinner) {")
    emote_section = html[emote_start:html.index("function finishDuelShot(", emote_start)]

    assert "function showYuiDuelResultEmote(scored, shotType, ball, pointWinner) {" in html
    assert "if (ball && ball.shooter === 'neko' && ball.isSmash) {" in emote_section
    assert "if (scored) {\n        showBubbleEmote('dominant', { expression: 'dominant', intensity: 'high', mood: 'surprised' });\n        return;\n      }" in emote_section
    assert "if (missType === 'net' || missType === 'net_touch') {" in emote_section
    assert "showBubbleEmote('awkward', { expression: 'awkward', intensity: 'medium', mood: 'surprised' });" in emote_section
    assert "if (pointWinner === 'neko') {" in emote_section
    assert "showBubbleEmote('joy', { expression: 'joy', intensity: 'medium', mood: 'happy' });" in emote_section
    assert "if (pointWinner === 'player' && scored && ball && ball.shooter === 'player') {" in emote_section
    assert "showBubbleEmote('surprised', { expression: 'surprised', intensity: 'medium', mood: 'surprised' });" in emote_section
    assert "showYuiDuelResultEmote(scored, shotType, ball, pointWinner);" in finish_duel


@pytest.mark.unit
def test_badminton_audio_config_contract():
    source = (ROOT / "static" / "game" / "games" / "badminton" / "badminton-audio-config.js")
    result_short = ROOT / "static" / "game" / "games" / "badminton" / "audio" / "badminton-result-win-short.mp3"
    banana_slip_goofy = ROOT / "static" / "game" / "games" / "badminton" / "audio" / "badminton-banana-slip-goofy.mp3"
    octopus_ink_poof = ROOT / "static" / "game" / "games" / "badminton" / "audio" / "badminton-octopus-ink-poof.mp3"
    original_result = ROOT / "static" / "game" / "games" / "soccer" / "audio" / "Battle_1_E.mp3"
    assert source.exists()
    assert not result_short.exists()
    assert banana_slip_goofy.exists()
    assert octopus_ink_poof.exists()
    assert original_result.exists()
    assert 1000 < banana_slip_goofy.stat().st_size < 16000
    assert 1000 < octopus_ink_poof.stat().st_size < 12000
    text = source.read_text(encoding="utf-8")
    assert "gameSystem.badminton.audioConfig" in text
    assert "audioMix" in text
    assert "line_in" in text
    assert "net" in text
    assert "Battle_Theme_1_L.mp3" in text
    assert "badminton-racket-shuttlecock-0537.mp3" in text
    assert "badminton-racket-shuttlecock-single.mp3" in text
    for index in range(1, 5):
        assert f"badminton-racket-shuttlecock-hit-{index}.mp3" in text
    assert "zapsplat_sport_badminton_racket_fast_swing_whoosh_001_76396.mp3" in text
    assert "whoosh: [{ src: racketSwing" in text
    assert "var racketShuttleHits = [" in text
    assert "var racketShuttleSingle =" in text
    assert "var resultWinShort =" not in text
    assert "var bananaSlipGoofy =" in text
    assert "var octopusInkPoof =" in text
    assert "bananaSlip: [{ src: bananaSlipGoofy" in text
    assert "octopusInk: [{ src: octopusInkPoof" in text
    assert "result: { gameOver: [{ src: '/static/game/games/soccer/audio/Battle_1_E.mp3', gainDb: 1.5 }] }" in text
    assert "badminton-result-win-short.mp3" not in text
    assert "shuttleContact: racketShuttleHits.concat([racketShuttleSingle]).map" in text


@pytest.mark.unit
def test_badminton_i18n_keys_are_registered_in_main_locales():
    required_keys = {
        "badminton.title",
        "badminton.loading.title",
        "badminton.loading.subtitle",
        "badminton.startScreen.readyToStart",
        "badminton.startScreen.startButton",
        "badminton.startScreen.never",
        "badminton.startTutorial.title",
        "badminton.startTutorial.aim",
        "badminton.startTutorial.charge",
        "badminton.startTutorial.release",
        "badminton.startTutorial.jump",
        "badminton.startTutorial.duel",
        "badminton.startTutorial.duel11",
        "badminton.modeSwitcher",
        "badminton.audio.controls",
        "badminton.memoryOption.label",
        "badminton.memoryOption.hint",
        "badminton.mode.spectator",
        "badminton.mode.duel",
        "badminton.hud.score",
        "badminton.hud.streak",
        "badminton.hud.record",
        "badminton.hud.duelScore",
        "badminton.hud.duelMisses",
        "badminton.hud.round",
        "badminton.hud.timer",
        "badminton.hud.practice",
        "badminton.hud.yourTurn",
        "badminton.hud.nekoTurn",
        "badminton.hud.unlimitedAttempts",
        "badminton.hud.chances",
        "badminton.hud.practiceTitle",
        "badminton.hud.attemptsTitle",
        "badminton.hud.duelTitle",
        "badminton.hud.on",
        "badminton.hud.off",
        "badminton.result.title",
        "badminton.result.leaderboard",
        "badminton.result.stats",
        "badminton.result.retry",
        "badminton.result.rating",
        "badminton.result.duel",
        "badminton.result.duelElimination",
        "badminton.result.practice",
        "badminton.result.summary",
        "badminton.result.attemptsSummary",
        "badminton.result.personalBest",
        "badminton.result.globalRank",
        "badminton.result.outcome.youWin",
        "badminton.result.outcome.nekoWin",
        "badminton.result.outcome.tie",
        "badminton.result.outcome.undecided",
        "badminton.leaderboard.title",
        "badminton.leaderboard.global",
        "badminton.leaderboard.local",
        "badminton.leaderboard.duel",
        "badminton.leaderboard.empty",
        "badminton.leaderboard.totalPlayers",
        "badminton.leaderboard.yourBest",
        "badminton.leaderboard.recent",
        "badminton.leaderboard.loading",
        "badminton.leaderboard.mode.duel",
        "badminton.table.score",
        "badminton.table.bestStreak",
        "badminton.table.farthest",
        "badminton.table.mode",
        "badminton.table.date",
        "badminton.debug.title",
        "badminton.debug.collapse",
        "badminton.debug.hide",
        "badminton.debug.distance",
        "badminton.debug.power",
        "badminton.debug.event",
        "badminton.debug.streak",
        "badminton.debug.reset",
        "badminton.debug.guide",
        "badminton.debug.sweet",
        "badminton.debug.markers",
        "badminton.debug.readout.modeState",
        "badminton.debug.readout.distance",
        "badminton.debug.readout.streaks",
        "badminton.debug.readout.score",
        "badminton.debug.readout.difficulty",
        "badminton.debug.readout.duel",
        "badminton.state.ready",
        "badminton.state.in_flight",
        "badminton.state.game_over",
        "badminton.state.neko_thinking",
        "badminton.stats.title",
        "badminton.stats.close",
        "badminton.stats.totalShots",
        "badminton.stats.farRate",
        "badminton.stats.trend",
        "badminton.stats.none",
        "badminton.theme.next",
        "badminton.theme.current",
        "badminton.theme.changed",
        "badminton.theme.labels.default",
        "badminton.theme.labels.miami",
        "badminton.court.netFront",
        "badminton.court.serviceLine",
        "badminton.court.backCourt",
        "badminton.court.baseline",
        "badminton.toast.difficulty",
        "badminton.toast.nekoShoot",
        "badminton.toast.nekoSmash",
        "badminton.toast.nekoSave",
        "badminton.toast.yuiCheatInk",
        "badminton.toast.yuiCheatBanana",
        "badminton.toast.yuiCheatBananaSlip",
        "badminton.toast.playerIcePowerup",
        "badminton.toast.yuiFrozen",
        "badminton.toast.nekoThinking",
        "badminton.toast.nekoTurn",
        "badminton.toast.copyNeko",
        "badminton.toast.nekoFailed",
        "badminton.toast.yourSet",
        "badminton.toast.yourTurn",
        "badminton.toast.reset",
        "badminton.toast.featureToggled",
        "badminton.toast.feature.guide",
        "badminton.toast.feature.sweet",
        "badminton.toast.feature.bgm",
        "badminton.toast.feature.markers",
        "badminton.toast.state.on",
        "badminton.toast.state.off",
        "badminton.tutorial.aim",
        "badminton.tutorial.charge",
        "badminton.tutorial.release",
        "badminton.shot.line_in",
        "badminton.shot.net_touch",
        "badminton.shot.zone_in",
        "badminton.shot.out",
        "badminton.shot.net",
        "badminton.shot.unknown",
        "badminton.shot.attempt",
        "badminton.lines.fallback",
        "badminton.lines.default.line_in",
        "badminton.lines.duel.line_in",
        "badminton.lines.pressure.lastTied",
        "badminton.lines.pressure.lastAhead",
        "badminton.lines.pressure.lastBehind",
        "badminton.lines.pressure.playerAhead",
        "badminton.lines.pressure.playerBehind",
        "badminton.lines.duel.clutch",
        "badminton.lines.duel.excuse",
        "badminton.lines.duel.difficultyLv2",
        "badminton.lines.duel.difficultyMax",
        "badminton.lines.mindGame",
        "badminton.lines.yuiCheat.banana",
        "badminton.lines.yuiCheat.octopus",
        "badminton.lines.yuiCheatScore.banana",
        "badminton.lines.yuiCheatScore.octopus",
        "badminton.lines.playerIceScore",
        "badminton.lines.easterEgg.lateNight",
        "badminton.lines.easterEgg.xmas",
        "badminton.lines.easterEgg.newYear",
        "badminton.lines.easterEgg.lineIn3",
        "badminton.lines.easterEgg.lineIn5",
        "badminton.lines.easterEgg.net3",
        "badminton.mood.happySuffix",
        "badminton.mood.sadPrefix",
        "badminton.mood.surprisedPrefix",
        "badminton.close",
    }
    line_keys = {
        "line_in",
        "net_touch",
        "zone_in",
        "out",
        "net",
        "shot_missed",
        "game_over",
        "long_aim",
        "close_to_record",
        "new_record",
        "streak_5",
        "streak_10",
        "streak_15",
        "streak_20",
    }
    required_keys.update(
        f"badminton.lines.{group}.{line_key}"
        for group in ("default", "duel")
        for line_key in line_keys
    )
    required_keys = {
        key.replace("badminton.", "badminton.", 1)
        for key in required_keys
    }

    for locale_path in LOCALES_DIR.glob("*.json"):
        payload = json.loads(locale_path.read_text(encoding="utf-8"))
        missing = sorted(key for key in required_keys if _get_nested(payload, key) is None)
        assert not missing, f"{locale_path.name} missing badminton i18n keys: {missing}"
        for removed_key in (
            "badminton.mode.shooter",
            "badminton.mode.timed",
            "badminton.mode.horse",
            "badminton.leaderboard.shooter",
            "badminton.leaderboard.mode.shooter",
            "badminton.leaderboard.mode.timed",
            "badminton.leaderboard.mode.horse",
            "badminton.lines.shooter",
            "badminton.lines.horse",
        ):
            assert _get_nested(payload, removed_key) is None
        duel_title = _get_nested(payload, "badminton.hud.duelTitle")
        eleven_point_markers = ("11", "十一", "１１")
        assert isinstance(duel_title, str) and any(
            marker in duel_title for marker in eleven_point_markers
        ), (
            f"{locale_path.name} badminton.hud.duelTitle should mention the 11-point win rule"
        )
        for line_key in line_keys:
            assert len(_get_nested(payload, f"badminton.lines.duel.{line_key}")) >= 4
        assert len(_get_nested(payload, "badminton.lines.duel.excuse")) >= 4
        assert len(_get_nested(payload, "badminton.lines.duel.difficultyLv2")) >= 4
        assert len(_get_nested(payload, "badminton.lines.duel.difficultyMax")) >= 6
        difficulty_max_lines = _get_nested(payload, "badminton.lines.duel.difficultyMax")
        expected_max_lines = {
            "en.json": [
                "From now on, I rule above and below.",
                "Now you are the challenger.",
                "If the future is yours, prove it!",
            ],
            "es.json": [
                "Desde ahora, mando en cielo y tierra.",
                "Ahora tú eres quien desafía.",
                "Si el futuro es tuyo, demuéstralo.",
            ],
            "ja.json": [
                "今から天地で私が一番。",
                "今度はあなたが挑戦者だよ。",
                "未来があなたのものなら、証明してみせて！",
            ],
            "ko.json": [
                "지금부터 하늘 아래 땅 위에선 내가 최고야.",
                "이제 네가 도전자야.",
                "미래가 네 것이라면, 증명해 봐!",
            ],
            "pt.json": [
                "A partir de agora, céu e terra são meus.",
                "Agora você é quem desafia.",
                "Se o futuro é seu, prove.",
            ],
            "ru.json": [
                "С этого момента я властвую над небом и землей.",
                "Теперь вызов бросаешь ты.",
                "Если будущее твое, докажи это!",
            ],
            "zh-CN.json": [
                "从现在起天上地下唯我独尊",
                "现在你才是挑战者",
                "如果未来是你的，证明给我看！",
            ],
            "zh-TW.json": [
                "從現在起天上地下唯我獨尊",
                "現在你才是挑戰者",
                "如果未來是你的，證明給我看！",
            ],
        }
        locale_expected_max_lines = expected_max_lines.get(locale_path.name)
        assert locale_expected_max_lines is not None, (
            f"{locale_path.name} missing difficultyMax line contract; "
            f"expected locales: {sorted(expected_max_lines)}"
        )
        for required_line in locale_expected_max_lines:
            assert required_line in difficulty_max_lines
        assert len(_get_nested(payload, "badminton.lines.mindGame")) >= 6
        assert len(_get_nested(payload, "badminton.lines.yuiCheat.banana")) >= 4
        assert len(_get_nested(payload, "badminton.lines.yuiCheat.octopus")) >= 4
        assert len(_get_nested(payload, "badminton.lines.yuiCheatScore.banana")) >= 4
        assert len(_get_nested(payload, "badminton.lines.yuiCheatScore.octopus")) >= 4
        assert len(_get_nested(payload, "badminton.lines.playerIceScore")) >= 4
        if locale_path.name == "zh-CN.json":
            assert "唔不算嘛不算嘛重来！" in _get_nested(payload, "badminton.lines.playerIceScore")


@pytest.mark.unit
def test_badminton_runtime_visible_text_uses_i18n_helpers():
    html = _badminton_html()

    expected_i18n_references = (
        "_i18n('leaderboard.empty'",
        "_i18n('result.duelElimination'",
        "_i18n('result.summary'",
        "_i18n('theme.current'",
        "_i18n('toast.nekoShoot'",
        "_i18n('tutorial.aim'",
        "_i18nArray('lines.mindGame'",
    )
    for expected in expected_i18n_references:
        assert expected in html

    expected_static_hooks = (
        'data-i18n="badminton.leaderboard.title"',
        'data-i18n="badminton.leaderboard.global"',
        'data-i18n="badminton.leaderboard.local"',
        'data-i18n="badminton.leaderboard.duel"',
        'data-i18n="badminton.table.score"',
        'data-i18n="badminton.table.bestStreak"',
        'data-i18n="badminton.table.farthest"',
        'data-i18n="badminton.table.mode"',
        'data-i18n="badminton.table.date"',
        'data-i18n="badminton.close"',
    )
    for expected in expected_static_hooks:
        assert expected in html

    forbidden_direct_visible_text = (
        "showAssistHint('Neko出手')",
        "showAssistHint('已重置')",
        "leaderboardMeta.textContent = '加载中...'",
        "emptyCell.textContent = '暂无记录'",
        "resultStats.textContent = '对战结果：'",
        "if (themeButton) themeButton.textContent = '主题：'",
        "updateTutorial('上下移动鼠标调整挥拍角度')",
    )
    for snippet in forbidden_direct_visible_text:
        assert snippet not in html


@pytest.mark.unit
def test_badminton_backspin_trigger_is_more_forgiving_than_perfect_shot():
    html = _badminton_html()

    assert "function getBackspinRate(" in html
    assert "function buildShuttleSpinRate(launchAngle, power, direction, impulse) {" in html
    assert "var angleTolerance = 6;" in html
    assert "var powerPadding = 5;" in html
    assert "return 4 + quality * 5;" in html
    assert "var baseSpinRate = direction * getBackspinRate(launchAngle, power, game.distance);" in html
    assert "var contactSpinRate = impulse.incomingSpeed ? direction * (10 + impulse.quality * 7 + clamp((impulse.incomingSpeed || 0) / 150, 0, 6)) : 0;" in html
    assert "var rawSpinRate = baseSpinRate + incomingSpinRate + contactSpinRate + smashSpinRate;" in html
    assert "var maxSpinRate = direction > 0 ? 10 : 24;" in html
    assert "return clamp(rawSpinRate, -maxSpinRate, maxSpinRate);" in html
    assert "yuiReturnSpinRate" not in html

    perfect_start = html.index("function isPerfect(")
    perfect_end = html.index("function isDuelMode(")
    perfect_section = html[perfect_start:perfect_end]
    assert "<= 2" in perfect_section
    assert "powerPadding" not in perfect_section


@pytest.mark.unit
def test_badminton_court_distances_use_badminton_line_calibration():
    html = _badminton_html()

    for expected in (
        "var BADMINTON_COURT_METERS =",
        "netFront: 1.22",
        "serviceLine: 4.19",
        "backCourt: 7.24",
        "baseline: 12.73",
        "var PX_PER_METER = BADMINTON_COURT_METERS.pxPerMeter",
        "function metersToShotPx(",
        "var COURT_DISTANCES =",
        "serviceLine: metersToShotPx(BADMINTON_COURT_METERS.serviceLine)",
        "backCourt: metersToShotPx(BADMINTON_COURT_METERS.backCourt)",
        "baseline: metersToShotPx(BADMINTON_COURT_METERS.baseline)",
        "netFront: metersToShotPx(BADMINTON_COURT_METERS.netFront)",
        "var COURT_DISTANCE_MARKS =",
        "data-debug-distance-key=\"netFront\"",
        "data-debug-distance-key=\"serviceLine\"",
        "data-debug-distance-key=\"backCourt\"",
        "data-debug-distance-key=\"baseline\"",
        "function refreshCourtDistanceButtons(",
    ):
        assert expected in html

    court_start = html.index("function drawCourt(")
    court_end = html.index("function drawStreakEffect(")
    court_section = html[court_start:court_end]
    for stale_legacy_court_marker in (
        "var laneLeftX = hoopCenterX - 252;",
        "var threeX = hoopCenterX - 405;",
        "var midCourtX = Math.max(86, threeX - 260);",
        "var restrictedRadiusX = 74;",
        "ctx.ellipse(hoopCenterX, BASE_H - 3, 405, 76",
        "hoopCenterX",
        "freeThrow",
        "threePoint",
        "midCourt",
        "restricted",
    ):
        assert stale_legacy_court_marker not in court_section

    for stale_legacy_court_color in (
        "#ffc36b",
        "#ff8f3d",
        "#c94f1e",
    ):
        assert stale_legacy_court_color not in court_section

    for stale_debug_distance in (
        'data-debug-distance="150"',
        'data-debug-distance="300"',
        'data-debug-distance="450"',
        'data-debug-distance="600"',
    ):
        assert stale_debug_distance not in html


@pytest.mark.unit
def test_badminton_aiming_overlay_does_not_double_draw_ink():
    html = _badminton_html()

    assert html.count("function drawYuiInkOverlay(now) {") == 1
    draw_start = html.index("function drawAiming(now) {")
    draw_end = html.index("function drawPlayerReturnHitCue(now)", draw_start)
    draw_section = html[draw_start:draw_end]
    assert draw_section.count("drawYuiInkOverlay(t);") == 1
    assert "drawPlayerReturnHitCue(t);" in draw_section
    assert "drawPlayerChargeMeter(game.charging && canPlayerChargeShot() ? clamp(game.power, 0, 100) : 0, t);" in draw_section


@pytest.mark.unit
def test_badminton_demo_exposes_electron_exit_button():
    html = _badminton_html()

    assert 'id="badminton-exit-button"' in html
    assert 'data-i18n="badminton.exit"' in html
    assert "var badmintonExitButton = document.getElementById('badminton-exit-button');" in html
    assert "function closeBadmintonBrowserFallback() {" in html
    assert "try { window.close(); } catch (_) {}" in html
    assert "if (!window.closed) window.location.assign('/');" in html
    assert "}, 150);" in html
    assert "function closeBadmintonWindow() {" in html
    assert "disposeBadmintonGame('exit_button');" in html
    assert "var host = window.nekoHost;" in html
    assert "Promise.resolve(host.closeWindow())" in html
    assert ".then(function (result) {" in html
    assert "if (result && result.ok === false) {" in html
    assert "closeBadmintonBrowserFallback();" in html
    assert ".catch(function () {" in html
    assert "addBadmintonEventListener(badmintonExitButton, 'click', closeBadmintonWindow);" in html


@pytest.mark.unit
def test_badminton_court_render_uses_static_canvas_cache():
    html = _badminton_html()

    for expected in (
        "var badmintonCourtCache = { key: '', canvas: null };",
        "function getBadmintonCourtCacheKey() {",
        "scenicReady: !!getLoadedScenicBackground(theme.scenic)",
        "function renderCourtToContext(renderCtx) {",
        "var previousCourtCtx = ctx;",
        "function invalidateBadmintonCourtCache() {",
        "badmintonCourtCache.key = '';",
    ):
        assert expected in html

    draw_start = html.index("function drawCourt() {")
    draw_end = html.index("function renderCourtToContext(", draw_start)
    draw_section = html[draw_start:draw_end]
    assert "var cacheKey = getBadmintonCourtCacheKey();" in draw_section
    assert "badmintonCourtCache.key === cacheKey" in draw_section
    assert "ctx.drawImage(badmintonCourtCache.canvas, 0, 0, BASE_W, BASE_H);" in draw_section
    assert "courtCanvas.width = BASE_W;" in draw_section
    assert "courtCanvas.height = BASE_H;" in draw_section


@pytest.mark.unit
def test_badminton_scenic_background_decode_invalidates_court_cache():
    html = _badminton_html()

    preload_start = html.index("function preloadScenicBackgrounds() {")
    preload_end = html.index("function getLoadedScenicBackground(key) {", preload_start)
    preload_section = html[preload_start:preload_end]
    assert "function markScenicBackgroundReady(image) {" in html
    assert "image.__ready = true;" in html
    assert "invalidateBadmintonCourtCache();" in html
    assert "if (typeof image.decode === 'function') {" in preload_section
    assert "image.decode().then(function () { markScenicBackgroundReady(image); }).catch(function () {});" in preload_section
    assert "image.onload = function () { markScenicBackgroundReady(image); };" in preload_section


@pytest.mark.unit
def test_badminton_aiming_overlay_skips_idle_frames():
    html = _badminton_html()

    assert "var aimingOverlayWasActive = false;" in html
    assert "function isAimingOverlayActive(now) {" in html
    assert "return !!(isIncomingPlayerReturnCandidate()" in html
    assert "getYuiInkState(t).active" in html
    overlay_start = html.index("function isAimingOverlayActive(now) {")
    overlay_end = html.index("function drawAiming(now) {", overlay_start)
    overlay_predicate = html[overlay_start:overlay_end]
    assert "game.charging && canPlayerChargeShot()" not in overlay_predicate

    draw_start = html.index("function drawAiming(now) {")
    draw_end = html.index("function drawPlayerChargeMeter(", draw_start)
    draw_section = html[draw_start:draw_end]
    assert "var overlayActive = isAimingOverlayActive(t);" in draw_section
    assert "drawPlayerChargeMeter(game.charging && canPlayerChargeShot() ? clamp(game.power, 0, 100) : 0, t);" in draw_section
    assert "if (!overlayActive && !aimingOverlayWasActive) return;" in draw_section
    assert "aimingOverlayWasActive = overlayActive;" in draw_section
    assert "aimingCtx.clearRect(0, 0, BASE_W, BASE_H);" in draw_section
    assert draw_section.index("if (!overlayActive && !aimingOverlayWasActive) return;") < draw_section.index("aimingCtx.clearRect(0, 0, BASE_W, BASE_H);")


@pytest.mark.unit
def test_badminton_player_charge_meter_uses_dom_transform_not_canvas_shadow():
    html = _badminton_html()

    assert 'id="player-charge-meter"' in html
    assert 'id="player-charge-fill"' in html
    assert "will-change: transform, opacity;" in html
    assert "var playerChargeMeter = document.getElementById('player-charge-meter');" in html
    assert "var playerChargeFill = document.getElementById('player-charge-fill');" in html

    charge_start = html.index("function drawPlayerChargeMeter(percent, now) {")
    charge_end = html.index("function ensureYuiInkOverlayCache()", charge_start)
    charge_section = html[charge_start:charge_end]
    assert "setDatasetValueIfChanged(playerChargeMeter, 'active', '1');" in charge_section
    assert "setStyleIfChanged(playerChargeMeter, playerChargeMeterStyleCache, 'transform'," in charge_section
    assert "setStyleIfChanged(playerChargeFill, playerChargeFillStyleCache, 'transform', 'scaleX('" in charge_section
    assert "aimingCtx.shadowBlur" not in charge_section
    assert "aimingCtx.globalCompositeOperation" not in charge_section


@pytest.mark.unit
def test_badminton_leaderboard_uses_document_fragment_batch_render():
    html = _badminton_html()

    render_start = html.index("function renderLeaderboard(entries, meta, mode) {")
    render_end = html.index("function showLeaderboard(mode) {", render_start)
    render_section = html[render_start:render_end]
    assert "var fragment = document.createDocumentFragment();" in render_section
    assert "fragment.appendChild(emptyRow);" in render_section
    assert "fragment.appendChild(tr);" in render_section
    assert "leaderboardBody.appendChild(fragment);" in render_section
    assert "leaderboardBody.appendChild(emptyRow);" not in render_section
    assert "leaderboardBody.appendChild(tr);" not in render_section


@pytest.mark.unit
def test_badminton_low_risk_frame_time_is_reused_in_overlay_hot_path():
    html = _badminton_html()

    assert "var badmintonFrameNow = 0;" in html
    assert "function getBadmintonFrameNow() {" in html
    assert "badmintonFrameNow = ts || performance.now();" in html
    assert "update(dt, badmintonFrameNow);" in html
    assert "drawAiming(badmintonFrameNow);" in html
    assert "var BADMINTON_HIDDEN_FRAME_DELAY_MS = 250;" in html
    assert "function scheduleBadmintonNextFrame() {" in html
    assert "if (document.hidden) {" in html
    assert "}, BADMINTON_HIDDEN_FRAME_DELAY_MS);" in html
    assert "scheduleBadmintonNextFrame();" in html

    overlay_start = html.index("function isAimingOverlayActive(")
    overlay_end = html.index("function drawAiming(", overlay_start)
    overlay_section = html[overlay_start:overlay_end]
    assert "var t = Number.isFinite(now) ? now : getBadmintonFrameNow();" in overlay_section
    assert "getYuiInkState(t).active" in overlay_section
    assert "performance.now()" not in overlay_section


@pytest.mark.unit
def test_badminton_low_risk_hud_text_updates_are_deduped():
    html = _badminton_html()

    assert "function setTextIfChanged(element, text) {" in html
    assert "if (element && element.textContent !== value) element.textContent = value;" in html
    assert "function setTitleIfChanged(element, title) {" in html

    hud_start = html.index("function updateHud() {")
    hud_end = html.index("function updateTutorial(", hud_start)
    hud_section = html[hud_start:hud_end]
    assert "setTextIfChanged(hud.score," in hud_section
    assert "setTextIfChanged(hud.streak," in hud_section
    assert "setTextIfChanged(hud.attempts," in hud_section
    assert "setTitleIfChanged(hud.attempts," in hud_section
    assert "setTextIfChanged(hud.record," in hud_section
    assert ".textContent =" not in hud_section

    debug_start = html.index("function updateDebugReadout() {")
    debug_end = html.index("function setDebugVisible(", debug_start)
    debug_section = html[debug_start:debug_end]
    assert "if (debugPanel && debugPanel.dataset.debugVisible !== 'true') return;" in debug_section
    assert "setTextIfChanged(debugPowerValue, String(Math.round(game.power || 0)));" in debug_section
    assert "setTextIfChanged(debugReadout, readoutText);" in debug_section


@pytest.mark.unit
def test_badminton_low_risk_resize_is_coalesced_with_animation_frame():
    html = _badminton_html()

    assert "var badmintonResizeFrame = 0;" in html
    assert "function scheduleBadmintonResize() {" in html
    assert "if (badmintonResizeFrame) return;" in html
    assert "badmintonResizeFrame = requestAnimationFrame(function () {" in html
    assert "badmintonResizeFrame = 0;" in html
    assert "resize();" in html
    assert "addBadmintonEventListener(window, 'resize', scheduleBadmintonResize);" in html
    assert "window.addEventListener('resize', resize);" not in html


@pytest.mark.unit
def test_badminton_low_risk_loading_and_theme_text_updates_are_deduped():
    html = _badminton_html()

    loading_start = html.index("function setBadmintonLoadingProgress(value) {")
    loading_end = html.index("function startBadmintonFakeProgress()", loading_start)
    loading_section = html[loading_start:loading_end]
    assert "var progressText = badmintonLoadingProgress + '%';" in loading_section
    assert "if (badmintonLoadingBar && badmintonLoadingBar.style.width !== progressText) badmintonLoadingBar.style.width = progressText;" in loading_section
    assert "setTextIfChanged(badmintonLoadingPercent, progressText);" in loading_section
    assert "badmintonLoadingPercent.textContent =" not in loading_section

    theme_start = html.index("function updateThemeButton() {")
    theme_end = html.index("function checkSeasonalEasterEggs()", theme_start)
    theme_section = html[theme_start:theme_end]
    assert "setTextIfChanged(themeButton, _i18n('theme.current'" in theme_section
    assert "themeButton.textContent =" not in theme_section


@pytest.mark.unit
def test_badminton_low_risk_volume_and_debug_input_text_updates_are_deduped():
    html = _badminton_html()

    bgm_start = html.index("if (bgmVolumeInput) {")
    bgm_end = html.index("if (themeButton)", bgm_start)
    volume_section = html[bgm_start:bgm_end]
    assert "setTextIfChanged(bgmVolumeValue, bgmVolumeInput.value + '%');" in volume_section
    assert "setTextIfChanged(sfxVolumeValue, sfxVolumeInput.value + '%');" in volume_section
    assert "bgmVolumeValue.textContent =" not in volume_section
    assert "sfxVolumeValue.textContent =" not in volume_section

    debug_input_start = html.index("if (debugPowerInput) {", html.index("if (debugPanel) {"))
    debug_input_end = html.index("if (debugGuideInput)", debug_input_start)
    debug_input_section = html[debug_input_start:debug_input_end]
    assert "setTextIfChanged(debugPowerValue, String(Math.round(game.power)));" in debug_input_section
    assert "debugPowerValue.textContent =" not in debug_input_section


@pytest.mark.unit
def test_badminton_low_risk_pointer_move_reuses_control_checks():
    html = _badminton_html()

    pointer_start = html.index("function handleBadmintonPointerMove(ev) {")
    pointer_end = html.index("function handleBadmintonPointerDown(ev) {", pointer_start)
    pointer_section = html[pointer_start:pointer_end]
    assert "if (ev && ev.__badmintonPointerMoveHandled) return;" in pointer_section
    assert "if (ev) ev.__badmintonPointerMoveHandled = true;" in pointer_section
    assert pointer_section.index("if (ev && ev.__badmintonPointerMoveHandled) return;") < pointer_section.index("rememberPlayerPointer(ev);")
    assert pointer_section.index("if (ev) ev.__badmintonPointerMoveHandled = true;") < pointer_section.index("rememberPlayerPointer(ev);")
    assert "var canMoveCourt = canPlayerMoveCourt();" in pointer_section
    assert "var canControlShot = canPlayerControlShot();" in pointer_section
    assert "if (canMoveCourt) updatePlayerCourtTarget(ev.clientX, ev.clientY);" in pointer_section
    assert "if (!canControlShot) return;" in pointer_section
    assert pointer_section.count("canPlayerMoveCourt()") == 1
    assert pointer_section.count("canPlayerControlShot()") == 1


@pytest.mark.unit
def test_badminton_low_risk_render_hot_path_reuses_frame_time():
    html = _badminton_html()

    render_start = html.index("function render(now) {")
    render_end = html.index("function markFirstFrameRendered()", render_start)
    render_section = html[render_start:render_end]
    assert "var t = Number.isFinite(now) ? now : getBadmintonFrameNow();" in render_section
    assert "drawFireBorder(t);" in render_section
    assert "drawYuiCheatItems(t);" in render_section
    assert "drawStreakEffect(t);" in render_section
    assert "render(badmintonFrameNow);" in html

    for name in ("drawStreakEffect", "drawFireBorder", "drawYuiCheatItems"):
        start = html.index(f"function {name}(now) {{")
        end = html.index("function ", start + 1)
        section = html[start:end]
        assert "var t = Number.isFinite(now) ? now : getBadmintonFrameNow();" in section
        assert "performance.now()" not in section


@pytest.mark.unit
def test_badminton_low_risk_input_and_tutorial_updates_are_deduped():
    html = _badminton_html()

    assert "function setInputValueIfChanged(element, value) {" in html
    assert "if (element && element.value !== stringValue) element.value = stringValue;" in html

    audio_sync_start = html.index("function _syncGameAudioVolumeControls() {")
    audio_sync_end = html.index("function getAudioCtx()", audio_sync_start)
    audio_sync_section = html[audio_sync_start:audio_sync_end]
    assert "setInputValueIfChanged(bgmVolumeInput, Math.round(bgmVolume * 100));" in audio_sync_section
    assert "setInputValueIfChanged(sfxVolumeInput, Math.round(sfxVolume * 100));" in audio_sync_section
    assert ".value = String(Math.round(" not in audio_sync_section

    debug_start = html.index("function updateDebugReadout() {")
    debug_end = html.index("function setDebugVisible(", debug_start)
    debug_section = html[debug_start:debug_end]
    assert "setInputValueIfChanged(debugPowerInput, Math.round(game.power || 0));" in debug_section
    assert "debugPowerInput.value =" not in debug_section

    tutorial_start = html.index("function updateTutorial(text) {")
    tutorial_end = html.index("function getTutorialText()", tutorial_start)
    tutorial_section = html[tutorial_start:tutorial_end]
    assert "setTextIfChanged(tutorialOverlay, text);" in tutorial_section
    assert "tutorialOverlay.textContent =" not in tutorial_section


@pytest.mark.unit
def test_badminton_low_risk_net_contact_effects_reuse_frame_time():
    html = _badminton_html()

    render_start = html.index("function render(now) {")
    render_end = html.index("function markFirstFrameRendered()", render_start)
    render_section = html[render_start:render_end]
    assert "drawNetFront(t);" in render_section

    front_start = html.index("function drawNetFront(now) {")
    front_end = html.index("function drawNetContactEffects(", front_start)
    front_section = html[front_start:front_end]
    assert "var t = Number.isFinite(now) ? now : getBadmintonFrameNow();" in front_section
    assert "drawNetContactEffects(t);" in front_section

    effects_start = html.index("function drawNetContactEffects(now) {")
    effects_end = html.index("function drawNetCord()", effects_start)
    effects_section = html[effects_start:effects_end]
    assert "var t = Number.isFinite(now) ? now : getBadmintonFrameNow();" in effects_section
    assert "var activeCount = 0;" in effects_section
    assert "netContactEffects[activeCount++] = retainedEffect;" in effects_section
    assert "netContactEffects.length = activeCount;" in effects_section
    assert "netContactEffects = netContactEffects.filter" not in effects_section
    assert "var age = clamp((t - effect.startAt) / effect.duration, 0, 1);" in effects_section
    assert "performance.now()" not in effects_section


@pytest.mark.unit
def test_badminton_net_contact_effects_are_lightweight_on_electron_or_high_dpr():
    html = _badminton_html()

    assert "var NET_CONTACT_EFFECT_LIGHT_DURATION_MS = 360;" in html
    assert "var NET_CONTACT_EFFECT_LIGHT_MAX = 3;" in html
    assert "var NET_CONTACT_EFFECT_LIGHT_DPR = 1.5;" in html
    assert "function isElectronBadmintonRuntime() {" in html
    assert "window.nekoHost || (navigator.userAgent && /Electron/i.test(navigator.userAgent))" in html
    assert "function shouldUseLightweightNetContactEffects() {" in html
    assert "return isElectronBadmintonRuntime() || dpr >= NET_CONTACT_EFFECT_LIGHT_DPR;" in html

    spawn_start = html.index("function spawnNetContactEffect(ball, crossing) {")
    spawn_end = html.index("function resetGame()", spawn_start)
    spawn_section = html[spawn_start:spawn_end]
    assert "var lightweight = shouldUseLightweightNetContactEffects();" in spawn_section
    assert "var maxEffects = lightweight ? NET_CONTACT_EFFECT_LIGHT_MAX : NET_CONTACT_EFFECT_MAX;" in spawn_section
    assert "duration: lightweight ? NET_CONTACT_EFFECT_LIGHT_DURATION_MS : NET_CONTACT_EFFECT_DURATION_MS," in spawn_section
    assert "netContactEffects = netContactEffects.slice(netContactEffects.length - maxEffects);" in spawn_section
    assert "lightweight: lightweight," in spawn_section

    effects_start = html.index("function drawNetContactEffects(now) {")
    effects_end = html.index("function drawNetCord()", effects_start)
    effects_section = html[effects_start:effects_end]
    assert "var lightweight = shouldUseLightweightNetContactEffects();" in effects_section
    assert "ctx.globalCompositeOperation = lightweight ? 'source-over' : 'lighter';" in effects_section
    assert "if (!lightweight) {" in effects_section
    assert "ctx.shadowBlur = lightweight ? 0 : 3 + 6 * alpha;" in effects_section
    assert "var sparkCount = lightweight ? 2 : 4;" in effects_section


@pytest.mark.unit
def test_badminton_low_risk_checked_and_aria_updates_are_deduped():
    html = _badminton_html()

    assert "function setCheckedIfChanged(element, checked) {" in html
    assert "if (element && element.checked !== value) element.checked = value;" in html
    assert "function setAriaHiddenIfChanged(element, hidden) {" in html
    assert "if (element && element.getAttribute('aria-hidden') !== value) element.setAttribute('aria-hidden', value);" in html

    memory_start = html.index("function _initBadmintonGameMemoryToggle() {")
    memory_end = html.index("function _shouldSkipBadmintonStartTutorial()", memory_start)
    memory_section = html[memory_start:memory_end]
    assert "setCheckedIfChanged(gameMemoryToggle, false);" in memory_section
    assert "gameMemoryToggle.checked = false;" not in memory_section

    debug_start = html.index("function updateDebugReadout() {")
    debug_end = html.index("function setDebugVisible(", debug_start)
    debug_section = html[debug_start:debug_end]
    assert "setCheckedIfChanged(debugGuideInput, !!assists.guide);" in debug_section
    assert "setCheckedIfChanged(debugSweetInput, !!assists.sweet);" in debug_section
    assert "setCheckedIfChanged(debugMarkersInput, !!distanceMarkersEnabled);" in debug_section
    assert ".checked =" not in debug_section

    visible_start = html.index("function setDebugVisible(visible) {")
    visible_end = html.index("function handleDebugButton(", visible_start)
    visible_section = html[visible_start:visible_end]
    assert "setDatasetValueIfChanged(debugPanel, 'debugVisible', visible ? 'true' : 'false');" in visible_section
    assert "setAriaHiddenIfChanged(debugPanel, !visible);" in visible_section
    assert "debugPanel.dataset.debugVisible =" not in visible_section
    assert "debugPanel.setAttribute('aria-hidden'" not in visible_section


@pytest.mark.unit
def test_badminton_shot_distance_caps_one_step_beyond_baseline():
    html = _badminton_html()

    assert "var POST_BASELINE_DISTANCE_STEP = 45;" in html
    assert (
        "var MAX_PLAYABLE_SHOT_DISTANCE = COURT_DISTANCES.baseline + POST_BASELINE_DISTANCE_STEP;"
        in html
    )
    assert "function getMaxPlayableShotDistance() {" in html
    assert "return MAX_PLAYABLE_SHOT_DISTANCE;" in html
    assert "function advanceShotDistance(step) {" in html
    assert "return Math.min(getMaxPlayableShotDistance(), game.distance + (Number(step) || 0));" in html
    assert "game.distance = advanceShotDistance(nextDistanceStep());" in html
    assert "game.distance = advanceShotDistance(POST_BASELINE_DISTANCE_STEP);" in html
    assert "clamp(Number(distance) || 200, 80, getMaxPlayableShotDistance())" in html
    assert "clamp(Number(px) || 200, 80, getMaxPlayableShotDistance())" in html
    assert "game.distance += nextDistanceStep();" not in html
    assert "Math.min(620, game.distance + 45)" not in html


@pytest.mark.unit
def test_badminton_llm_control_contract_accepts_mood_and_difficulty():
    parsed = gr_runtime._parse_control_instructions(
        '认真点喵\n{"mood":"angry","expression":"tease","intensity":"high","difficulty":"max"}',
        game_type="badminton",
    )

    assert parsed == {
        "line": "认真点喵",
        "control": {
            "mood": "angry",
            "expression": "tease",
            "intensity": "high",
            "difficulty": "max",
        },
    }


@pytest.mark.unit
def test_badminton_duel_prompt_mentions_difficulty_control():
    prompt = prompts_badminton.get_badminton_system_prompt("zh", mode="duel")

    assert "difficulty" in prompt
    assert "max, lv2, lv3, lv4" in prompt


@pytest.mark.unit
@pytest.mark.parametrize("mode", ("spectator", "shooter", "timed", "horse"))
@pytest.mark.parametrize("lang", ("zh", "en", "ja", "ko", "ru", "es", "pt"))
def test_badminton_non_duel_prompts_do_not_advertise_difficulty_control(lang, mode):
    prompt = prompts_badminton.get_badminton_system_prompt(lang, mode=mode)

    assert '"difficulty"' not in prompt
    assert "max, lv2, lv3, lv4" not in prompt


@pytest.mark.unit
def test_badminton_spectator_prompt_matches_default_badminton_contract():
    zh = prompts_badminton.get_badminton_system_prompt("zh", mode="spectator")
    en = prompts_badminton.get_badminton_system_prompt("en", mode="spectator")

    assert "羽毛球小游戏" in zh
    assert "自由练习" not in zh
    assert "本模式不按三次机会淘汰" in zh
    assert "三次机会用完" not in zh
    assert "badminton minigame" in en
    assert "free-practice" not in en
    assert "This mode is not a three-miss elimination run" in en
    assert "all three chances are gone" not in en


@pytest.mark.unit
@pytest.mark.parametrize("lang", ("zh", "en", "ja", "ko", "ru", "es", "pt"))
@pytest.mark.parametrize("removed_mode", ("shooter", "timed", "horse"))
def test_badminton_removed_modes_use_spectator_prompt(lang, removed_mode):
    spectator = prompts_badminton.get_badminton_system_prompt(lang, mode="spectator")
    prompt = prompts_badminton.get_badminton_system_prompt(lang, mode=removed_mode)

    assert prompt == spectator


@pytest.mark.unit
@pytest.mark.parametrize("lang", ("zh", "en", "ja", "ko", "ru", "es", "pt"))
def test_badminton_quick_lines_mode_prompts_are_distinct_and_localized(lang):
    spectator = prompts_badminton.get_badminton_quick_lines_prompt(lang, mode="spectator")

    prompt = prompts_badminton.get_badminton_quick_lines_prompt(lang, mode="duel")
    assert prompt != spectator
    if lang != "en":
        assert "Current mode is" not in prompt


@pytest.mark.unit
@pytest.mark.parametrize("removed_mode", ("shooter", "timed", "horse"))
def test_badminton_removed_modes_use_spectator_quick_lines_prompt(removed_mode):
    spectator = prompts_badminton.get_badminton_quick_lines_prompt("zh", mode="spectator")

    assert prompts_badminton.get_badminton_quick_lines_prompt("zh", mode=removed_mode) == spectator


@pytest.mark.unit
def test_badminton_quick_lines_uses_dedicated_prompt_module_for_neko_core_locales():
    from config.prompts import prompts_badminton

    assert prompts_badminton.NEKO_CORE_LOCALES == (
        "zh-CN",
        "zh-TW",
        "en",
        "ja",
        "ko",
        "ru",
        "es",
        "pt",
    )
    router_source = "".join(q.read_text(encoding="utf-8") for q in sorted((ROOT / "main_routers" / "game_router").glob("*.py")))
    assert "_BADMINTON_QUICK_LINES_FALLBACK" not in router_source

    english_prompt = prompts_badminton.get_badminton_quick_lines_prompt("en", mode="duel")
    simplified_prompt = prompts_badminton.get_badminton_quick_lines_prompt("zh-CN", mode="duel")
    traditional_prompt = prompts_badminton.get_badminton_quick_lines_prompt("zh-TW", mode="duel")
    assert simplified_prompt != english_prompt
    assert traditional_prompt != simplified_prompt
    assert "正在看球的 Yui" in simplified_prompt
    assert "正在看球的 NEKO" not in simplified_prompt
    assert "Yui reacting" in english_prompt
    assert "NEKO reacting" not in english_prompt
    assert "duel" in english_prompt
    assert "對拉" in traditional_prompt

    for locale in prompts_badminton.NEKO_CORE_LOCALES:
        fallback = prompts_badminton.get_badminton_quick_lines_fallback(locale)
        assert set(fallback) == prompts_badminton.BADMINTON_QUICK_LINE_KEYS
        assert all(fallback[key] for key in prompts_badminton.BADMINTON_QUICK_LINE_KEYS)


@pytest.mark.unit
def test_badminton_prompt_localizations_do_not_fallback_to_english():
    english_spectator = prompts_badminton.get_badminton_system_prompt("en", mode="spectator")
    english_duel = prompts_badminton.get_badminton_system_prompt("en", mode="duel")
    english_quick = prompts_badminton.get_badminton_quick_lines_prompt("en", mode="spectator")
    english_pregame = prompts_badminton.get_badminton_pregame_context_prompt("en")

    for lang in ("zh", "ja", "ko", "ru", "es", "pt"):
        assert prompts_badminton.get_badminton_system_prompt(lang, mode="spectator") != english_spectator
        assert prompts_badminton.get_badminton_system_prompt(lang, mode="duel") != english_duel
        assert prompts_badminton.get_badminton_quick_lines_prompt(lang, mode="spectator") != english_quick
        assert prompts_badminton.get_badminton_pregame_context_prompt(lang) != english_pregame


@pytest.mark.unit
def test_badminton_pregame_context_normalize_and_prompt_injection():
    context, invalid = gr_pregame._normalize_badminton_pregame_context(
        {
            "gameStance": "competitive",
            "initialMood": "happy",
            "initialExpression": "hype",
            "initialIntensity": "high",
            "initialDifficulty": "max",
            "openingLine": "来比一局",
            "expressionPolicy": "更兴奋地盯着比分",
        },
        mode="duel",
    )

    assert invalid is False
    assert context["initialExpression"] == "hype"
    assert context["initialIntensity"] == "high"
    assert context["expressionPolicy"] == "更兴奋地盯着比分"

    prompt = gr_runtime._build_game_prompt(
        "badminton",
        "Neko",
        "傲娇猫娘",
        pre_game_context=context,
        language="zh",
        mode="duel",
    )
    assert "羽毛球开局上下文" in prompt
    assert "来比一局" in prompt
    assert "对战难度控制补充" in prompt


@pytest.mark.unit
def test_badminton_duel_difficulty_addendum_is_localized_per_full_locale():
    """The addendum used to be an inline ``if lang == "zh"`` fork in
    ``main_routers/game_router/balance.py``, so Traditional Chinese — and every
    locale other than Chinese and English — fell through to the English branch.

    Asserted on the getter, which is the layer that has to keep zh-CN and zh-TW
    apart. ``_normalize_prompt_lang`` (the short-locale scheme the rest of this
    module's tables use) spells Simplified Chinese ``zh``, so routing this table
    through it would miss its ``zh-CN`` row. Until issue #2500 step 2 it also
    collapsed zh-TW, which would have left that entry unreachable while still
    reading as compliant to the static ``check_prompt_zh_tw`` gate.
    """
    get = prompts_badminton.get_badminton_duel_difficulty_control_prompt

    assert get("zh-TW") != get("zh-CN")
    assert "對戰難度控制補充" in get("zh-TW")
    assert "对战难度控制补充" in get("zh-CN") == get("zh")
    for steam_code in ("tchinese", "zh-Hant", "zh-HK"):
        assert get(steam_code) == get("zh-TW"), steam_code

    # Every locale gets its own text; none silently rides the English branch.
    rendered = {loc: get(loc) for loc in ("zh-CN", "zh-TW", "en", "ja", "ko", "ru", "es", "pt")}
    assert len(set(rendered.values())) == len(rendered), "有 locale 拿到了别人的文案"

    # Wire literals the runtime parses back out must survive translation.
    for loc, text in rendered.items():
        assert '"difficulty"' in text, loc
        assert "max|lv2|lv3|lv4" in text, loc
        assert "spectator" in text, loc
        assert "balanceHint" in text, loc


@pytest.mark.unit
def test_badminton_duel_prompt_reaches_traditional_users_end_to_end():
    """Guards the *other* collapse: the table can be perfect and still never be
    reached, because ``session_pool`` used to pass ``char_info["user_language"]``
    (already short-normalized to ``zh``) rather than ``user_language_full``.
    """
    prompt = gr_runtime._build_game_prompt(
        "badminton",
        "Neko",
        "傲娇猫娘",
        language="zh-TW",
        mode="duel",
    )
    assert "對戰難度控制補充" in prompt
    assert "对战难度控制补充" not in prompt


@pytest.mark.unit
def test_badminton_pregame_opening_line_keeps_spec_length_cap():
    context, invalid = gr_pregame._normalize_badminton_pregame_context(
        {
            "openingLine": "1234567890123456",
        },
        mode="spectator",
    )

    assert invalid is True
    assert context["openingLine"] == ""


@pytest.mark.unit
def test_badminton_duel_balance_hint_and_anger_cap():
    hint = gr_balance._build_badminton_duel_balance_hint(
        {"duel": {"player_score": 1, "neko_score": 6, "round": 2, "max_rounds": 8}}
    )
    assert hint["state"] == "neko_leading"
    assert hint["diff"] == 5
    assert hint["remainingPoints"] == 12

    final_pending = gr_balance._build_badminton_duel_balance_hint(
        {"duel": {"player_score": 6, "neko_score": 4, "round": 5, "max_rounds": 5, "active_shooter": "neko"}}
    )
    assert final_pending["state"] == "player_leading"
    assert final_pending["remainingRounds"] == 0
    assert final_pending["remainingPoints"] == 2

    miss_pressure = gr_balance._build_badminton_duel_balance_hint(
        {"duel": {"player_score": 4, "neko_score": 6, "round": 6, "player_misses": 2, "neko_misses": 1, "max_misses": 3}}
    )
    assert miss_pressure["playerMissesLeft"] == 1
    assert miss_pressure["nekoMissesLeft"] == 2
    assert miss_pressure["maxMisses"] == 3

    current_state_fallback = gr_balance._build_badminton_duel_balance_hint(
        {"currentState": {"duel": {"playerScore": 1, "nekoScore": 5, "playerMisses": 2, "nekoMisses": 0, "maxMisses": 3}}}
    )
    assert current_state_fallback["state"] == "neko_leading"
    assert current_state_fallback["diff"] == 4
    assert current_state_fallback["playerMissesLeft"] == 1

    merged_current_state = gr_balance._build_badminton_duel_balance_hint(
        {
            "duel": {"player_score": 4},
            "currentState": {"duel": {"playerScore": 1, "nekoScore": 6, "playerMisses": 2, "nekoMisses": 1, "maxMisses": 3}},
        }
    )
    assert merged_current_state["diff"] == 2
    assert merged_current_state["playerMissesLeft"] == 1
    assert merged_current_state["nekoMissesLeft"] == 2

    miss_elimination_ignores_round_decider = gr_balance._build_badminton_duel_balance_hint(
        {"duel": {"player_score": 0, "neko_score": 9, "round": 5, "max_rounds": 5, "player_misses": 1, "neko_misses": 1, "max_misses": 3}}
    )
    assert miss_elimination_ignores_round_decider["state"] == "neko_leading"

    route_state = {
        "preGameContext": {"gameStance": "punishing", "initialMood": "angry"},
        "anger_pressure_accumulated": 24,
    }
    event = {
        "kind": "shot_missed",
        "mode": "duel",
        "label": "player_duel_shot",
        "difficulty": "max",
        "duel": {"player_score": 1, "neko_score": 6, "round": 5, "max_rounds": 8},
    }
    cap = gr_balance._build_badminton_duel_anger_pressure_cap(event, route_state)
    assert cap["reached"] is True

    result = gr_balance._apply_badminton_anger_pressure_cap(
        {"line": "继续", "control": {"difficulty": "max"}},
        {**event, "angerPressureCap": cap},
    )
    assert result["control"]["difficulty"] == "lv3"
    assert result["anger_pressure_cap"]["adjusted"] is True


@pytest.mark.unit
def test_game_memory_generic_keys_update_legacy_policy_fields():
    state = {}
    gr_runtime._update_game_memory_enabled_from_payload(
        state,
        {
            "game_memory_enabled": True,
            "game_memory_player_interaction_enabled": False,
            "game_memory_event_reply_enabled": True,
            "game_memory_archive_enabled": False,
            "game_memory_postgame_context_enabled": True,
        },
    )

    assert state["soccer_game_memory_enabled"] is True
    assert state["soccer_game_memory_player_interaction_enabled"] is False
    assert state["soccer_game_memory_event_reply_enabled"] is True
    assert state["soccer_game_memory_archive_enabled"] is False
    assert state["soccer_game_memory_postgame_context_enabled"] is True
    assert state["game_memory_enabled"] is True
    assert state["game_memory_archive_enabled"] is False


@pytest.mark.unit
def test_badminton_completed_result_records_score_before_returning():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "function persistCompletedResult() {" in html
    assert "if (game.resultRecorded || isPracticeMode()) return null;" in html
    assert "var entry = recordGame(game.bestStreak, getRunMaxDistancePx(), game.totalScore, game.shotTypeCount);" in html
    assert "persistCompletedResult();" in html


@pytest.mark.unit
def test_badminton_scoring_waits_for_route_end_and_records_run_max():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "function getRunMaxDistancePx() {" in html
    assert "var routeEndPromise = null;" in html
    assert "var completedSessionId = sessionId;" in html
    assert "var completedLanlanName = getRouteLanlanName();" in html
    assert "var routeEndReady = endedRoute && routeEndPromise ? routeEndPromise.catch(function () {}) : Promise.resolve();" in html
    assert "if (routeEndResult && routeEndResult.state) applyRouteIdentity(routeEndResult.state);" in html
    assert "var scoreLanlanName = completedLanlanName || getRouteLanlanName();" in html
    assert "session_id: completedSessionId," in html
    assert "lanlan_name: scoreLanlanName," in html
    assert "var entry = recordGame(game.bestStreak, getRunMaxDistancePx(), game.totalScore, game.shotTypeCount);" in html
    assert "keepalive: true" in html
    assert "var routeEndRequest = fetch(url, { method: 'POST'" in html
    assert "routeEndPromise = routeEndRequest;" in html
    assert "return res.json().catch(function () { return { ok: res.ok }; });" in html

    session_capture_index = html.index("var completedSessionId = sessionId;")
    route_ready_index = html.index("var routeEndReady = endedRoute && routeEndPromise ? routeEndPromise.catch(function () {}) : Promise.resolve();")
    persist_index = html.index("function persistCompletedResult() {")
    record_index = html.index("var entry = recordGame(", persist_index)
    assert session_capture_index < route_ready_index
    assert route_ready_index < record_index


@pytest.mark.unit
def test_badminton_reset_abandons_active_route_before_rotating_session():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    reset_start = html.index("function resetGame() {")
    reset_section = html[reset_start:html.index("function updateHud()", reset_start)]
    end_route_start = html.index("function endRoute(")
    end_route_section = html[end_route_start:html.index("function cycleTheme()", end_route_start)]

    assert "var shouldRestartRoute = endedRoute || routeActive || routeStartPromise || routeEndPromise || heartbeatTimer || drainTimer;" in reset_section
    assert "var routeSessionToEnd = sessionId;" in reset_section
    assert "var routeWasEnded = endedRoute;" in reset_section
    assert "var pendingRouteStart = routeStartPromise;" in reset_section
    assert "var pendingRouteEnd = routeEndPromise;" in reset_section
    assert "sessionId = createBadmintonSessionId();" in reset_section
    assert "var routeReadyForEnd = pendingRouteStart ? Promise.resolve(pendingRouteStart).catch(function () { return null; }) : Promise.resolve();" in reset_section
    assert "var restartAfterRouteEnd = routeReadyForEnd.then(function () {" in reset_section
    assert "if (routeWasEnded) return pendingRouteEnd || Promise.resolve();" in reset_section
    assert "return endRoute(false, { force: true, sessionId: routeSessionToEnd, detached: true });" in reset_section
    assert "Promise.resolve(restartAfterRouteEnd).catch(function () {}).then(function () {" in reset_section
    assert "badmintonGameDisposed" in reset_section
    assert "startRoute();" in reset_section
    assert reset_section.index("var routeSessionToEnd = sessionId;") < reset_section.index("sessionId = createBadmintonSessionId();")
    assert reset_section.index("var routeWasEnded = endedRoute;") < reset_section.index("endedRoute = false;")
    assert reset_section.index("var pendingRouteEnd = routeEndPromise;") < reset_section.index("routeEndPromise = null;")
    assert reset_section.index("sessionId = createBadmintonSessionId();") < reset_section.index("return endRoute(false, { force: true, sessionId: routeSessionToEnd, detached: true });")
    assert "var detached = options && options.detached;" in end_route_section
    assert "var routeEndSessionId = (options && options.sessionId) || sessionId;" in end_route_section
    assert "if (!detached) routeActive = false;" in end_route_section
    assert "heartbeatTimer = 0;" in end_route_section
    assert "drainTimer = 0;" in end_route_section
    assert "if (!detached && res && res.state && sessionId === routeEndSessionId) applyRouteIdentity(res.state);" in end_route_section


@pytest.mark.unit
def test_badminton_route_start_pending_close_is_cancelled_by_dispose():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    start_route = html[html.index("function startRoute() {"):html.index("function startRouteAfterCharacterReady()", html.index("function startRoute() {"))]
    dispose_section = html[html.index("function disposeBadmintonGame(reason) {"):html.index("function cycleTheme()", html.index("function disposeBadmintonGame(reason) {"))]

    assert "var routeStartPromise = null;" in html
    assert "return post('/route/start'" in start_route
    assert "if (badmintonGameDisposed || sessionId !== routeSessionId || endedRoute || game.state === 'game_over')" in start_route
    assert "if (badmintonGameDisposed) return Promise.resolve({ ok: false, reason: 'disposed' });" in start_route
    assert "if (badmintonGameDisposed) return res;" in start_route
    assert "endRoute(true, { force: true })" not in start_route
    assert "var pendingRouteStart = routeStartPromise;" in dispose_section
    assert "var routeEndAfterStart = pendingRouteStart ? Promise.resolve(pendingRouteStart).catch(function () { return null; }).then(function (res) {" in dispose_section
    assert "if (res && res.ok) return endRoute(true, { force: true });" in dispose_section
    assert "return endRoute(true);" in dispose_section
    assert "Promise.resolve(routeEndAfterStart).catch(function () {});" in dispose_section
    assert "badmintonGameDisposed = true;" in dispose_section


@pytest.mark.unit
def test_badminton_deferred_character_resolution_starts_only_active_requests():
    node_path = shutil.which("node")
    if not node_path:
        pytest.skip("node is required for the badminton character race harness")

    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    quick_start = html.index("function loadGeneratedQuickLines() {")
    quick_source = html[quick_start:html.index("var voiceArbiter =", quick_start)]
    route_start = html.index("function startRoute() {")
    route_source = html[
        route_start:html.index("function startRouteAfterCharacterReady()", route_start)
    ]
    harness = textwrap.dedent(
        r"""
        const assert = require('node:assert/strict');
        let badmintonGameDisposed = false;
        let endedRoute = false;
        let sessionId = 'session-1';
        let currentMode = 'duel';
        let currentMood = 'normal';
        let currentThemeKey = 'default';
        let routeStartPromise = null;
        let routeActive = false;
        let heartbeatTimer = 0;
        let drainTimer = 0;
        let generatedQuickLinesRequestSeq = 0;
        let generatedQuickLines = {};
        const debugMode = false;
        const game = { state: 'ready', recordDistance: 0 };
        const posts = [];
        let characterPromise;

        const loadLocalLeaderboard = () => ({ personal_best: null, recent_games: [] });
        const getRouteLanlanName = () => 'Mimi';
        const getDuelDifficultyName = () => 'normal';
        const getConversationLanguagePayload = () => ({ render_language: 'en' });
        const _badmintonGameMemoryPolicyPayload = () => ({});
        const getRequestLanguage = () => 'en';
        const normalizeRequestLanguagePrimary = (language) => language;
        const loadBadmintonCharacter = () => characterPromise;
        const post = (path, body, timeoutMs) => {
          posts.push({ path, body, timeoutMs });
          if (path === '/route/start') return Promise.resolve({ ok: false });
          return Promise.resolve({ ok: true, lines: {} });
        };

        __QUICK_SOURCE__
        __ROUTE_SOURCE__

        function deferredCharacter() {
          let resolve;
          const promise = new Promise((done) => { resolve = done; });
          return { promise, resolve };
        }

        (async () => {
          let deferred = deferredCharacter();
          characterPromise = deferred.promise;
          const endedStart = startRoute();
          endedRoute = true;
          deferred.resolve('ja');
          await endedStart;
          assert.equal(posts.filter((call) => call.path === '/route/start').length, 0);

          endedRoute = false;
          game.state = 'ready';
          deferred = deferredCharacter();
          characterPromise = deferred.promise;
          const gameOverStart = startRoute();
          game.state = 'game_over';
          deferred.resolve('ja');
          await gameOverStart;
          assert.equal(posts.filter((call) => call.path === '/route/start').length, 0);

          game.state = 'ready';
          badmintonGameDisposed = false;
          deferred = deferredCharacter();
          characterPromise = deferred.promise;
          const quickLines = loadGeneratedQuickLines();
          badmintonGameDisposed = true;
          deferred.resolve('ja');
          await quickLines;
          assert.equal(posts.filter((call) => call.path === '/quick-lines').length, 0);

          badmintonGameDisposed = false;
          endedRoute = false;
          game.state = 'ready';
          deferred = deferredCharacter();
          characterPromise = deferred.promise;
          const activeStart = startRoute();
          assert.equal(posts.filter((call) => call.path === '/route/start').length, 0);
          deferred.resolve('ja');
          await activeStart;
          assert.equal(posts.filter((call) => call.path === '/route/start').length, 1);
          assert.equal(routeStartPromise, null);
          assert.equal(heartbeatTimer, 0);
          assert.equal(drainTimer, 0);
          process.stdout.write('ok');
        })().catch((error) => {
          process.stderr.write(error && error.stack ? error.stack : String(error));
          process.exitCode = 1;
        });
        """
    ).replace("__QUICK_SOURCE__", quick_source).replace("__ROUTE_SOURCE__", route_source)

    result = run_node_script(
        node_path,
        harness,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "ok"


@pytest.mark.unit
def test_badminton_frame_loop_is_cancellable_on_dispose():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    frame_section = html[html.index("function loop(ts) {"):html.index("function getRouteLanlanName()", html.index("function loop(ts) {"))]

    assert "var badmintonFrameRequestId = 0;" in html
    assert "var badmintonHiddenFrameTimer = 0;" in html
    assert "if (badmintonGameDisposed) return;" in frame_section
    assert "badmintonHiddenFrameTimer = setTimeout(function () {" in frame_section
    assert "badmintonFrameRequestId = requestAnimationFrame(loop);" in frame_section
    assert "requestAnimationFrame(loop);" not in frame_section.replace("badmintonFrameRequestId = requestAnimationFrame(loop);", "")


@pytest.mark.unit
def test_badminton_generated_quick_lines_override_static_i18n_lines():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "var generatedQuickLines = {};" in html
    assert "var generated = generatedQuickLines[key] || [];" in html
    assert "if (!(options && options.skipGenerated) && generated.length) return generated[Math.floor(Math.random() * generated.length)] || '';" in html
    assert "generatedQuickLines[key] = pool;" in html
    assert "quickLines[key] = pool;" not in html


@pytest.mark.unit
def test_badminton_request_language_ignores_template_default_until_i18n_resolves():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    start = html.index("function getRequestLanguage()")
    request_language = html[start:html.index("function api(path)", start)]

    assert "function normalizeRequestLanguagePrimary(value) {" in html
    assert "var hasResolvedI18nLanguage = false;" in request_language
    assert "var documentLangIsTemplateDefault = /^zh(?:-CN)?$/i.test(documentLang) && !hasResolvedI18nLanguage;" in request_language
    assert "if (documentLang && !documentLangIsTemplateDefault) candidates.push(documentLang);" in request_language
    assert request_language.index("localStorage.getItem('i18nextLng')") < request_language.index("navigator.language")


@pytest.mark.unit
def test_badminton_non_chinese_static_quick_line_fallbacks_are_not_chinese():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "var quickLinesFallbackEn = {" in html
    assert "var quickLinesFallbackJa = {" in html
    assert "var YUI_PASSIVE_LINES_DUEL_EN = {" in html
    assert "var YUI_PASSIVE_LINES_DUEL_JA = {" in html
    assert "function getQuickLineFallbackSource(primary) {" in html
    assert "if (primary === 'zh') return YUI_PASSIVE_LINES_DUEL;" in html
    assert "if (primary === 'ja') return YUI_PASSIVE_LINES_DUEL_JA;" in html
    assert "return YUI_PASSIVE_LINES_DUEL_EN;" in html
    assert "function getDefaultQuickLineFallback(primary) {" in html
    assert "if (primary === 'zh') return quickLines;" in html
    assert "if (primary === 'ja') return quickLinesFallbackJa;" in html
    assert "return quickLinesFallbackEn;" in html


@pytest.mark.unit
def test_badminton_unknown_quick_line_key_does_not_fallback_to_game_over():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    pick_line = html[html.index("function pickLine(key, primary, options) {"):html.index("function getRequestLanguage()", html.index("function pickLine(key, primary, options) {"))]

    assert "function getNeutralQuickLineFallback(primary) {" in html
    assert "var isGameOverKey = key === 'game_over';" in pick_line
    assert "var fallback = source[key] || defaultFallback[key] || (isGameOverKey ? (source.game_over || defaultFallback.game_over) : null) || getNeutralQuickLineFallback(primary);" in pick_line
    assert "(isGameOverKey ? (source.game_over || defaultFallback.game_over) : null)" in pick_line
    assert "source[key] || defaultFallback[key] || source.game_over" not in pick_line
    assert "|| source.game_over || defaultFallback.game_over" not in pick_line
    assert "|| getNeutralQuickLineFallback(primary)" in pick_line


@pytest.mark.unit
def test_badminton_game_over_line_is_suppressed_after_first_session_fallback():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    suppress_start = html.index("function shouldSuppressRepeatedGameOverLine(event) {")
    suppress_section = html[suppress_start:html.index("function speakLine(line, control, event) {", suppress_start)]
    speak_start = html.index("function speakLine(line, control, event) {")
    speak_section = html[speak_start:html.index("function getActiveAvatarContainer()", speak_start)]

    assert "var gameOverLineSpokenSessionId = '';" in html
    assert "if (!event || event.kind !== 'game_over') return false;" in suppress_section
    assert "var eventSessionId = String(event.session_id || sessionId || '');" in suppress_section
    assert "if (gameOverLineSpokenSessionId === eventSessionId) return true;" in suppress_section
    assert "gameOverLineSpokenSessionId = eventSessionId;" in suppress_section
    assert "if (shouldSuppressRepeatedGameOverLine(event || {})) return;" in speak_section
    assert speak_section.index("if (shouldSuppressRepeatedGameOverLine(event || {})) return;") < speak_section.index("line = replaceUnexpectedChineseFallbackLine(line, event);")
    assert speak_section.index("if (shouldSuppressRepeatedGameOverLine(event || {})) return;") < speak_section.index("if (!shouldReadBadmintonVoice(event, control)) {")


@pytest.mark.unit
def test_badminton_reset_game_clears_game_over_line_gate():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    reset_start = html.index("function resetGame() {")
    reset_section = html[reset_start:html.index("game.state = 'ready';", reset_start)]

    assert "gameOverLineSpokenSessionId = '';" in reset_section
    assert reset_section.index("gameOverLineSpokenSessionId = '';") < reset_section.index("sessionId = createBadmintonSessionId();")


@pytest.mark.unit
def test_badminton_chat_empty_or_inactive_response_uses_safe_fallback():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    helper_start = html.index("function shouldSuppressChatFallback(res) {")
    helper_section = html[helper_start:html.index("function buildBadmintonCurrentStatePayload()", helper_start)]
    send_event_start = html.index("function sendGameEvent(")
    send_event = html[send_event_start:html.index("function loadLocalLeaderboard(", send_event_start)]

    assert "return !!(res && res.suppress_fallback);" in helper_section
    assert "reason === 'rate_limited'" not in helper_section
    assert "reason === 'route_inactive'" not in helper_section
    assert "reason === 'route_not_active'" not in helper_section
    assert "reason === 'client_timeout'" not in helper_section
    assert "function pickChatFallbackLine(res, event) {" in helper_section
    assert "if (shouldSuppressChatFallback(res)) return '';" in helper_section
    assert "return pickLine(getFallbackLineKey(event), null, {});" in helper_section
    assert helper_section.index("if (shouldSuppressChatFallback(res)) return '';") < helper_section.index("return pickLine(getFallbackLineKey(event), null, {});")
    assert "if (res && res.line) speakLine(moodStyleLine(res.line, currentMood), res.control || {}, event);" in send_event
    assert "var fallbackLine = pickChatFallbackLine(res, event);" in send_event
    assert "if (fallbackLine) speakLine(fallbackLine, {}, event);" in send_event
    assert "var fallbackLine = pickChatFallbackLine({ error: 'request_failed' }, event);" in send_event


@pytest.mark.unit
def test_badminton_speak_line_guards_non_chinese_from_chinese_fallback_text():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "function isLikelyChineseFallbackLine(line) {" in html
    assert "var _jaFallbackLineLookup = null;" in html
    assert "function isKnownJapaneseFallbackLine(text) {" in html
    assert "var _zhFallbackLineLookup = null;" in html
    assert "function isKnownChineseFallbackLine(text) {" in html
    assert "function replaceUnexpectedChineseFallbackLine(line, event) {" in html
    assert "if (primary === 'zh') return text;" in html
    assert "if (primary === 'ja' && isKnownJapaneseFallbackLine(text)) return text;" in html
    assert "if (primary === 'ja' && !isKnownChineseFallbackLine(text)) return text;" in html
    assert "if (!isLikelyChineseFallbackLine(text)) return text;" in html
    assert "var fallback = pickLine(fallbackKey, primary, { skipGenerated: true });" in html
    assert "if (!(options && options.skipGenerated) && generated.length)" in html
    assert "return fallback || (primary === 'ja' ? 'もう一回やる？' : 'One more rally.');" in html

    speak_start = html.index("function speakLine(line, control, event) {")
    speak_section = html[speak_start:html.index("var isUserReply =", speak_start)]
    assert "line = replaceUnexpectedChineseFallbackLine(line, event);" in speak_section
    bubble_start = html.index("function showBubble(line, control, event) {")
    bubble_section = html[bubble_start:html.index("function getBubblePriority(control)", bubble_start)]
    assert "var text = replaceUnexpectedChineseFallbackLine(line, event);" in bubble_section
    assert "showBubble(line, control, event);" in html


@pytest.mark.unit
def test_badminton_localechange_refreshes_generated_quick_lines():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    start = html.index("addBadmintonEventListener(window, 'localechange', function () {")
    localechange = html[start:html.index("});", start) + len("});")]

    assert "generatedQuickLines = {};" in localechange
    assert "if (routeActive) loadGeneratedQuickLines();" in localechange
    assert "var quickLinesRequestLanguage = getRequestLanguage();" in html
    assert "var quickLinesRequestPrimary = normalizeRequestLanguagePrimary(quickLinesRequestLanguage);" in html
    assert "if (quickLinesRequestPrimary !== normalizeRequestLanguagePrimary(getRequestLanguage())) return;" in html


@pytest.mark.unit
def test_badminton_route_end_payload_contains_archive_score():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "finalScore: {" in html
    assert "player: game.totalScore," in html
    assert "ai: isDuelMode() ? game.duel.nekoScore : 0," in html
    assert "var roundCompleted = game.state === 'game_over';" in html
    assert "reason: roundCompleted ? 'badminton_game_over' : 'badminton_abandoned'," in html
    assert "roundCompleted: roundCompleted," in html
    assert "round_completed: roundCompleted," in html
    assert "postgameProactive: roundCompleted," in html
    assert "state: game.state," in html
    assert "currentState: {\n        game: 'badminton',\n        state: game.state,\n        mode: currentMode,\n        score: {" in html
    assert "max_distance_px: getRunMaxDistancePx()," in html


@pytest.mark.unit
def test_badminton_chat_replies_are_ignored_after_session_or_mode_changes():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    send_event_start = html.index("function sendGameEvent(")
    send_event = html[send_event_start:html.index("function loadLocalLeaderboard(", send_event_start)]

    stale_reply_guard = "if (event.session_id !== sessionId || event.mode !== currentMode) return;"
    assert stale_reply_guard in send_event
    guard_index = send_event.index(stale_reply_guard)
    control_index = send_event.index("if (res && res.control) {")
    line_index = send_event.index("if (res && res.line) speakLine(")
    assert guard_index < control_index
    assert guard_index < line_index
    catch_index = send_event.index(".catch(function () {")
    catch_guard_index = send_event.index(stale_reply_guard, catch_index)
    assert catch_index < catch_guard_index


@pytest.mark.unit
def test_badminton_route_start_timeout_covers_backend_pregame_generation():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    start = html.index("function startRoute() {")
    section = html[start:html.index("function startRouteAfterCharacterReady()", start)]

    assert "return post('/route/start'" in section
    assert "}, getConversationLanguagePayload(), _badmintonGameMemoryPolicyPayload()), 22000);" in section


@pytest.mark.unit
def test_badminton_heartbeat_sends_live_current_state():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    heartbeat_index = html.index("post('/route/heartbeat'")
    heartbeat_section = html[max(0, heartbeat_index - 500):heartbeat_index + 500]
    current_state_start = html.index("function buildBadmintonCurrentStatePayload() {")
    current_state_section = html[current_state_start:html.index("function sendGameEvent(", current_state_start)]

    assert "post('/route/heartbeat'" in heartbeat_section
    assert "currentState: buildBadmintonCurrentStatePayload()" in heartbeat_section
    assert re.search(r"score:\s*{\s*player:\s*game\.totalScore", current_state_section)
    assert re.search(
        r"ai:\s*isDuelMode\(\)\s*\?\s*game\.duel\.nekoScore\s*:\s*0",
        current_state_section,
    )
    assert re.search(r"total_score:\s*game\.totalScore", current_state_section)


@pytest.mark.unit
def test_badminton_memory_toggle_does_not_auto_enable_from_history():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    init_start = html.index("function _initBadmintonGameMemoryToggle() {")
    init_section = html[init_start:html.index("function getAudioCtx()", init_start)]

    assert "setCheckedIfChanged(gameMemoryToggle, false);" in init_section
    assert "_hasHistoricalBadmintonRecord" not in html
    assert "bd_record_distance" not in init_section
    assert "bd_leaderboard" not in init_section


@pytest.mark.unit
def test_badminton_duel_player_shots_update_recorded_stats():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    finish_duel = html[html.index("function finishDuelShot("):html.index("function finishShot(", html.index("function finishDuelShot("))]

    assert "if (shooter === 'player') {" in finish_duel
    assert "game.streak += 1;" in finish_duel
    assert "game.madeCount += 1;" in finish_duel
    assert "game.bestStreak = Math.max(game.bestStreak, game.streak);" in finish_duel
    assert "if (game.shotTypeCount[shotType] != null) game.shotTypeCount[shotType] += 1;" in finish_duel
    assert "newRecord = previousDistance > game.recordDistance;" in finish_duel
    assert "game.recordDistance = previousDistance;" in finish_duel
    assert "writeBadmintonStorage('bd_record_distance', String(Math.round(game.recordDistance)));" in finish_duel
    assert "kind: newRecord ? 'new_record' : (scored ? 'shot_result' : 'shot_missed')," in finish_duel
    assert "is_new_record: newRecord," in finish_duel
    assert "game.streak = 0;" in finish_duel


@pytest.mark.unit
def test_badminton_duel_uses_eleven_miss_elimination_instead_of_five_round_cap():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    finish_duel = html[html.index("function finishDuelShot("):html.index("function finishShot(", html.index("function finishDuelShot("))]

    assert "playerMisses: 0," in html
    assert "nekoMisses: 0," in html
    assert "maxMisses: 11," in html
    assert "game.duel.playerMisses = 0;" in html
    assert "game.duel.nekoMisses = 0;" in html
    assert "point = 1;" in finish_duel
    assert "pointWinner = scored ? shooter : (shooter === 'player' ? 'neko' : 'player');" in finish_duel
    assert "if (shooter === 'player') game.duel.nekoMisses += 1;" in finish_duel
    assert "if (shooter === 'player') game.duel.playerMisses += 1;" in finish_duel
    assert "else game.duel.nekoMisses += 1;" in finish_duel
    duel_finished = "var duelFinished = game.duel.playerMisses >= game.duel.maxMisses || game.duel.nekoMisses >= game.duel.maxMisses;"
    assert duel_finished in finish_duel
    assert finish_duel.index(duel_finished) < finish_duel.index("kind: newRecord ? 'new_record' : (scored ? 'shot_result' : 'shot_missed'),")
    assert "if (!duelFinished) {\n      sendGameEvent({" in finish_duel
    assert "} else if (pointWinner === 'neko') {" in finish_duel
    assert "player_misses: game.duel.playerMisses" in html
    assert "neko_misses: game.duel.nekoMisses" in html
    assert "max_misses: game.duel.maxMisses" in html
    assert "result: scored ? 'scored' : 'missed'" in finish_duel
    assert "duel_outcome: didPlayerWinDuel() ? 'player_win' : 'neko_win'" in finish_duel
    assert "game.duel.playerScore > game.duel.nekoScore" not in html
    assert "isDuelMode() && isDuelEliminated() && didPlayerWinDuel()" in html
    assert "_i18n('hud.duelMisses'" in html
    assert "_i18n('result.duelElimination'" in html
    assert "maxRounds: 5" not in html
    assert "game.duel.round >= game.duel.maxRounds" not in html
    assert "max_rounds: game.duel.maxRounds" not in html


@pytest.mark.unit
def test_badminton_start_menu_bgm_tracks_start_screen_state():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    resolve_start = html.index("function _bdResolvePlaylist() {")
    resolve_section = html[resolve_start:html.index("function _bdHasPlayableBgmTarget", resolve_start)]
    start_screen_start = html.index("function startBadmintonFromStartScreen(options) {")
    start_screen_section = html[start_screen_start:html.index("function showBadmintonStartScreenIfNeeded()", start_screen_start)]
    activation_start = html.index("function activateBadmintonStartMenuAudio(ev) {")
    activation_section = html[activation_start:html.index("function hideBadmintonStartOverlay()", activation_start)]

    assert "var badmintonStartMenuAudioActivated = false;" in html
    assert "if (!badmintonStartAccepted) {\n        return { key: 'startMenu', playlist: config.bgm.startMenu, repeat: true };\n      }" in resolve_section
    assert resolve_section.index("if (!badmintonStartAccepted)") < resolve_section.index("if (game.state === 'game_over')")
    assert "badmintonStartButton.contains(ev.target)" in activation_section
    assert "badmintonStartMenuAudioActivated = true;" in activation_section
    assert "if (ac && ac.state === 'suspended') ac.resume().catch(function () {});" in activation_section
    assert "badmintonGameAudio.unlock();" in activation_section
    assert "if (bgmEnabled) startBgm();" in activation_section
    assert "removeBadmintonStartMenuAudioListeners();" in activation_section
    assert "badmintonStartAccepted = true;" in start_screen_section
    assert "removeBadmintonStartMenuAudioListeners();" in start_screen_section
    assert start_screen_section.index("badmintonStartAccepted = true;") < start_screen_section.index("if (!options.skipTutorial) resumeAudio();")
    assert "var badmintonStartMenuAudioListenerCleanups = [];" in html
    assert "badmintonStartMenuAudioListenerCleanups.push(addBadmintonEventListener(window, 'pointerdown', activateBadmintonStartMenuAudio, true));" in html
    assert "badmintonStartMenuAudioListenerCleanups.push(addBadmintonEventListener(window, 'keydown', activateBadmintonStartMenuAudio, true));" in html
    assert "badmintonStartMenuAudioListenerCleanups.push(addBadmintonEventListener(window, 'touchstart', activateBadmintonStartMenuAudio, true));" in html
    assert "while (badmintonStartMenuAudioListenerCleanups.length)" in html
    assert "var cleanupIndex = badmintonEventCleanupFns.indexOf(cleanup);" in html
    assert "if (cleanupIndex >= 0) badmintonEventCleanupFns.splice(cleanupIndex, 1);" in html
    assert "window.removeEventListener('pointerdown', activateBadmintonStartMenuAudio, true);" not in html


@pytest.mark.unit
def test_badminton_mood_bgm_uses_looped_configs_instead_of_end_segments():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    audio_config = (ROOT / "static" / "game" / "games" / "badminton" / "badminton-audio-config.js").read_text(encoding="utf-8")
    resolve_start = html.index("function _bdResolvePlaylist() {")
    resolve_section = html[resolve_start:html.index("function _bdHasPlayableBgmTarget", resolve_start)]
    difficulty_start = html.index("function setDuelDifficulty(name) {")
    difficulty_section = html[difficulty_start:html.index("function getPlayerWinDifficultyIndex", difficulty_start)]

    assert "if (moodBgm.loop) {" in resolve_section
    assert "if (currentMood === 'angry' && getDuelDifficultyName() !== 'max') {" in resolve_section
    assert "moodBgm = null;" in resolve_section
    assert "if ((currentMood === 'happy' || currentMood === 'relaxed') && ['lv3', 'lv4'].indexOf(getDuelDifficultyName()) === -1) {" in resolve_section
    assert "var moodBgmKey = (currentMood === 'happy' || currentMood === 'relaxed')" in resolve_section
    assert "? 'mood:chocobos'" in resolve_section
    assert "key: moodBgmKey" in resolve_section
    assert "loopedConfig: moodBgm" in resolve_section
    assert "audio.playLoopedBgm(resolved.loopedConfig" in html
    assert "happy: {\n          intro: '/static/game/games/soccer/audio/Chocobos_S.mp3',\n          loop: '/static/game/games/soccer/audio/Chocobos_L.mp3'," in audio_config
    assert "relaxed: {\n          intro: '/static/game/games/soccer/audio/Chocobos_S.mp3',\n          loop: '/static/game/games/soccer/audio/Chocobos_L.mp3'," in audio_config
    assert "angry: {\n          loop: '/static/game/games/soccer/audio/纯狐_心之所在_L.mp3',\n          outro: '/static/game/games/soccer/audio/纯狐_心之所在_E.mp3'," in audio_config
    assert "calm: []," in audio_config
    assert "sad: []," in audio_config
    assert "surprised: []," in audio_config
    assert "happy: {\n          gainDb:" not in audio_config
    assert "angry: {\n          gainDb: -2.94," not in audio_config
    assert "surprised: {\n          gainDb:" not in audio_config
    assert "surprised: [{ src: '/static/game/games/soccer/audio/Battle_1_E.mp3'" not in audio_config
    assert "var changed = duelDifficultyIdx !== i;" in difficulty_section
    assert "if (changed && bgmEnabled) badmintonGameAudio.sync('difficulty-changed');" in difficulty_section
    assert difficulty_section.index("duelDifficultyIdx = i;") < difficulty_section.index("if (changed && bgmEnabled) badmintonGameAudio.sync('difficulty-changed');")


@pytest.mark.unit
def test_badminton_reselects_in_game_bgm_when_returning_from_mood_bgm():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    pick_start = html.index("function _bdPickInGameBgm() {")
    pick_section = html[pick_start:html.index("function _bdShouldReselectInGameBgm()", pick_start)]
    reselect_start = html.index("function _bdShouldReselectInGameBgm() {")
    reselect_section = html[reselect_start:html.index("var _bdSelectedInGameBgm", reselect_start)]
    resolve_start = html.index("function _bdResolvePlaylist() {")
    resolve_section = html[resolve_start:html.index("function _bdHasPlayableBgmTarget", resolve_start)]

    assert "return variants[Math.floor(Math.random() * variants.length)];" in pick_section
    assert "return _bdBgmCurrentKey.indexOf('mood:') === 0;" in reselect_section
    assert "var _bdSelectedInGameBgm = _bdPickInGameBgm();" in html
    assert "if (_bdShouldReselectInGameBgm()) {\n        _bdSelectedInGameBgm = _bdPickInGameBgm();\n      }" in resolve_section
    assert resolve_section.index("if (_bdShouldReselectInGameBgm())") > resolve_section.index("return { key: moodBgmKey, playlist: moodBgm, repeat: true };")
    assert resolve_section.index("if (_bdShouldReselectInGameBgm())") < resolve_section.index("if (_bdSelectedInGameBgm && _bdSelectedInGameBgm.loop)")


@pytest.mark.unit
def test_badminton_result_bgm_only_starts_once_per_completed_game():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    show_result = html[html.index("function showResult() {"):html.index("function persistCompletedResult()", html.index("function showResult() {"))]
    reset_start = html.index("function resetGame() {")
    reset_section = html[reset_start:html.index("function updateHud()", reset_start)]
    result_bgm_start = html.index("function playResultBgmOnce() {")
    result_bgm_section = html[result_bgm_start:html.index("function showResult()", result_bgm_start)]
    sync_bgm_start = html.index("function syncBgm(reason) {")
    sync_bgm_section = html[sync_bgm_start:html.index("function resetSyncKey()", sync_bgm_start)]
    reset_sync_start = html.index("function resetSyncKey() {")
    reset_sync_section = html[reset_sync_start:html.index("(function scheduleAudioPreload()", reset_sync_start)]

    assert "resultBgmPlayed: false," in html
    assert "function playResultBgmOnce() {" in html
    assert "if (game.resultBgmPlayed) return false;" in result_bgm_section
    assert "game.resultBgmPlayed = true;" in result_bgm_section
    assert "badmintonGameAudio.sync('game-over');" in result_bgm_section
    assert "playResultBgmOnce();" in show_result
    assert "badmintonGameAudio.sync('game-over');" not in show_result.replace("playResultBgmOnce();", "")
    assert "game.resultBgmPlayed = false;" in reset_section
    assert "badmintonGameAudio.resetSyncKey();" in reset_section
    assert "if (bgmEnabled) badmintonGameAudio.sync('reset');" in reset_section
    assert reset_section.index("game.duel.sandbagShots = 0;") < reset_section.index("if (bgmEnabled) badmintonGameAudio.sync('reset');")
    assert "var _bdResultBgmTriggered = false;" in html
    assert "if (resolved.key === 'gameOver') {" in sync_bgm_section
    assert "if (_bdResultBgmTriggered) return;" in sync_bgm_section
    assert "_bdResultBgmTriggered = true;" in sync_bgm_section
    assert "_bdResultBgmTriggered = false;" in reset_sync_section


@pytest.mark.unit
def test_badminton_normal_chat_request_carries_client_timeout_for_memory_guard():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    chat_start = html.index("var chatClientTimeoutMs = 6500;")
    chat_section = html[chat_start:html.index(".then(function (res) {", chat_start)]

    assert "client_timeout_ms: chatClientTimeoutMs" in chat_section
    assert "}), chatClientTimeoutMs)" in chat_section


@pytest.mark.unit
def test_badminton_user_reply_voice_deadline_survives_inflight_guard():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    speak_start = html.index("function speakLine(line, control, event) {")
    speak_section = html[speak_start:html.index("function getActiveAvatarContainer()", speak_start)]

    assert "if (isUserReply) {" in speak_section
    assert "voiceArbiter.inFlight.expiresAt + VOICE_ARBITER_DEFAULTS.tailWaitMs" in speak_section
    assert "entry.expiresAt = Math.max(" in speak_section


@pytest.mark.unit
def test_badminton_voice_request_does_not_use_browser_playback_bridge():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    request_start = html.index("function _requestVoicePlayback(entry) {")
    request_section = html[request_start:html.index("function _flushVoiceArbiter()", request_start)]

    assert "readVoiceOccupancy()" not in request_section
    assert "playbackDeferredOnce" not in request_section
    assert "voice_deferred_once" not in request_section
    assert "showBubble(entry.line, entry.control, entry.event);" in request_section


@pytest.mark.unit
def test_badminton_debug_voice_mode_allows_tts_when_debugging():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    mirror_start = html.index("function _mirrorAndSpeak(entry) {")
    mirror_section = html[mirror_start:html.index("function _requestVoicePlayback(entry) {", mirror_start)]

    assert "var debugVoiceMode = modeParams ? modeParams.get('debug_voice') === '1' : false;" in html
    assert "var debugVoiceMuted = modeParams ? modeParams.get('debug_mute_voice') === '1' : false;" in html
    assert "SPEECH_PLAYBACK_STATE_KEY" not in html
    assert "SPEECH_PLAYBACK_CHANNEL_NAME" not in html
    assert "function normalizeSpeechPlaybackState(raw) {" not in html
    assert "function readVoiceOccupancy() {" not in html
    assert "function waitForProjectVoicePlayback(speechId, timeoutMs, requestStartedAt) {" not in html
    assert "initSpeechPlaybackStateBridge();" not in html
    assert "function closeSpeechPlaybackStateBridge() {" not in html
    assert "function speakLineLocally(line) {" not in html
    assert "SpeechSynthesisUtterance" not in html
    assert "speechSynthesis" not in html
    assert "if (debugMode && debugVoiceMuted && !debugVoiceMode && !(entry.event && entry.event.force_voice_in_debug)) return Promise.resolve();" in mirror_section
    assert "hasProjectVoicePlaybackBridge" not in mirror_section
    assert "missing_project_voice_playback_bridge" not in mirror_section
    assert "lastVoiceRequest = speakPayload;" in mirror_section
    assert "function shouldInterruptVoiceAudio(event) {" in html
    assert "return kind === 'yui_cheat_item' || kind === 'yui_cheat_hit' || kind === 'yui_cheat_score' || kind === 'player_ice_score';" in html
    assert "interrupt_audio: shouldInterruptVoiceAudio(entry.event)," in mirror_section
    assert "function requestMirror() {" in mirror_section
    assert "function requestSpeak() {" in mirror_section
    assert "if (shouldInterruptVoiceAudio(entry.event)) {" in mirror_section
    assert "void requestMirror();" in mirror_section
    assert "return requestSpeak();" in mirror_section
    assert "return requestMirror().then(requestSpeak);" in mirror_section
    assert "reason: 'project_speak_failed'" in mirror_section
    assert "return post('/speak', speakPayload, 3500).then(function (res) {" in mirror_section


@pytest.mark.unit
def test_badminton_voice_selection_reads_only_high_value_yui_lines():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    selector_start = html.index("function shouldReadBadmintonVoice(event, control) {")
    selector_section = html[selector_start:html.index("function speakLine(line, control, event) {", selector_start)]
    speak_start = html.index("function speakLine(line, control, event) {")
    speak_section = html[speak_start:html.index("function getActiveAvatarContainer()", speak_start)]

    assert "if (event && (event.force_voice || event.force_voice_in_debug || event.voice_always)) return true;" in selector_section
    assert "kind === 'yui_cheat_item' || kind === 'yui_cheat_hit' || kind === 'yui_cheat_score'" in selector_section
    assert "kind === 'difficulty_ramp'" in selector_section
    assert "kind === 'game_over' && (event.duel_outcome === 'neko_win' || event.point_winner === 'neko')" not in selector_section
    assert "event.point_winner === 'neko' && label === 'neko_duel_shot'" in selector_section
    assert "kind === 'user_reply' || event.isUserReply || event.source === 'user_reply'" in selector_section
    assert "if (!shouldReadBadmintonVoice(event, control)) {" in speak_section
    assert "lastVoiceMutedReason = 'voice_event_not_selected';" in speak_section
    assert "showBubble(line, control || {}, event);" in speak_section
    assert "muted: true" in speak_section
    assert "lastMutedReason: lastVoiceMutedReason" in html


@pytest.mark.unit
def test_badminton_drain_reads_nested_result_line():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "var result = item && typeof item.result === 'object' ? item.result : null;" in html
    assert "(result && (result.line || result.text || result.content))" in html
    assert "var control = (item && item.control) || (result && result.control) || {};" in html
    assert "speakLine(line, control, Object.assign({" in html
    assert "kind: 'user_reply'," in html


@pytest.mark.unit
def test_badminton_duel_voice_request_carries_client_timeout_for_memory_guard():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    voice_start = html.index("function buildNekoDuelTurnEvent() {")
    voice_section = html[voice_start:html.index("function queueNekoDuelTurnVoice()", voice_start)]

    assert "client_timeout_ms: 2200" in voice_section


@pytest.mark.unit
def test_badminton_voice_entries_freeze_route_identity():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "function resetVoiceArbiter() {" in html
    assert "voiceArbiter.pending = null;" in html
    assert "voiceArbiter.inFlight = null;" in html
    assert "function _voiceEntryMatchesCurrentSession(entry) {" in html
    assert "if (!_voiceEntryMatchesCurrentSession(entry)) return Promise.resolve();" in html
    assert "if (!_voiceEntryMatchesCurrentSession(pending)) {" in html
    assert "var entrySessionId = String((event && event.session_id) || sessionId || '');" in html
    assert "var entryLanlanName = String((event && (event.lanlan_name || event.lanlanName)) || getRouteLanlanName() || lanlanName || '');" in html
    assert "sessionId: entrySessionId," in html
    assert "lanlanName: entryLanlanName," in html
    assert "var entrySessionId = entry.sessionId || sessionId;" in html
    assert "session_id: entrySessionId," in html
    assert "lanlan_name: entryLanlanName," in html


@pytest.mark.unit
def test_badminton_delayed_results_are_bound_to_session():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "var resultTimer = 0;" in html
    assert "function scheduleShowResult(delayMs) {" in html
    assert "var resultSessionId = sessionId;" in html
    assert "persistCompletedResult();" in html
    assert "if (sessionId !== resultSessionId || game.state !== 'game_over') return;" in html
    assert "clearTimeout(resultTimer);" in html
    assert "setTimeout(showResult, 900);" not in html
    assert "setTimeout(showResult, 500);" not in html


@pytest.mark.unit
def test_badminton_starts_route_after_character_resolution_before_avatar_loading():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "initNekoAvatar().finally(function () { startRoute(); });" not in html
    assert "var badmintonCharacterPromise = null;" in html
    assert "return badmintonCharacterPromise;" in html
    after_ready_start = html.index("function startRouteAfterCharacterReady() {")
    after_ready = html[after_ready_start:html.index("function endRoute(", after_ready_start)]
    assert "return startRoute();" in after_ready
    assert "var routeLanlanName = getRouteLanlanName();" in html
    assert "var routeSessionId = sessionId;" in html
    assert "lanlan_name: routeLanlanName" in html
    assert "if (badmintonGameDisposed || sessionId !== routeSessionId || endedRoute || game.state === 'game_over') {" in html
    assert "applyRouteIdentity(res.state);" in html
    assert "function startBadmintonFromStartScreen(options) {" in html
    assert "badmintonRouteStartPromise = startRouteAfterCharacterReady();" in html
    startup = html[html.rindex("afterInitialPaint(function () {"):]
    assert startup.index("var characterReady = loadBadmintonCharacter();") < startup.index("var nekoAvatarReady = initNekoAvatar();")
    assert "scheduleBadmintonLoadingDismiss([characterReady, playerAvatarReady, nekoAvatarReady]);" in startup


@pytest.mark.unit
def test_badminton_vrm_waits_for_three_before_resolving_modules():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    wait_start = html.index("function waitForVRMModules() {")
    wait_section = html[wait_start:html.index("function fitVRMToContainer(", wait_start)]

    assert "function waitForThreeModule() {" in wait_section
    assert "if (window.THREE) return Promise.resolve();" in wait_section
    assert "window.addEventListener('three-ready', resolve, { once: true });" in wait_section
    assert "return Promise.all([vrmModulesReady, waitForThreeModule()]).then(function () {});" in wait_section


@pytest.mark.unit
def test_badminton_net_visual_extends_along_y_axis_for_depth():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "var NET_VISUAL_DEPTH_X = 8;" in html
    assert "var NET_VISUAL_DEPTH_Y = 34;" in html
    assert "var NET_VISUAL_DEPTH_OPACITY = 0.36;" in html
    assert "function getNetVisualDepthPoint(point, depthT) {" in html
    assert "var side = point.x < BADMINTON.netX ? -1 : 1;" in html
    assert "x: point.x + side * NET_VISUAL_DEPTH_X * t" in html
    assert "y: point.y + NET_VISUAL_DEPTH_Y * t" in html
    assert "function lerpNetPoint(a, b, t) {" in html
    assert "function drawNetDepthVolume() {" in html
    depth_start = html.index("function drawNetDepthVolume() {")
    depth_section = html[depth_start:html.index("function drawNetFront(now)", depth_start)]
    assert "var bottomLeft = netNodes[0][NET_ROWS - 1];" in depth_section
    assert "var rearLeft = getNetVisualDepthPoint(bottomLeft, 1);" in depth_section
    assert "var depthFill = ctx.createLinearGradient(bottomLeft.x, bottomLeft.y, rearLeft.x, rearLeft.y);" in depth_section
    assert "ctx.lineTo(rearRight.x, rearRight.y);" in depth_section
    assert "for (var band = 1; band <= 4; band++) {" in depth_section
    assert "var leftBand = lerpNetPoint(bottomLeft, rearLeft, t);" in depth_section
    assert "for (var col = 1; col < 5; col++) {" in depth_section
    assert "var front = lerpNetPoint(bottomLeft, bottomRight, spanT);" in depth_section
    assert "ctx.moveTo(rearLeft.x, rearLeft.y);" in depth_section
    assert "ctx.lineTo(rearRight.x, rearRight.y);" in depth_section
    assert "rearTopLeft" not in depth_section
    assert "rearTopRight" not in depth_section
    assert "baseShade" not in depth_section
    assert "ctx.ellipse(" not in depth_section
    assert "NET_VISUAL_DEPTH_Y * 0.52" not in depth_section
    assert "drawNetDepthVolume();" in html
    assert "drawNetBack();\n    drawNetDepthVolume();\n    drawYuiCheatItems(t);" in html


@pytest.mark.unit
def test_badminton_scene_uses_compact_avatars_and_net():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "var YUI_SHOOTER_W = 244;" in html
    assert "var YUI_SHOOTER_H = 366;" in html
    assert "var YUI_SHOOTER_SCALE = 0.86;" in html
    assert "var PLAYER_AVATAR_W = 292;" in html
    assert "var PLAYER_AVATAR_H = 396;" in html
    assert "var PLAYER_FIGURE_SCALE = 1.02;" in html
    assert "var BALL_R = 18;" in html
    assert "var SHUTTLE_VISUAL_R = 10;" in html
    assert "var SHUTTLE_FLIGHT_VISUAL_R = 6;" in html
    assert "var SENSEI_VRM_PATH = '/static/vrm/sensei.vrm';" in html
    assert "window.badmintonPlayerVrmManager = window.badmintonPlayerVrmManager || null;" in html
    assert "var visibleH = h * 1.28;" in html
    assert "cam.near = 0.01;" in html
    assert "cam.far = 100;" in html
    assert "document.getElementById('player-sensei-vrm-container')" in html
    assert "document.getElementById('player-sensei-vrm-canvas')" in html
    assert "await waitForVRMModules();" in html
    assert "await manager.core.init('player-sensei-vrm-canvas', 'player-sensei-vrm-container', null, { embed: true });" in html
    assert "var senseiProbe = await fetch(SENSEI_VRM_PATH, { cache: 'no-store' });" in html
    assert "if (!senseiProbe.ok) throw new Error('Sensei VRM model missing: ' + SENSEI_VRM_PATH);" in html
    assert "loader.load(SENSEI_VRM_PATH, resolve, null, reject);" in html
    assert "manager.currentModel = { vrm: vrm, gltf: gltf, scene: vrm.scene, url: SENSEI_VRM_PATH };" in html
    assert html.count("await manager.playVRMAAnimation('/static/vrm/animation/wait03.vrma.gz', {") == 2
    assert "isIdle: true" in html
    assert "playIdleAnimation" not in html
    assert "SENSEI_LIVE2D_PATH" not in html
    assert "badmintonPlayerLive2DManager" not in html
    assert "_i18n('hud.nekoTurn', 'Yui 挥拍')" in html
    assert "_i18n('hud.duelTitle', 'Yui 对拉模式：轮流挥拍，先到 11 分获胜')" in html
    assert "_i18n('result.outcome.nekoWin', 'Yui 赢了')" in html
    assert "Neko挥拍" not in html
    assert "Neko 对拉模式" not in html
    assert "courtLeft: 70," in html
    assert "courtRight: 850," in html
    assert "netX: 460," in html
    assert "netTop: 250," in html
    assert "netBottom: 400," in html
    assert "var NET_VISUAL_LINE_WIDTH = 4;" in html
    assert "var NET_VISUAL_Z_HEIGHT = 98;" in html
    assert "var NET_VISUAL_BOTTOM_CLEARANCE = 18;" in html
    assert "var NET_COLLISION_HALF_WIDTH = 24;" in html
    assert "var NET_SPRING_K = 0.18;" in html
    assert "var NET_DAMPING = 0.84;" in html
    assert "var NET_GRAVITY = 0;" in html
    assert "var NET_BALL_FORCE = 0.22;" in html
    assert "var NET_BALL_FRICTION = 0.018;" in html
    assert "var NET_COLS = 17;" in html
    assert "var NET_ROWS = 7;" in html
    assert "var NET_CONTACT_RADIUS = 42;" in html
    assert "var NET_CONTACT_IMPULSE = 320;" in html
    assert "var NET_CONTACT_HOLD_MS = 90;" in html
    assert "function getSideViewCourtTop() {" in html
    assert "return FLOOR_Y - 62;" in html
    assert "function getSideViewCourtBottom() {" in html
    assert "function getNetSurfacePoint(t, v) {" in html
    assert "return getNetBottomY(t) - NET_VISUAL_Z_HEIGHT;" in html
    assert "return FLOOR_Y - NET_VISUAL_BOTTOM_CLEARANCE;" in html
    assert "var lineT = clamp(Number(t) || 0, 0, 1);" in html
    assert "var heightT = clamp(Number(v) || 0, 0, 1);" in html
    assert "x: BADMINTON.netX + (lineT - 0.5) * NET_VISUAL_LINE_WIDTH" in html
    assert "y: topY + (bottomY - topY) * heightT" in html
    assert "var NET_VISUAL_THICKNESS = 28;" not in html
    assert "var NET_VISUAL_SIDE_INSET = 34;" not in html
    assert "NET_VISUAL_Y_SPAN" not in html
    assert "NET_VISUAL_PERSPECTIVE_X_SPREAD" not in html
    assert "var netWidth = 0;" not in html
    assert "var netHalfWidth = NET_COLLISION_HALF_WIDTH;" in html
    assert "pinned: row === 0 || row === NET_ROWS - 1 || col === 0 || col === NET_COLS - 1" in html
    assert "var topLeft = netNodes[0][0];" in html
    assert "var topRight = netNodes[NET_COLS - 1][0];" in html
    assert "var bottomLeft = netNodes[0][NET_ROWS - 1];" in html
    assert "var bottomRight = netNodes[NET_COLS - 1][NET_ROWS - 1];" in html
    assert "meshGrad.addColorStop" not in html
    assert "var rippleY = (rowT - 0.5) * 0.16 * NET_CONTACT_IMPULSE * impact * weight;" in html
    assert "var dropY = NET_CONTACT_IMPULSE * impact * weight;" not in html
    assert "for (var row = 0; row < NET_ROWS; row++) {" in html
    assert "for (var meshCol = 0; meshCol < NET_COLS; meshCol += 2) {" in html
    assert "var tapeGrad = ctx.createLinearGradient(topLeft.x, topLeft.y, topRight.x, topRight.y);" in html
    assert "ctx.moveTo(topLeft.x - 1, topLeft.y + 1);" in html
    assert "ctx.lineTo(topRight.x + 1, topRight.y + 1);" in html
    assert "ctx.lineTo(bottomLeft.x, bottomLeft.y - 1);" in html
    assert "ctx.lineTo(bottomRight.x, bottomRight.y - 1);" in html
    assert "ctx.lineTo(bottom.x + Math.sin" not in html
    assert "var wallGrad = ctx.createLinearGradient(0, 0, 0, 178);" in html
    assert "var glow = ctx.createRadialGradient(lx, 76, 1, lx, 76, 96);" in html
    assert "var rafters = [" in html
    assert "for (var truss = 80; truss < BASE_W; truss += 150)" in html
    assert "var acousticPanels = [" in html
    assert "var spectatorShade = ctx.createLinearGradient(0, 144, 0, 188);" in html
    assert "var sponsorBoards = [" in html
    assert "var wallScoreboardX = BASE_W / 2 - 42;" in html
    assert "ctx.fillRect(wallScoreboardX, wallScoreboardY, 84, 34);" in html
    assert "var apronGrad = ctx.createLinearGradient(0, outerTop, 0, outerBottom);" in html
    assert "var theme = THEMES[currentThemeKey] || THEMES.default;" in html
    assert "label: '羽毛球场'" in html
    assert "scenic: 'indoor'" in html
    assert "sunset: {" not in html
    assert "night: {" not in html
    assert "miami: {" in html
    assert "label: '迈阿密落日'" in html
    assert "floor: ['#f5d99f', '#d7b56d', '#9d7847']" in html
    assert "courtLine: 'rgba(255,255,248,.96)'" in html
    assert "scenic: 'miami'" in html
    assert '<link rel="preload" as="image" href="/static/game/games/badminton/images/indoor-badminton-arena-bg-v4.jpg?v=20260618g">' in html
    assert '<link rel="preload" as="image" href="/static/game/games/badminton/images/miami-beach-sunset-bg-v2.webp?v=20260618b">' in html
    assert "var INDOOR_BADMINTON_ARENA_BACKGROUND_SRC = '/static/game/games/badminton/images/indoor-badminton-arena-bg-v4.jpg?v=20260618g';" in html
    assert "var MIAMI_BEACH_BACKGROUND_SRC = '/static/game/games/badminton/images/miami-beach-sunset-bg-v2.webp?v=20260618b';" in html
    assert "indoor: INDOOR_BADMINTON_ARENA_BACKGROUND_SRC" in html
    assert "miami: MIAMI_BEACH_BACKGROUND_SRC" in html
    assert "function preloadScenicBackgrounds() {" in html
    assert "function getLoadedScenicBackground(key) {" in html
    assert "var scenicBackgroundImage = getLoadedScenicBackground(theme.scenic);" in html
    assert "ctx.drawImage(scenicBackgroundImage, 0, 0, BASE_W, BASE_H);" in html
    assert "if (!scenicBackgroundImage) {" in html
    assert "wallGrad.addColorStop(0, sky[0] || '#183647');" in html
    assert "if (theme.scenic === 'miami') {" in html
    assert "var sunsetGlow = ctx.createRadialGradient(BASE_W * 0.72, 76, 4, BASE_W * 0.72, 76, 190);" in html
    assert "var oceanGrad = ctx.createLinearGradient(0, 116, 0, 178);" in html
    assert "var beachHorizonGrad = ctx.createLinearGradient(0, 160, 0, 214);" in html
    assert "for (var palm = 0; palm < 4; palm++)" in html
    assert "var palmLean = palm % 2 ? -10 : 12;" in html
    assert "ctx.bezierCurveTo(palmLean * 0.16, -18, palmLean * 0.45, -42, palmLean, -66);" in html
    assert "for (var trunkBand = 0; trunkBand < 5; trunkBand++)" in html
    assert "for (var palmLeaf = 0; palmLeaf < 9; palmLeaf++)" in html
    assert "ctx.strokeStyle = palmLeaf % 2 ? 'rgba(180,167,92,.38)' : 'rgba(77,124,76,.56)';" in html
    assert "if (theme.scenic !== 'miami') {" in html
    assert "var beachUmbrellas = [" in html
    assert "floorGrad.addColorStop(0, floor[0] || '#1b6a68');" in html
    assert "var wetSandGrad = ctx.createLinearGradient(0, 158, 0, 286);" in html
    assert "for (var foam = 0; foam < 3; foam++)" in html
    assert "var drySandGrad = ctx.createLinearGradient(0, 214, 0, BASE_H);" in html
    assert "for (var beachSandLine = 0; beachSandLine < 14; beachSandLine++)" in html
    assert "for (var sandDot = 0; sandDot < 120; sandDot++)" in html
    assert "for (var sandSpeck = 0; sandSpeck < 260; sandSpeck++)" in html
    assert "for (var windStreak = 0; windStreak < 18; windStreak++)" in html
    assert "for (var beachFoot = 0; beachFoot < 8; beachFoot++)" in html
    assert "for (var ropePost = 0; ropePost < 6; ropePost++)" in html
    assert "for (var apronRipple = 0; apronRipple < 7; apronRipple++)" in html
    assert "courtGrad.addColorStop(0, 'rgba(250,221,159,.98)');" in html
    assert "for (var sandRipple = 0; sandRipple < 9; sandRipple++)" in html
    assert "for (var courtSandGrain = 0; courtSandGrain < 150; courtSandGrain++)" in html
    assert "var courtFootprints = [" in html
    assert "var sunSandReflection = ctx.createRadialGradient(BASE_W * 0.72, 192, 8, BASE_W * 0.72, 192, 270);" in html
    assert "var courtLineShadow = theme.scenic === 'miami' ? 'rgba(114,76,36,.24)' : 'rgba(6,58,51,.22)';" in html
    assert "var backWallShadow = ctx.createLinearGradient(0, 158, 0, 236);" in html
    assert "var floorSheen = ctx.createRadialGradient(BASE_W * 0.5, court.courtTop + 12, 10, BASE_W * 0.5, court.courtTop + 12, 420);" in html
    assert "var lightReflection = ctx.createLinearGradient(0, court.courtTop - 2, 0, court.courtTop + 90);" in html
    assert "var groundTop = getSideViewCourtTop();" in html
    assert "var groundBottom = getSideViewCourtBottom();" in html
    assert "var zoneGlow = ctx.createLinearGradient(court.courtLeft, 0, court.courtRight, 0);" in html
    assert "var safetyPadGrad = ctx.createLinearGradient(0, outerTop, 0, outerBottom);" in html
    assert "ctx.fillRect(court.courtLeft - outerPad, outerTop, outerPad - 6, outerBottom - outerTop);" in html
    assert "var gearItems = [" in html
    assert "var chairX = court.netX + 28;" in html
    assert "ctx.fillRect(chairX + 8, chairY + 8, 22, 10);" in html
    assert "var shuttleRackX = court.courtLeft - 32;" in html
    assert "for (var tube = 0; tube < 3; tube++)" in html
    assert "var matTexture = ctx.createLinearGradient(court.courtLeft, groundTop, court.courtRight, groundTop);" in html
    assert "for (var matGrain = court.courtLeft + 18; matGrain < court.courtRight; matGrain += 28)" in html
    assert "ctx.fillRect(court.courtLeft + 2, groundTop + 4, court.netX - court.courtLeft - 4, groundBottom - groundTop - 8);" in html
    assert "var serviceWearY = groundTop + (groundBottom - groundTop) * 0.58;" in html
    assert "var serviceWearGrad = ctx.createRadialGradient(court.netX - 135, serviceWearY, 12, court.netX - 135, serviceWearY, 116);" in html
    assert "for (var scuff = 0; scuff < 10; scuff++)" in html
    assert "ctx.ellipse(court.netX, serviceWearY, 88, 21, 0, 0, Math.PI * 2);" in html
    assert "var shortServiceOffset = halfCourtLength * (1.98 / 6.70);" in html
    assert "var doublesLongServiceInset = halfCourtLength * (0.76 / 6.70);" in html
    assert "ctx.moveTo(serviceLines.leftShortServiceX, groundTop + 6);" in html
    assert "ctx.moveTo(serviceLines.rightShortServiceX, groundTop + 6);" in html
    assert "ctx.moveTo(serviceLines.leftDoublesLongServiceX, groundTop + 6);" in html
    assert "ctx.moveTo(serviceLines.rightDoublesLongServiceX, groundTop + 6);" in html
    assert "var netTop = getNetSurfacePoint(0.5, 0);" in html
    assert "var netBottom = getNetSurfacePoint(0.5, 1);" in html
    assert "var leftTop = getNetSurfacePoint(0, 0);" in html
    assert "var rightBottom = getNetSurfacePoint(1, 1);" in html
    assert "var sidePostGrad = ctx.createLinearGradient(leftTop.x, netTop.y, rightTop.x, netBottom.y);" in html
    assert "ctx.strokeStyle = sidePostGrad;" in html
    assert "ctx.lineTo(leftBottom.x, leftBottom.y + 6);" in html
    assert "ctx.moveTo(leftBottom.x - 5, leftBottom.y + 4);" in html
    assert "ctx.lineTo(rightBottom.x + 5, rightBottom.y + 4);" in html
    assert "ctx.ellipse(leftBottom.x, leftBottom.y + 7, 5, 2.4, 0, 0, Math.PI * 2);" not in html
    assert "postFoot" not in html
    assert "ctx.rect(leftBottom.x - 4" not in html
    assert "ctx.moveTo(topLeft.x, topLeft.y + 5);" in html
    assert "ctx.lineTo(topRight.x, topRight.y + 5);" in html
    assert "var postX = BADMINTON.netX;" not in html
    assert "var clampGrad = ctx.createLinearGradient(postX - 5, topClampY, postX + 5, topClampY);" not in html
    assert "ctx.roundRect(postX - 5, topClampY - 2.2, 10, 4.4, 2.1);" not in html
    assert "ctx.strokeStyle = 'rgba(255,68,68,.78)';" not in html
    assert "ctx.strokeStyle = 'rgba(255,68,68,.62)';" not in html
    assert "ctx.rect(BADMINTON.courtLeft, groundTop - 8, BADMINTON.courtRight - BADMINTON.courtLeft, groundBottom - groundTop + 18);" in html
    assert "ctx.fillRect(court.courtLeft, groundTop, court.courtRight - court.courtLeft, groundBottom - groundTop);" in html
    assert "BADMINTON.courtTop + (BADMINTON.courtBottom - BADMINTON.courtTop) * t" not in html
    assert "ctx.rect(BADMINTON.courtLeft, BADMINTON.courtTop, BADMINTON.courtRight - BADMINTON.courtLeft, BADMINTON.courtBottom - BADMINTON.courtTop);" not in html
    assert "ctx.fillRect(court.courtLeft, court.courtTop, court.courtRight - court.courtLeft, court.courtBottom - court.courtTop);" not in html
    assert "var shadowAlpha = clamp(0.30 - shuttleZ / 760, 0.06, 0.30);" in html
    assert "ctx.ellipse(ball.x, FLOOR_Y + 3, SHUTTLE_FLIGHT_VISUAL_R * 1.35 * shadowScale" in html
    assert "var DISTANCE_MARKERS_STORAGE_KEY = 'bd_badminton_distance_markers_enabled';" in html
    assert "var distanceMarkersEnabled = readJson(DISTANCE_MARKERS_STORAGE_KEY, false) === true;" in html
    assert "ctx.fillRect(court.targetLeft" not in html
    assert "ctx.strokeRect(court.targetLeft" not in html
    assert "plank:" not in html
    assert "threeLine:" not in html
    assert "laneFill:" not in html
    assert "setStyleIfChanged(playerSenseiContainer, playerAvatarStyleCache, 'width', PLAYER_AVATAR_W + 'px');" in html
    assert "setStyleIfChanged(playerSenseiContainer, playerAvatarStyleCache, 'height', PLAYER_AVATAR_H + 'px');" in html
    assert "container.style.width = YUI_SHOOTER_W + 'px';" in html
    assert "container.style.height = YUI_SHOOTER_H + 'px';" in html
    assert "container.style.left = sx + 'px';" in html
    assert "container.style.top = sy + 'px';" in html
    assert "yuiShooterStyleCache" not in html
    assert "setDatasetValueIfChanged(container, 'courtAvatar', 'opponent');" in html
    assert "var baseX = getYuiX();" in html
    assert "function getYuiShotOrigin() {" in html
    assert "var origin = impulse.contact || getRacketContactPoint(shotShooter);" in html
    assert "var direction = shotShooter === 'neko' ? -1 : 1;" in html
    assert "shuttle.vx = direction * v * Math.cos(radians) + contactDeflection;" in html
    assert "function screenYToCourtZ(screenY) {" in html
    assert "function courtZToScreenY(z) {" in html
    assert "function getMidcourtNetY() {" in html
    assert "return screenYToCourtZ(getNetBottomY(0.5));" in html
    assert "function getNetTopZ() {" in html
    assert "return screenYToCourtZ(getNetTopY(0.5));" in html
    assert "function syncShuttleCourtCoordinates(shuttle) {" in html
    assert "function didShuttleCrossMidcourtNet(shuttle, direction) {" in html
    assert "function getMidcourtNetCrossing(shuttle) {" in html
    assert "function isShuttleInsideNetZ(shuttle, crossing) {" in html
    assert "function didShuttleLegallyClearNet(shuttle, crossing) {" in html
    assert "shuttle.courtY = origin.x;" in html
    assert "shuttle.z = screenYToCourtZ(origin.y);" in html
    assert "shuttle.vCourtY = shuttle.vx;" in html
    assert "shuttle.vz = -(shuttle.vy || 0);" in html
    assert "var crossedNet = didShuttleCrossMidcourtNet(ball, direction);" in html
    assert "var netCrossing = getMidcourtNetCrossing(ball);" in html
    assert "if (!didShuttleLegallyClearNet(ball, netCrossing)) {" in html
    assert "ball.legalNetClearance = true;" in html
    assert "z >= getNetBottomZ() - shuttleRadius * 0.18" in html
    assert "z <= getNetTopZ() + shuttleRadius * 0.12" in html
    assert "function applyNetContactImpulse(ball, crossing) {" in html
    assert "var incomingSpeed = typeof ball.speed === 'function'" in html
    assert "var impact = clamp(incomingSpeed / 330, 0.68, 1.55);" in html
    assert "node.vx += recoilX;" in html
    assert "node.vy += rippleY;" in html
    assert "function doesNetTouchCarryToTargetSide(ball, crossing) {" in html
    assert "var clearsTape = z >= netTopZ - shuttleRadius * 0.26;" in html
    assert "var hasCarry = forwardSpeed >= 115 || risingSpeed >= 36;" in html
    assert "var aboveTape = z >= netTopZ;" in html
    assert "return clearsTape && (hasCarry || aboveTape);" in html
    assert "function applyMidcourtNetContact(ball, crossing) {" in html
    assert "ball.x = crossing.screenX;" in html
    assert "ball.y = crossing.screenY;" in html
    assert "ball.netTouched = true;" in html
    assert "ball.netCarryToTargetSide = carriesToTargetSide;" in html
    assert "ball.legalNetClearance = false;" in html
    assert "ball.crossedNet = carriesToTargetSide;" in html
    assert "ball.netContactHoldUntil = ball.netContactAt + NET_CONTACT_HOLD_MS;" in html
    assert "applyNetContactImpulse(ball, crossing);" in html
    assert "var reboundDirection = carriesToTargetSide ? direction : -direction;" in html
    assert "ball.vx = reboundDirection * clamp(Math.abs(ball.vx || 0) * horizontalScale, minHorizontalSpeed, maxHorizontalSpeed);" in html
    assert "ball.vy = Math.max(Math.abs(ball.vy || 0) * 0.10 + (carriesToTargetSide ? 132 : 150), carriesToTargetSide ? 132 : 150);" in html
    assert "applyMidcourtNetContact(ball, netCrossing);" in html
    assert "var targetLeft = direction > 0 ? BADMINTON.netX + ballRadius * 0.5 : BADMINTON.courtLeft;" in html
    assert "var targetRight = direction > 0 ? BADMINTON.courtRight : BADMINTON.netX - ballRadius * 0.5;" in html
    assert "var outPastBackLine = direction > 0" in html
    assert "var baseX = getPlayerX() + 10;" not in html
    assert "container.dataset.courtAvatar = 'shooter';" not in html
    assert "playerSenseiContainer.style.opacity = nekoVisible ? '0' : '0.96';" not in html
    assert "var featherCone = ctx.createLinearGradient(0, -radius * 2.05, 0, radius * 0.06);" in html
    assert "ctx.bezierCurveTo(-radius * 0.62, -radius * 2.05, radius * 0.62, -radius * 2.05, radius * 0.92, -radius * 1.70);" in html
    assert "for (var spine = -4; spine <= 4; spine++)" in html
    assert "var spinStripe = ctx.createLinearGradient(radius * 0.18, -radius * 1.52, radius * 0.34, radius * 0.10);" in html
    assert "spinStripe.addColorStop(0, 'rgba(91,198,214,.58)');" in html
    assert "spinStripe.addColorStop(0.58, 'rgba(255,226,142,.72)');" in html
    assert "spinStripe.addColorStop(1, 'rgba(40,117,136,.62)');" in html
    assert "ctx.lineWidth = 2.2;" in html
    assert "ctx.moveTo(radius * 0.12, -radius * 1.50);" in html
    assert "ctx.quadraticCurveTo(radius * 0.54, -radius * 0.70, radius * 0.25, radius * 0.08);" in html
    assert "ctx.fillStyle = '#11191b';" in html
    assert "ctx.ellipse(0, radius * 0.16, radius * 0.42, radius * 0.095" in html
    assert "cork.addColorStop(0, '#fffdf5');" in html
    assert "ctx.bezierCurveTo(-radius * 0.39, radius * 0.34, -radius * 0.29, radius * 0.58, 0, radius * 0.62);" in html
    assert "var skirt =" not in html
    assert "for (var seam = -1; seam <= 1; seam++)" not in html
    assert "var collar =" not in html
    assert "cork.addColorStop(0.58, '#e4b76c');" not in html
    assert "ctx.ellipse(0, radius * 0.34, radius * 0.50, radius * 0.34" not in html
    assert "var firstTrailIndex = ball.isSmash ? Math.max(0, trail.length - 5) : 0;" in html
    assert "ctx.globalAlpha = ball.isSmash ? 0.50 : (ball.wasPerfect ? 0.62 : 0.48);" in html
    assert "ctx.strokeStyle = ball.isSmash ? 'rgba(255,210,118,.72)' : (ball.wasPerfect ? 'rgba(255,244,178,.62)' : 'rgba(204,241,255,.48)');" in html
    assert "ctx.strokeStyle = 'rgba(255,143,61,.34)';" not in html
    assert "drawShuttlecock(ball.x, ball.y, SHUTTLE_FLIGHT_VISUAL_R, ball.spinAngle || 0);" in html
    assert "if (ball.y < -SHUTTLE_FLIGHT_VISUAL_R * 2) {" in html
    assert "var markerX = clamp(ball.x, BADMINTON.courtLeft + 12, BADMINTON.courtRight - 12);" in html
    assert "function drawPlayerServeShuttleHint() {" in html
    assert "if (playerSenseiReady && playerSenseiContainer && playerSenseiContainer.dataset.heldShuttle3d === 'ready') return;" in html
    assert "if (playerSenseiReady) {\n      drawPlayerServeShuttleHint();\n      return;\n    }" in html
    assert "var hintX = px + 44;" in html
    assert "var hintY = py - 108;" in html
    assert "drawShuttlecock(hintX, hintY, SHUTTLE_VISUAL_R * 0.92, -0.28);" in html
    assert "drawShuttlecock(heldBall.x, heldBall.y, SHUTTLE_VISUAL_R);" not in html
    assert "drawShuttlecock(ball.x, ball.y, BALL_R" not in html
    assert "drawShuttlecock(heldBall.x, heldBall.y, BALL_R" not in html
    assert "var avatarW = yuiShooterLayout ? YUI_SHOOTER_W : 180;" in html
    assert "var avatarH = yuiShooterLayout ? YUI_SHOOTER_H : 270;" in html
    assert "Math.round(sx - PLAYER_AVATAR_W / 2)" in html
    assert "Math.round(sy - PLAYER_AVATAR_H)" in html
    assert '<canvas id="yui-live2d-racket-canvas" aria-hidden="true"></canvas>' in html
    assert "#yui-live2d-racket-canvas {" in html
    assert '#neko-l2d-container[data-live2d="ready"] #yui-live2d-racket-canvas' in html
    assert "var yuiLive2dRacketCanvas = document.getElementById('yui-live2d-racket-canvas');" in html
    assert "var yuiLive2dRacketCtx = yuiLive2dRacketCanvas ? yuiLive2dRacketCanvas.getContext('2d') : null;" in html
    assert "var BADMINTON_RACKET_SPRITE_SRC = '/static/game/games/badminton/images/badminton-racket-sprite.svg?v=20260618a';" in html
    assert "function preloadBadmintonRacketSprite() {" in html
    assert "function isBadmintonRacketSpriteReady() {" in html
    assert "try { preloadBadmintonRacketSprite(); } catch (_) { badmintonRacketSpriteImage = null; }" in html
    draw_start = html.index("function drawPlayer() {")
    draw_section = html[draw_start:html.index("function drawAiming(now)", draw_start)]
    assert "var s = PLAYER_FIGURE_SCALE;" in draw_section
    assert "ctx.arc(px, py - 62 * s, 16 * s" in draw_section
    assert "ctx.lineWidth = 8 * s;" in draw_section
    assert "function drawBadmintonRacket(cx, cy, scale, rotation, mirror) {" in html
    assert "function drawBadmintonRacketOnContext(renderCtx, cx, cy, scale, rotation, mirror) {" in html
    assert "if (isBadmintonRacketSpriteReady()) {" in html
    assert "renderCtx.drawImage(badmintonRacketSpriteImage, -30 * s, -116 * s, 60 * s, 168 * s);" in html
    assert "function getBadmintonRacketGripAnchor(handX, handY, scale, rotation, mirror) {" in html
    assert "var localGripX = isBadmintonRacketSpriteReady() ? 0 : -23 * s;" in html
    assert "var localGripY = isBadmintonRacketSpriteReady() ? 32 * s : 28 * s;" in html
    assert "var gripX = side * (localGripX * cos - localGripY * sin);" in html
    assert "var gripY = localGripX * sin + localGripY * cos;" in html
    assert "function renderYuiLive2dRacket() {" in html
    assert "function getYuiLive2dModelFrame(rect) {" in html
    assert "function getYuiLive2dFallbackHandPoint(modelFrame, shooting, charging) {" in html
    assert "x: modelFrame.left + modelFrame.width * (shooting ? 0.62 : (charging ? 0.60 : 0.58))" in html
    assert "function normalizeYuiLive2dDrawableRect(entry, domRect, layoutW, layoutH) {" in html
    assert "function getYuiLive2dHandAnchorFromDrawables(domRect, modelFrame, layoutW, layoutH, shooting, charging) {" in html
    assert "manager._getRenderableDrawableScreenRects(null, null, true);" in html
    assert "var inViewportSpace = domRect &&" in html
    assert "scaleX = layoutW / domRect.width;" in html
    assert "var minX = modelFrame.left + modelFrame.width * 0.64;" in html
    assert "var maxX = modelFrame.left + modelFrame.width * 0.94;" in html
    assert "function setYuiLive2dParameter(id, value) {" in html
    assert "function applyYuiLive2dRacketPose(shooting, charging) {" in html
    assert "drawBadmintonRacketOnContext(renderCtx, anchorX, anchorY, racketScale, rotation, racketMirror);" in html
    assert "renderYuiLive2dRacket();" in html
    assert "var shooting = nekoL2dContainer.classList.contains('shooting');" in html
    assert "var charging = nekoL2dContainer.classList.contains('charging');" in html
    assert "var saving = nekoL2dContainer.classList.contains('saving');" in html
    assert "var smashing = nekoL2dContainer.classList.contains('smashing');" in html
    assert "YUI_LIVE2D_RACKET_LAYOUT_SAMPLE_MS" in html
    assert "YUI_LIVE2D_RACKET_IDLE_RENDER_MS" in html
    assert "var yuiLive2dRacketLayoutCache = { sampledAt: 0, rect: null, layoutW: 0, layoutH: 0, dpr: 0, modelFrame: null };" in html
    assert "var yuiLive2dRacketLastRenderAt = 0;" in html
    assert "function resetYuiLive2dRacketLayoutCache() {" in html
    assert "function getYuiLive2dRacketLayout(now, forceRefresh) {" in html
    assert "if (!forceRefresh && cached.rect && cached.dpr === dpr && t - (cached.sampledAt || 0) < YUI_LIVE2D_RACKET_LAYOUT_SAMPLE_MS)" in html
    assert "var layout = getYuiLive2dRacketLayout(now, activeRacketAction);" in html
    assert "if (!activeRacketAction && yuiLive2dRacketLastRenderAt && now - yuiLive2dRacketLastRenderAt < YUI_LIVE2D_RACKET_IDLE_RENDER_MS) return;" in html
    assert "resetYuiLive2dRacketLayoutCache();" in html
    assert "var modelFrame = layout.modelFrame;" in html
    assert "var fallbackHand = getYuiLive2dFallbackHandPoint(modelFrame, shooting, charging);" in html
    assert "function getCachedYuiLive2dDrawableHand(domRect, modelFrame, layoutW, layoutH, shooting, charging, now) {" in html
    assert "YUI_LIVE2D_RACKET_ANCHOR_SAMPLE_MS" in html
    assert "var drawableHand = getCachedYuiLive2dDrawableHand(rect, modelFrame, layoutW, layoutH, shooting, charging, now);" in html
    assert "domRect: { left: rect.left, top: rect.top, width: rect.width, height: rect.height }" in html
    assert "var handX = drawableHand ? drawableHand.x : fallbackHand.x;" in html
    assert "var handY = drawableHand ? drawableHand.y : fallbackHand.y;" in html
    assert "var racketScale = Math.max(0.98, Math.min(1.08, modelFrame.height / 320));" in html
    assert "handX += modelFrame.width * 0.08;" in html
    assert "handY += modelFrame.height * 0.055;" in html
    assert "var rotation = -0.10 + (shooting ? swing * 0.14 : (charging ? -0.02 : 0));" in html
    assert "var racketMirror = 1;" in html
    assert "var racketAnchor = getBadmintonRacketGripAnchor(handX, handY, racketScale, rotation, racketMirror);" in html
    assert "var anchorX = racketAnchor.x;" in html
    assert "var anchorY = racketAnchor.y;" in html
    assert "window.__badmintonYuiRacketDebug = {" in html
    assert "source: drawableHand ? drawableHand.source : 'fallback'," in html
    assert "if (debugMode && modeParams && modeParams.get('racket_anchor') === '1') {" in html
    assert "renderCtx.arc(handX, handY, 4.5, 0, Math.PI * 2);" in html
    assert "setYuiLive2dParameter('Param75', 0);" in html
    assert "setYuiLive2dParameter('Param90', 0);" in html
    assert "setYuiLive2dParameter('Param95', 0);" in html
    assert "setYuiLive2dParameter('Param77', active ? 1 : 0);" in html
    assert "setYuiLive2dParameter('Param91', active ? pose : 0);" in html
    assert "setYuiLive2dParameter('Param96', active ? pose : 0);" in html
    assert "#player-sensei-vrm-container::after" not in html
    assert "#player-sensei-vrm-container::before" not in html
    assert '.neko-avatar-container[data-court-avatar="opponent"]::after' not in html
    assert '.neko-avatar-container[data-court-avatar="opponent"]::before' not in html
    assert "sensei-racket-head-sweep" not in html
    assert "sensei-racket-handle-sweep" not in html
    assert "yui-racket-head-sweep" not in html
    assert "yui-racket-handle-sweep" not in html
    assert "using CSS fallback" not in html
    assert "function createVrmBadmintonRacket(name, options) {" in html
    assert "function createPlayerVrmRacket() {" in html
    assert "function createVrmHeldShuttlecock(name, options) {" in html
    assert "function drawVrmHeldShuttlecockTexture(ctx, size) {" in html
    assert "function drawBadmintonShuttlecockOnContext(renderCtx, x, y, radius, rotation) {" in html
    assert "function getPlayerVrmHeldShuttleCanvasPoint() {" in html
    assert "function getPlayerHeldShuttleServeHandCanvasPoint() {" in html
    assert 'id="player-sensei-held-shuttle-canvas"' in html
    assert "#player-sensei-held-shuttle-canvas { position: absolute; inset: 0; z-index: 2; pointer-events: none; }" in html
    assert "function drawPlayerVrmHeldShuttleOverlay() {" in html
    assert "return game.heldShuttleVisible && !game.ball && !game.pendingSwing && game.state === 'ready' && isPlayerTurn();" in html
    held_shuttle_texture_start = html.index("function drawVrmHeldShuttlecockTexture(ctx, size) {")
    held_shuttle_texture_section = html[held_shuttle_texture_start:html.index("function createVrmHeldShuttlecock(name, options) {", held_shuttle_texture_start)]
    assert "drawBadmintonShuttlecockOnContext(ctx, size * 0.50, size * 0.58, size * 0.23, -0.28);" in held_shuttle_texture_section
    assert "ctx.ellipse(-43, 0, 12, 43, 0, 0, Math.PI * 2);" not in held_shuttle_texture_section
    assert "drawHeldShuttleTieRing" not in held_shuttle_texture_section
    draw_shuttle_start = html.index("function drawShuttlecock(x, y, radius, rotation) {")
    draw_shuttle_section = html[draw_shuttle_start:html.index("function drawBackspinBall(ball) {", draw_shuttle_start)]
    assert "drawBadmintonShuttlecockOnContext(ctx, x, y, radius, rotation);" in draw_shuttle_section
    held_shuttle_create_start = html.index("function createVrmHeldShuttlecock(name, options) {")
    held_shuttle_create_section = html[held_shuttle_create_start:html.index("function syncPlayerHeldShuttleVisibility() {", held_shuttle_create_start)]
    assert "new THREE.CanvasTexture(textureCanvas)" in held_shuttle_create_section
    assert "drawVrmHeldShuttlecockTexture(textureCtx, 256);" in held_shuttle_create_section
    assert "new THREE.SpriteMaterial({" in held_shuttle_create_section
    assert "held-shuttle-vrm-hand-sprite" in held_shuttle_create_section
    assert "sprite.scale.set(0.34, 0.34, 1);" in held_shuttle_create_section
    assert "held-shuttle-shared-2d-style-sprite" not in held_shuttle_create_section
    assert "held-shuttle-2d-style-cork-depth" not in held_shuttle_create_section
    assert "new THREE.CylinderGeometry(0.012, 0.014, 0.018, 18)" not in held_shuttle_create_section
    assert "held-shuttle-realistic-model" not in held_shuttle_create_section
    assert "held-shuttle-feather-plane" not in held_shuttle_create_section
    assert "held-shuttle-feather-rib" not in held_shuttle_create_section
    assert "new THREE.BufferGeometry()" not in held_shuttle_create_section
    assert "var featherPlaneCount" not in held_shuttle_create_section
    assert "new THREE.ConeGeometry" not in held_shuttle_create_section
    held_shuttle_overlay_start = html.index("function getPlayerVrmHeldShuttleCanvasPoint() {")
    held_shuttle_overlay_section = html[held_shuttle_overlay_start:html.index("function drawAiming(now)", held_shuttle_overlay_start)]
    assert "playerVrmHeldShuttle.localToWorld(heldPoint);" in held_shuttle_overlay_section
    assert "heldPoint.clone().project(window.badmintonPlayerVrmManager.camera);" in held_shuttle_overlay_section
    assert "var hand = getPlayerVrmBone(vrm, 'leftHand');" in held_shuttle_overlay_section
    assert "localX = (projected.x + 1) * 0.5 * rect.width + rect.width * 0.012;" in held_shuttle_overlay_section
    assert "localY = (1 - (projected.y + 1) * 0.5) * rect.height + rect.height * 0.195;" in held_shuttle_overlay_section
    assert "source = 'player-vrm-held-shuttle-left-hand';" in held_shuttle_overlay_section
    assert "source = 'player-vrm-held-shuttle-serve-hand-fallback';" in held_shuttle_overlay_section
    assert "window.__badmintonPlayerHeldShuttleDebug = {" in held_shuttle_overlay_section
    assert "window.__badmintonPlayerHeldShuttleDebug = { source: 'player-vrm-held-shuttle-mesh' };" in held_shuttle_overlay_section
    assert "function clearPlayerHeldShuttleOverlayCanvas() {" in held_shuttle_overlay_section
    assert "playerSenseiHeldShuttleCtx.clearRect(0, 0, rect.width, rect.height);" in held_shuttle_overlay_section
    assert "var point = getPlayerHeldShuttleServeHandCanvasPoint() || getPlayerVrmHeldShuttleCanvasPoint();" in held_shuttle_overlay_section
    assert "var corkOffsetX = -Math.sin(heldRotation) * heldRadius * 0.42;" in held_shuttle_overlay_section
    assert "drawBadmintonShuttlecockOnContext(playerSenseiHeldShuttleCtx, shuttleX, shuttleY, heldRadius, heldRotation);" in held_shuttle_overlay_section
    render_start = html.index("function render(now) {")
    render_section = html[render_start:html.index("function loop(ts) {", render_start)]
    assert "drawBall();\n    drawPlayerVrmHeldShuttleOverlay();" in render_section
    assert "function attachPlayerHeldShuttleToHand(vrm) {" in html
    assert "function syncPlayerHeldShuttleVisibility() {" in html
    assert "function attachPlayerRacketToHand(vrm) {" in html
    held_shuttle_attach_start = html.index("function attachPlayerHeldShuttleToHand(vrm) {")
    held_shuttle_attach_section = html[held_shuttle_attach_start:html.index("function attachYuiRacketToHand(vrm) {", held_shuttle_attach_start)]
    assert "var hand = getPlayerVrmAttachmentBone(vrm, 'leftHand');" in held_shuttle_attach_section
    assert "rightHand" not in held_shuttle_attach_section
    assert "shuttle.renderOrder = 10;" in html
    assert "x: 0.040,\n      y: 0.000,\n      z: 0.135," in html
    assert "function attachYuiRacketToHand(vrm) {" in html
    assert "function getPlayerVrmRacketContactPoint() {" in html
    assert "var sweetSpot = new THREE.Vector3(0, 0.475, 0.002);" in html
    assert "playerVrmRacket.localToWorld(sweetSpot);" in html
    assert "sweetSpot.clone().project(window.badmintonPlayerVrmManager.camera);" in html
    assert "clientX / Math.max(1, window.innerWidth) * BASE_W" in html
    assert "window.__badmintonPlayerRacketDebug = {" in html
    assert "var live2dPath = charData.live2d_path || '/static/yui-lolita/yui-lolita.model3.json';" in html
    assert "window.lanlan_config.model_type = 'live2d';" in html
    assert "window.lanlan_config.live3d_sub_type = '';" in html
    assert "await initLive2DAvatar(live2dPath);" in html
    assert "await initVRMAvatar(vrmPath);" not in html
    assert "var hand = getPlayerVrmAttachmentBone(vrm, 'rightHand');" in html
    assert "hand.add(playerVrmRacket);" in html
    assert "hand.add(playerVrmHeldShuttle);" in html
    assert "hand.add(yuiVrmRacket);" in html
    assert "playerSenseiContainer.dataset.racket3d = 'ready';" in html
    assert "playerSenseiContainer.dataset.heldShuttle3d = 'ready';" in html
    assert "nekoVrmContainer.dataset.racket3d = 'ready';" in html
    assert "if (!attachPlayerRacketToHand(vrm)) {" in html
    assert "if (!attachPlayerHeldShuttleToHand(vrm)) {" in html
    assert "if (!attachYuiRacketToHand(vrm)) {" in html
    assert "delete nekoVrmContainer.dataset.racket3d;" in html
    assert "playerVrmHeldShuttle.visible = !!shouldDrawHeldShuttle();" in html
    assert "syncPlayerHeldShuttleVisibility();" in html
    assert "game.heldShuttleVisible = false;" in html
    player_init_start = html.index("manager.currentModel = { vrm: vrm, gltf: gltf, scene: vrm.scene, url: SENSEI_VRM_PATH };")
    player_init_section = html[player_init_start:html.index("if (manager.renderer && manager.renderer.domElement)", player_init_start)]
    assert player_init_section.index("if (!attachPlayerRacketToHand(vrm)) {") < player_init_section.index("fitVRMToContainer(manager, vrm, playerSenseiContainer);")
    yui_vrm_start = html.index("manager.currentModel = { vrm: vrm, gltf: gltf, scene: vrm.scene, url: vrmPath };")
    yui_vrm_section = html[yui_vrm_start:html.index("if (manager.renderer && manager.renderer.domElement)", yui_vrm_start)]
    assert yui_vrm_section.index("if (!attachYuiRacketToHand(vrm)) {") < yui_vrm_section.index("fitVRMToAudience(manager, vrm);")
    assert "new THREE.CylinderGeometry(0.0088, 0.0108, 0.16, 12)" in html
    assert "butt.name = 'racket-butt-cap';" in html
    assert "gripWrap.name = 'racket-grip-wrap';" in html
    assert "shaft.name = 'racket-shaft';" in html
    assert "new THREE.TorusGeometry(0.082, 0.0042, 10, 56)" in html
    assert "head.scale.set(0.68, 1.28, 1);" in html
    assert "racket.scale.setScalar(options.scale == null ? 1.18 : options.scale);" in html
    assert "node.frustumCulled = false;" in html
    assert "scale: 1.26," in html
    assert "scale: 1.20," in html
    assert "x: 0.006," in html
    assert "y: 0.002," in html
    assert "filter: saturate(1.14);" in html
    assert "#player-sensei-vrm-container.shooting { animation: sensei-shooting .42s ease-out; }" not in html
    assert "#player-sensei-vrm-container.charging { animation: sensei-charging .72s ease-in-out infinite; }" not in html
    assert "function applyPlayerVrmPoseFrame(action, progress) {" in html
    assert "function setPlayerVrmBonePose(vrm, name, x, y, z, weight) {" in html
    assert "startPlayerVrmPose(action, duration);" in html
    assert "playerVrmRestPose = capturePlayerVrmRestPose(vrm);" in html
    pose_start = html.index("function applyPlayerVrmPoseFrame(action, progress) {")
    pose_section = html[pose_start:html.index("function startPlayerVrmPose(", pose_start)]
    assert "rightUpperArm" not in pose_section
    assert "rightLowerArm" not in pose_section
    assert "leftUpperArm" not in pose_section
    assert "rightHand" not in pose_section
    assert "drawBadmintonRacket(heldBall.x + 8 * s, heldBall.y - 4 * s, s" in draw_section
    assert "ctx.arc(heldBall.x + 8 * s, heldBall.y - 4 * s, 46 * s" in draw_section


@pytest.mark.unit
def test_badminton_mouse_input_is_gated_to_player_controlled_shots():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "function canPlayerControlShot() {" in html
    assert "if (isBadmintonLoadingActive()) return false;" in html
    assert "if (game.state !== 'ready' || game.pendingSwing || !isPlayerTurn()) return false;" in html
    assert "if (isPlayerReceivingReturn()) return canPlayerReturnIncomingShuttle();" in html
    assert "return !isPositionTransitioning;" in html
    assert "var playerShotInFlight = game.ball && game.ball.shooter === 'player' && game.ball.direction === 1 && !game.ball.resolved;" in html
    assert "var yuiTurnActive = game.state === 'neko_thinking' || game.duel.activeShooter === 'neko';" in html
    assert "var yuiSwinging = game.pendingSwing && game.pendingSwing.shooter === 'neko';" in html
    assert "function isPlayerPostServeMoveLocked() {" in html
    assert "return performance.now() < (game.playerMoveLockedUntil || 0);" in html
    assert "return isDuelMode() && (isPlayerServeSetup() || canPlayerControlShot() || (playerShotInFlight && !isPlayerPostServeMoveLocked()) || yuiTurnActive || incomingYuiBall || yuiSwinging);" in html
    assert "function isIncomingPlayerShuttleInReach(ball) {" in html
    assert "var PLAYER_RETURN_REACH_X = 84;" in html
    assert "var PLAYER_RETURN_REACH_Y = 108;" in html
    assert "function canPlayerReturnIncomingShuttle() {" in html

    assert "function shouldIgnoreBadmintonPointerEvent(ev) {" in html
    assert "#badminton-loading, #utility-controls, #result-panel, #leaderboard-panel, #stats-panel, #game-audio-controls, #bd-debug-panel" in html
    mousemove = html[
        html.index("function handleBadmintonPointerMove(ev) {"):
        html.index("function handleBadmintonPointerDown(ev) {")
    ]
    assert "if (isBadmintonLoadingActive()) return;" in mousemove
    assert "if (shouldIgnoreBadmintonPointerEvent(ev)) return;" in mousemove
    assert "rememberPlayerPointer(ev);" in mousemove
    assert "if (canMoveCourt) updatePlayerCourtTarget(ev.clientX, ev.clientY);" in mousemove
    assert "var canControlShot = canPlayerControlShot();" in mousemove
    assert "if (!canControlShot) return;" in mousemove
    assert mousemove.index("if (!canControlShot) return;") < mousemove.index("game.aimAngle =")

    mousedown = html[
        html.index("function handleBadmintonPointerDown(ev) {"):
        html.index("addBadmintonEventListener(window, 'mouseup'")
    ]
    assert "if (isBadmintonLoadingActive()) return;" in mousedown
    assert "addBadmintonEventListener(canvas, 'mousemove', handleBadmintonPointerMove);" in mousedown
    assert "addBadmintonEventListener(canvas, 'mousedown', handleBadmintonPointerDown);" in mousedown
    assert "addBadmintonEventListener(window, 'mousemove', function (ev) {" in mousedown
    assert "addBadmintonEventListener(window, 'mousedown', function (ev) {" in mousedown
    assert "handleBadmintonPointerDown(ev);" in mousedown
    assert "if (!canPlayerChargeShot()) return;" in mousedown
    assert "if (game.state !== 'ready') return;" not in mousedown
    assert "if (!isPlayerTurn()) return;" not in mousedown

    mouseup = html[
        html.index("addBadmintonEventListener(window, 'mouseup'"):
        html.index("addBadmintonEventListener(window, 'keydown'")
    ]
    assert "if (isBadmintonLoadingActive()) return;" in mouseup
    assert "if (game.charging && returnIncomingPlayerShuttle()) return;" in mouseup
    assert "if (game.charging && canPlayerControlShot()) shoot();" in mouseup
    assert "game.charging = false;" in mouseup


@pytest.mark.unit
def test_badminton_player_serve_briefly_locks_court_movement():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    queue_start = html.index("function queueRacketSwing(angle, power, shooter, options) {")
    queue_section = html[queue_start:html.index("function returnIncomingPlayerShuttle()", queue_start)]
    return_start = html.index("function returnIncomingPlayerShuttle() {")
    return_section = html[return_start:html.index("function shoot()", return_start)]
    reset_start = html.index("function resetCourtMovement() {")
    reset_section = html[reset_start:html.index("function resetYuiCheats()", reset_start)]

    assert "var PLAYER_POST_SERVE_MOVE_LOCK_MS = 420;" in html
    assert "playerMoveLockedUntil: 0," in html
    assert "if (shotShooter === 'player' && !incomingBall) {" in queue_section
    assert "game.playerMoveLockedUntil = performance.now() + SWING_IMPACT_DELAY_MS + PLAYER_POST_SERVE_MOVE_LOCK_MS;" in queue_section
    assert "queueRacketSwing(playerReturnAngle, returnPower, 'player', { incomingBall: game.ball });" in return_section
    assert "game.playerMoveLockedUntil" not in return_section
    assert "game.playerMoveLockedUntil = 0;" in reset_section


@pytest.mark.unit
def test_badminton_removed_stale_assist_hud_and_hotkeys():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert 'id="assist-label"' not in html
    assert "G/S/M 辅助" not in html
    assert "G{{guide}} / S{{sweet}} / M{{music}}" not in html
    assert "G {{guide}} / S {{sweet}} / M {{music}}" not in html
    keydown = html[
        html.index("addBadmintonEventListener(window, 'keydown'"):
        html.index("if (bgmVolumeInput)", html.index("addBadmintonEventListener(window, 'keydown'"))
    ]
    assert "key === 'g'" not in keydown
    assert "key === 's'" not in keydown
    assert "showToggleHint('guide'" not in keydown
    assert "showToggleHint('sweet'" not in keydown
    assert "key === 'm'" in keydown


@pytest.mark.unit
def test_badminton_racket_swing_applies_physics_to_shuttle():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "var SWING_IMPACT_DELAY_MS = 120;" in html
    assert "var SHUTTLE_MASS_KG = 0.005;" in html
    assert "var SHUTTLE_DRAG_PER_SECOND = 0.42;" in html
    assert "var SHUTTLE_FLIGHT_HEIGHT_SCALE = 0.94;" in html
    assert "heldShuttleVisible: true," in html
    assert "pendingSwing: null," in html
    assert "shuttleSeq: 0," in html

    assert "function buildSwingImpulse(angle, power, shooter, incomingBall) {" in html
    assert "var contact = incomingBall ? { x: incomingBall.x, y: incomingBall.y } : getRacketContactPoint(shooter);" in html
    assert "var isSave = !!swingOptions.save && shooter === 'neko' && !isSmash;" in html
    assert "var minTimingQuality = shooter === 'neko' ? (isSave ? 0.46 : 0.62) : 0.34;" in html
    assert "var timingQuality = incomingBall ? clamp(1 - contactError / 118, minTimingQuality, 1) : 1;" in html
    assert "incomingSpeed: incomingSpeed," in html
    assert "incomingVx: incomingVx," in html
    assert "incomingVy: incomingVy," in html
    assert "incomingBallId: incomingBall ? incomingBall.id : 0," in html
    assert "incomingBall: incomingBall || null," in html
    assert "speed: 275 + force * 600 + quality * 82 + Math.min(100, incomingSpeed * 0.10) + smashSpeedBonus," in html
    assert "contact: contact" in html
    assert "function shotNoise(seed, salt) {" in html
    assert "function buildShuttleAerodynamics(launchAngle, power, direction, impulse, shuttleId, hitCount) {" in html
    assert "var playerReturnWobble = impulse.incomingSpeed && direction > 0 ? 1 : 0;" in html
    assert "sliceAccel: direction * sliceBase * (0.35 + force * 0.65 + incoming * 0.35 + playerReturnWobble * 0.22)," in html
    assert "floatLift: (20 + quality * 34 + spinBias * 26) * (impulse.isSmash ? 0.22 : 1)," in html
    assert "lateDrop: 84 + force * 74 + incoming * 42 + playerReturnWobble * 34 + (impulse.isSmash ? 160 + (impulse.smashQuality || 0) * 100 : 0)," in html
    assert "velocityBrake: 0.36 + force * 0.30 + highAngle * 0.18 + incoming * 0.16 + playerReturnWobble * 0.08 - smashBias * 0.12," in html
    assert "brakeDelay: impulse.isSmash ? 0.08 : 0.16 + highAngle * 0.10 - playerReturnWobble * 0.03," in html
    assert "apexDrop: 50 + highAngle * 42 + force * 32 + incoming * 24 + playerReturnWobble * 34 + smashBias * 90," in html
    assert "glideDrift: direction * (shotNoise(seed, 5) - 0.5) * (18 + imperfect * 40 + flatShot * 28 + playerReturnWobble * 18)," in html
    assert "flutterStrength: (10 + imperfect * 34 + incoming * 16 + playerReturnWobble * 12) * (impulse.isSmash ? 0.55 : 1)," in html
    assert "function queueRacketSwing(angle, power, shooter, options) {" in html
    assert "var incomingBall = options && options.incomingBall ? options.incomingBall : null;" in html
    assert "game.state = 'swinging';" in html
    assert "game.heldShuttleVisible = false;" in html
    assert "if (game.ball && game.ball.shooter !== shotShooter && !incomingBall) game.ball = null;" in html
    assert "playShotWhoosh();" in html
    assert "launchShot(swing.angle, swing.power, swing.shooter, swing.impulse);" in html
    assert "}, SWING_IMPACT_DELAY_MS);" in html

    assert "function launchShot(angle, power, shooter, swingImpulse) {" in html
    assert "var hitShuttle = impulse.incomingBall || null;" in html
    assert "var shuttleId = hitShuttle ? hitShuttle.id : ++game.shuttleSeq;" in html
    assert "var shuttle = hitShuttle || {};" in html
    assert "shuttle.id = shuttleId;" in html
    assert "shuttle.radius = BALL_R;" in html
    assert "shuttle.diameter = BALL_R * 2;" in html
    assert "shuttle.massKg = SHUTTLE_MASS_KG;" in html
    assert "shuttle.dragPerSecond = SHUTTLE_DRAG_PER_SECOND;" in html
    assert "shuttle.swingForce = impulse.force;" in html
    assert "shuttle.swingQuality = impulse.quality;" in html
    assert "shuttle.timingQuality = impulse.timingQuality;" in html
    assert "shuttle.incomingSpeed = impulse.incomingSpeed || 0;" in html
    assert "shuttle.returnedFromShuttleId = impulse.incomingBallId || 0;" in html
    assert "shuttle.hitCount = (hitShuttle ? (hitShuttle.hitCount || 1) : 0) + 1;" in html
    assert "shuttle.aero = buildShuttleAerodynamics(launchAngle, power, direction, impulse, shuttle.id, shuttle.hitCount);" in html
    assert "shuttle.awaitingReturnBy = '';" in html
    assert "shuttle.groundedReturnAt = 0;" in html
    assert "var lastShuttleContactToken = '';" in html
    assert "var lastShuttleContactAt = 0;" in html
    assert "function playShuttleContact(quality, token) {" in html
    assert "if (contactToken && contactToken === lastShuttleContactToken) return;" in html
    assert "if (nowMs - lastShuttleContactAt < 90) return;" in html
    contact_section = html[html.index("function playShuttleContact(quality, token) {"):html.index("function playShotResultSound", html.index("function playShuttleContact(quality, token) {"))]
    result_section = html[html.index("function playShotResultSound(shotType, scored) {"):html.index("function playShuttleContactSound", html.index("function playShotResultSound(shotType, scored) {"))]
    assert "badmintonGameAudio.playSfx('shuttleContact'" in contact_section
    assert "badmintonGameAudio.playSfx('shot." not in result_section
    assert "playShuttleContact(impulse.quality, shuttle.id + ':' + shuttle.hitCount);" in html
    assert "var contactDeflection = impulse.incomingSpeed ? (1 - impulse.quality) * (shotShooter === 'neko' ? -24 : 24) : 0;" in html
    assert "var verticalDeflection = impulse.incomingSpeed ? clamp((impulse.incomingVy || 0) * -0.10, -55, 55) : 0;" in html
    assert "var aiReturnLift = 0;" in html
    assert "var baseVy = -v * Math.sin(radians) * impulse.lift + verticalDeflection + aiReturnLift;" in html
    assert "shuttle.vz = -shuttle.vy * SHUTTLE_FLIGHT_HEIGHT_SCALE;" in html
    assert "var smashDownVelocity = impulse.isSmash ? (shotShooter === 'neko' ? 180 + impulse.smashQuality * 145 + impulse.force * 54 : 250 + impulse.smashQuality * 210 + impulse.force * 90) : 0;" in html
    assert "shuttle.vy = impulse.isSmash ? smashDownVelocity : baseVy;" in html
    assert "function buildShuttleSpinRate(launchAngle, power, direction, impulse) {" in html
    assert "var baseSpinRate = direction * getBackspinRate(launchAngle, power, game.distance);" in html
    assert "var incomingSpinRate = impulse.incomingSpeed ? direction * clamp(impulse.incomingSpeed / 42, 0, 26) * (1.15 - impulse.quality * 0.35) : 0;" in html
    assert "var contactSpinRate = impulse.incomingSpeed ? direction * (10 + impulse.quality * 7 + clamp((impulse.incomingSpeed || 0) / 150, 0, 6)) : 0;" in html
    assert "var smashSpinRate = impulse.isSmash ? direction * (12 + impulse.smashQuality * 18) : 0;" in html
    assert "var rawSpinRate = baseSpinRate + incomingSpinRate + contactSpinRate + smashSpinRate;" in html
    assert "var maxSpinRate = direction > 0 ? 10 : 24;" in html
    assert "return clamp(rawSpinRate, -maxSpinRate, maxSpinRate);" in html
    assert "shuttle.spinRate = buildShuttleSpinRate(launchAngle, power, direction, impulse);" in html
    assert "angle: game.ball.angle," in html
    assert "yuiReturnSpinRate" not in html
    assert "if (shotShooter === 'neko' && Math.abs(shuttle.spinRate) < 9)" not in html
    assert "var drag = clamp((ball.dragPerSecond || 0) * subDt, 0, 0.18);" in html
    assert "var playerReturnWobble = impulse.incomingSpeed && direction > 0 ? 1 : 0;" in html
    assert "var playerReturnAngle = clamp(game.aimAngle - 10, 34, 48);" in html
    assert "var returnPower = clamp(game.power || 56, 24, 100);" in html
    assert "queueRacketSwing(playerReturnAngle, returnPower, 'player', { incomingBall: game.ball });" in html
    assert "var aero = ball.aero || null;" in html
    assert "var flutter = Math.sin((aero.flutterPhase || 0) + aero.age * (aero.flutterRate || 0)) * (aero.flutterStrength || 0);" in html
    assert "var slice = (aero.sliceAccel || 0) * Math.exp(-aero.age * 2.2);" in html
    assert "var floatLift = (aero.floatLift || 0) * Math.exp(-aero.age * 4.1) * (0.35 + speedFactor * 0.75);" in html
    assert "var lateDrop = (aero.lateDrop || 0) * clamp((aero.age - 0.12) / 0.46, 0, 1) * (ball.vy > -120 ? 1 : 0.42);" in html
    assert "var brakeReady = clamp((aero.age - (aero.brakeDelay || 0)) / 0.24, 0, 1);" in html
    assert "var brakeFactor = clamp(speed / 360, 0.20, 1);" in html
    assert "var velocityBrake = clamp((aero.velocityBrake || 0) * brakeFactor * brakeFactor * brakeReady * subDt, 0, 0.12);" in html
    assert "var dropSnap = (aero.apexDrop || 0) * brakeReady * clamp((ball.vy + 150) / 430, 0, 1);" in html
    assert "var glide = (aero.glideDrift || 0) * clamp((aero.age - 0.18) / 0.52, 0, 1) * (1 - speedFactor * 0.55);" in html
    assert "var dragPulse = 1 + speedFactor * 0.85 + (aero.dragPulse || 0) * speedFactor * clamp((aero.age - 0.08) / 0.46, 0, 1);" in html
    assert "ball.vx += (slice + flutter + glide) * subDt;" in html
    assert "ball.vy += (-floatLift + lateDrop + dropSnap) * subDt;" in html
    assert "ball.vx *= (1 - velocityBrake);" in html
    assert "ball.vy *= (1 - velocityBrake * 0.55);" in html
    assert "function stepAwaitingPlayerReturn(dt) {" in html
    assert "stepBallPhysics(ball, subDt);" in html
    assert "var ballRadius = ball.radius || BALL_R;" in html
    assert "function drawPlayerServeShuttleHint() {" in html
    assert "if (!shouldDrawHeldShuttle()) return;" in html
    assert "window.__badmintonPlayerHeldShuttleDebug = null;" in html
    assert "drawPlayerServeShuttleHint();" in html

    state_start = html.index("getState: function () {")
    state_section = html[state_start:html.index("resetGame: resetGame", state_start)]
    assert "heldShuttleVisible: game.heldShuttleVisible," in state_section
    assert "pendingSwing: game.pendingSwing ?" in state_section
    assert "currentShuttle: game.ball ?" in state_section
    assert "diameter: game.ball.diameter," in state_section
    assert "dragPerSecond: game.ball.dragPerSecond," in state_section
    assert "spinAngle: game.ball.spinAngle || 0," in state_section
    assert "spinRate: game.ball.spinRate || 0," in state_section
    assert "timingQuality: game.ball.timingQuality," in state_section
    assert "incomingSpeed: game.ball.incomingSpeed," in state_section
    assert "returnedFromShuttleId: game.ball.returnedFromShuttleId," in state_section
    assert "y: getShuttleCourtY(game.ball, false)," in state_section
    assert "z: getShuttleCourtZ(game.ball, false)," in state_section
    assert "screenX: game.ball.x," in state_section
    assert "screenY: game.ball.y," in state_section


@pytest.mark.unit
def test_badminton_shuttle_and_banana_rendering_use_sprite_cache():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    for expected in (
        "var badmintonSpriteCache = {};",
        "function getBadmintonSpriteCacheKey(kind, radius) {",
        "function renderBadmintonShuttleSprite(spriteCtx, radius) {",
        "drawBadmintonShuttlecockVector(spriteCtx, radius);",
        "function getBadmintonShuttleSprite(radius) {",
        "function renderBadmintonBananaSprite(spriteCtx, radius) {",
        "drawBananaPeelVector(spriteCtx, radius);",
        "function getBadmintonBananaSprite(radius) {",
    ):
        assert expected in html

    shuttle_start = html.index("function drawBadmintonShuttlecockOnContext(renderCtx, x, y, radius, rotation) {")
    shuttle_section = html[shuttle_start:html.index("function drawShuttlecock(x, y, radius, rotation) {", shuttle_start)]
    assert "var sprite = getBadmintonShuttleSprite(r);" in shuttle_section
    assert "renderCtx.drawImage(sprite, -sprite.width / 2, -sprite.height / 2);" in shuttle_section
    assert "drawBadmintonShuttlecockVector(renderCtx, r);" in shuttle_section

    banana_start = html.index("function drawBananaPeelOnContext(renderCtx, x, y, radius, rotation) {")
    banana_section = html[banana_start:html.index("function drawBackspinBall(ball) {", banana_start)]
    assert "var sprite = getBadmintonBananaSprite(r);" in banana_section
    assert "renderCtx.drawImage(sprite, -sprite.width / 2, -sprite.height / 2);" in banana_section
    assert "drawBananaPeelVector(renderCtx, r);" in banana_section


@pytest.mark.unit
def test_badminton_player_ice_powerup_uses_preloaded_png_sprite():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    ice_png = ROOT / "static" / "game" / "games" / "badminton" / "images" / "player-ice-powerup.png"
    frozen_crystal_png = ROOT / "static" / "game" / "games" / "badminton" / "images" / "yui-frozen-crystals.png"

    assert ice_png.exists()
    assert 1000 < ice_png.stat().st_size < 40000
    assert frozen_crystal_png.exists()
    assert 1000 < frozen_crystal_png.stat().st_size < 40000
    assert '/static/game/games/badminton/images/player-ice-powerup.png?v={{ static_asset_version }}' in html
    assert '/static/game/games/badminton/images/yui-frozen-crystals.png?v={{ static_asset_version }}' in html
    assert 'id="yui-frozen-crystal-overlay"' in html
    assert "#yui-frozen-crystal-overlay" in html
    assert "z-index: 12;" in html
    assert "var PLAYER_ICE_POWERUP_SPRITE_SRC = badmintonAssetUrl('/static/game/games/badminton/images/player-ice-powerup.png');" in html
    assert "var YUI_FROZEN_CRYSTAL_SPRITE_SRC = badmintonAssetUrl('/static/game/games/badminton/images/yui-frozen-crystals.png');" in html
    assert "function preloadPlayerIcePowerupSprite() {" in html
    assert "function isPlayerIcePowerupSpriteReady() {" in html
    assert "try { preloadPlayerIcePowerupSprite(); } catch (_) { playerIcePowerupSpriteImage = null; }" in html
    assert "function preloadYuiFrozenCrystalSprite() {" in html
    assert "function isYuiFrozenCrystalSpriteReady() {" in html
    assert "try { preloadYuiFrozenCrystalSprite(); } catch (_) { yuiFrozenCrystalSpriteImage = null; }" in html

    draw_start = html.index("function drawPlayerIcePowerupSprite(x, y, size, rotation) {")
    draw_section = html[draw_start:html.index("function drawYuiCheatItems(now) {", draw_start)]
    assert "if (!isPlayerIcePowerupSpriteReady()) return;" in draw_section
    assert "ctx.drawImage(playerIcePowerupSpriteImage, -drawW / 2, -drawH * 0.86, drawW, drawH);" in draw_section
    assert "function updateYuiFrozenCrystalOverlay(now) {" in draw_section
    assert "if (!isDuelMode() || !isYuiFrozen(t) || !isYuiFrozenCrystalSpriteReady()) {" in draw_section
    assert "yuiFrozenCrystalOverlay.style.left = '-9999px';" in draw_section
    assert "yuiFrozenCrystalOverlay.style.top = '-9999px';" in draw_section
    assert "yuiFrozenCrystalOverlay.style.transform = 'translate(-50%, -82%) scale('" in draw_section
    assert "ctx.drawImage(yuiFrozenCrystalSpriteImage" not in draw_section
    assert "ctx.createLinearGradient" not in draw_section
    assert "ctx.lineTo" not in draw_section
    assert "drawPlayerPowerupItems(t);" in html
    assert "updateYuiFrozenCrystalOverlay(t);" in html


@pytest.mark.unit
def test_badminton_player_ice_powerup_freezes_yui():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "var PLAYER_POWERUP_ICE_RADIUS = 22;" in html
    assert "var PLAYER_POWERUP_ICE_DRAW_SIZE = 12;" in html
    assert "var PLAYER_POWERUP_ICE_FREEZE_MS = 3000;" in html
    assert "var PLAYER_POWERUP_ICE_SPAWN_CHANCE = 0.10;" in html
    assert "var YUI_FROZEN_CRYSTAL_DRAW_WIDTH = 82;" in html
    assert "playerPowerup: { items: [], freezeUntil: 0, nextIceSpawnAt: performance.now() + PLAYER_POWERUP_ICE_SPAWN_MIN_MS, seq: 0 }" in html
    assert "function spawnPlayerIcePowerup(options) {" in html
    assert "function applyYuiFreeze(item) {" in html
    assert "kind: 'player_powerup_hit'," in html
    assert "label: 'ice_freeze_yui'," in html
    assert "function cancelPendingYuiSwingForFreeze(now) {" in html
    assert "if (!game.pendingSwing || game.pendingSwing.shooter !== 'neko') return false;" in html
    assert "game.pendingSwing = null;" in html
    assert "game.state = 'in_flight';" in html
    assert "game.state = 'neko_thinking';" in html
    assert "cancelPendingYuiSwingForFreeze(now);" in html
    assert "function isYuiFrozen(now) {" in html
    assert "function getYuiFreezeState(now) {" in html

    update_start = html.index("function updateCourtMovement(dt) {")
    update_section = html[update_start:html.index("function getPlayerX()", update_start)]
    assert "updatePlayerPowerups(dt);" in update_section
    assert "if (isYuiFrozen()) {" in update_section
    assert "yuiChaseSpeedX = 0;" in update_section
    assert "yuiChaseSpeedY = 0;" in update_section

    yui_return_start = html.index("function maybeYuiReturnIncomingShuttle(ball) {")
    yui_return_section = html[yui_return_start:html.index("function maybePlayerReceiveIncomingShuttle(ball)", yui_return_start)]
    assert "if (isYuiFrozen()) {" in yui_return_section
    assert "ball.scoredDuringYuiFreeze = true;" in yui_return_section
    assert "return false;" in yui_return_section

    neko_turn_start = html.index("function startNekoDuelTurn() {")
    neko_turn_section = html[neko_turn_start:html.index("function scheduleNekoDuelTurn()", neko_turn_start)]
    assert "var freeze = getYuiFreezeState();" in neko_turn_section
    assert "game.duel.timer = setTimeout(startNekoDuelTurn, freeze.remaining_ms + 120);" in neko_turn_section

    assert "playerPowerup: (function () {" in html
    assert "yui_freeze: getYuiFreezeState(now)," in html
    assert "sprite_ready: isPlayerIcePowerupSpriteReady()" in html
    assert "frozen_crystal_ready: isYuiFrozenCrystalSpriteReady()" in html
    assert "_debugSpawnPlayerIcePowerup: debugSpawnPlayerIcePowerup," in html


@pytest.mark.unit
def test_badminton_shuttle_trail_uses_ring_buffer_without_changing_public_state():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    for expected in (
        "var SHUTTLE_TRAIL_LIMIT = 20;",
        "function initShuttleTrail(shuttle, x, y) {",
        "shuttle.trailHead = 0;",
        "shuttle.trailLength = 0;",
        "function pushShuttleTrailPoint(shuttle, x, y) {",
        "var slot = trail[shuttle.trailHead];",
        "shuttle.trailHead = (shuttle.trailHead + 1) % SHUTTLE_TRAIL_LIMIT;",
        "function getShuttleTrailPoints(shuttle) {",
        "return points;",
        "initShuttleTrail(shuttle, origin.x, origin.y);",
        "pushShuttleTrailPoint(ball, ball.x, ball.y);",
        "pushShuttleTrailPoint(b, b.x, b.y);",
    ):
        assert expected in html

    assert "ball.trail.push({ x: ball.x, y: ball.y });" not in html
    assert "b.trail.push({ x: b.x, y: b.y });" not in html
    assert "ball.trail.length > 20" not in html
    assert "b.trail.length > 20" not in html

    draw_start = html.index("function drawBackspinBall(ball) {")
    draw_section = html[draw_start:html.index("function drawNet()", draw_start)]
    assert "var trail = getShuttleTrailPoints(ball);" in draw_section
    assert "var firstTrailIndex = ball.isSmash ? Math.max(0, trail.length - 5) : 0;" in draw_section
    assert "for (var i = firstTrailIndex + 1; i < trail.length; i++)" in draw_section
    assert "ctx.createLinearGradient" not in draw_section
    assert "ctx.shadowBlur" not in draw_section

    state_start = html.index("getState: function () {")
    state_section = html[state_start:html.index("resetGame: resetGame", state_start)]
    assert "trail: getShuttleTrailPoints(game.ball)," in state_section


@pytest.mark.unit
def test_badminton_duel_scores_on_valid_landings_inside_lines():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "rallyHits: 0," in html
    assert "rally_hits: game.duel.rallyHits," in html
    assert "var pointWinner = '';" in html
    assert "pointWinner = scored ? shooter : (shooter === 'player' ? 'neko' : 'player');" in html
    assert "if (pointWinner === 'player') {\n      game.duel.playerScore += point;" in html
    assert "} else {\n      game.duel.nekoScore += point;" in html
    assert "if (scored) {\n      if (shooter === 'player') game.duel.nekoMisses += 1;" in html
    assert "result: scored ? 'scored' : 'missed'," in html
    assert "point_winner: pointWinner," in html
    assert "rally_hits: game.duel.rallyHits," in html
    assert "} else if (pointWinner === 'neko') {\n      game.duel.round += 1;\n      scheduleNekoDuelTurn();" in html
    assert "showAssistHint(_i18n('toast.nekoThinking', 'Yui 准备回球'));" in html
    assert "var point = scored ? (previousDistance >= 405 ? 2 : 1) : 0;" not in html
    assert "if (shooter === 'player') game.duel.playerScore += point;" not in html
    assert "else game.duel.nekoScore += point;" not in html


@pytest.mark.unit
def test_badminton_yui_octopus_ink_has_stronger_screen_coverage():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert BADMINTON_INK_OVERLAY.exists()
    assert BADMINTON_INK_OVERLAY.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    from PIL import Image
    with Image.open(BADMINTON_INK_OVERLAY) as ink_overlay:
        assert ink_overlay.size == (900, 500)
        alpha = ink_overlay.getchannel("A")
        assert alpha.getpixel((450, 240)) == 255
        assert alpha.getpixel((80, 80)) == 0
        assert alpha.getpixel((850, 450)) == 0
        screen_splatter_points = [
            (150, 250),
            (760, 250),
            (250, 105),
            (690, 125),
            (240, 340),
            (700, 335),
            (450, 380),
            (300, 410),
        ]
        assert sum(1 for point in screen_splatter_points if alpha.getpixel(point) >= 90) >= 7
    assert "alpha: active ? clamp(0.38 + fade * 0.52, 0, 0.9) : 0," in html
    assert "aimingCtx.fillRect(0, 0, BASE_W, BASE_H);" in html
    assert '<html lang="zh-CN" data-static-asset-version="{{ static_asset_version }}">' in html
    assert '<link rel="preload" as="image" href="/static/game/games/badminton/images/yui-octopus-ink-overlay.png?v={{ static_asset_version }}">' in html
    assert "function badmintonAssetUrl(path) {" in html
    assert "var YUI_CHEAT_INK_OVERLAY_SRC = badmintonAssetUrl('/static/game/games/badminton/images/yui-octopus-ink-overlay.png');" in html
    assert "var yuiInkOverlayImage = null;" in html
    assert "function preloadYuiInkOverlayImage() {" in html
    assert "function isYuiInkOverlayImageReady() {" in html
    assert "try { preloadYuiInkOverlayImage(); } catch (_) { yuiInkOverlayImage = null; }" in html
    assert "function ensureYuiInkOverlayCache() {" in html
    assert "function drawYuiInkArcRings(ink, t) {" in html
    assert "var centralInk = inkCtx.createRadialGradient(BASE_W * 0.50, BASE_H * 0.48" in html
    assert "centralInk.addColorStop(0, 'rgba(0,0,0,1)');" in html
    assert "centralInk.addColorStop(0.30, 'rgba(0,0,0,.99)');" in html
    assert "centralInk.addColorStop(0.58, 'rgba(0,2,7,.90)');" in html
    assert "function drawKartInkSplat(cx, cy, rx, ry, rot, alpha) {" in html
    assert "drawKartInkSplat(BASE_W * 0.50, BASE_H * 0.49" in html
    assert "inkCtx.fillStyle = 'rgba(0,0,0,.97)';" in html
    assert "function drawCenterInkBlob(cx, cy, rx, ry, rot, alpha) {" in html
    assert "var centerSplashes = [" in html
    assert "for (var drip = 0; drip < 8; drip++) {" in html
    assert "for (var speck = 0; speck < 22; speck++) {" in html
    assert "inkCtx.bezierCurveTo(" in html
    assert "var inkOverlay = isYuiInkOverlayImageReady() ? yuiInkOverlayImage : ensureYuiInkOverlayCache();" in html
    assert "if (!inkOverlay) return;" in html
    assert "aimingCtx.drawImage(inkOverlay," in html
    assert "aimingCtx.globalAlpha = Math.min(0.42, ink.alpha * 0.46);" in html
    assert "aimingCtx.fillStyle = 'rgba(0,0,0,.78)';" in html
    assert "aimingCtx.globalAlpha = Math.min(1, ink.alpha * 1.12);" in html
    assert "drawYuiInkArcRings(ink, t);" in html
    assert "if (!ink || ink.alpha <= 0.58) return;" in html
    cache_start = html.index("function ensureYuiInkOverlayCache() {")
    cache_end = html.index("function drawYuiInkArcRings(ink, t) {", cache_start)
    cache_section = html[cache_start:cache_end]
    assert "rgba(114,216,226,.42)" not in cache_section
    assert "function drawInkBlob(" not in html
    assert "var edgeVeil =" not in html
    assert "badmintonGameAudio.playSfx('yuiCheat.octopusInk', { volumeMultiplier: 0.92 });" in html


@pytest.mark.unit
def test_badminton_yui_cheat_items_warn_player_with_bubble_lines():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    ink_start = html.index("function activateYuiInk() {")
    ink_section = html[ink_start:html.index("function spawnYuiCheat(kind, options) {", ink_start)]
    spawn_start = html.index("function spawnYuiCheat(kind, options) {")
    spawn_section = html[spawn_start:html.index("function maybeTriggerYuiCheat(reason) {", spawn_start)]

    assert "function showYuiCheatLine(kind) {" in html
    assert "function showYuiCheatHitLine(kind) {" in html
    assert "function showYuiCheatScoreLine(kind) {" in html
    assert "function markYuiCheatHit(kind) {" in html
    assert "function getYuiCheatScoreTauntKind(pointWinner) {" in html
    assert "var YUI_CHEAT_RANDOM_CHANCE = 0.72;" in html
    assert "var YUI_CHEAT_CLOSE_SCORE_CHANCE = 0.86;" in html
    assert "var YUI_CHEAT_COOLDOWN_MS = 5200;" in html
    assert "_i18nArray('lines.yuiCheat.banana'" in html
    assert "_i18nArray('lines.yuiCheat.octopus'" in html
    assert "_i18nArray('lines.yuiCheatScore.banana'" in html
    assert "_i18nArray('lines.yuiCheatScore.octopus'" in html
    assert "_i18nArray('lines.playerIceScore', ['唔不算嘛不算嘛重来！'])" in html
    assert "function showYuiPlayerIceScoreLine() {" in html
    assert '#neko-bubble[data-variant="yui-cheat"] { background: #cfeaff;' in html
    assert '#neko-bubble[data-variant="yui-cheat-score"] { background: #fff0b8;' in html
    assert "bubble.dataset.variant = (control && control.bubbleVariant) || '';" in html
    assert "var bubblePriorityUntil = 0;" in html
    assert "function shouldKeepCurrentBubble(control) {" in html
    assert "if (shouldKeepCurrentBubble(control || {})) return false;" in html
    assert "if (shouldKeepCurrentBubble(emoteControl)) return false;" in html
    assert "bubblePriorityUntil = Date.now() + holdMs;" in html
    assert "var control = { expression: 'tease', intensity: 'medium', mood: 'surprised', bubbleVariant: 'yui-cheat', bubblePriority: 8, bubbleHoldMs: 3200, bubbleDurationMs: 4200 };" in html
    assert "showBubble(line, control, event);" in html
    assert "speakLine(line, control, event);" in html
    assert "kind: 'yui_cheat_item'," in html
    assert "label: 'yui_cheat_' + kind," in html
    assert "kind: 'player_ice_score'," in html
    assert "label: 'player_ice_score'," in html
    assert "item_kind: kind," in html
    assert "item_kind: 'ice'," in html
    assert "force_voice_in_debug: true," in html
    assert "voice_deadline_ms: 6200" in html
    assert "voice_deadline_ms: 6800" in html
    assert "voice_deadline_ms: 5200" in html
    assert "if (kind === 'yui_cheat_item') return 0;" in html
    assert "if (kind === 'yui_cheat_hit') return 0;" in html
    assert "if (kind === 'yui_cheat_score') return 0;" in html
    assert "if (kind === 'player_ice_score') return 0;" in html
    assert "kind === 'player_ice_score') return true;" in html
    assert "kind === 'player_ice_score';" in html
    assert "if (shouldInterruptVoiceAudio(entry.event)) {" in html
    assert "if (shouldInterruptVoiceAudio(entry.event) && !voiceArbiter.inFlight.isUserReply) {" in html
    assert "if (shouldInterruptVoiceAudio(pending.event) && !voiceArbiter.inFlight.isUserReply) {" in html
    assert "voiceArbiter.inFlight = null;" in html
    assert "void _requestVoicePlayback(entry);" in html
    assert "void _requestVoicePlayback(pending);" in html
    assert "eventKind: String(entry.event && entry.event.kind || '')" in html
    assert "local_voice_already_spoken: true," in html
    send_event_start = html.index("function sendGameEvent(event) {")
    send_event = html[send_event_start:html.index("function loadLocalLeaderboard(", send_event_start)]
    assert "if (event.local_voice_already_spoken) return;" in send_event
    assert send_event.index("if (event.local_voice_already_spoken) return;") < send_event.index("if (debugMode) {")
    assert "showYuiCheatLine('octopus');" in ink_section
    assert "showYuiCheatLine('banana');" in spawn_section
    assert "markYuiCheatHit('banana');" in html
    assert "showYuiCheatHitLine('banana');" in html
    assert "kind: 'yui_cheat_hit'," in html
    finish_start = html.index("function finishDuelShot(scored, shotType, ball) {")
    finish_section = html[finish_start:html.index("function finishShot(scored, shotType, ball) {", finish_start)]
    assert "var playerIceScore = pointWinner === 'player' && shooter === 'player' && !!(ball && ball.scoredDuringYuiFreeze);" in finish_section
    assert "if (playerIceScore) showYuiPlayerIceScoreLine();" in finish_section
    assert "var cheatScoreTauntKind = getYuiCheatScoreTauntKind(pointWinner);" in finish_section
    assert "if (cheatScoreTauntKind) showYuiCheatScoreLine(cheatScoreTauntKind);" in finish_section


@pytest.mark.unit
def test_badminton_banana_slip_flips_player_avatar_360_degrees():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    banana_start = html.index("function buildYuiCheatBanana(options) {")
    banana_section = html[banana_start:html.index("function activateYuiInk() {", banana_start)]

    assert "function randomYuiBananaCourtX() {" in html
    assert "var x = Number.isFinite(opts.x) ? opts.x : randomYuiBananaCourtX();" in banana_section
    assert "var y = Number.isFinite(opts.y) ? opts.y : getPlayerGroundFootY();" in banana_section
    assert "function getPlayerGroundFootY() {" in html
    assert "playerGroundFootY: getPlayerGroundFootY()," in html
    assert "var dy = getPlayerFootY() - item.y;" in html
    assert "getPlayerX()" not in banana_section
    assert "game.playerCourt.targetX + defaultOffset" not in banana_section
    assert "var YUI_CHEAT_BANANA_SLIP_ROTATION_RAD = Math.PI * 2;" in html
    assert "var YUI_CHEAT_BANANA_SPEED_MULTIPLIER = 0.22;" in html
    assert "var YUI_CHEAT_BANANA_SLOW_MS = 1800;" in html
    assert "var easedProgress = 1 - Math.pow(1 - progress, 3);" in html
    assert "effect.spinAngle = YUI_CHEAT_BANANA_SLIP_ROTATION_RAD * easedProgress;" in html
    assert "speed_multiplier: getPlayerMovementSpeedMultiplier(now)," in html
    assert "return t < (effect.slowUntil || 0) ? YUI_CHEAT_BANANA_SPEED_MULTIPLIER : 1;" in html
    assert "badmintonGameAudio.playSfx('yuiCheat.bananaSlip', { volumeMultiplier: 0.9 });" in html
    assert "rotateY(var(--sensei-slip-rotation)) rotate(-3deg) scale(.97)" in html
    assert "ctx.scale(Math.cos(slipRotation), 1);" in html
    assert "Math.sin(progress * Math.PI * 5)" not in html


@pytest.mark.unit
def test_badminton_yui_returns_incoming_shuttle_before_landing():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "function maybeYuiReturnIncomingShuttle(ball) {" in html
    yui_return_start = html.index("function maybeYuiReturnIncomingShuttle(ball) {")
    yui_return_section = html[yui_return_start:html.index("function finishDuelShot(", yui_return_start)]
    assert "if (isYuiFrozen()) {" in yui_return_section
    assert "ball.scoredDuringYuiFreeze = true;" in yui_return_section
    assert "return false;" in yui_return_section
    assert "if (ball.shooter !== 'player' || ball.direction !== 1 || !ball.crossedNet) return false;" in html
    assert "var contact = getYuiShotOrigin();" in html
    assert "var shuttleCourtY = getShuttleCourtY(ball, false);" in html
    assert "var previousShuttleCourtY = getShuttleCourtY(ball, true);" in html
    assert "var shuttleZ = getShuttleCourtZ(ball, false);" in html
    assert "var yuiContactTopZ = screenYToCourtZ(contact.y - 26);" in html
    assert "var yuiContactBottomZ = screenYToCourtZ(FLOOR_Y - 110);" in html
    assert "var YUI_RACKET_HIT_REACH_X = 46;" in html
    assert "var YUI_RACKET_HIT_REACH_Y = 62;" in html
    assert "var YUI_RACKET_RESCUE_GATE_REACH_X = 38;" in html
    assert "var YUI_RACKET_RESCUE_GATE_REACH_Y = 54;" in html
    assert "function getYuiVisibleRacketContactPoint() {" in html
    assert "source: 'live2d-racket-head'" in html
    assert "function getYuiRacketContactPoint() {" in html
    assert "x: getYuiX() + YUI_RACKET_BODY_OFFSET_X," in html
    assert "return getYuiVisibleRacketContactPoint() || getYuiShotOrigin();" in html
    assert "function getYuiNeutralRacketContactPoint() {" in html
    assert "source: 'neutral-racket-anchor'" in html
    assert "function getYuiRacketRangeStateForPoint(incomingBall, racket, reachX, reachY) {" in html
    assert "var contact = racket || getYuiNeutralRacketContactPoint();" in html
    assert "function getYuiRacketRangeState(incomingBall, reachX, reachY) {" in html
    assert "var neutral = getYuiNeutralRacketContactPoint();" in html
    assert "var visible = getYuiVisibleRacketContactPoint();" in html
    assert "var best = getYuiRacketRangeStateForPoint(incomingBall, neutral, reachX, reachY);" in html
    assert "var visibleState = getYuiRacketRangeStateForPoint(incomingBall, visible, reachX, reachY);" in html
    assert "if (visibleState.normalized < best.normalized) best = visibleState;" in html
    assert "function getYuiVisibleOrNeutralRacketRangeState(incomingBall, reachX, reachY) {" in html
    assert "return getYuiRacketRangeStateForPoint(incomingBall, visible || getYuiNeutralRacketContactPoint(), reachX, reachY);" in html
    assert "function getYuiRacketHitRangeState(incomingBall) {" in html
    assert "return getYuiVisibleOrNeutralRacketRangeState(incomingBall, YUI_RACKET_HIT_REACH_X, YUI_RACKET_HIT_REACH_Y);" in html
    assert "function isYuiRacketHitInRange(incomingBall) {" in html
    assert "return getYuiRacketHitRangeState(incomingBall).normalized <= 1;" in html
    assert "function getYuiRacketRescueGateState(incomingBall) {" in html
    assert "return getYuiVisibleOrNeutralRacketRangeState(incomingBall, YUI_RACKET_RESCUE_GATE_REACH_X, YUI_RACKET_RESCUE_GATE_REACH_Y);" in html
    assert "var YUI_RACKET_SAVE_REACH_X = 66;" in html
    assert "var YUI_RACKET_SAVE_REACH_Y = 72;" in html
    assert "var YUI_SHORT_DROP_SAVE_REACH_X = 72;" in html
    assert "var YUI_SHORT_DROP_SAVE_REACH_Y = 92;" in html
    assert "var YUI_SHORT_DROP_SAVE_NET_WINDOW_PX = 112;" in html
    assert "var YUI_SHORT_DROP_SAVE_MAX_Z = 96;" in html
    assert "var YUI_SHORT_DROP_SAVE_MIN_Z = 4;" in html
    assert "var YUI_SHORT_DROP_SAVE_POWER_MAX = 32;" in html
    assert "function getYuiRacketSaveRangeState(incomingBall) {" in html
    assert "return getYuiVisibleOrNeutralRacketRangeState(incomingBall, YUI_RACKET_SAVE_REACH_X, YUI_RACKET_SAVE_REACH_Y);" in html
    assert "function getYuiShortDropSaveRangeState(incomingBall) {" in html
    assert "return getYuiVisibleOrNeutralRacketRangeState(incomingBall, YUI_SHORT_DROP_SAVE_REACH_X, YUI_SHORT_DROP_SAVE_REACH_Y);" in html
    assert "function isYuiSmashSaveReachable(incomingBall) {" in html
    assert "return !!(incomingBall && incomingBall.isSmash) && getYuiRacketRescueGateState(incomingBall).normalized > 1 && getYuiRacketSaveRangeState(incomingBall).normalized <= 1;" in html
    assert "function isYuiShortDropSaveReachable(incomingBall) {" in html
    short_drop_save_start = html.index("function isYuiShortDropSaveReachable(incomingBall) {")
    short_drop_save_section = html[short_drop_save_start:html.index("function getYuiSmashHeightQuality(incomingBall)", short_drop_save_start)]
    assert "var shuttleCourtY = getShuttleCourtY(incomingBall, false);" in short_drop_save_section
    assert "var nearNetLeft = BADMINTON.netX + shuttleRadius * 0.5;" in short_drop_save_section
    assert "var nearNetRight = nearNetLeft + YUI_SHORT_DROP_SAVE_NET_WINDOW_PX;" in short_drop_save_section
    assert "if (shuttleCourtY < nearNetLeft || shuttleCourtY > nearNetRight) return false;" in short_drop_save_section
    assert "var shuttleZ = getShuttleCourtZ(incomingBall, false);" in short_drop_save_section
    assert "var minSaveZ = Math.max(YUI_SHORT_DROP_SAVE_MIN_Z, shuttleRadius * 0.25 + 0.5);" in short_drop_save_section
    assert "if (shuttleZ <= minSaveZ || shuttleZ > YUI_SHORT_DROP_SAVE_MAX_Z) return false;" in short_drop_save_section
    assert "if (shotPower > YUI_SHORT_DROP_SAVE_POWER_MAX) return false;" in short_drop_save_section
    assert "return getYuiRacketHitRangeState(incomingBall).normalized > 1 && getYuiShortDropSaveRangeState(incomingBall).normalized <= 1;" in short_drop_save_section
    assert "var YUI_SMASH_MIN_SHUTTLE_Z = 64;" in html
    assert "var YUI_SMASH_FULL_SHUTTLE_Z = 138;" in html
    assert "function getYuiSmashHeightQuality(incomingBall) {" in html
    assert "var shuttleZ = getShuttleCourtZ(incomingBall, false);" in html
    assert "return clamp((shuttleZ - YUI_SMASH_MIN_SHUTTLE_Z) / Math.max(1, YUI_SMASH_FULL_SHUTTLE_Z - YUI_SMASH_MIN_SHUTTLE_Z), 0, 1);" in html
    assert "function getBadmintonRacketHeadCenter(anchorX, anchorY, scale, rotation, mirror) {" in html
    assert "var racketHead = getBadmintonRacketHeadCenter(anchorX, anchorY, racketScale, rotation, racketMirror);" in html
    assert "headX: racketHead.x," in html
    assert "headY: racketHead.y," in html
    assert "var yuiReachLeft = BADMINTON.netX + shuttleRadius * 0.5;" in html
    assert "var yuiReachRight = BADMINTON.courtRight - shuttleRadius * 0.5;" in html
    assert "var yuiRacketHitState = getYuiRacketHitRangeState(ball);" in html
    assert "var yuiRacketHitInRange = yuiRacketHitState.normalized <= 1;" in html
    assert "var crossesYuiReach = Math.max(previousShuttleCourtY, shuttleCourtY) >= yuiReachLeft" in html
    assert "&& Math.min(previousShuttleCourtY, shuttleCourtY) <= yuiReachRight;" in html
    assert "var inYuiReach = yuiRacketHitInRange || (crossesYuiReach" in html
    assert "&& shuttleZ <= yuiContactTopZ" in html
    assert "&& shuttleZ >= yuiContactBottomZ);" in html
    assert "var yuiShortDropSave = isYuiShortDropSaveReachable(ball);" in html
    assert "var yuiSave = isYuiSmashSaveReachable(ball) || yuiShortDropSave;" in html
    assert "if (!inYuiReach && !yuiSave) return false;" in html
    assert "if (!yuiRacketHitInRange && !yuiSave) return false;" in html
    assert "game.duel.activeShooter = 'neko';" in html
    assert "game.duel.rallyHits += 1;" in html
    assert "var returnPressureBoost = Math.min(5, game.duel.rallyHits * 0.30);" in html
    assert "shot.angle = clamp(shot.angle - Math.min(2.5, game.duel.rallyHits * 0.16), 38, 52);" in html
    assert "shot.power = clamp(shot.power + returnPressureBoost, 48, 66);" in html
    assert "var yuiSmashQuality = getYuiSmashQuality(ball, shot);" in html
    assert "var yuiSmash = !yuiSave && shouldYuiSmashReturn(ball, shot, yuiSmashQuality);" in html
    assert "if (yuiSave) {" in html
    assert "shot.angle = clamp(shot.angle + 6, 46, 58);" in html
    assert "shot.power = clamp(shot.power - 3, 45, 60);" in html
    assert "shot.angle = clamp(shot.angle - (10 + yuiSmashQuality * 5), 24, 46);" in html
    assert "shot.power = clamp(shot.power + 7 + yuiSmashQuality * 9, 56, 78);" in html
    assert "showAssistHint(_i18n(yuiSave ? 'toast.nekoSave' : (yuiSmash ? 'toast.nekoSmash' : 'toast.nekoReturn')" in html
    assert "queueRacketSwing(shot.angle, shot.power, 'neko', { incomingBall: ball, smash: yuiSmash, smashQuality: yuiSmashQuality, save: yuiSave });" in html

    update_start = html.index("function update(dt, now) {")
    update_section = html[update_start:html.index("function drawDistanceMarkers()", update_start)]
    assert "stepBallPhysics(b, subDt);" in update_section
    assert "if (b.resolved && canonicalShotType(b.pendingShotType) === 'net') {" in update_section
    assert update_section.index("if (b.resolved && canonicalShotType(b.pendingShotType) === 'net') {") < update_section.index("stepBallPhysics(b, subDt);")
    assert "if (!b.hitNet && maybeYuiReturnIncomingShuttle(b)) break;" in update_section
    assert update_section.index("updateShuttleNetCrossing(b);") < update_section.index("if (!b.hitNet && maybeYuiReturnIncomingShuttle(b)) break;")
    assert update_section.index("if (!b.hitNet && maybeYuiReturnIncomingShuttle(b)) break;") < update_section.index("checkShuttleLanding(b);")
    assert update_section.index("if (!b.hitNet && maybeYuiReturnIncomingShuttle(b)) break;") < update_section.index("if (maybePlayerReceiveIncomingShuttle(b)) break;")
    net_crossing_start = html.index("function updateShuttleNetCrossing(ball) {")
    net_crossing_section = html[net_crossing_start:html.index("function checkShuttleLanding(ball) {", net_crossing_start)]
    assert "var crossedNet = didShuttleCrossMidcourtNet(ball, direction);" in net_crossing_section
    assert "if (!didShuttleLegallyClearNet(ball, netCrossing)) {" in net_crossing_section
    assert "applyMidcourtNetContact(ball, netCrossing);" in net_crossing_section
    assert "ball.legalNetClearance = true;" in net_crossing_section
    awaiting_return_start = html.index("function stepAwaitingPlayerReturn(dt) {")
    awaiting_return_section = html[awaiting_return_start:html.index("function queueShotResolution(ball, scored, shotType, delayMs) {", awaiting_return_start)]
    assert "if (ball.resolved) {" in awaiting_return_section
    assert "finishShot(ball.pendingScored, ball.pendingShotType, ball);" in awaiting_return_section
    assert "if (!ball.hitNet && performance.now() > ball.returnDeadlineAt) {" in awaiting_return_section
    assert awaiting_return_section.index("if (ball.resolved) {") < awaiting_return_section.index("if (!ball.hitNet && performance.now() > ball.returnDeadlineAt) {")
    assert "if (ball.hitNet) {\n      ball.awaitingReturnBy = '';" not in awaiting_return_section
    landing_start = html.index("function checkShuttleLanding(ball) {")
    landing_section = html[landing_start:html.index("function maybeYuiReturnIncomingShuttle(ball)", landing_start)]
    assert "returnedFromShuttleId" not in landing_section
    assert "if (updateShuttleNetCrossing(ball)) return;" in landing_section
    assert "if (ball.netContactHoldUntil && performance.now() < ball.netContactHoldUntil) return;" in landing_section
    assert "var landingCourtY = getShuttleCourtY(ball, false);" in landing_section
    assert "var inTargetX = landingCourtY >= targetLeft && landingCourtY <= targetRight;" in landing_section
    assert "var landedOnTargetSide = direction > 0 ? landingCourtY >= targetLeft : landingCourtY <= targetRight;" in landing_section
    assert "var inTargetX = ball.x >= targetLeft && ball.x <= targetRight;" not in landing_section
    assert "var landedOnTargetSide = direction > 0 ? ball.x >= targetLeft : ball.x <= targetRight;" not in landing_section
    assert "if (ball.hitNet) {" in landing_section
    assert "var netCarriedOver = ball.netCarryToTargetSide === true;" in landing_section
    assert "var netScored = netCarriedOver && landedOnTargetSide && inTargetX && inTargetY;" in landing_section
    assert "var netShotType = (!netCarriedOver || !landedOnTargetSide) ? 'net' : (netScored ? 'net_touch' : 'out');" in landing_section
    assert "queueShotResolution(ball, netScored, netShotType, netScored ? 140 : 80);" in landing_section
    assert "var legallyOverNet = ball.legalNetClearance === true;" in landing_section
    assert "var scored = legallyOverNet && inTargetX && inTargetY;" in landing_section
    assert "var shotType = (!legallyOverNet || !landedOnTargetSide) ? 'net' : 'out';" in landing_section
    assert "shotType = ball.hitNet ? 'net_touch' : (nearBaseline ? 'line_in' : (nearNet ? 'net_touch' : 'zone_in'));" in landing_section
    assert "var scored = !ball.hitNet && ball.crossedNet && inTargetX && inTargetY;" not in landing_section
    net_contact_start = net_crossing_section.index("if (!didShuttleLegallyClearNet(ball, netCrossing)) {")
    net_contact_section = net_crossing_section[
        net_contact_start:
        net_crossing_section.index("return false;", net_contact_start)
    ]
    assert "applyMidcourtNetContact(ball, netCrossing);" in net_contact_section
    assert "queueShotResolution(ball, false, 'net'" not in net_contact_section
    assert "function applyMidcourtNetContact(ball, crossing) {" in html


@pytest.mark.unit
def test_badminton_duel_avatars_move_to_receive_shuttles():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "var PLAYER_MOVE_SPEED = 1040;" in html
    assert "var PLAYER_POST_SERVE_MOVE_LOCK_MS = 420;" in html
    assert "var YUI_MOVE_SPEED = 360;" in html
    assert "var YUI_RETURN_CHASE_SPEED = 720;" in html
    assert "var YUI_RETURN_CHASE_Y_SPEED = 540;" in html
    assert "var PLAYER_COURT_X_MAX = BADMINTON.netX - 58;" in html
    assert "transition: opacity .24s ease;" in html
    assert "transition: transform .3s ease, opacity .24s ease;" not in html
    assert "playerCourt: { x: PLAYER_START_X, y: PLAYER_START_Y, targetX: PLAYER_START_X, targetY: PLAYER_START_Y }," in html
    assert "playerMoveLockedUntil: 0," in html
    assert "yuiCourt: { x: YUI_START_X, y: YUI_START_Y, targetX: YUI_START_X, targetY: YUI_START_Y }," in html
    assert "function updatePlayerCourtTarget(clientX, clientY) {" in html
    assert "var normX = clamp(clientX / Math.max(1, window.innerWidth), 0, 1);" in html
    assert "function getPlayerCourtMoveBounds() {" in html
    assert "minX: serviceLines.leftDoublesLongServiceX + 14," in html
    assert "maxX: serviceLines.leftShortServiceX - 14" in html
    assert "var moveBounds = getPlayerCourtMoveBounds();" in html
    assert "game.playerCourt.targetX = clamp(normX * BASE_W, moveBounds.minX, moveBounds.maxX);" in html
    assert "var lastPlayerPointer = null;" in html
    assert "function rememberPlayerPointer(ev) {" in html
    assert "function syncPlayerCourtTargetFromPointer() {" in html
    assert "game.playerCourt.x = game.playerCourt.targetX;" not in html
    assert "var playerMoveRange = PLAYER_COURT_X_MAX - PLAYER_COURT_X_MIN;" not in html
    assert "game.playerCourt.targetY = PLAYER_START_Y;" in html
    assert "var normY = clamp(clientY / Math.max(1, window.innerHeight), 0, 1);" not in html
    assert "function updateYuiCourtTarget() {" in html
    assert "if (ball && !ball.resolved && ball.shooter === 'player' && ball.direction === 1) {" in html
    assert "var YUI_COURT_X_MIN = BADMINTON.netX + 56;" in html
    assert "var YUI_RACKET_BODY_OFFSET_X = 58;" in html
    assert "var visibleRacket = getYuiVisibleRacketContactPoint();" in html
    assert "targetX = clamp(game.yuiCourt.x + ((ball.x || 0) - visibleRacket.x), YUI_COURT_X_MIN, YUI_COURT_X_MAX);" in html
    assert "targetY = clamp(game.yuiCourt.y + ((ball.y || 0) - visibleRacket.y), YUI_COURT_Y_MIN, YUI_COURT_Y_MAX);" in html
    assert "targetX = clamp(ball.x - YUI_RACKET_BODY_OFFSET_X, YUI_COURT_X_MIN, YUI_COURT_X_MAX);" in html
    assert "targetY = clamp(ball.y + 76, YUI_COURT_Y_MIN, YUI_COURT_Y_MAX);" in html
    assert "function isYuiChasingIncomingShuttle() {" in html
    assert "return !!(ball && !ball.resolved && ball.shooter === 'player' && ball.direction === 1 && ball.crossedNet);" in html
    assert "function updateCourtMovement(dt) {" in html
    assert "syncPlayerCourtTargetFromPointer();" in html
    assert "var playerMoveSpeed = PLAYER_MOVE_SPEED * getPlayerMovementSpeedMultiplier();" in html
    assert "var yuiChaseSpeedX = isYuiChasingIncomingShuttle() ? YUI_RETURN_CHASE_SPEED : YUI_MOVE_SPEED;" in html
    assert "var yuiChaseSpeedY = isYuiChasingIncomingShuttle() ? YUI_RETURN_CHASE_Y_SPEED : YUI_MOVE_SPEED * 0.5;" in html
    assert "game.playerCourt.x = moveToward(game.playerCourt.x, game.playerCourt.targetX, playerMoveSpeed, dt);" in html
    assert "game.playerCourt.targetY = PLAYER_START_Y;" in html
    assert "game.playerCourt.y = PLAYER_START_Y;" in html
    assert "game.playerCourt.y = moveToward(game.playerCourt.y, game.playerCourt.targetY, PLAYER_MOVE_SPEED * 0.55, dt);" not in html
    assert "game.yuiCourt.x = moveToward(game.yuiCourt.x, game.yuiCourt.targetX, yuiChaseSpeedX, dt);" in html
    assert "game.yuiCourt.y = moveToward(game.yuiCourt.y, game.yuiCourt.targetY, yuiChaseSpeedY, dt);" in html
    assert "function canPlayerMoveCourt() {" in html
    assert "if (isBadmintonLoadingActive()) return false;" in html
    assert "function isPlayerPostServeMoveLocked() {" in html
    assert "return isDuelMode() && (isPlayerServeSetup() || canPlayerControlShot() || (playerShotInFlight && !isPlayerPostServeMoveLocked())" in html
    assert "var incomingYuiBall = game.ball && game.ball.shooter === 'neko' && game.ball.direction === -1 && !game.ball.resolved;" in html
    assert "var canMoveCourt = canPlayerMoveCourt();" in html
    assert "if (canMoveCourt) updatePlayerCourtTarget(ev.clientX, ev.clientY);" in html
    assert "if (isDuelMode()) updateCourtMovement(dt);" in html
    assert "playerCourt: Object.assign({}, game.playerCourt)," in html
    assert "playerMoveLockRemainingMs: Math.max(0, Math.round((game.playerMoveLockedUntil || 0) - performance.now()))," in html
    assert "var playerRacketContact = getRacketContactPoint('player');" in html
    assert "playerRacketContact: playerRacketContact ? {" in html
    assert "source: playerRacketContact.source || 'fallback'" in html
    assert "yuiCourt: Object.assign({}, game.yuiCourt)," in html
    assert "y: getYuiEyeY() - 10" in html


@pytest.mark.unit
def test_badminton_player_can_receive_and_return_yui_shuttle():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "function isPlayerReceivingReturn() {" in html
    assert "return !!(game.ball && game.ball.awaitingReturnBy === 'player');" in html
    assert "return getPlayerVrmRacketContactPoint() || getShotOrigin();" in html
    assert "return isPlayerReceivingReturn() && !game.ball.resolved && !game.ball.hitNet && isIncomingPlayerShuttleInReach(game.ball);" in html
    assert "function maybePlayerReceiveIncomingShuttle(ball) {" in html
    assert "if (ball.shooter !== 'neko' || ball.direction !== -1 || ball.awaitingReturnBy) return false;" in html
    assert "var shuttleCourtY = getShuttleCourtY(ball, false);" in html
    assert "var shuttleZ = getShuttleCourtZ(ball, false);" in html
    assert "var hasReachedPlayerSide = ball.crossedNet || shuttleCourtY <= getMidcourtNetY() + 24;" in html
    assert "if (!hasReachedPlayerSide) return false;" in html
    assert "var inPlayerReach = shuttleCourtY <= getMidcourtNetY() + 24" in html
    assert "&& shuttleZ <= screenYToCourtZ(-520)" in html
    assert "&& shuttleZ >= screenYToCourtZ(FLOOR_Y - 8);" in html
    assert "game.duel.activeShooter = 'player';" in html
    assert "game.state = 'ready';" in html
    assert "ball.awaitingReturnBy = 'player';" in html
    assert "ball.returnDeadlineAt = performance.now() + 2400;" in html
    assert "showAssistHint(_i18n('toast.playerReturn', '接住 Yui 的回球'));" in html
    assert 'id="assist-hint-text"' in html
    assert 'id="assist-charge-meter"' not in html
    assert 'id="assist-charge-fill"' not in html
    assert "function getAssistChargeMeterState() {" not in html
    assert "function updateAssistChargeMeter() {" not in html
    assert "function isIncomingPlayerReturnCandidate() {" in html
    assert "function canPrechargePlayerReturn() {" not in html
    assert "function canPlayerPrepareIncomingReturn() {" in html
    assert "if (isPlayerReceivingReturn()) return game.state === 'ready' && isIncomingPlayerReturnCandidate();" in html
    assert "return game.state === 'in_flight' && isIncomingPlayerReturnCandidate();" in html
    assert "function canPlayerChargeShot() {" in html
    assert "return canPlayerControlShot() || canPlayerPrepareIncomingReturn();" in html
    assert "var PLAYER_CHARGE_SPEED = 145;" in html
    assert "function stepPlayerCharge(dt) {" in html
    assert "game.power += game.chargeDir * dt * PLAYER_CHARGE_SPEED;" in html
    assert "if (game.charging && canPrechargePlayerReturn()) stepPlayerCharge(dt);" not in html
    assert "var isChargingPlayerShot = game.charging && canPlayerChargeShot();" in html
    assert "if (isChargingPlayerShot) stepPlayerCharge(dt);" in html
    assert "if (game.ball && game.ball.shooter !== shotShooter && !incomingBall) game.ball = null;" in html
    assert "var preserveIncomingCharge = game.charging && canPlayerPrepareIncomingReturn();" in html
    assert "game.charging = preserveIncomingCharge;" in html
    assert "if (!preserveIncomingCharge) {" in html
    assert "var receivingReturn = isPlayerReceivingReturn();" in html
    assert "var shotPower = receivingReturn ? clamp(game.power || 56, 24, 100) : game.power;" in html
    assert "var returnPower = clamp(game.power || 56, 24, 100);" in html
    assert "queueRacketSwing(playerReturnAngle, returnPower, 'player', { incomingBall: game.ball });" in html
    assert "queueRacketSwing(game.aimAngle, shotPower, 'player', { forceScore: shouldGuaranteeFirstTutorialShot, incomingBall: receivingReturn ? game.ball : null });" in html

    update_start = html.index("function update(dt, now) {")
    update_section = html[update_start:html.index("function drawDistanceMarkers()", update_start)]
    assert "if (awaitingPlayerReturn && stepAwaitingPlayerReturn(dt)) return;" in update_section
    assert "function stepAwaitingPlayerReturn(dt) {" in html
    awaiting_start = html.index("function stepAwaitingPlayerReturn(dt) {")
    awaiting_section = html[awaiting_start:html.index("function queueShotResolution", awaiting_start)]
    assert awaiting_section.index("stepBallPhysics(ball, subDt);") < awaiting_section.index("checkShuttleLanding(ball);")
    assert awaiting_section.index("checkShuttleLanding(ball);") < awaiting_section.index("updateNetPhysics(subDt);")
    assert "if (!ball.hitNet && performance.now() > ball.returnDeadlineAt) {" in html
    assert "if (!ball.groundedReturnAt) ball.groundedReturnAt = performance.now();" in html
    assert "if (performance.now() - ball.groundedReturnAt < 1100) continue;" in html
    assert "finishShot(false, 'out', ball);" in html
    assert "if (maybePlayerReceiveIncomingShuttle(b)) break;" in update_section
    assert "b.y < -520" not in update_section
    assert "b.x < -50 || b.x > 950 || b.y > 550" in update_section
    assert update_section.index("checkShuttleLanding(b);") < update_section.index("if (maybePlayerReceiveIncomingShuttle(b)) break;")

    draw_start = html.index("function drawAiming(now) {")
    draw_section = html[draw_start:html.index("function drawBall()", draw_start)]
    assert "aimingCtx.clearRect(0, 0, BASE_W, BASE_H);" in draw_section
    assert "function drawReturnChargeTrace(" not in draw_section
    assert "function drawPlayerChargeMeter(" in draw_section
    assert "drawPlayerChargeMeter(game.charging && canPlayerChargeShot() ? clamp(game.power, 0, 100) : 0, t);" in draw_section
    assert "if (!isIncomingPlayerReturnCandidate()) return;" in draw_section
    assert "if (!canReturnNow) return;" in draw_section
    assert "var cueAlpha = 1;" in draw_section
    assert "'rgba(114,216,255,.055)'" not in draw_section
    assert "'rgba(114,216,255,.25)'" not in draw_section
    assert "var returnPercent = clamp(returnDeadlineMs / 2400 * 100, 0, 100);" not in draw_section
    assert "drawReturnChargeTrace" not in draw_section

    charge_start = html.index("function drawPlayerChargeMeter(percent, now) {")
    charge_section = html[charge_start:html.index("function ensureYuiInkOverlayCache()", charge_start)]
    assert "var meterX = (getPlayerX() - meterW / 2) * screenScaleX;" in charge_section
    assert "var meterY = (getPlayerY() - 146) * screenScaleY;" in charge_section
    assert "aimingCtx.shadowBlur" not in charge_section
    assert "aimingCtx.quadraticCurveTo(controlX, controlY, endX, endY);" in draw_section
    return_cue_start = html.index("function drawPlayerReturnHitCue(now) {")
    return_cue_section = html[return_cue_start:html.index("function drawBall()", return_cue_start)]
    assert "aimingCtx.createLinearGradient" not in return_cue_section
    assert "aimingCtx.createRadialGradient" not in return_cue_section
    assert "aimingCtx.shadowBlur" not in return_cue_section
    assert "var chargePercent = game.charging ? clamp(game.power, 0, 100) : clamp(returnDeadlineMs / 2400 * 100, 0, 100);" not in draw_section

    pointer_down = html[
        html.index("function handleBadmintonPointerDown(ev) {"):
        html.index("addBadmintonEventListener(canvas, 'mousemove'", html.index("function handleBadmintonPointerDown(ev) {"))
    ]
    assert "if (returnIncomingPlayerShuttle()) return;" not in pointer_down
    assert "if (!canPlayerChargeShot()) return;" in pointer_down
    assert pointer_down.index("if (!canPlayerChargeShot()) return;") < pointer_down.index("game.charging = true;")

    mousedown = html[
        html.index("function handleBadmintonPointerDown(ev) {"):
        html.index("addBadmintonEventListener(window, 'mouseup'")
    ]
    assert "game.power = 56;" not in mousedown
    assert "if (!canPlayerChargeShot()) return;" in mousedown
    assert "canPrechargePlayerReturn" not in mousedown
    assert mousedown.index("if (!canPlayerChargeShot()) return;") < mousedown.index("game.charging = true;")
    mouseup = html[html.index("addBadmintonEventListener(window, 'mouseup'"):html.index("addBadmintonEventListener(window, 'keydown'")]
    assert "if (game.charging && returnIncomingPlayerShuttle()) return;" in mouseup


@pytest.mark.unit
def test_badminton_first_tutorial_line_in_is_practice_only():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "function isPracticeMode() {\n    return false;\n  }" in html
    assert "var shouldGuaranteeFirstTutorialShot = firstTutorialShotGuaranteed && isPracticeMode() && game.tutorialStep === 3;" in html


@pytest.mark.unit
def test_badminton_space_jump_enables_air_smash():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "var PLAYER_JUMP_SPEED = 430;" in html
    assert "var PLAYER_JUMP_GRAVITY = 1350;" in html
    assert "var PLAYER_SMASH_MIN_JUMP_OFFSET = 28;" in html
    assert "var PLAYER_SMASH_CONTACT_LIFT = 56;" in html
    assert "var PLAYER_SMASH_SWEET_REACH_X = 58;" in html
    assert "var PLAYER_SMASH_SWEET_REACH_Y = 68;" in html
    assert "var PLAYER_SMASH_FORWARD_BIAS_X = 18;" in html
    assert "var PLAYER_SMASH_UPWARD_BIAS_Y = -26;" in html
    assert "playerJump: { offset: 0, velocity: 0, active: false, cooldownUntil: 0, lastSmashAt: 0 }," in html
    assert "function startPlayerJump() {" in html
    assert "function updatePlayerJump(dt) {" in html
    assert "function getPlayerSmashQuality(incomingBall) {" in html
    assert "function getPlayerSmashSweetSpot() {" in html
    assert "function isPlayerSmashReady(incomingBall) {" in html
    smash_ready_section = html[
        html.index("function isPlayerSmashReady(incomingBall) {"):
        html.index("function getYuiRacketHitRangeState(incomingBall) {")
    ]
    assert "if (isPlayerServeSetup()) return false;" not in smash_ready_section
    assert "return getPlayerSmashQuality(incomingBall) >= 0.22;" in smash_ready_section
    assert "function getYuiSmashQuality(incomingBall, shot) {" in html
    assert "function getYuiSmashHeightQuality(incomingBall) {" in html
    assert "function shouldYuiSmashReturn(incomingBall, shot, quality) {" in html
    assert "if (!incomingBall || !shot || quality < 0.52) return false;" in html
    assert "var heightQuality = getYuiSmashHeightQuality(incomingBall);" in html
    assert "if (getYuiSmashHeightQuality(incomingBall) <= 0) return false;" in html
    assert "var yuiSmashClearance = contact.y - (incomingBall.y || contact.y);" not in html
    assert "if (yuiSmashClearance < 58) return false;" not in html
    assert "return clamp(game.playerCourt.y - getPlayerJumpOffset(), PLAYER_COURT_Y_MIN - PLAYER_JUMP_MAX_OFFSET, PLAYER_COURT_Y_MAX);" in html
    assert "game.playerJump.offset += game.playerJump.velocity * dt;" in html
    assert "game.playerJump.velocity -= PLAYER_JUMP_GRAVITY * dt;" in html
    assert "updatePlayerJump(dt);" in html

    swing_section = html[
        html.index("function buildSwingImpulse(angle, power, shooter, incomingBall) {"):
        html.index("function launchShot(angle, power, shooter, swingImpulse) {")
    ]
    assert "var swingOptions = arguments.length > 4 && arguments[4] ? arguments[4] : {};" in swing_section
    assert "var isSmash = !!swingOptions.smash;" in swing_section
    assert "var isSave = !!swingOptions.save && shooter === 'neko' && !isSmash;" in swing_section
    assert "var smashQuality = isSmash ? (shooter === 'neko' ? clamp(Number(swingOptions.smashQuality) || 0.72, 0, 1) : getPlayerSmashQuality(incomingBall)) : 0;" in swing_section
    assert "if (isSmash && smashQuality <= 0) isSmash = false;" in swing_section
    assert "var smashSpeedBonus = isSmash ? 130 + smashQuality * 150 : 0;" in swing_section
    assert "isSmash: isSmash," in swing_section
    assert "smashQuality: smashQuality," in swing_section

    queue_section = html[
        html.index("function queueRacketSwing(angle, power, shooter, options) {"):
        html.index("function returnIncomingPlayerShuttle() {")
    ]
    assert "var smash = !!(options && options.smash);" in queue_section
    assert "if (shotShooter === 'player') smash = smash || isPlayerSmashReady(incomingBall);" in queue_section
    assert "var save = !!(options && options.save) && shotShooter === 'neko' && !smash;" in queue_section
    assert "var smashQuality = options && typeof options.smashQuality === 'number' ? options.smashQuality : 0;" in queue_section
    assert "impulse: buildSwingImpulse(angle, power, shotShooter, incomingBall, { smash: smash, smashQuality: smashQuality, save: save })," in queue_section
    assert "save: save," in queue_section
    assert "if (shotShooter === 'player') setPlayerAction('shooting', 460);" in queue_section
    assert "else setYuiAction(save ? 'saving' : (smash ? 'smashing' : 'shooting'), save ? 560 : (smash ? 520 : 420));" in queue_section
    assert "if (smash && shotShooter === 'player') {" in queue_section
    assert "showAssistHint(_i18n('toast.smash', '跳杀！'));" in queue_section
    assert ".neko-avatar-container[data-court-avatar=\"opponent\"].smashing { animation: yui-smashing .52s cubic-bezier(.2,.82,.18,1); }" in html
    assert ".neko-avatar-container[data-court-avatar=\"opponent\"].saving { animation: yui-saving .56s cubic-bezier(.18,.86,.2,1); }" in html
    assert "@keyframes yui-smashing {" in html
    assert "@keyframes yui-saving {" in html
    assert "container.classList.remove('charging', 'shooting', 'smashing', 'saving', 'react-celebrate', 'react-disappointed');" in html

    launch_section = html[
        html.index("function launchShot(angle, power, shooter, swingImpulse) {"):
        html.index("function queueRacketSwing(angle, power, shooter, options) {")
    ]
    assert "var launchAngle = impulse.isSmash ? clamp(angle - (20 + impulse.smashQuality * 12), 12, 48) : angle;" in launch_section
    assert "y: origin.y - PLAYER_SMASH_CONTACT_LIFT * (0.7 + impulse.smashQuality * 0.6)" in launch_section
    assert "var smashDownVelocity = impulse.isSmash ? (shotShooter === 'neko' ? 180 + impulse.smashQuality * 145 + impulse.force * 54 : 250 + impulse.smashQuality * 210 + impulse.force * 90) : 0;" in launch_section
    assert "shuttle.vy = impulse.isSmash ? smashDownVelocity : baseVy;" in launch_section
    assert "shuttle.isSmash = !!impulse.isSmash;" in launch_section
    assert "shuttle.smashQuality = impulse.smashQuality || 0;" in launch_section

    keydown = html[
        html.index("addBadmintonEventListener(window, 'keydown'"):
        html.index("if (bgmVolumeInput)", html.index("addBadmintonEventListener(window, 'keydown'"))
    ]
    assert "key === ' ' || ev.code === 'Space'" in keydown
    assert "if (!ev.repeat) startPlayerJump();" in keydown
    assert "jump: startPlayerJump," in html
    assert "playerJump: {" in html
    assert "isSmash: !!(game.pendingSwing.impulse && game.pendingSwing.impulse.isSmash)," in html
    assert "isSmash: !!game.ball.isSmash," in html


@pytest.mark.unit
def test_badminton_unload_can_retry_pending_route_end_with_beacon():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "var routeEndFetchPending = false;" in html
    assert "if (!detached && endedRoute && !force && !(useBeacon && routeEndFetchPending && !routeEndBeaconDelivered)) return routeEndPromise || Promise.resolve({ ok: true, skipped: true });" in html
    assert "routeEndFetchPending = true;" in html
    assert "routeEndBeaconDelivered = true;" in html


@pytest.mark.unit
def test_badminton_leaderboard_mode_and_post_errors_are_explicit():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")
    mode_section = html[html.index("function formatLeaderboardMode(value) {"):html.index("function pickLocalizedLine", html.index("function formatLeaderboardMode(value) {"))]
    post_section = html[html.index("function post(path, body, timeoutMs) {"):html.index("function _isBadmintonGameMemoryEnabled", html.index("function post(path, body, timeoutMs) {"))]

    assert "if (mode === 'duel') return _i18n('leaderboard.mode.duel', " in mode_section
    assert "if (mode === 'shooter') return _i18n('leaderboard.mode.shooter', " in mode_section
    assert "if (mode === 'practice') return _i18n('leaderboard.mode.practice', " in mode_section
    assert "return mode || _i18n('leaderboard.mode.duel', " in mode_section
    assert "if (!r.ok) {" in post_section
    assert "return r.text().then(function (text) {" in post_section
    assert "reason: 'http_error'" in post_section
    assert "return r.json().catch(function () { return { ok: r.ok }; });" in post_section


@pytest.mark.unit
def test_badminton_game_storage_is_scoped_per_lanlan_with_legacy_fallback():
    html = BADMINTON_TEMPLATE.read_text(encoding="utf-8")

    assert "function encodeBadmintonStorageScopeName(value) {" in html
    assert "encodeURIComponent(raw.toLowerCase())" in html
    assert ".replace(/%/g, '~')" in html
    assert "function getBadmintonStorageScope() {" in html
    assert "function badmintonStorageKey(key) {" in html
    assert "return 'bd:' + scope + ':' + key;" in html
    assert "replace(/[^a-z0-9_-]+/g, '_')" not in html
    assert "function readBadmintonStorage(key) {" in html
    assert "var scopedKey = badmintonStorageKey(key);" in html
    assert "if (scopedValue != null) return scopedValue;" in html
    assert "if (scopedKey !== key) return localStorage.getItem(key);" in html
    assert "function writeBadmintonStorage(key, value) {" in html
    assert "localStorage.setItem(badmintonStorageKey(key), value);" in html
    assert "function removeBadmintonStorage(key) {" in html
    assert "var scopedKey = badmintonStorageKey(key);" in html
    assert "localStorage.removeItem(scopedKey);" in html
    assert "if (scopedKey !== key) localStorage.removeItem(key);" in html

    assert "var raw = readBadmintonStorage(key);" in html
    assert "writeBadmintonStorage(key, JSON.stringify(value));" in html
    assert "readBadmintonStorage('bd_record_distance')" in html
    assert "writeBadmintonStorage('bd_record_distance', String(Math.round(game.recordDistance)))" in html
    assert "localStorage.getItem('bd_record_distance')" not in html
    assert "localStorage.setItem('bd_record_distance'" not in html
