import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.node_harness import run_node_script


ROOT = Path(__file__).resolve().parents[2]
MUSIC_UI_PATH = ROOT / "static" / "jukebox" / "music_ui.js"
MUSIC_UI_CSS_PATH = ROOT / "static" / "css" / "music_ui.css"
PROACTIVE_UI_PATH = ROOT / "static" / "app" / "app-proactive.js"
APP_CHAT_PATH = ROOT / "static" / "app" / "app-chat.js"
APP_WEBSOCKET_PATH = ROOT / "static" / "app" / "app-websocket.js"
WEBSOCKET_ROUTER_PATH = ROOT / "main_routers" / "websocket_router.py"
LOCALES_DIR = ROOT / "static" / "locales"
MUSIC_ROUTER_PATH = ROOT / "main_routers" / "music_router.py"
MUSIC_CRAWLERS_PATH = ROOT / "utils" / "music_crawlers.py"
DEFAULT_MUSIC_COVER_PATH = ROOT / "static" / "assets" / "music" / "music-cover-placeholder.png"
PAGES_ROUTER_PATH = ROOT / "main_routers" / "pages_router.py"
MUSIC_PLAYER_TEMPLATES = (ROOT / "templates" / "index.html", ROOT / "templates" / "chat.html")


def test_music_dispatch_waits_for_media_and_reports_real_failure():
    source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    dispatch_source = APP_CHAT_PATH.read_text(encoding="utf-8")

    assert "waitForMusicMediaReady" in source
    assert "const result = await executePlay(" in source
    assert "window.sendMusicMessageDetailed" in source
    assert "window.sendMusicMessage = async function" in source
    assert "return result.ok === true" in source
    assert "canTryNextCandidate" in source
    assert "canTryNextMusicCandidate(mediaResult.reason)" in source
    retryable_failures = source.split("const canTryNextMusicCandidate", 1)[1].split("].includes(reason);", 1)[0]
    assert "'media_error'" in retryable_failures
    assert "'track_too_long'" in retryable_failures
    assert "'load_timeout'" in retryable_failures
    assert "musicPlayResult(false, 'unsupported_stream', true)" in source
    assert "musicPlayResult(false, 'unsafe_url', true)" in source
    assert "MAX_RECOMMENDED_TRACK_DURATION_SECONDS = 10 * 60" in source
    assert "duration >= MAX_RECOMMENDED_TRACK_DURATION_SECONDS" in source
    assert "playbackOptions.source === 'proactive'" in source
    assert "window.dispatchMusicPlayDetailed" in dispatch_source
    assert "window.dispatchMusicPlay = async function" in dispatch_source
    assert "sendMusicMessageDetailed(trackInfo, true, options)" in dispatch_source
    assert "return new Promise(function (resolve)" in dispatch_source
    assert "musicDispatchResult(false, 'ui_not_ready', false)" in dispatch_source
    assert "result.ok === true && options.source === 'proactive'" in dispatch_source
    assert "return 'queued'" not in dispatch_source
    assert "isUnsupportedMusicStream" in source
    assert "endsWith('.m3u8')" in source
    assert "const backendProxyDomains = new Set(MUSIC_CONFIG.allowlist)" in source
    assert "const toBackendMusicProxyUrl = (url) =>" in source
    safe_url_source = source.split("const isSafeUrl = (url) => {", 1)[1].split(
        "const normalizeMusicCoverUrl", 1
    )[0]
    assert "if (parsed.protocol === 'http:') return pluginHttpUrls.has(parsed.href);" in safe_url_source
    assert "if (parsed.protocol !== 'https:') return false;" in safe_url_source
    assert "MUSIC_CONFIG.allowlist.some" in safe_url_source
    for blocked_protocol in ("ftp:", "file:", "data:", "javascript:"):
        assert blocked_protocol not in safe_url_source

    proxy_source = source.split("const toBackendMusicProxyUrl = (url) =>", 1)[1].split(
        "const isMusicOccupied", 1
    )[0]
    assert "if (parsed.protocol !== 'https:') return url;" in proxy_source
    assert "['http:', 'https:'].includes(parsed.protocol)" not in proxy_source
    assert "trackInfo.url = toBackendMusicProxyUrl(originalUrl)" in source
    assert "trackInfo.url.includes('music.163.com')" not in source


def test_plugin_http_allowlist_matches_only_normalized_complete_urls():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the music URL allowlist browser contract test")

    source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    normalize_url = source.split("const normalizeMusicUrlEscapes = (url) => {", 1)[1].split(
        "/**", 1
    )[0]
    extract_hostname = source.split("const extractHostname = (input) => {", 1)[1].split(
        "const isSafeUrl = (url) => {", 1
    )[0]
    safe_url = source.split("const isSafeUrl = (url) => {", 1)[1].split(
        "const normalizeMusicCoverUrl", 1
    )[0]
    plugin_api = source.split("const MusicPluginAPI = {", 1)[1].split(
        "// --- 暴露接口 ---", 1
    )[0]
    script = textwrap.dedent(
        f"""
        const MUSIC_CONFIG = {{ allowlist: ['localhost', '127.0.0.1', '::1', 'example.com'] }};
        const pluginHttpUrls = new Set();
        const window = {{
          dispatchEvent() {{}},
        }};
        class CustomEvent {{ constructor(type) {{ this.type = type; }} }}
        const normalizeMusicUrlEscapes = (url) => {{{normalize_url}
        const extractHostname = (input) => {{{extract_hostname}
        const isSafeUrl = (url) => {{{safe_url}
        const MusicPluginAPI = {{{plugin_api}

        const exactUrls = [
          'http://localhost:48916/plugin/music_pusher/ui/uploads/song.mp3',
          'http://127.0.0.1:48916/plugin/music_pusher/ui/uploads/song.mp3',
          'http://[::1]:48916/plugin/music_pusher/ui/uploads/song.mp3',
        ];
        MusicPluginAPI.addAllowlist(['localhost', '127.0.0.1', '::1']);
        MusicPluginAPI.addAllowlist(exactUrls[0]);
        MusicPluginAPI.addAllowlist(exactUrls.slice(1));

        for (const url of exactUrls) {{
          if (!isSafeUrl(url)) throw new Error(`registered HTTP URL rejected: ${{url}}`);
        }}
        if (!isSafeUrl('HTTP://LOCALHOST:48916/plugin/music_pusher/ui/uploads/song.mp3')) {{
          throw new Error('equivalent normalized localhost URL rejected');
        }}
        if (isSafeUrl('http://localhost:48917/plugin/music_pusher/ui/uploads/song.mp3')) {{
          throw new Error('different HTTP port allowed');
        }}
        if (isSafeUrl('http://localhost:48916/plugin/music_pusher/ui/uploads/other.mp3')) {{
          throw new Error('different HTTP path allowed');
        }}
        if (isSafeUrl('http://localhost:48916/plugin/music_pusher/ui/uploads/song.mp3?other=1')) {{
          throw new Error('different HTTP query allowed');
        }}
        if (isSafeUrl('http://127.0.0.1/unregistered.mp3')) {{
          throw new Error('host-only loopback entry allowed HTTP');
        }}
        if (!isSafeUrl('https://media.example.com/song.mp3')) {{
          throw new Error('HTTPS hostname allowlist regressed');
        }}
        for (const url of ['ftp://example.com/song.mp3', 'file:///song.mp3', 'data:audio/mp3;base64,AA==']) {{
          if (isSafeUrl(url)) throw new Error(`non-HTTP(S) URL allowed: ${{url}}`);
        }}
        if (!isSafeUrl('/api/music/proxy?url=x')) throw new Error('internal API URL rejected');

        MusicPluginAPI.addAllowlist([], [
          'http://localhost:80/default.mp3',
          'http://[::1]:80/default.mp3',
        ]);
        if (!isSafeUrl('http://localhost/default.mp3')) throw new Error('localhost default port was not normalized');
        if (!isSafeUrl('http://[::1]/default.mp3')) throw new Error('IPv6 default port was not normalized');

        const escapedUrl = 'http://localhost:48916/song.mp3?token=one&amp;amp;part=two';
        MusicPluginAPI.addAllowlist(escapedUrl);
        if (!isSafeUrl('http://localhost:48916/song.mp3?token=one&part=two')) {{
          throw new Error('HTML-escaped HTTP URL was not normalized like playback');
        }}
        const encodedUrls = [
          'http://localhost:48916/encoded.mp3?token=one&amp%3Bpart=two',
          'http://localhost:48916/percent.mp3?token=one%26amp%3Bpart=two',
        ];
        MusicPluginAPI.addAllowlist(encodedUrls);
        for (const encoded of encodedUrls) {{
          const playbackUrl = normalizeMusicUrlEscapes(encoded);
          if (!isSafeUrl(playbackUrl)) throw new Error(`escaped HTTP URL rejected: ${{encoded}}`);
        }}
        if (isSafeUrl('http://localhost:48916/song.mp3?token=one&part=other')) {{
          throw new Error('normalization widened exact query matching');
        }}
        """
    )
    result: subprocess.CompletedProcess[str] = run_node_script(
        node,
        script,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_exact_http_allowlist_is_carried_without_enabling_http_proxy():
    source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    websocket_source = APP_WEBSOCKET_PATH.read_text(encoding="utf-8")
    pusher_source = (ROOT / "plugin" / "plugins" / "music_pusher" / "__init__.py").read_text(
        encoding="utf-8"
    )
    schema_source = (ROOT / "plugin" / "sdk" / "shared" / "core" / "push_message_schema.py").read_text(
        encoding="utf-8"
    )
    bridge_source = (ROOT / "plugin" / "server" / "messaging" / "proactive_bridge.py").read_text(
        encoding="utf-8"
    )
    runtime_source = (ROOT / "app" / "main_server" / "character_runtime.py").read_text(
        encoding="utf-8"
    )

    assert 'metadata={"domains": domains, "http_urls": http_urls, "event_id": event_id}' in pusher_source
    assert 'ui_part["http_urls"] = list(md_local["http_urls"])' in schema_source
    assert '"http_urls": list(http_urls)' in bridge_source
    assert '"http_urls": event.get("http_urls")' in runtime_source
    assert "response.http_urls || []" in websocket_source
    assert "getHttpUrls: () => [...pluginHttpUrls]" in source
    assert "trackInfo.url = normalizeMusicUrlEscapes(trackInfo.url);" in source
    assert "new URL(normalizeMusicUrlEscapes(value))" in source

    proxy_source = source.split("const toBackendMusicProxyUrl = (url) =>", 1)[1].split(
        "const isMusicOccupied", 1
    )[0]
    assert "if (parsed.protocol !== 'https:') return url;" in proxy_source
    assert "pluginHttpUrls" not in proxy_source


def test_proactive_music_reuses_one_card_scope_and_only_shortens_intermediate_attempts():
    source = PROACTIVE_UI_PATH.read_text(encoding="utf-8")
    player_source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    candidate_loop = source.split("var proactiveMusicFallbackDeadlineAt", 1)[1].split(
        "// 【重构】统一处理链接", 1
    )[0]

    assert "const MUSIC_CANDIDATE_FALLBACK_BUDGET_MS = 10000" in source
    assert "const MUSIC_CANDIDATE_ATTEMPT_TIMEOUT_MS = 3000" in source
    assert "const MUSIC_MEDIA_LOAD_TIMEOUT_MS = 10000" in player_source
    assert "var proactiveMusicCardScopeId = 'proactive:'" in source
    assert "for (var musicIndex = 0; musicIndex < musicLinks.length; musicIndex++)" in candidate_loop
    assert "var hasNextMusicCandidate = musicIndex < musicLinks.length - 1" in candidate_loop
    assert "dispatchResult = await window.dispatchMusicPlayDetailed(track, {" in candidate_loop
    assert "cardScopeId: proactiveMusicCardScopeId" in candidate_loop
    assert "hasNextCandidate: hasNextMusicCandidate" in candidate_loop
    assert "fallbackDeadlineAt: hasNextMusicCandidate" in candidate_loop
    assert "? proactiveMusicFallbackDeadlineAt" in candidate_loop
    assert "candidateTimeoutMs: hasNextMusicCandidate" in candidate_loop
    assert "? MUSIC_CANDIDATE_ATTEMPT_TIMEOUT_MS" in candidate_loop
    assert "if (dispatchResult.ok === true)" in candidate_loop
    assert "if (dispatchResult.canTryNextCandidate !== true)" in candidate_loop
    assert "音乐派发因非候选错误停止" in candidate_loop
    assert "音乐候选不可用，尝试下一条" in candidate_loop
    assert "finally {" in candidate_loop
    assert "!dispatchedTrackUrl" in candidate_loop
    assert "window.finalizeMusicCandidateCardFailure(lastAttemptedMusicTrack, {" in candidate_loop
    assert "cardScopeId: proactiveMusicCardScopeId" in candidate_loop
    assert "musicLinks = normalizedLinks.filter" in source
    assert "name: musicLink.title || '未知曲目'" not in source
    assert "artist: musicLink.artist || '未知艺术家'" not in source


def test_music_candidate_fallback_preserves_and_finalizes_only_the_matching_card_scope():
    source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    scoped_card_source = source.split("const getMusicCardScopeKey", 1)[1].split(
        "const getCandidateMediaReadyTimeoutMs", 1
    )[0]
    destroy_source = source.split("const destroyMusicPlayer = (", 1)[1].split(
        "const getMusicPlayerInstance", 1
    )[0]
    update_card_source = source.split("const updateMusicCard = (state, track) => {", 1)[1].split(
        "// --- 状态追踪", 1
    )[0]

    assert "String(options.source || 'music') + ':' + String(options.cardScopeId)" in scoped_card_source
    assert "requestedScopeKey !== musicCardScopeKey" in scoped_card_source
    assert "updateMusicCard('error', track)" in scoped_card_source
    assert "musicCardMessageId = null" in scoped_card_source
    assert "musicCardScopeKey = ''" in scoped_card_source
    assert "preserveMusicCard = false" in destroy_source
    assert "if (!preserveMusicCard || fullTeardown)" in destroy_source
    assert "!['error', 'ended'].includes(musicCardState)" in destroy_source
    assert destroy_source.index("!['error', 'ended'].includes(musicCardState)") < destroy_source.index(
        "updateMusicCard('ended', currentPlayingTrack)"
    )
    assert update_card_source.index("musicCardState = state;") < update_card_source.index(
        "const host = window.reactChatWindowHost;"
    )
    assert "shouldDeferCandidateFailureUi(playbackOptions, result.reason)" in source
    assert "window.finalizeMusicCandidateCardFailure = finalizeScopedMusicCardFailure" in source


def test_proactive_request_rechecks_music_state_before_search():
    source = PROACTIVE_UI_PATH.read_text(encoding="utf-8")
    player_source = MUSIC_UI_PATH.read_text(encoding="utf-8")

    assert "const isMusicOccupied = () =>" in player_source
    assert "localAudio && !localAudio.ended && !localPlayer._loadError" in player_source
    assert "mirrorBarLastState && mirrorBarLastState.track" in player_source
    assert "window.isMusicOccupied = isMusicOccupied" in player_source
    assert "var musicPlayingBeforeRequest" in source
    assert "var musicOccupiedBeforeRequest = isMusicOccupiedNow()" in source
    assert "var musicRateLimitedBeforeRequest" in source
    assert "requestBody.is_music_occupied = !!musicOccupiedBeforeRequest" in source
    assert (
        "requestBody.enabled_modes = requestBody.enabled_modes.filter(function (mode) "
        "{ return mode !== 'music'; });"
    ) in source
    assert source.index("var musicOccupiedBeforeRequest") < source.index(
        "var proactiveBody = JSON.stringify(requestBody)"
    )


def test_new_track_cancels_pending_media_readiness_wait():
    source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    send_source = source.split(
        "window.sendMusicMessageDetailed = async function", 1
    )[1].split("window.sendMusicMessage = async function", 1)[0]

    assert "let pendingMusicMediaReadyCancel = null;" in source
    assert "cancelWait = () => finish(false, 'superseded');" in source
    assert "if (pendingMusicMediaReadyCancel) pendingMusicMediaReadyCancel();" in send_source
    assert send_source.index("++latestMusicRequestToken") < send_source.index(
        "pendingMusicMediaReadyCancel()"
    )
    allowlist_wait = send_source.index("await new Promise((resolve) => {")
    stale_guard = send_source.index("if (currentToken !== latestMusicRequestToken) {")
    assert send_source.index("const currentToken = ++latestMusicRequestToken;") < allowlist_wait
    assert allowlist_wait < stale_guard < send_source.index("isUnsupportedMusicStream")


def test_websocket_reconnect_invalidates_pending_music_dispatches():
    chat_source = APP_CHAT_PATH.read_text(encoding="utf-8")
    player_source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    websocket_source = APP_WEBSOCKET_PATH.read_text(encoding="utf-8")
    connect_source = websocket_source.split(
        "function connectWebSocket()", 1
    )[1].split("// ---- onopen ----", 1)[0]

    same_socket_guard = connect_source.index(
        "S.socket && S.socket.readyState === WebSocket.OPEN"
    )
    reset_media = connect_source.index("window.cancelPendingMusicMediaReady();")
    reset_queued = connect_source.index("window.cancelQueuedMusicDispatch();")
    new_socket = connect_source.index("S.socket = new WebSocket(wsUrl);")
    assert same_socket_guard < reset_media < reset_queued < new_socket

    chat_reset = chat_source.split(
        "window.cancelQueuedMusicDispatch = function ()", 1
    )[1].split("window.dispatchMusicPlay = async function", 1)[0]
    assert "_musicDispatchId++;" in chat_reset
    assert "_queuedMusicDispatchCancel();" in chat_reset
    assert "requestId" not in chat_reset

    player_reset = player_source.split(
        "window.cancelPendingMusicMediaReady = () =>", 1
    )[1].split("// ---", 1)[0]
    assert "latestMusicRequestToken++;" in player_reset
    assert "if (pendingMusicMediaReadyCancel) pendingMusicMediaReadyCancel();" in player_reset
    assert "requestId" not in player_reset


def test_music_player_reports_confirmed_state_to_backend():
    player_source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    router_source = WEBSOCKET_ROUTER_PATH.read_text(encoding="utf-8")

    assert (
        "function reportMusicPlaybackState(state, track, playbackContext, failureReason)"
        in player_source
    )
    assert "function createMusicPlaybackReportContext(playbackId, options, track, token)" in player_source
    assert "function getOwnedMusicPlaybackReportContext(player, state)" in player_source
    assert "function normalizeMusicEventTimestamp(event)" in player_source
    assert "action: 'music_playback_state'" in player_source
    assert "playback_window_id: MUSIC_COORD_SENDER_ID" in player_source
    assert "playback_started_at: context.lifecycleStartedAt" in player_source
    assert "reason: state === 'error'" in player_source
    assert "String(failureReason || 'unknown').slice(0, 32)" in player_source
    assert "localPlayer._musicPlaybackReportContext = playbackReportContext" in player_source
    ownership_source = player_source.split(
        "function getOwnedMusicPlaybackReportContext(player, state)", 1
    )[1].split("// ---", 1)[0]
    assert "context.token !== player._latestToken" in ownership_source
    assert "context.playbackId !== getCurrentMusicPlaybackId()" in ownership_source
    assert "latestMusicRequestToken" not in ownership_source
    assert "getOwnedMusicPlaybackReportContext(boundPlayer, 'playing')" in player_source
    assert "getOwnedMusicPlaybackReportContext(boundPlayer, playbackState)" in player_source
    assert "getOwnedMusicPlaybackReportContext(boundPlayer, 'ended')" in player_source
    assert "getOwnedMusicPlaybackReportContext(boundPlayer, 'error')" in player_source
    assert ") !== reportContext" in player_source
    assert "reportMusicPlaybackState('playing', null, reportContext)" in player_source
    assert "reportMusicPlaybackState('ended', null, reportContext)" in player_source
    assert (
        "function reportMusicPlaybackFailureIfNeeded(playbackContext, failureReason, deferFailureUi)"
        in player_source
    )
    failure_helper = player_source.split(
        "function reportMusicPlaybackFailureIfNeeded", 1
    )[1].split("function getOwnedMusicPlaybackReportContext", 1)[0]
    assert "playbackContext.lastReportedState === 'error'" in failure_helper
    assert "!['playing', 'paused'].includes(playbackContext.lastReportedState)" in failure_helper
    assert "reportMusicPlaybackState('error', null, playbackContext, failureReason)" in failure_helper
    media_failure_source = player_source.split(
        "const mediaResult = await mediaReadyPromise;", 1
    )[1].split("playbackReportContext.mediaReady = true;", 1)[0]
    media_report = media_failure_source.index("reportMusicPlaybackFailureIfNeeded(")
    media_ui_gate = media_failure_source.index(
        "if (!playbackReportContext.failureUiHandled && !deferFailureUi)"
    )
    assert media_report < media_ui_gate
    assert "reportMusicPlaybackState(" not in media_failure_source

    error_handler = player_source.split(
        "boundPlayer.on('error', (err) => {", 1
    )[1].split("// 进度条与播放按钮点击", 1)[0]
    delayed_error_handler = error_handler.split("setTimeout(() => {", 1)[1]
    error_report = delayed_error_handler.index("reportMusicPlaybackFailureIfNeeded(")
    deferred_return = delayed_error_handler.index("if (deferFailureUi) return;")
    error_ui_gate = delayed_error_handler.index("if (reportContext.failureUiHandled) return;")
    assert error_report < deferred_return < error_ui_gate
    assert "reportMusicPlaybackState(" not in delayed_error_handler
    assert "mediaResult.reason" in player_source
    assert "'player_error'" in player_source
    assert "localPlayer === boundPlayer && boundPlayer._latestToken === tokenAtEvent" in player_source
    assert 'elif action == "music_playback_state":' in router_source
    assert "handle_music_playback_state(" in router_source
    superseded_gate = router_source.split(
        "if session_id.get(lanlan_name) != this_session_id:", 1
    )[1].split("action = message.get(\"action\")", 1)[0]
    assert "if _is_music_playback_state_message(message):" in superseded_gate
    assert superseded_gate.index("_is_music_playback_state_message") < superseded_gate.index(
        "await websocket.close()"
    )


def test_music_player_rejects_errors_queued_before_the_current_source_lifecycle():
    player_source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    readiness_handler = player_source.split(
        "const waitForMusicMediaReady = (", 1
    )[1].split("const getMusicPlayerInstance", 1)[0]
    error_handler = player_source.split(
        "boundPlayer.on('error', (err) => {",
        1,
    )[1].split("// 进度条与播放按钮点击", 1)[0]

    assert "const sourceLifecycleStartedAt = getMusicLifecycleTimestamp();" in readiness_handler
    assert "const eventTimestamp = normalizeMusicEventTimestamp(event);" in readiness_handler
    assert "eventTimestamp < sourceLifecycleStartedAt" in readiness_handler
    assert "window.queueMicrotask(onError)" not in readiness_handler
    assert "if (!audio.error && audio.readyState >= 1)" in readiness_handler
    assert "lifecycleStartedAt: getMusicLifecycleTimestamp()" in player_source
    assert "mediaReady: false" in player_source
    assert "const eventTimestamp = normalizeMusicEventTimestamp(err);" in error_handler
    assert "eventTimestamp < reportContext.lifecycleStartedAt" in error_handler
    assert "eventTimestamp === null && reportContext.mediaReady !== true" in error_handler
    delayed_handler = error_handler.split("setTimeout(() => {", 1)[1]
    assert "getOwnedMusicPlaybackReportContext(boundPlayer, 'error') !== reportContext" in delayed_handler
    assert delayed_handler.index("getOwnedMusicPlaybackReportContext") < delayed_handler.index(
        "boundPlayer._loadError = true;"
    )
    assert "playbackReportContext.mediaReady = true;" in player_source


def test_same_track_retry_refreshes_context_and_rebuilds_loading_player():
    player_source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    duplicate_path = player_source.split(
        "// 5秒去重逻辑", 1
    )[1].split("if (isSameTrack(trackInfo) && !isPlayerInDOM())", 1)[0]

    assert "duplicateAudio.readyState >= 2" in duplicate_path
    assert "setMusicPlaybackContext(playbackOptions);" in duplicate_path
    assert "duplicatePlayer._musicPlaybackReportContext = duplicateReportContext;" in duplicate_path
    assert "reportMusicPlaybackState('playing', null, duplicateReportContext);" in duplicate_path

    fast_path = player_source.split(
        "if (isSameTrack(trackInfo) && isPlayerInDOM()) {",
        1,
    )[1].split("// A single <audio> cannot identify", 1)[0]
    assert "player.audio.readyState < 2" in fast_path
    assert fast_path.index("player.audio.readyState < 2") < fast_path.index(
        "setMusicPlaybackContext(playbackOptions);"
    )
    assert "player._latestToken = latestMusicRequestToken;" in fast_path
    assert fast_path.index("player._latestToken = latestMusicRequestToken;") < fast_path.index(
        "player._musicPlaybackReportContext = playbackReportContext;"
    )


def test_same_track_fast_path_rebuilds_missing_player_instance():
    player_source = MUSIC_UI_PATH.read_text(encoding="utf-8")

    fast_path = player_source.split(
        "if (isSameTrack(trackInfo) && isPlayerInDOM()) {",
        1,
    )[1].split("// A single <audio> cannot identify", 1)[0]
    assert "if (!player) {" in fast_path
    assert "destroyMusicPlayer(true, false, false);" in fast_path
    assert fast_path.index("if (!player) {") < fast_path.index(
        "player._musicPlaybackReportContext = playbackReportContext;"
    )


def test_same_url_replacement_uses_a_fresh_audio_element():
    player_source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    send_source = player_source.split(
        "window.sendMusicMessageDetailed = async function", 1
    )[1].split("window.sendMusicMessage = async function", 1)[0]
    same_url_guard = send_source.split(
        "const currentAudioForRequest = localPlayer && localPlayer.audio;", 1
    )[1].split("try {", 1)[0]

    assert "currentAudioForRequest.currentSrc || currentAudioForRequest.src" in same_url_guard
    assert "resolveMusicUrl(currentAudioUrl) === resolveMusicUrl(trackInfo.url)" in same_url_guard
    assert "destroyMusicPlayer(true, false, false);" in same_url_guard

    teardown_source = player_source.split(
        "const destroyMusicPlayer =", 1
    )[1].split("// --- 查找并替换整个 loadAPlayerLibrary 函数 ---", 1)[0]
    revoke_context = teardown_source.index(
        "localPlayer._musicPlaybackReportContext = null;"
    )
    pause_player = teardown_source.index("localPlayer.pause();")
    assert revoke_context < pause_player


def test_stale_remote_owner_cannot_hold_music_occupancy_forever():
    player_source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    occupancy = player_source.split(
        "const isMusicOccupied = () => {", 1
    )[1].split("const getMusicCurrentTrack", 1)[0]

    assert "const remoteOccupied = isRemoteMusicActive();" in occupancy
    assert "!remoteMusicSenders.has(mirrorBarLeaderSender)" in occupancy
    assert "teardownMirrorBar(false);" in occupancy
    assert "setMirrorBarLeader(null);" in occupancy
    assert occupancy.index("teardownMirrorBar(false);") < occupancy.index(
        "setMirrorBarLeader(null);"
    )
    assert occupancy.index("isRemoteMusicActive()") < occupancy.index(
        "const mirrorOccupied"
    )


def test_missing_music_cover_stays_out_of_data_and_uses_frontend_placeholder():
    player_source = MUSIC_UI_PATH.read_text(encoding="utf-8")
    player_style = MUSIC_UI_CSS_PATH.read_text(encoding="utf-8")
    crawler_source = MUSIC_CRAWLERS_PATH.read_text(encoding="utf-8")

    assert "'cover': cover or ''" in crawler_source
    assert "dummyimage.com" not in crawler_source
    assert "defaultCoverPath: '/static/assets/music/music-cover-placeholder.png'" in player_source
    assert "const normalizeMusicCoverUrl = (cover) =>" in player_source
    assert "hostname.endsWith('.music.126.net')" in player_source
    assert "parsed.protocol = 'https:'" in player_source
    assert "const normalizedCover = normalizeMusicCoverUrl(cover)" in player_source
    assert "thumbnailUrl: displayCoverUrl" in player_source
    assert "applyMusicCover" not in player_source
    assert player_source.count('class="music-bar-equalizer"') == 2
    assert player_source.count('class="music-bar-equalizer-bar"') == 6
    assert ".music-player-bar.is-playing .music-bar-equalizer-bar" in player_style
    assert "@keyframes musicBarEqualizer" in player_style
    assert "music-bar-fallback" not in player_source
    assert "dummyimage.com" not in player_source
    assert DEFAULT_MUSIC_COVER_PATH.stat().st_size > 0


def test_music_player_assets_are_versioned_with_the_page():
    pages_source = PAGES_ROUTER_PATH.read_text(encoding="utf-8")

    assert '_PROJECT_ROOT / "static/jukebox/music_ui.js"' in pages_source
    assert '_PROJECT_ROOT / "static/css/music_ui.css"' in pages_source
    assert '_PROJECT_ROOT / "static/assets/music/music-cover-placeholder.png"' in pages_source
    for template_path in MUSIC_PLAYER_TEMPLATES:
        template_source = template_path.read_text(encoding="utf-8")
        assert '/static/css/music_ui.css?v={{ static_asset_version }}' in template_source
        assert '/static/jukebox/music_ui.js?v={{ static_asset_version }}' in template_source


def test_all_locales_define_music_player_labels_and_failures():
    required = {
        "unknownTrack",
        "unknownArtist",
        "unknownSource",
        "volumeControl",
        "closePlayer",
        "trackTooLong",
        "loadTimeout",
        "loading",
        "playError",
        "loadError",
        "loginRequired",
        "playlistAmbiguous",
        "sourceEmpty",
    }

    for locale_path in sorted(LOCALES_DIR.glob("*.json")):
        data = json.loads(locale_path.read_text(encoding="utf-8"))
        assert required <= set(data["music"]), locale_path.name


def test_music_proxy_streams_one_upstream_response_and_tees_small_cache():
    source = MUSIC_ROUTER_PATH.read_text(encoding="utf-8")

    assert "StreamingResponse(" in source
    assert "_stream_music_response(" in source
    assert "async def _stream_music(" not in source
    assert "cache_body = bytearray() if cache_key else None" in source
    assert "if cache_key and cache_body is not None:" in source
