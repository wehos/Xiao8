import json
import re
import time

import pytest
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, expect

from main_routers import system_router


def _install_badminton_test_hooks(page: Page) -> None:
    page.add_init_script(
        """
        (() => {
          if (sessionStorage.getItem('__badmintonE2ELocalStorageCleared') !== '1') {
            localStorage.clear();
            sessionStorage.setItem('__badmintonE2ELocalStorageCleared', '1');
          }
          window.__badmintonE2EEvents = [];
          window.AudioContext = window.AudioContext || function () {
            return { state: 'running', currentTime: 0, resume: async () => {},
              createOscillator: () => ({ type: 'sine', frequency: { setValueAtTime(){}, exponentialRampToValueAtTime(){} }, connect(){}, start(){}, stop(){} }),
              createGain: () => ({ gain: { setValueAtTime(){}, exponentialRampToValueAtTime(){}, linearRampToValueAtTime(){} }, connect(){} }),
              createBuffer: () => ({ getChannelData: () => new Float32Array(1) }),
              createBufferSource: () => ({ buffer: null, connect(){}, start(){} }),
              createBiquadFilter: () => ({ type: 'lowpass', frequency: { setValueAtTime(){}, exponentialRampToValueAtTime(){} }, Q: { setValueAtTime(){} }, gain: { setValueAtTime(){} }, connect(){} }),
              createDynamicsCompressor: () => ({ threshold: { setValueAtTime(){} }, knee: { setValueAtTime(){} }, ratio: { setValueAtTime(){} }, attack: { setValueAtTime(){} }, release: { setValueAtTime(){} }, connect(){} }),
              createDelay: () => ({ delayTime: { setValueAtTime(){} }, connect(){} }),
              destination: {}
            };
          };
          window.webkitAudioContext = window.AudioContext;
        })();
        """
    )


def _goto_badminton(
    page: Page,
    running_server: str,
    mode: str,
    debug: bool = True,
    debug_voice: bool = False,
    wait_loading: bool = True,
    auto_start: bool = True,
) -> None:
    _install_badminton_test_hooks(page)
    lanlan_name = "e2e-yui"
    session_id = f"e2e-badminton-{mode}"
    state = system_router._mini_game_invite_get_state(lanlan_name)
    state["delivered_at"] = time.time() - 1
    state["responded_at"] = time.time()
    state["pending_session_id"] = session_id
    state["last_game_type"] = "badminton"
    debug_query = "&debug=1" if debug else ""
    if debug_voice:
        debug_query += "&debug_voice=1"
    elif debug:
        debug_query += "&debug_mute_voice=1"
    page.goto(
        f"{running_server}/badminton_demo"
        f"?mode={mode}&lanlan_name={lanlan_name}&session_id={session_id}{debug_query}"
    )
    expect(page.locator("#game")).to_be_attached(timeout=15000)
    page.wait_for_function("window.BadmintonDemo && window.BadmintonDemo.getState")
    if not wait_loading:
        return
    page.wait_for_function(
        """() => {
          const loading = document.getElementById('badminton-loading');
          return !loading || window.__badmintonInitialLoadingHidden === true || loading.hidden === true;
        }""",
        timeout=10000,
    )
    if auto_start:
        start_button = page.locator("#badminton-start-button")
        try:
            start_button.wait_for(state="visible", timeout=3000)
            start_button.click()
            expect(page.locator("#badminton-start-overlay")).not_to_be_visible(timeout=3000)
        except PlaywrightTimeoutError:
            pass


@pytest.mark.e2e
def test_badminton_start_tutorial_memory_and_never_prompt(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel", auto_start=False)

    expect(page.locator("#badminton-start-overlay")).to_be_visible(timeout=3000)
    expect(page.locator("#badminton-start-tutorial")).to_be_visible()
    duel_rule = page.locator('#badminton-start-tutorial [data-i18n="badminton.startTutorial.duel11"]')
    expect(duel_rule).to_contain_text("11")
    expect(duel_rule).not_to_contain_text("3")
    expect(page.locator("#badminton-start-overlay #game-memory-option")).to_be_visible()
    expect(page.locator("#badminton-start-never-option")).to_be_visible()

    assert page.evaluate("window.BadmintonDemo.getState().canControlShot") is False
    page.locator("#bd-game-memory-toggle").check()
    page.locator("#bd-start-never-toggle").check()
    page.locator("#badminton-start-button").click()
    expect(page.locator("#badminton-start-overlay")).not_to_be_visible(timeout=3000)
    page.wait_for_function("window.BadmintonDemo.getState().canControlShot === true", timeout=5000)
    tutorial_key = page.evaluate(
        """() => {
          const rawName = String(
            (window.lanlan_config && window.lanlan_config.lanlan_name)
              || new URLSearchParams(window.location.search).get('lanlan_name')
              || 'default'
          ).trim();
          let scope = 'default';
          if (rawName) {
            scope = encodeURIComponent(rawName.toLowerCase())
              .replace(/%/g, '~')
              .replace(/[^a-z0-9_~-]+/g, '_') || 'default';
          }
          return `bd:${scope}:bd_start_tutorial_dismissed`;
        }"""
    )
    assert page.evaluate("(key) => localStorage.getItem(key)", tutorial_key) == "1"
    assert page.evaluate("localStorage.getItem('bd_start_tutorial_dismissed')") is None

    page.reload()
    page.wait_for_function(
        """() => {
          const loading = document.getElementById('badminton-loading');
          return window.__badmintonInitialLoadingHidden === true || (loading && loading.hidden === true);
        }""",
        timeout=10000,
    )
    expect(page.locator("#badminton-start-overlay")).not_to_be_visible()
    page.wait_for_function("window.BadmintonDemo.getState().canControlShot === true", timeout=5000)


def _force_shot_result(page: Page, scored: bool = True) -> None:
    before_results = page.evaluate("window.BadmintonDemo.getState().attemptsResults.length")
    page.evaluate(
        """(scored) => {
          const api = window.BadmintonDemo;
          api._debugFinishShot(scored, scored ? 'line_in' : 'out');
        }""",
        scored,
    )
    page.wait_for_function(
        """(beforeResults) => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.attemptsResults.length > beforeResults;
        }""",
        arg=before_results,
    )
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && (state.state === 'ready' || state.state === 'neko_thinking' || state.state === 'game_over');
        }"""
    )


def _wait_for_badminton_speak_payload(
    page: Page, speak_payloads: list[dict], event_kind: str, timeout_ms: int = 3000
) -> dict:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        payload = next(
            (
                payload
                for payload in reversed(speak_payloads)
                if payload.get("event", {}).get("kind") == event_kind
            ),
            None,
        )
        if payload:
            return payload
        page.wait_for_timeout(50)
    pytest.fail(f"Timed out waiting for badminton speak event: {event_kind}")


@pytest.mark.e2e
def test_badminton_legacy_modes_fall_back_to_duel(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "spectator")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    state = page.evaluate("window.BadmintonDemo.getState()")
    assert state["mode"] == "duel"
    expect(page.locator('[data-mode="spectator"]')).to_have_count(0)
    expect(page.locator('[data-mode="shooter"]')).to_have_count(0)


@pytest.mark.e2e
def test_badminton_duel_eleven_player_misses_and_restart(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    for _ in range(11):
        _force_shot_result(page, False)

    expect(page.locator("#result-panel")).to_have_class(re.compile(r"\bshow\b"))
    state = page.evaluate("window.BadmintonDemo.getState()")
    assert state["state"] == "game_over"
    assert state["mode"] == "duel"
    assert state["duel"]["player_misses"] == 11
    assert state["score"] == 0
    page.locator("#restart-button").click()
    page.wait_for_function("window.BadmintonDemo.getState().score === 0")


@pytest.mark.e2e
def test_badminton_mode_switcher_is_removed(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "legacy")

    expect(page.locator("#mode-switcher")).to_have_count(0)
    state = page.evaluate("window.BadmintonDemo.getState()")
    assert state["mode"] == "duel"


@pytest.mark.e2e
def test_badminton_local_leaderboard_records_game(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    _force_shot_result(page, True)
    for _ in range(11):
        _force_shot_result(page, False)

    page.locator("#leaderboard-button").click()
    expect(page.locator("#leaderboard-panel")).to_have_class(re.compile(r"\bshow\b"))
    page.locator('.lb-tab[data-tab="local"]').click()
    expect(page.locator("#leaderboard-body tr")).to_have_count(1)


@pytest.mark.e2e
def test_badminton_public_api_and_debug_panel(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    state = page.evaluate("window.BadmintonDemo.getState()")
    assert {"mode", "state", "score", "streak", "distance"} <= set(state)
    assert page.evaluate("window.BadmintonDemo.setDuelDifficulty('max'); window.BadmintonDemo.getDifficulty()") == "max"
    assert page.evaluate("window.BadmintonDemo.setMood('happy'); window.BadmintonDemo.getMood()") == "happy"
    expect(page.locator("#bd-debug-panel")).to_be_visible()


@pytest.mark.e2e
def test_badminton_player_swing_hides_held_shuttle_then_launches_physical_shuttle(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready' && window.BadmintonDemo.getState().canControlShot")
    initial = page.evaluate("window.BadmintonDemo.getState()")
    assert initial["heldShuttleVisible"] is True
    assert initial["pendingSwing"] is None
    assert initial["currentShuttle"] is None

    page.evaluate("window.BadmintonDemo.shoot()")
    swinging = page.evaluate("window.BadmintonDemo.getState()")
    assert swinging["state"] == "swinging"
    assert swinging["heldShuttleVisible"] is False
    assert swinging["pendingSwing"]["shooter"] == "player"
    assert swinging["currentShuttle"] is None

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.state === 'in_flight' && state.currentShuttle;
        }"""
    )
    launched = page.evaluate("window.BadmintonDemo.getState()")
    shuttle = launched["currentShuttle"]
    assert launched["heldShuttleVisible"] is False
    assert launched["pendingSwing"] is None
    assert shuttle["id"] >= 1
    assert shuttle["radius"] == 18
    assert shuttle["diameter"] == 36
    assert shuttle["massKg"] == 0.005
    assert shuttle["dragPerSecond"] == 0.42
    assert shuttle["swingForce"] >= 0
    assert shuttle["shooter"] == "player"


@pytest.mark.e2e
def test_badminton_player_can_move_while_own_shuttle_is_in_flight(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready' && window.BadmintonDemo.getState().canControlShot")
    before_shot = page.evaluate("window.BadmintonDemo.getState().playerCourt")
    page.evaluate("window.BadmintonDemo.shoot()")
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.state === 'in_flight' &&
            state.currentShuttle && state.currentShuttle.shooter === 'player';
        }"""
    )

    viewport = page.viewport_size or {"width": 1280, "height": 720}
    page.mouse.move(viewport["width"] - 8, 24)
    page.wait_for_function(
        """(beforeX) => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.playerCourt &&
            state.playerCourt.targetX > beforeX + 16 &&
            state.playerCourt.x > beforeX + 16;
        }""",
        arg=before_shot["targetX"],
    )

    moved = page.evaluate("window.BadmintonDemo.getState()")
    assert moved["state"] == "in_flight"
    assert moved["currentShuttle"]["shooter"] == "player"
    assert moved["playerCourt"]["targetX"] > before_shot["targetX"]
    assert moved["playerCourt"]["x"] > before_shot["x"]
    assert moved["charging"] is False
    assert moved["pendingSwing"] is None


@pytest.mark.e2e
def test_badminton_shuttle_flight_decelerates_then_drops(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready' && window.BadmintonDemo.getState().canControlShot")
    page.evaluate("window.BadmintonDemo.shoot()")
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.state === 'in_flight' && state.currentShuttle;
        }"""
    )
    launched = page.evaluate("window.BadmintonDemo.getState().currentShuttle")
    initial_vx = abs(launched["vx"])
    initial_vy = launched["vy"]
    assert initial_vx > 100

    page.wait_for_timeout(220)
    midflight = page.evaluate("window.BadmintonDemo.getState().currentShuttle")
    assert midflight is not None
    assert abs(midflight["vx"]) < initial_vx * 0.97
    assert abs(midflight["vx"]) > initial_vx * 0.68

    page.wait_for_timeout(140)
    lateflight = page.evaluate("window.BadmintonDemo.getState().currentShuttle")
    assert lateflight is not None
    assert lateflight["vy"] > max(initial_vy, midflight["vy"]) + 45


@pytest.mark.e2e
def test_badminton_space_jump_turns_air_swing_into_smash(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready' && window.BadmintonDemo.getState().canControlShot")
    page.evaluate(
        """() => {
          window.dispatchEvent(new KeyboardEvent('keydown', {
            key: ' ',
            code: 'Space',
            bubbles: true,
            cancelable: true
          }));
        }"""
    )
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.playerJump && state.playerJump.active && state.playerJump.offset > 0;
        }""",
        timeout=2000,
    )
    page.wait_for_function(
        """() => {
          const api = window.BadmintonDemo;
          const state = api && api.getState();
          if (!state || !state.playerJump || !state.playerJump.smashReady || !state.canControlShot) return false;
          window.__badmintonSmashReadySnapshot = state.playerJump;
          api.shoot();
          return true;
        }""",
        timeout=3500,
    )
    jumping = page.evaluate("window.__badmintonSmashReadySnapshot")
    assert jumping["smashReady"] is True

    swinging = page.evaluate("window.BadmintonDemo.getState()")
    assert swinging["state"] in {"swinging", "in_flight"}
    if swinging["state"] == "swinging":
        assert swinging["pendingSwing"]["shooter"] == "player"
        assert swinging["pendingSwing"]["isSmash"] is True
        assert swinging["pendingSwing"]["smashQuality"] > 0
    else:
        assert swinging["currentShuttle"]["shooter"] == "player"
        assert swinging["currentShuttle"]["isSmash"] is True
        assert swinging["currentShuttle"]["smashQuality"] > 0

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.state === 'in_flight' && state.currentShuttle && state.currentShuttle.isSmash;
        }""",
        timeout=3000,
    )
    launched = page.evaluate("window.BadmintonDemo.getState()")
    assert launched["currentShuttle"]["shooter"] == "player"
    assert launched["currentShuttle"]["isSmash"] is True
    assert launched["currentShuttle"]["smashQuality"] > 0
    assert launched["currentShuttle"]["vy"] > 0
    initial_screen_y = launched["currentShuttle"]["screenY"]

    page.wait_for_timeout(180)
    descending = page.evaluate("window.BadmintonDemo.getState()")
    assert descending["currentShuttle"]["isSmash"] is True
    assert descending["currentShuttle"]["screenY"] > initial_screen_y + 8


@pytest.mark.e2e
def test_badminton_player_serve_hint_draws_or_attaches_shuttle_above_player(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready' && window.BadmintonDemo.getState().heldShuttleVisible")
    page.wait_for_function(
        """() => {
          const container = document.getElementById('player-sensei-vrm-container');
          if (container && container.dataset.heldShuttle3d === 'ready') {
            window.__bdHeldShuttleSample = { heldShuttle3d: true, brightPixels: 0 };
            return true;
          }
          const state = window.BadmintonDemo.getState();
          const canvas = document.getElementById('game');
          const ctx = canvas.getContext('2d');
          const scaleX = canvas.width / 900;
          const scaleY = canvas.height / 500;
          const hintX = state.playerCourt.x + 44;
          const hintY = state.playerCourt.y - 108;
          const sx = Math.max(0, Math.round((hintX - 22) * scaleX));
          const sy = Math.max(0, Math.round((hintY - 24) * scaleY));
          const sw = Math.round(44 * scaleX);
          const sh = Math.round(48 * scaleY);
          const data = ctx.getImageData(sx, sy, sw, sh).data;
          let bright = 0;
          for (let i = 0; i < data.length; i += 4) {
              if (data[i] > 210 && data[i + 1] > 210 && data[i + 2] > 210 && data[i + 3] > 120) bright++;
            }
          window.__bdHeldShuttleSample = { heldShuttle3d: false, brightPixels: bright };
          return bright > 12;
        }""",
        timeout=2000,
    )
    sample = page.evaluate("window.__bdHeldShuttleSample")
    assert sample["heldShuttle3d"] is True or sample["brightPixels"] > 12


@pytest.mark.e2e
def test_badminton_player_serve_setup_stays_between_service_lines(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready' && window.BadmintonDemo.getState().canControlShot")
    initial = page.evaluate("window.BadmintonDemo.getState()")
    lines = initial["courtServiceLines"]
    expected_serve_x = (lines["leftDoublesLongServiceX"] + lines["leftShortServiceX"]) / 2
    assert abs(initial["playerServeX"] - expected_serve_x) < 0.001
    assert abs(initial["playerCourt"]["x"] - expected_serve_x) < 1.5
    assert abs(initial["playerCourt"]["targetX"] - expected_serve_x) < 1.5

    box = page.locator("#game").bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] * 0.88, box["y"] + box["height"] * 0.52)
    page.evaluate(
        """() => {
          const x = window.innerWidth * 0.88;
          const y = window.innerHeight * 0.52;
          window.dispatchEvent(new PointerEvent('pointermove', { clientX: x, clientY: y, bubbles: true }));
          window.dispatchEvent(new MouseEvent('mousemove', { clientX: x, clientY: y, bubbles: true }));
        }"""
    )
    page.wait_for_function(
        """(initialX) => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.canMoveCourt && state.playerServeSetup && state.playerCourt &&
            state.playerCourt.targetX > initialX + 20 &&
            state.playerCourt.x > initialX + 20;
        }""",
        arg=initial["playerCourt"]["x"],
        timeout=3000,
    )
    after_move = page.evaluate("window.BadmintonDemo.getState()")
    assert after_move["playerCourt"]["targetX"] > expected_serve_x + 20
    assert after_move["playerCourt"]["x"] > expected_serve_x + 20
    assert lines["leftDoublesLongServiceX"] < after_move["playerCourt"]["targetX"] < lines["leftShortServiceX"]
    assert lines["leftDoublesLongServiceX"] < after_move["playerCourt"]["x"] < lines["leftShortServiceX"]
    assert abs(after_move["playerCourt"]["targetY"] - initial["playerCourt"]["targetY"]) < 0.001
    assert abs(after_move["playerCourt"]["y"] - initial["playerCourt"]["y"]) < 0.001


@pytest.mark.e2e
def test_badminton_yui_swings_back_when_player_shuttle_reaches_her_side(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          const contact = window.BadmintonDemo._debugGetYuiRacketContactPoint();
          window.BadmintonDemo._debugSetPlayerShuttleForYuiReturnBall({
            x: contact.x,
            prevX: contact.x - 14,
            courtY: contact.x,
            prevCourtY: contact.x - 14,
            y: contact.y,
            prevY: contact.y
          });
        }"""
    )

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && (
            (state.pendingSwing && state.pendingSwing.shooter === 'neko') ||
            (state.currentShuttle && state.currentShuttle.shooter === 'neko')
          );
        }""",
        timeout=5000,
    )
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          if (!state || state.state !== 'in_flight' || !state.currentShuttle || state.currentShuttle.shooter !== 'neko') return false;
          window.__bdYuiReturnSample = {
            id: state.currentShuttle.id,
            shooter: state.currentShuttle.shooter,
            spinRate: state.currentShuttle.spinRate,
            spinAngle: state.currentShuttle.spinAngle
          };
          return Math.abs(state.currentShuttle.spinRate) >= 8;
        }""",
        timeout=5000,
    )
    returned = page.evaluate("window.__bdYuiReturnSample")
    assert returned["shooter"] == "neko"
    assert abs(returned["spinRate"]) >= 8
    page.wait_for_function(
        """(sample) => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          if (!state || !state.currentShuttle || state.currentShuttle.id !== sample.id) return false;
          const angleDelta = Math.abs(state.currentShuttle.spinAngle - sample.spinAngle);
          if (state.currentShuttle.hitNet) {
            window.__bdYuiSpinSample = {
              id: state.currentShuttle.id,
              spinAngle: state.currentShuttle.spinAngle,
              angleDelta,
              hitNet: true
            };
            return true;
          }
          if (angleDelta < 0.5) return false;
          window.__bdYuiSpinSample = {
            id: state.currentShuttle.id,
            spinAngle: state.currentShuttle.spinAngle,
            angleDelta,
            hitNet: false
          };
          return true;
        }""",
        arg=returned,
        timeout=1000,
    )
    spinning = page.evaluate("window.__bdYuiSpinSample")
    assert spinning["id"] == returned["id"]
    if not spinning["hitNet"]:
        assert spinning["angleDelta"] >= 0.5


@pytest.mark.e2e
def test_badminton_yui_returns_frontcourt_shuttle_between_net_and_service_line(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    setup = page.evaluate(
        """() => {
          const state = window.BadmintonDemo.getState();
          const contact = window.BadmintonDemo._debugGetYuiRacketContactPoint();
          const id = window.BadmintonDemo._debugSetPlayerShuttleForYuiReturnBall({
            x: contact.x,
            prevX: contact.x - 14,
            courtY: contact.x,
            prevCourtY: contact.x - 14,
            y: contact.y,
            prevY: contact.y
          });
          const next = window.BadmintonDemo.getState();
          return {
            id,
            contact,
            ball: next.currentShuttle,
            netX: 460,
            rightShortServiceX: state.courtServiceLines.rightShortServiceX
          };
        }"""
    )
    assert setup["ball"]["shooter"] == "player"
    assert setup["ball"]["crossedNet"] is True
    assert setup["ball"]["y"] > setup["netX"]
    assert abs(setup["ball"]["screenX"] - setup["contact"]["x"]) <= setup["contact"]["reachX"] + 1

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && (
            (state.pendingSwing && state.pendingSwing.shooter === 'neko') ||
            (state.currentShuttle && state.currentShuttle.shooter === 'neko')
          );
        }""",
        timeout=3000,
    )
    returned = page.evaluate("window.BadmintonDemo.getState()")
    assert (
        returned["pendingSwing"] and returned["pendingSwing"]["shooter"] == "neko"
    ) or (
        returned["currentShuttle"] and returned["currentShuttle"]["shooter"] == "neko"
    )


@pytest.mark.e2e
@pytest.mark.parametrize("segment", ["racket_center", "racket_outer_edge"])
def test_badminton_yui_returns_shuttle_from_back_service_segments(mock_page: Page, running_server: str, segment: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    setup = page.evaluate(
        """(segment) => {
          const state = window.BadmintonDemo.getState();
          const contact = window.BadmintonDemo._debugGetYuiRacketContactPoint();
          const offset = segment === 'racket_outer_edge' ? contact.reachX * 0.55 : 0;
          const courtY = contact.x + offset;
          const id = window.BadmintonDemo._debugSetPlayerShuttleForYuiReturnBall({
            x: courtY,
            prevX: courtY - 18,
            courtY,
            prevCourtY: courtY - 18,
            y: contact.y,
            prevY: contact.y
          });
          const next = window.BadmintonDemo.getState();
          return {
            id,
            courtY,
            contact,
            ball: next.currentShuttle
          };
        }""",
        segment,
    )
    assert abs(setup["courtY"] - setup["contact"]["x"]) <= setup["contact"]["reachX"]

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && (
            (state.pendingSwing && state.pendingSwing.shooter === 'neko') ||
            (state.currentShuttle && state.currentShuttle.shooter === 'neko')
          );
        }""",
        timeout=3000,
    )
    returned = page.evaluate("window.BadmintonDemo.getState()")
    assert (
        returned["pendingSwing"] and returned["pendingSwing"]["shooter"] == "neko"
    ) or (
        returned["currentShuttle"] and returned["currentShuttle"]["shooter"] == "neko"
    )


@pytest.mark.e2e
def test_badminton_yui_does_not_swing_when_shuttle_is_outside_racket_hit_range(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    setup = page.evaluate(
        """() => {
          const state = window.BadmintonDemo.getState();
          const lines = state.courtServiceLines;
          const courtY = Math.min(842, lines.rightDoublesLongServiceX + 34);
          const id = window.BadmintonDemo._debugSetPlayerShuttleForYuiReturnBall({
            x: courtY,
            prevX: courtY,
            courtY,
            prevCourtY: courtY,
            vx: 0,
            vCourtY: 0,
            y: 322,
            prevY: 322,
            z: 128,
            prevZ: 128
          });
          const next = window.BadmintonDemo.getState();
          return { id, courtY, ball: next.currentShuttle, rightDoublesLongServiceX: lines.rightDoublesLongServiceX };
        }"""
    )
    assert setup["rightDoublesLongServiceX"] < setup["courtY"] < 850
    assert setup["ball"]["shooter"] == "player"
    assert setup["ball"]["crossedNet"] is True

    page.wait_for_timeout(450)
    state = page.evaluate("window.BadmintonDemo.getState()")
    assert state["pendingSwing"] is None
    assert state["currentShuttle"]
    assert state["currentShuttle"]["shooter"] == "player"


@pytest.mark.e2e
def test_badminton_yui_saves_player_smash_just_outside_racket_hit_range(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          window.__badmintonObservedYuiSave = null;
          window.__badmintonStopYuiSaveObserver = false;
          const observe = () => {
            const state = window.BadmintonDemo && window.BadmintonDemo.getState();
            const saving = !!document.querySelector('.neko-avatar-container[data-court-avatar="opponent"].saving');
            if (state && state.pendingSwing && state.pendingSwing.shooter === 'neko' &&
              state.pendingSwing.save === true) {
              window.__badmintonObservedYuiSave = {
                shooter: state.pendingSwing.shooter,
                save: state.pendingSwing.save,
                isSmash: state.pendingSwing.isSmash,
                saving
              };
              return;
            }
            if (!window.__badmintonStopYuiSaveObserver) requestAnimationFrame(observe);
          };
          requestAnimationFrame(observe);
        }"""
    )
    setup = page.evaluate(
        """() => {
          const contact = window.BadmintonDemo._debugGetYuiRacketContactPoint();
          const dx = contact.reachX + 12;
          const dy = 0;
          const courtY = contact.x + dx;
          const screenY = contact.y + dy;
          const id = window.BadmintonDemo._debugSetPlayerShuttleForYuiReturnBall({
            x: courtY,
            prevX: courtY - 8,
            courtY,
            prevCourtY: courtY - 8,
            y: screenY,
            prevY: screenY,
            vx: 24,
            vCourtY: 24,
            vy: 0,
            vz: 0,
            isSmash: true,
            smashQuality: 0.82,
            incomingSpeed: 680
          });
          return {
            id,
            contact,
            normalNormalized: Math.pow(dx / contact.reachX, 2) + Math.pow(dy / contact.reachY, 2),
            saveNormalized: Math.pow(dx / contact.saveReachX, 2) + Math.pow(dy / contact.saveReachY, 2)
          };
        }"""
    )
    assert setup["contact"]["saveReachX"] == 66
    assert setup["contact"]["saveReachY"] == 72
    assert setup["normalNormalized"] > 1
    assert setup["saveNormalized"] <= 1

    try:
        page.wait_for_function(
            "window.__badmintonObservedYuiSave !== null",
            timeout=3000,
        )
    finally:
        page.evaluate("window.__badmintonStopYuiSaveObserver = true")
    saved = page.evaluate("window.__badmintonObservedYuiSave")
    assert saved["shooter"] == "neko"
    assert saved["save"] is True
    assert saved["isSmash"] is False
    assert saved["saving"] is True


@pytest.mark.e2e
def test_badminton_yui_smashes_reachable_high_shuttle(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate("Math.random = () => 0")
    page.evaluate("window.BadmintonDemo.setDuelDifficulty('max')")
    page.evaluate("window.BadmintonDemo._debugSetDuelScore({ playerScore: 4, nekoScore: 0, rallyHits: 7 })")
    setup = page.evaluate(
        """() => {
          const contact = window.BadmintonDemo._debugGetYuiRacketContactPoint();
          const screenY = contact.y - contact.reachY * 0.84;
          const courtY = contact.x;
          const id = window.BadmintonDemo._debugSetPlayerShuttleForYuiReturnBall({
            x: courtY,
            prevX: courtY - 18,
            courtY,
            prevCourtY: courtY - 18,
            y: screenY,
            prevY: screenY,
            z: Math.max(0, 450 - screenY),
            prevZ: Math.max(0, 450 - screenY),
            vx: 36,
            vCourtY: 36,
            vy: 0,
            vz: 0
          });
          return { id, contact, screenY, shuttleZ: Math.max(0, 450 - screenY) };
        }"""
    )
    assert setup["shuttleZ"] > 64

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          const smashing = !!document.querySelector('.neko-avatar-container[data-court-avatar="opponent"].smashing');
          return state && state.pendingSwing && state.pendingSwing.shooter === 'neko' &&
            state.pendingSwing.isSmash === true && state.pendingSwing.smashQuality > 0.52 && smashing;
        }""",
        timeout=3000,
    )
    smashed = page.evaluate("window.BadmintonDemo.getState()")
    assert smashed["pendingSwing"]["shooter"] == "neko"
    assert smashed["pendingSwing"]["isSmash"] is True
    assert smashed["pendingSwing"]["smashQuality"] > 0.52


@pytest.mark.e2e
def test_badminton_yui_chases_crossed_shuttle_without_stalling(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    setup = page.evaluate(
        """() => {
          const contact = window.BadmintonDemo._debugGetYuiRacketContactPoint();
          const state = window.BadmintonDemo.getState();
          const targetX = Math.min(842, contact.x + 110);
          const id = window.BadmintonDemo._debugSetPlayerShuttleForYuiReturnBall({
            x: targetX,
            prevX: targetX - 6,
            courtY: targetX,
            prevCourtY: targetX - 6,
            y: contact.y,
            prevY: contact.y,
            vx: 0,
            vCourtY: 0
          });
          return {
            id,
            targetX,
            before: state.yuiCourt,
            contact
          };
        }"""
    )

    page.wait_for_timeout(180)
    moved = page.evaluate(
        """() => {
          const state = window.BadmintonDemo.getState();
          const contact = window.BadmintonDemo._debugGetYuiRacketContactPoint();
          return { yuiCourt: state.yuiCourt, contact };
        }"""
    )

    assert moved["yuiCourt"]["x"] > setup["before"]["x"] + 42
    assert abs(setup["targetX"] - moved["contact"]["x"]) < abs(setup["targetX"] - setup["contact"]["x"])


@pytest.mark.e2e
def test_badminton_duel_yui_valid_landing_scores_for_yui(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate("window.BadmintonDemo._debugFinishShot(true, 'line_in', { shooter: 'neko' })")
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.duel && state.duel.neko_score === 1 &&
            state.duel.player_misses === 1;
        }""",
        timeout=4000,
    )
    scored = page.evaluate("window.BadmintonDemo.getState()")
    assert scored["duel"]["neko_score"] == 1
    assert scored["duel"]["player_misses"] == 1
    assert scored["duel"]["neko_misses"] == 0
    assert scored["attemptsResults"][-1]["point_winner"] == "neko"


@pytest.mark.e2e
def test_badminton_duel_yui_backline_out_scores_for_player(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          window.BadmintonDemo._debugSetAwaitingPlayerReturnBall({
            id: 901,
            x: 25,
            y: 430,
            prevX: 30,
            prevY: 430,
            courtY: 25,
            prevCourtY: 30,
            z: 20,
            prevZ: 20,
            vx: -24,
            vy: 0,
            vCourtY: -24,
            vz: 0,
            radius: 18,
            shooter: 'neko',
            direction: -1,
            crossedNet: true,
            resolved: false,
            awaitingReturnBy: 'player',
            returnDeadlineAt: performance.now() + 2400,
            groundedReturnAt: 0,
            angle: 54,
            power: 56,
            trail: []
          });
        }"""
    )

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.duel && state.duel.player_score === 1;
        }""",
        timeout=4000,
    )
    scored = page.evaluate("window.BadmintonDemo.getState()")
    assert scored["duel"]["player_score"] == 1
    assert scored["duel"]["neko_score"] == 0
    assert scored["duel"]["neko_misses"] == 1
    assert scored["duel"]["player_misses"] == 0
    assert scored["attemptsResults"][-1]["shooter"] == "neko"
    assert scored["attemptsResults"][-1]["shot_type"] == "out"
    assert scored["attemptsResults"][-1]["point_winner"] == "player"


@pytest.mark.e2e
def test_badminton_yui_awaiting_return_still_hits_midcourt_net(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          window.BadmintonDemo._debugSetAwaitingPlayerReturnBall({
            id: 902,
            x: 466,
            y: 358,
            prevX: 470,
            prevY: 358,
            courtY: 466,
            prevCourtY: 470,
            z: 92,
            prevZ: 92,
            vx: -360,
            vy: 0,
            vCourtY: -360,
            vz: 0,
            radius: 18,
            shooter: 'neko',
            direction: -1,
            crossedNet: false,
            resolved: false,
            awaitingReturnBy: 'player',
            returnDeadlineAt: performance.now() + 2400,
            groundedReturnAt: 0,
            angle: 43,
            power: 52,
            trail: []
          });
        }"""
    )

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          if (!state || !state.currentShuttle || !state.currentShuttle.hitNet) return false;
          window.__bdYuiAwaitingNetSample = {
            hitNet: state.currentShuttle.hitNet,
            crossedNet: state.currentShuttle.crossedNet,
            receivingReturn: state.receivingReturn,
            incomingReturnInReach: state.incomingReturnInReach,
            canControlShot: state.canControlShot,
            attemptsResultsLength: state.attemptsResults.length,
            netEffect: window.__badmintonNetEffectDebug || null,
            vx: state.currentShuttle.vx,
            vy: state.currentShuttle.vy
          };
          return true;
        }""",
        timeout=2000,
    )
    netted = page.evaluate("window.__bdYuiAwaitingNetSample")
    assert netted["hitNet"] is True
    assert netted["crossedNet"] is False
    assert netted["receivingReturn"] is True
    assert netted["incomingReturnInReach"] is False
    assert netted["canControlShot"] is False
    assert netted["attemptsResultsLength"] == 0
    assert netted["netEffect"] is not None
    assert netted["netEffect"]["count"] >= 1
    assert netted["netEffect"]["strength"] > 0
    assert abs(netted["vx"]) < 360
    assert netted["vy"] > 0

    page.wait_for_timeout(180)
    after_net_contact = page.evaluate("window.BadmintonDemo.getState()")
    assert after_net_contact["attemptsResults"] == []
    assert after_net_contact["currentShuttle"] is not None
    assert after_net_contact["currentShuttle"]["hitNet"] is True

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.attemptsResults.length > 0;
        }""",
        timeout=3500,
    )
    resolved = page.evaluate("window.BadmintonDemo.getState()")
    assert resolved["attemptsResults"][-1]["shooter"] == "neko"
    assert resolved["attemptsResults"][-1]["shot_type"] == "net"
    assert resolved["attemptsResults"][-1]["point_winner"] == "player"
    assert resolved["duel"]["player_score"] == 1
    assert resolved["duel"]["neko_score"] == 0
    assert resolved["duel"]["neko_misses"] == 1
    assert resolved["duel"]["player_misses"] == 0

    page.evaluate("window.BadmintonDemo.resetGame()")
    page.wait_for_function(
        """() => {
          return window.__badmintonNetEffectDebug
            && window.__badmintonNetEffectDebug.count === 0;
        }"""
    )


@pytest.mark.e2e
def test_badminton_yui_close_net_hit_below_tape_scores_for_player(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          window.BadmintonDemo._debugSetAwaitingPlayerReturnBall({
            id: 905,
            x: 466,
            y: 346,
            prevX: 470,
            prevY: 346,
            courtY: 466,
            prevCourtY: 470,
            z: 104,
            prevZ: 104,
            vx: -360,
            vy: 0,
            vCourtY: -360,
            vz: 0,
            radius: 18,
            shooter: 'neko',
            direction: -1,
            crossedNet: false,
            resolved: false,
            awaitingReturnBy: 'player',
            returnDeadlineAt: performance.now() + 2400,
            groundedReturnAt: 0,
            angle: 43,
            power: 52,
            trail: []
          });
        }"""
    )

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          if (!state || !state.currentShuttle || !state.currentShuttle.hitNet) return false;
          window.__bdYuiCloseNetHitSample = {
            hitNet: state.currentShuttle.hitNet,
            crossedNet: state.currentShuttle.crossedNet,
            netCarryToTargetSide: state.currentShuttle.netCarryToTargetSide,
            y: state.currentShuttle.y,
            z: state.currentShuttle.z
          };
          return true;
        }""",
        timeout=2000,
    )
    netted = page.evaluate("window.__bdYuiCloseNetHitSample")
    assert netted["hitNet"] is True
    assert netted["crossedNet"] is False
    assert netted["netCarryToTargetSide"] is False
    assert netted["z"] < 116

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.attemptsResults.length > 0;
        }""",
        timeout=3500,
    )
    resolved = page.evaluate("window.BadmintonDemo.getState()")
    result = resolved["attemptsResults"][-1]
    assert result["shooter"] == "neko"
    assert result["shot_type"] == "net"
    assert result["point_winner"] == "player"
    assert resolved["duel"]["player_score"] == 1
    assert resolved["duel"]["neko_score"] == 0
    assert resolved["duel"]["neko_misses"] == 1
    assert resolved["duel"]["player_misses"] == 0


@pytest.mark.e2e
def test_badminton_yui_low_midcourt_blocked_shot_scores_for_player(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          window.BadmintonDemo._debugSetAwaitingPlayerReturnBall({
            id: 906,
            x: 466,
            y: 406,
            prevX: 470,
            prevY: 406,
            courtY: 466,
            prevCourtY: 470,
            z: 44,
            prevZ: 44,
            vx: -360,
            vy: 0,
            vCourtY: -360,
            vz: 0,
            radius: 18,
            shooter: 'neko',
            direction: -1,
            crossedNet: false,
            legalNetClearance: false,
            resolved: false,
            awaitingReturnBy: 'player',
            returnDeadlineAt: performance.now() + 2400,
            groundedReturnAt: 0,
            angle: 32,
            power: 52,
            trail: []
          });
        }"""
    )

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          if (!state || !state.currentShuttle || !state.currentShuttle.hitNet) return false;
          window.__bdYuiLowBlockedNetSample = {
            hitNet: state.currentShuttle.hitNet,
            crossedNet: state.currentShuttle.crossedNet,
            legalNetClearance: state.currentShuttle.legalNetClearance,
            netCarryToTargetSide: state.currentShuttle.netCarryToTargetSide,
            z: state.currentShuttle.z
          };
          return true;
        }""",
        timeout=2000,
    )
    netted = page.evaluate("window.__bdYuiLowBlockedNetSample")
    assert netted["hitNet"] is True
    assert netted["crossedNet"] is False
    assert netted["legalNetClearance"] is False
    assert netted["netCarryToTargetSide"] is False

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.attemptsResults.length > 0;
        }""",
        timeout=3500,
    )
    resolved = page.evaluate("window.BadmintonDemo.getState()")
    result = resolved["attemptsResults"][-1]
    assert result["shooter"] == "neko"
    assert result["shot_type"] == "net"
    assert result["point_winner"] == "player"
    assert resolved["duel"]["player_score"] == 1
    assert resolved["duel"]["neko_score"] == 0
    assert resolved["duel"]["neko_misses"] == 1
    assert resolved["duel"]["player_misses"] == 0


@pytest.mark.e2e
def test_badminton_yui_net_hit_cannot_become_player_timeout_point(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          window.BadmintonDemo._debugSetAwaitingPlayerReturnBall({
            id: 907,
            x: 466,
            y: 406,
            prevX: 470,
            prevY: 406,
            courtY: 466,
            prevCourtY: 470,
            z: 44,
            prevZ: 44,
            vx: -360,
            vy: 0,
            vCourtY: -360,
            vz: 0,
            radius: 18,
            shooter: 'neko',
            direction: -1,
            crossedNet: false,
            legalNetClearance: false,
            resolved: false,
            awaitingReturnBy: 'player',
            returnDeadlineAt: performance.now() + 35,
            groundedReturnAt: 0,
            angle: 32,
            power: 52,
            trail: []
          });
        }"""
    )

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.currentShuttle && state.currentShuttle.hitNet;
        }""",
        timeout=2000,
    )
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.attemptsResults.length > 0;
        }""",
        timeout=3500,
    )
    resolved = page.evaluate("window.BadmintonDemo.getState()")
    result = resolved["attemptsResults"][-1]
    assert result["shooter"] == "neko"
    assert result["shot_type"] == "net"
    assert result["point_winner"] == "player"
    assert resolved["duel"]["player_score"] == 1
    assert resolved["duel"]["neko_score"] == 0


@pytest.mark.e2e
def test_badminton_net_effect_debug_global_stays_debug_only(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel", debug=False, wait_loading=False)

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          window.BadmintonDemo._debugSetAwaitingPlayerReturnBall({
            id: 904,
            x: 466,
            y: 358,
            prevX: 470,
            prevY: 358,
            courtY: 466,
            prevCourtY: 470,
            z: 92,
            prevZ: 92,
            vx: -360,
            vy: 0,
            vCourtY: -360,
            vz: 0,
            radius: 18,
            shooter: 'neko',
            direction: -1,
            crossedNet: false,
            resolved: false,
            awaitingReturnBy: 'player',
            returnDeadlineAt: performance.now() + 2400,
            groundedReturnAt: 0,
            angle: 43,
            power: 52,
            trail: []
          });
        }"""
    )

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.currentShuttle && state.currentShuttle.hitNet;
        }""",
        timeout=2000,
    )
    assert page.evaluate("typeof window.__badmintonNetEffectDebug") == "undefined"


@pytest.mark.e2e
def test_badminton_yui_return_above_visible_net_does_not_hit_midcourt_net(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          window.BadmintonDemo._debugSetAwaitingPlayerReturnBall({
            id: 903,
            x: 466,
            y: 280,
            prevX: 470,
            prevY: 280,
            courtY: 466,
            prevCourtY: 470,
            z: 170,
            prevZ: 170,
            vx: -360,
            vy: 0,
            vCourtY: -360,
            radius: 18,
            shooter: 'neko',
            direction: -1,
            crossedNet: false,
            resolved: false,
            awaitingReturnBy: 'player',
            returnDeadlineAt: performance.now() + 2400,
            groundedReturnAt: 0,
            angle: 43,
            power: 52,
            trail: []
          });
        }"""
    )

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.currentShuttle && state.currentShuttle.crossedNet;
        }""",
        timeout=2000,
    )
    above_net = page.evaluate("window.BadmintonDemo.getState()")
    assert above_net["currentShuttle"]["hitNet"] is False
    assert above_net["currentShuttle"]["screenY"] < 316


@pytest.mark.e2e
def test_badminton_player_return_requires_shuttle_to_enter_character_reach(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          const state = window.BadmintonDemo.getState();
          const contact = state.playerRacketContact || { x: state.playerCourt.x + 42, y: state.playerCourt.y - 76 };
          window.BadmintonDemo._debugSetAwaitingPlayerReturnBall({
            x: contact.x + 180,
            y: contact.y - 180,
            prevX: contact.x + 184,
            prevY: contact.y - 180,
            vx: 0,
            vy: 0,
            returnDeadlineAt: performance.now() + 5000
          });
        }"""
    )

    far = page.evaluate("window.BadmintonDemo.getState()")
    assert far["receivingReturn"] is True
    assert far["incomingReturnInReach"] is False
    assert far["canControlShot"] is False

    page.evaluate("window.BadmintonDemo.shoot()")
    blocked = page.evaluate("window.BadmintonDemo.getState()")
    assert blocked["state"] == "ready"
    assert blocked["pendingSwing"] is None
    assert blocked["currentShuttle"]["awaitingReturnBy"] == "player"

    page.evaluate(
        """() => {
          const state = window.BadmintonDemo.getState();
          const contact = state.playerRacketContact || { x: state.playerCourt.x + 42, y: state.playerCourt.y - 76 };
          window.BadmintonDemo._debugSetAwaitingPlayerReturnBall({
            x: contact.x,
            y: contact.y,
            prevX: contact.x + 4,
            prevY: contact.y,
            vx: -360,
            vy: -80,
            returnDeadlineAt: performance.now() + 5000
          });
        }"""
    )
    near = page.evaluate("window.BadmintonDemo.getState()")
    assert near["receivingReturn"] is True
    assert near["incomingReturnInReach"] is True
    assert near["canControlShot"] is True

    viewport = page.viewport_size or {"width": 1280, "height": 720}
    page.mouse.move(viewport["width"] * 0.5, viewport["height"] * 0.9)
    page.evaluate("window.BadmintonDemo.shoot()")
    swinging = page.evaluate("window.BadmintonDemo.getState()")
    assert swinging["state"] in {"swinging", "in_flight"}
    if swinging["state"] == "swinging":
        assert swinging["pendingSwing"]["shooter"] == "player"
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          if (!state || state.state !== 'in_flight' || !state.currentShuttle || state.currentShuttle.shooter !== 'player') return false;
          window.__bdPlayerReturnSpinSample = {
            id: state.currentShuttle.id,
            spinRate: state.currentShuttle.spinRate,
            angle: state.currentShuttle.angle
          };
          return true;
        }""",
        timeout=2000,
    )
    player_return = page.evaluate("window.__bdPlayerReturnSpinSample")
    assert abs(player_return["spinRate"]) <= 10
    assert player_return["angle"] <= 48


@pytest.mark.e2e
def test_badminton_player_return_hit_cue_only_draws_inside_reach(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    count_cue_pixels = """
      () => {
        const canvas = document.getElementById('aiming-canvas');
        if (!canvas) return 0;
        const ctx = canvas.getContext('2d');
        const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
        let lit = 0;
        for (let i = 3; i < data.length; i += 4) {
          if (data[i] > 20) lit++;
        }
        return lit;
      }
    """

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          const state = window.BadmintonDemo.getState();
          const contact = state.playerRacketContact || { x: state.playerCourt.x + 42, y: state.playerCourt.y - 76 };
          window.BadmintonDemo._debugSetAwaitingPlayerReturnBall({
            x: contact.x + 180,
            y: contact.y - 180,
            prevX: contact.x + 184,
            prevY: contact.y - 180,
            vx: 0,
            vy: 0,
            returnDeadlineAt: performance.now() + 5000
          });
        }"""
    )
    page.wait_for_timeout(80)
    assert page.evaluate(count_cue_pixels) == 0

    page.evaluate(
        """() => {
          const state = window.BadmintonDemo.getState();
          const contact = state.playerRacketContact || { x: state.playerCourt.x + 42, y: state.playerCourt.y - 76 };
          window.BadmintonDemo._debugSetAwaitingPlayerReturnBall({
            x: contact.x,
            y: contact.y,
            prevX: contact.x + 4,
            prevY: contact.y,
            vx: -360,
            vy: -80,
            returnDeadlineAt: performance.now() + 5000
          });
        }"""
    )
    page.wait_for_function(f"({count_cue_pixels})() > 12", timeout=2000)
    assert page.evaluate("window.BadmintonDemo.getState().incomingReturnInReach") is True


@pytest.mark.e2e
def test_badminton_vrm_overlay_does_not_block_player_swing(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready' && window.BadmintonDemo.getState().canControlShot")
    page.wait_for_selector("#player-sensei-vrm-container:not([hidden])")
    box = page.locator("#player-sensei-vrm-container").bounding_box()
    assert box is not None

    page.mouse.move(box["x"] + 10, box["y"] + 10)
    page.mouse.down()
    charging = page.evaluate("window.BadmintonDemo.getState()")
    assert charging["charging"] is True

    page.mouse.up()
    swinging = page.evaluate("window.BadmintonDemo.getState()")
    assert swinging["state"] in {"swinging", "in_flight"}
    if swinging["state"] == "swinging":
        assert swinging["pendingSwing"]["shooter"] == "player"
    else:
        assert swinging["currentShuttle"]["shooter"] == "player"


@pytest.mark.e2e
def test_badminton_allows_player_movement_and_jump_but_blocks_shot_during_yui_turn(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate("window.BadmintonDemo._debugFinishShot(false, 'out')")
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.state === 'neko_thinking' && state.duel.active_shooter === 'neko';
        }"""
    )
    before_move = page.evaluate("window.BadmintonDemo.getState().playerCourt")

    viewport = page.viewport_size or {"width": 1280, "height": 720}
    page.mouse.move(viewport["width"] - 8, 12)
    page.evaluate(
        """() => {
          window.dispatchEvent(new KeyboardEvent('keydown', {
            key: ' ',
            code: 'Space',
            bubbles: true,
            cancelable: true
          }));
        }"""
    )
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          if (!state || !state.playerJump || !state.playerJump.active || state.playerJump.offset <= 0) return false;
          window.__bdYuiTurnJumpSnapshot = state.playerJump;
          return true;
        }""",
        timeout=2000,
    )
    page.mouse.down()
    page.mouse.up()
    page.evaluate("window.BadmintonDemo.shoot()")
    page.wait_for_function(
        """(beforeX) => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.playerCourt &&
            state.playerCourt.targetX > beforeX + 16 &&
            state.playerCourt.x > beforeX + 16;
        }""",
        arg=before_move["targetX"],
    )

    state = page.evaluate("window.BadmintonDemo.getState()")
    jump = page.evaluate("window.__bdYuiTurnJumpSnapshot")
    assert jump["active"] is True
    assert jump["offset"] > 0
    assert state["playerCourt"]["targetX"] > before_move["targetX"]
    assert state["playerCourt"]["x"] > before_move["x"]
    assert state["charging"] is False
    assert not (state["pendingSwing"] and state["pendingSwing"]["shooter"] == "player")
    assert not (state["currentShuttle"] and state["currentShuttle"]["shooter"] == "player")


@pytest.mark.e2e
def test_badminton_yui_cheats_with_item_when_duel_score_is_close(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate("window.BadmintonDemo._debugSetDuelScore({ playerScore: 8, nekoScore: 8, round: 16 })")
    page.evaluate("window.BadmintonDemo._debugForceNextYuiCheat('banana')")
    page.evaluate("window.BadmintonDemo._debugFinishShot(false, 'out')")
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.yuiCheat && state.yuiCheat.items.length === 1 &&
            state.yuiCheat.items[0].kind === 'banana';
        }""",
        timeout=3000,
    )

    state = page.evaluate("window.BadmintonDemo.getState()")
    assert state["duel"]["player_score"] == 8
    assert state["duel"]["neko_score"] == 9
    assert state["yuiCheat"]["last_used_kind"] == "banana"
    assert state["yuiCheat"]["items"][0]["kind"] == "banana"
    assert state["yuiCheat"]["items"][0]["radius"] == 18


@pytest.mark.e2e
def test_badminton_yui_banana_cheat_warns_player_with_bubble(mock_page: Page, running_server: str):
    page = mock_page
    speak_payloads = []

    def capture_speak(route):
        speak_payloads.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true,"audio_sent":true,"speech_id":"e2e-yui-cheat"}',
        )

    page.route(
        "**/api/game/badminton/mirror-assistant",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true}',
        ),
    )
    page.route("**/api/game/badminton/speak", capture_speak)
    _goto_badminton(page, running_server, "duel", debug=True, debug_voice=False)

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          const state = window.BadmintonDemo.getState();
          window.BadmintonDemo._debugSpawnYuiCheat('banana', {
            x: state.playerCourt.x + 92,
            y: state.playerFootY
          });
        }"""
    )
    page.wait_for_function(
        """() => {
          const bubble = document.getElementById('neko-bubble');
          return bubble && /香蕉皮|踩到|这一步|跳过去/.test(bubble.textContent || '');
        }""",
        timeout=1500,
    )
    expect(page.locator("#neko-bubble")).to_be_visible()
    assert page.locator("#neko-bubble").get_attribute("data-variant") == "yui-cheat"
    assert page.evaluate(
        """() => window.getComputedStyle(document.getElementById('neko-bubble')).backgroundColor"""
    ) == "rgb(207, 234, 255)"
    item_payload = _wait_for_badminton_speak_payload(page, speak_payloads, "yui_cheat_item")
    assert re.search(r"香蕉皮|踩到|这一步|跳过去", item_payload["line"])
    assert item_payload["event"]["label"] == "yui_cheat_banana"
    assert item_payload["event"]["item_kind"] == "banana"
    assert item_payload["event"]["force_voice_in_debug"] is True
    assert item_payload["event"]["voice_deadline_ms"] == 6200
    assert item_payload["interrupt_audio"] is True
    page.evaluate("window.BadmintonDemo.showBubble('普通回合台词', { mood: 'happy' })")
    page.wait_for_timeout(120)
    expect(page.locator("#neko-bubble")).to_contain_text(re.compile("香蕉皮|踩到|这一步|跳过去"))


@pytest.mark.e2e
def test_badminton_yui_voice_defer_releases_stuck_playback_state(
    mock_page: Page, running_server: str
):
    page = mock_page
    speak_payloads = []

    def capture_speak(route):
        speak_payloads.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true,"audio_sent":true,"speech_id":"e2e-yui-cheat-after-defer"}',
        )

    page.route(
        "**/api/game/badminton/mirror-assistant",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true}',
        ),
    )
    page.route("**/api/game/badminton/speak", capture_speak)
    _goto_badminton(page, running_server, "duel", debug=True, debug_voice=False)

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          localStorage.setItem('neko_speech_playback_state', JSON.stringify({
            type: 'speech_playback_state',
            active: true,
            remainingSeconds: 30,
            updatedAt: Date.now(),
            audioContextState: 'suspended',
            speechId: 'stuck-main-app-speech'
          }));
          const state = window.BadmintonDemo.getState();
          window.BadmintonDemo._debugSpawnYuiCheat('banana', {
            x: state.playerCourt.x + 92,
            y: state.playerFootY
          });
        }"""
    )
    page.wait_for_function(
        """() => {
          const bubble = document.getElementById('neko-bubble');
          return bubble && bubble.dataset.variant === 'yui-cheat';
        }""",
        timeout=1500,
    )

    item_payload = _wait_for_badminton_speak_payload(page, speak_payloads, "yui_cheat_item")
    assert item_payload["interrupt_audio"] is True


@pytest.mark.e2e
def test_badminton_yui_cheat_voice_does_not_wait_for_mirror_assistant(
    mock_page: Page, running_server: str
):
    page = mock_page
    speak_payloads = []
    page.add_init_script(
        """
        (() => {
          const originalFetch = window.fetch.bind(window);
          window.__badmintonBlockedMirrorAssistant = 0;
          window.fetch = (input, init) => {
            const url = String(input && input.url ? input.url : input || '');
            if (url.indexOf('/api/game/badminton/mirror-assistant') !== -1) {
              window.__badmintonBlockedMirrorAssistant += 1;
              return new Promise(() => {});
            }
            return originalFetch(input, init);
          };
        })();
        """
    )

    def capture_speak(route):
        speak_payloads.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true,"audio_sent":true,"speech_id":"e2e-yui-cheat-no-mirror-wait"}',
        )

    page.route("**/api/game/badminton/speak", capture_speak)
    _goto_badminton(page, running_server, "duel", debug=True, debug_voice=False)

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          const state = window.BadmintonDemo.getState();
          window.BadmintonDemo._debugSpawnYuiCheat('banana', {
            x: state.playerCourt.x + 92,
            y: state.playerFootY
          });
        }"""
    )

    item_payload = _wait_for_badminton_speak_payload(page, speak_payloads, "yui_cheat_item")
    assert page.evaluate("window.__badmintonBlockedMirrorAssistant") >= 1
    assert item_payload["interrupt_audio"] is True


@pytest.mark.e2e
def test_badminton_short_deadline_voice_ignores_stuck_playback_state(
    mock_page: Page, running_server: str
):
    page = mock_page
    speak_payloads = []

    def capture_speak(route):
        speak_payloads.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true,"audio_sent":true,"speech_id":"e2e-short-deadline"}',
        )

    page.route(
        "**/api/game/badminton/mirror-assistant",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true}',
        ),
    )
    page.route("**/api/game/badminton/speak", capture_speak)
    _goto_badminton(page, running_server, "duel", debug=True, debug_voice=False)

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          localStorage.setItem('neko_speech_playback_state', JSON.stringify({
            type: 'speech_playback_state',
            active: true,
            remainingSeconds: 30,
            updatedAt: Date.now(),
            audioContextState: 'suspended',
            speechId: 'stuck-main-app-speech'
          }));
          window.BadmintonDemo.say('short deadline voice', {
            mood: 'happy',
            event: {
              kind: 'long_aim',
              label: 'short_deadline_regression',
              force_voice_in_debug: true,
              voice_deadline_ms: 900
            }
          });
        }"""
    )

    deadline = time.time() + 4
    while time.time() < deadline and not speak_payloads:
        page.wait_for_timeout(25)

    assert speak_payloads
    assert speak_payloads[-1]["line"] == "short deadline voice"
    assert speak_payloads[-1]["event"]["label"] == "short_deadline_regression"


@pytest.mark.e2e
def test_badminton_project_voice_unobserved_does_not_start_local_speech(
    mock_page: Page, running_server: str
):
    page = mock_page
    speak_payloads = []
    page.add_init_script(
        """
        (() => {
          window.__spokenText = [];
          window.SpeechSynthesisUtterance = function (text) { this.text = text; };
          Object.defineProperty(window, 'speechSynthesis', {
            configurable: true,
            value: {
              getVoices: () => [],
              cancel: () => {},
              speak: (utterance) => {
                window.__spokenText.push(utterance && utterance.text ? utterance.text : String(utterance || ''));
              }
            }
          });
        })();
        """
    )

    def capture_speak(route):
        speak_payloads.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true,"audio_sent":true,"audio_queued":true,"speech_id":"e2e-unobserved-project-voice"}',
        )

    page.route(
        "**/api/game/badminton/mirror-assistant",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true}',
        ),
    )
    page.route("**/api/game/badminton/speak", capture_speak)
    _goto_badminton(page, running_server, "duel", debug=True, debug_voice=False)

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          window.BadmintonDemo.say('unobserved project voice', {
            mood: 'happy',
            event: {
              kind: 'long_aim',
              label: 'unobserved_project_voice_regression',
              force_voice_in_debug: true,
              voice_deadline_ms: 2600
            }
          });
        }"""
    )

    page.wait_for_function(
        """() => {
          const s = window.BadmintonDemo.getState();
          return !!(
            s &&
            s.voice &&
            s.voice.lastResult &&
            s.voice.lastResult.speech_id === 'e2e-unobserved-project-voice' &&
            s.voice.lastFallbackReason === ''
          );
        }""",
        timeout=3000,
    )
    assert speak_payloads
    state = page.evaluate("window.BadmintonDemo.getState()")
    assert speak_payloads[-1]["line"] == "unobserved project voice"
    assert state["voice"]["lastResult"]["speech_id"] == "e2e-unobserved-project-voice"
    assert state["voice"]["lastFallbackReason"] == ""
    assert page.evaluate("window.__spokenText") == []


@pytest.mark.e2e
def test_badminton_voice_selection_mutes_ordinary_hint_but_keeps_bubble(
    mock_page: Page, running_server: str
):
    page = mock_page
    speak_payloads = []

    page.route(
        "**/api/game/badminton/mirror-assistant",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true}',
        ),
    )

    def capture_speak(route):
        speak_payloads.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true,"audio_sent":true,"audio_queued":true,"speech_id":"ordinary-hint"}',
        )

    page.route("**/api/game/badminton/speak", capture_speak)
    _goto_badminton(page, running_server, "duel", debug=True, debug_voice=True)

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    speak_payloads.clear()
    page.evaluate(
        """() => {
          window.BadmintonDemo.say('ordinary hint muted', {
            mood: 'happy',
            event: {
              kind: 'long_aim',
              label: 'ordinary_hint',
              voice_deadline_ms: 2600
            }
          });
        }"""
    )

    expect(page.locator("#neko-bubble")).to_contain_text("ordinary hint muted")
    page.wait_for_timeout(700)
    assert speak_payloads == []
    state = page.evaluate("window.BadmintonDemo.getState()")
    assert state["voice"]["lastResult"]["muted"] is True
    assert state["voice"]["lastMutedReason"] == "voice_event_not_selected"


@pytest.mark.e2e
def test_badminton_banana_peel_flips_and_slows_grounded_player(mock_page: Page, running_server: str):
    page = mock_page
    speak_payloads = []

    def capture_speak(route):
        speak_payloads.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true,"audio_sent":true,"speech_id":"e2e-yui-cheat-hit"}',
        )

    page.route(
        "**/api/game/badminton/mirror-assistant",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true}',
        ),
    )
    page.route("**/api/game/badminton/speak", capture_speak)
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => window.BadmintonDemo._debugSetAwaitingPlayerReturnBall({
          x: 220,
          prevX: 236,
          y: 300,
          prevY: 300,
          vx: -12,
          vCourtY: -12
        })"""
    )
    before = page.evaluate("window.BadmintonDemo.getState().playerCourt")
    page.evaluate(
        """() => {
          const state = window.BadmintonDemo.getState();
          window.BadmintonDemo._debugSpawnYuiCheat('banana', { x: state.playerCourt.x, y: state.playerFootY });
        }"""
    )
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.yuiCheat && state.yuiCheat.player_effect &&
            state.yuiCheat.player_effect.slipping === true;
        }""",
        timeout=2000,
    )
    movement = page.evaluate(
        """() => new Promise((resolve) => {
          window.dispatchEvent(new MouseEvent('mousemove', {
            clientX: window.innerWidth - 8,
            clientY: window.innerHeight * 0.5,
            bubbles: true
          }));
          const afterInput = window.BadmintonDemo.getState();
          const samples = [];
          const startedAt = performance.now();
          function sample() {
            const state = window.BadmintonDemo.getState();
            samples.push(state.yuiCheat.player_effect.spin_angle);
            if (performance.now() - startedAt >= 560) {
              resolve({
                afterInput,
                spinSamples: samples,
                elapsedMs: performance.now() - startedAt,
                finalState: window.BadmintonDemo.getState()
              });
              return;
            }
            setTimeout(sample, 40);
          }
          sample();
        })"""
    )

    state = movement["finalState"]
    assert state["yuiCheat"]["items"] == []
    assert movement["afterInput"]["playerCourt"]["targetX"] > before["targetX"] + 16
    assert movement["afterInput"]["yuiCheat"]["player_effect"]["slipping"] is True
    assert movement["afterInput"]["yuiCheat"]["player_effect"]["speed_multiplier"] == pytest.approx(0.22)
    hit_payload = _wait_for_badminton_speak_payload(page, speak_payloads, "yui_cheat_hit")
    assert hit_payload["event"]["label"] == "yui_cheat_hit_banana"
    assert hit_payload["event"]["item_kind"] == "banana"
    assert hit_payload["interrupt_audio"] is True
    assert max(movement["spinSamples"]) > 5.2
    slow_distance = state["playerCourt"]["x"] - before["x"]
    expected_slow_distance = (
        1040
        * movement["afterInput"]["yuiCheat"]["player_effect"]["speed_multiplier"]
        * (movement["elapsedMs"] / 1000)
    )
    assert slow_distance <= expected_slow_distance + 18


@pytest.mark.e2e
def test_badminton_default_banana_spawns_on_player_foot_line_with_random_court_x_without_instant_slip(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    spawned = page.evaluate(
        """() => {
          const originalRandom = Math.random;
          Math.random = () => 0.999;
          try {
            const state = window.BadmintonDemo.getState();
            const item = window.BadmintonDemo._debugSpawnYuiCheat('banana');
            return {
              itemX: item.x,
              itemY: item.y,
              playerX: state.playerCourt.x,
              playerCourtY: state.playerCourt.y,
              playerFootY: state.playerFootY,
              courtLeft: 80,
              netX: 460,
              courtBottom: 520
            };
          } finally {
            Math.random = originalRandom;
          }
        }"""
    )
    page.wait_for_timeout(350)

    state = page.evaluate("window.BadmintonDemo.getState()")
    assert spawned["courtLeft"] < spawned["itemX"] < spawned["netX"]
    assert abs(spawned["itemX"] - spawned["playerX"]) > 20
    assert spawned["playerCourtY"] - 40 <= spawned["itemY"] <= spawned["courtBottom"]
    assert abs(spawned["itemY"] - spawned["playerFootY"]) <= 1
    assert state["yuiCheat"]["items"][0]["kind"] == "banana"
    assert state["yuiCheat"]["player_effect"]["slipping"] is False


@pytest.mark.e2e
def test_badminton_default_banana_spawns_on_ground_foot_line_while_player_jumps(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    spawned = page.evaluate(
        """() => new Promise((resolve) => {
          window.BadmintonDemo._debugSetAwaitingPlayerReturnBall();
          window.BadmintonDemo.jump();
          function waitForJump() {
            const jumpingState = window.BadmintonDemo.getState();
            if (jumpingState.playerJump && jumpingState.playerJump.offset > 18) {
              const originalRandom = Math.random;
              Math.random = () => 0.999;
              try {
                const item = window.BadmintonDemo._debugSpawnYuiCheat('banana');
                const state = window.BadmintonDemo.getState();
                resolve({
                  itemY: item.y,
                  playerFootY: state.playerFootY,
                  playerGroundFootY: state.playerGroundFootY,
                  jumpOffset: state.playerJump.offset
                });
              } finally {
                Math.random = originalRandom;
              }
              return;
            }
            requestAnimationFrame(waitForJump);
          }
          waitForJump();
        })"""
    )

    assert spawned["jumpOffset"] > 18
    assert spawned["playerFootY"] < spawned["playerGroundFootY"] - 12
    assert abs(spawned["itemY"] - spawned["playerGroundFootY"]) <= 1


@pytest.mark.e2e
def test_badminton_player_can_jump_over_banana_peel(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          window.BadmintonDemo._debugSetAwaitingPlayerReturnBall();
          const state = window.BadmintonDemo.getState();
          window.BadmintonDemo._debugSpawnYuiCheat('banana', { x: state.playerCourt.x, y: state.playerFootY });
          window.BadmintonDemo.jump();
        }"""
    )
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.playerJump && state.playerJump.offset > 8;
        }""",
        timeout=2000,
    )
    page.wait_for_timeout(350)

    state = page.evaluate("window.BadmintonDemo.getState()")
    assert state["yuiCheat"]["items"][0]["kind"] == "banana"
    assert state["yuiCheat"]["player_effect"]["slipping"] is False


@pytest.mark.e2e
def test_badminton_octopus_ink_covers_player_screen(mock_page: Page, running_server: str):
    page = mock_page
    speak_payloads = []

    def capture_speak(route):
        speak_payloads.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true,"audio_sent":true,"speech_id":"e2e-yui-cheat-octopus"}',
        )

    page.route(
        "**/api/game/badminton/mirror-assistant",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true}',
        ),
    )
    page.route("**/api/game/badminton/speak", capture_speak)
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate("window.BadmintonDemo._debugSpawnYuiCheat('octopus')")
    expect(page.locator("#neko-bubble")).to_be_visible(timeout=1200)
    expect(page.locator("#neko-bubble")).to_contain_text(re.compile("墨汁|视野|眨眼|看不清"))
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.yuiCheat && state.yuiCheat.ink &&
            state.yuiCheat.ink.active === true && state.yuiCheat.ink.alpha > 0;
        }""",
        timeout=2000,
    )

    state = page.evaluate("window.BadmintonDemo.getState()")
    assert state["yuiCheat"]["last_used_kind"] == "octopus"
    assert state["yuiCheat"]["ink"]["active"] is True
    assert state["yuiCheat"]["ink"]["alpha"] >= 0.75
    item_payload = _wait_for_badminton_speak_payload(page, speak_payloads, "yui_cheat_item")
    assert item_payload["event"]["label"] == "yui_cheat_octopus"
    assert item_payload["event"]["item_kind"] == "octopus"
    assert item_payload["interrupt_audio"] is True


@pytest.mark.e2e
@pytest.mark.parametrize("kind", ["banana", "octopus"])
def test_badminton_yui_taunts_after_cheat_hit_scores_point(
    mock_page: Page, running_server: str, kind: str
):
    page = mock_page
    speak_payloads = []

    def capture_speak(route):
        speak_payloads.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true,"audio_sent":true,"speech_id":"e2e-yui-cheat-score"}',
        )

    page.route(
        "**/api/game/badminton/mirror-assistant",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true}',
        ),
    )
    page.route("**/api/game/badminton/speak", capture_speak)
    _goto_badminton(page, running_server, "duel", debug=False)

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    if kind == "banana":
        page.evaluate(
            """() => {
              const state = window.BadmintonDemo.getState();
              window.BadmintonDemo._debugSpawnYuiCheat('banana', {
                x: state.playerCourt.x,
                y: state.playerFootY
              });
            }"""
        )
        page.wait_for_function(
            """() => {
              const state = window.BadmintonDemo && window.BadmintonDemo.getState();
              return state && state.yuiCheat && state.yuiCheat.player_effect &&
                state.yuiCheat.player_effect.slipping === true;
            }""",
            timeout=2000,
        )
    else:
        page.evaluate("window.BadmintonDemo._debugSpawnYuiCheat('octopus')")
        page.wait_for_function(
            """() => {
              const state = window.BadmintonDemo && window.BadmintonDemo.getState();
              return state && state.yuiCheat && state.yuiCheat.ink &&
                state.yuiCheat.ink.active === true && state.yuiCheat.ink.alpha > 0;
            }""",
            timeout=2000,
        )

    speak_payloads.clear()
    page.evaluate("window.BadmintonDemo._debugFinishShot(false, 'out', { shooter: 'player' })")
    page.wait_for_function(
        """() => {
          const bubble = document.getElementById('neko-bubble');
          return bubble && bubble.dataset.variant === 'yui-cheat-score' &&
            (bubble.textContent || '').trim().length > 0;
        }""",
        timeout=2000,
    )

    score_payload = _wait_for_badminton_speak_payload(
        page, speak_payloads, "yui_cheat_score", timeout_ms=6000
    )
    event = score_payload["event"]
    assert event["kind"] == "yui_cheat_score"
    assert event["label"] == f"yui_cheat_score_{kind}"
    assert event["item_kind"] == kind
    assert score_payload["interrupt_audio"] is True


@pytest.mark.e2e
def test_badminton_duel_valid_landing_scores_for_shooter(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate("window.BadmintonDemo._debugFinishShot(true, 'line_in')")
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.state === 'ready' && state.duel.active_shooter === 'player' &&
            state.duel.player_score === 1;
        }"""
    )
    returned = page.evaluate("window.BadmintonDemo.getState()")
    assert returned["score"] == 1
    assert returned["lastShotScore"] == 1
    assert returned["duel"]["player_score"] == 1
    assert returned["duel"]["neko_score"] == 0
    assert returned["duel"]["neko_misses"] == 1
    assert returned["duel"]["rally_hits"] == 0

    page.evaluate("window.BadmintonDemo.resetGame()")
    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate("window.BadmintonDemo._debugFinishShot(false, 'out')")
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.state === 'neko_thinking' && state.duel.active_shooter === 'neko';
        }"""
    )
    missed = page.evaluate("window.BadmintonDemo.getState()")
    assert missed["score"] == 0
    assert missed["duel"]["player_score"] == 0
    assert missed["duel"]["neko_score"] == 1
    assert missed["duel"]["player_misses"] == 1
    assert missed["duel"]["rally_hits"] == 0


@pytest.mark.e2e
def test_badminton_player_net_touch_landing_on_yui_side_scores_for_player(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          window.BadmintonDemo._debugSetPlayerShuttleForYuiReturnBall({
            x: 548,
            prevX: 536,
            courtY: 548,
            prevCourtY: 536,
            y: 448,
            prevY: 438,
            z: 2,
            prevZ: 12,
            vx: 28,
            vy: 130,
            vCourtY: 28,
            vz: -130,
            shooter: 'player',
            direction: 1,
            crossedNet: true,
            netCarryToTargetSide: true,
            hitNet: true,
            netTouched: true,
            netContactHoldUntil: 0,
            resolved: false
          });
        }"""
    )
    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.attemptsResults.length > 0;
        }""",
        timeout=2500,
    )

    resolved = page.evaluate("window.BadmintonDemo.getState()")
    result = resolved["attemptsResults"][-1]
    assert result["shooter"] == "player"
    assert result["shot_type"] == "net_touch"
    assert result["point_winner"] == "player"
    assert resolved["duel"]["player_score"] == 1
    assert resolved["duel"]["neko_score"] == 0
    assert resolved["duel"]["neko_misses"] == 1
    assert resolved["duel"]["player_misses"] == 0


@pytest.mark.e2e
def test_badminton_player_net_touch_landing_on_player_side_scores_for_yui(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    page.wait_for_function("window.BadmintonDemo.getState().state === 'ready'")
    page.evaluate(
        """() => {
          window.BadmintonDemo._debugSetPlayerShuttleForYuiReturnBall({
            x: 330,
            prevX: 332,
            courtY: 330,
            prevCourtY: 332,
            y: 448,
            prevY: 438,
            z: 2,
            prevZ: 12,
            vx: -28,
            vy: 130,
            vCourtY: -28,
            vz: -130,
            shooter: 'player',
            direction: 1,
            crossedNet: false,
            hitNet: true,
            netTouched: true,
            netContactHoldUntil: 0,
            resolved: false
          });
        }"""
    )

    page.wait_for_function(
        """() => {
          const state = window.BadmintonDemo && window.BadmintonDemo.getState();
          return state && state.attemptsResults.length > 0;
        }""",
        timeout=3500,
    )

    resolved = page.evaluate("window.BadmintonDemo.getState()")
    result = resolved["attemptsResults"][-1]
    assert result["shooter"] == "player"
    assert result["shot_type"] == "net"
    assert result["point_winner"] == "neko"
    assert resolved["duel"]["player_score"] == 0
    assert resolved["duel"]["neko_score"] == 1
    assert resolved["duel"]["neko_misses"] == 0
    assert resolved["duel"]["player_misses"] == 1


@pytest.mark.e2e
def test_badminton_route_and_public_api(mock_page: Page, running_server: str):
    page = mock_page
    _goto_badminton(page, running_server, "duel")

    expect(page).to_have_url(re.compile(r"/badminton_demo"))
    expect(page).to_have_title(re.compile("羽毛球挑战"))
    expect(page.locator("#badminton-loading")).to_have_count(1)
    page.wait_for_function(
        """() => {
          const loading = document.getElementById('badminton-loading');
          return window.__badmintonInitialLoadingHidden === true ||
            (loading && loading.hidden === true);
        }""",
        timeout=10000,
    )
    expect(page.locator("#badminton-loading")).not_to_be_visible()
    assert page.evaluate("typeof window.BadmintonDemo.getState") == "function"
    assert page.evaluate("typeof window.BadmintonDemo.shoot") == "function"
    page.evaluate(
        """() => {
          const loading = document.getElementById('badminton-loading');
          loading.hidden = false;
          loading.classList.remove('hide');
          window.__badmintonInitialLoadingHidden = false;
        }"""
    )
    blocked_before = page.evaluate("window.BadmintonDemo.getState()")
    viewport = page.viewport_size or {"width": 1280, "height": 720}
    page.mouse.move(viewport["width"] - 8, 18)
    page.mouse.down()
    page.mouse.up()
    page.evaluate("window.BadmintonDemo.shoot()")
    blocked_after = page.evaluate("window.BadmintonDemo.getState()")
    assert blocked_after["canControlShot"] is False
    assert blocked_after["playerCourt"]["targetX"] == blocked_before["playerCourt"]["targetX"]
    assert blocked_after["pendingSwing"] is None
    assert blocked_after["currentShuttle"] is None
    page.evaluate(
        """() => {
          const loading = document.getElementById('badminton-loading');
          loading.classList.add('hide');
          loading.hidden = true;
          window.__badmintonInitialLoadingHidden = true;
        }"""
    )
    state = page.evaluate("window.BadmintonDemo.getState()")
    assert state["mode"] == "duel"
