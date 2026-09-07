import re
from pathlib import Path

import pytest
from playwright.sync_api import Page


REPO_ROOT = Path(__file__).resolve().parents[2]
JUKEBOX_PARTS_DIR = REPO_ROOT / "static" / "jukebox" / "jukebox"
JUKEBOX_PARTS = sorted(JUKEBOX_PARTS_DIR.glob("*.js"))
JUKEBOX_SCRIPT = "\n".join(part.read_text(encoding="utf-8") for part in JUKEBOX_PARTS)
JUKEBOX_LOADER_SCRIPT = (REPO_ROOT / "static" / "jukebox" / "jukebox-loader.js").read_text(encoding="utf-8")
JUKEBOX_TEMPLATE = (REPO_ROOT / "templates" / "jukebox.html").read_text(encoding="utf-8")
JUKEBOX_MANAGER_TEMPLATE = (REPO_ROOT / "templates" / "jukebox_manager.html").read_text(encoding="utf-8")
VRM_ANIMATION_SCRIPT = (REPO_ROOT / "static" / "vrm" / "vrm-animation.js").read_text(encoding="utf-8")

HARNESS_HTML = """
<!DOCTYPE html>
<html>
<body>
  <div class="jukebox-container open">
    <div class="jukebox-header">
      <div class="jukebox-header-left"></div>
      <div class="jukebox-header-drag-fill"></div>
      <div class="jukebox-header-buttons"></div>
    </div>
    <div class="jukebox-content">
      <table class="jukebox-table">
        <colgroup>
          <col class="jukebox-col-sequence">
          <col class="jukebox-col-song">
          <col class="jukebox-col-artist">
          <col class="jukebox-col-action">
        </colgroup>
        <thead>
          <tr>
            <th class="jukebox-sequence-th">
              <div class="jukebox-sequence-header">
                <span>序号</span>
                <button type="button" class="jukebox-sort-lock-btn" onclick="Jukebox.toggleSongSortLock(event)" aria-label="解锁歌曲排序" aria-pressed="false"></button>
              </div>
            </th>
            <th>歌曲</th>
            <th>艺术家</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody id="jukebox-song-list"></tbody>
      </table>
    </div>
    <div class="jukebox-controls-row">
      <div class="jukebox-progress">
        <span id="jukebox-time-current">0:00</span>
        <input type="range" id="jukebox-progress-slider" min="0" max="100" step="0.1" value="0">
        <span id="jukebox-time-total">0:00</span>
      </div>
      <div class="jukebox-playback-controls">
        <div id="jukebox-mode-controls" class="jukebox-mode-controls"></div>
        <button id="jukebox-control-prev" type="button" onclick="Jukebox.playAdjacentSong(-1)"></button>
        <button id="jukebox-control-play-pause" type="button" onclick="Jukebox.toggleGlobalPlayPause()"></button>
        <button id="jukebox-control-next" type="button" onclick="Jukebox.playAdjacentSong(1)"></button>
        <div class="jukebox-volume-wrapper">
          <button id="jukebox-speaker-btn" class="jukebox-speaker-btn" type="button">
            <span class="speaker-icon"></span>
            <span class="speaker-muted-icon" style="display: none;"></span>
          </button>
          <div class="jukebox-volume-popup">
            <div class="jukebox-volume-slider-container">
              <div class="jukebox-volume-track"></div>
              <input type="range" id="jukebox-volume-slider" min="0" max="1" step="0.01" value="1">
            </div>
            <div id="jukebox-volume-value">100%</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</body>
</html>
"""


def setup_jukebox_page(mock_page: Page) -> None:
    mock_page.set_content(HARNESS_HTML)
    mock_page.evaluate(
        """
        () => {
          const store = {};
          Object.defineProperty(window, 'localStorage', {
            configurable: true,
            value: {
              getItem(key) {
                return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
              },
              setItem(key, value) {
                store[key] = String(value);
              },
              removeItem(key) {
                delete store[key];
              },
              clear() {
                Object.keys(store).forEach((key) => delete store[key]);
              }
            }
          });
          window.__jukeboxLocalStore = store;
          window.t = (key, fallback) => typeof fallback === 'string' ? fallback : key;
        }
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_SCRIPT)
    mock_page.evaluate("() => window.Jukebox.injectStyles()")
    mock_page.evaluate(
        """
        () => {
          window.Jukebox.State.songs = [
            { id: 'song1', name: 'Song 1', artist: 'A' },
            { id: 'song2', name: 'Song 2', artist: 'B' },
            { id: 'song3', name: 'Song 3', artist: 'C' }
          ];
          window.Jukebox.State.songElements = {};
          window.Jukebox.State.playbackMode = 'sequence';
          window.Jukebox.renderList();
          window.Jukebox.renderPlaybackControls();
        }
        """
    )


@pytest.mark.frontend
def test_jukebox_loader_native_mode_keeps_animation_facade(mock_page: Page):
    mock_page.set_content(
        """
        <script>
          window.nativeToggled = false;
          window.__nekoJukeboxToggle = function() {
            window.nativeToggled = true;
          };
          window.t = (key, fallback) => typeof fallback === 'string' ? fallback : key;
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    result = mock_page.evaluate(
        """
        async () => {
          const calls = [];
          window.lanlan_config = { model_type: 'live3d', live3d_sub_type: 'mmd' };
          window.mmdManager = {
            currentAnimationUrl: '/idle.vmd',
            currentModel: { mesh: { skeleton: { pose: () => calls.push('pose') } } },
            animationModule: {
              stop: () => calls.push('stop'),
              pause: () => calls.push('pause'),
              play: () => calls.push('module-play')
            },
            cursorFollow: {
              setAnimationMode: (mode) => calls.push('cursor:' + mode)
            },
            loadAnimation: async (path) => calls.push('load:' + path),
            playAnimation: (mode) => calls.push('play:' + mode)
          };

          await window.Jukebox.playVMD('/dance.vmd');
          window.Jukebox.togglePause();
          window.Jukebox.togglePause();
          window.Jukebox.stopVMD(true);
          window.Jukebox.toggle();

          return {
            hasFacade: window.Jukebox.__nativeBridgeFacade === true,
            hasExecuteControl: typeof window.Jukebox.executeControl === 'function',
            hasInit: typeof window.Jukebox.init === 'function',
            nativeToggled: window.nativeToggled,
            webLoaderToggle: !!window.__nekoJukeboxToggle.__nekoJukeboxWebLoader,
            loaderReady: !!window.__nekoJukeboxLoader,
            state: {
              isPlaying: window.Jukebox.State.isPlaying,
              isVMDPlaying: window.Jukebox.State.isVMDPlaying,
              isPaused: window.Jukebox.State.isPaused
            },
            calls
          };
        }
        """
    )

    assert result == {
        "hasFacade": True,
        "hasExecuteControl": True,
        "hasInit": True,
        "nativeToggled": True,
        "webLoaderToggle": False,
        "loaderReady": True,
        "state": {
            "isPlaying": False,
            "isVMDPlaying": False,
            "isPaused": False,
        },
        "calls": [
            "load:/dance.vmd",
            "play:dance",
            "pause",
            "cursor:idle",
            "module-play",
            "cursor:dance",
            "stop",
        ],
    }


@pytest.mark.frontend
def test_jukebox_vrm_dance_holds_motion_runtime_until_stop_in_web_and_pet(mock_page: Page):
    mock_page.set_content(
        """
        <script>
          window.__nekoJukeboxToggle = function() {};
          window.t = (key, fallback) => fallback || key;
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    native_calls = mock_page.evaluate(
        """
        async () => {
          const calls = [];
          window.lanlan_config = { model_type: 'live3d', live3d_sub_type: 'vrm' };
          window.NekoMotion = {
            holdExternalPlayback: async (owner, options) => {
              calls.push(`hold:${owner}:${options.token}`);
              return true;
            },
            releaseExternalPlayback: async (owner, options) => {
              calls.push(`release:${owner}:${options.resume}`);
              return true;
            }
          };
          window.vrmManager = {
            playVRMAAnimation: async (url) => {
              calls.push('play:' + url);
              return true;
            },
            stopVRMAAnimation: () => calls.push('stop')
          };

          await window.Jukebox.playVRMA('/dance.vrma');
          window.Jukebox.stopVMD(false);
          await new Promise((resolve) => setTimeout(resolve, 0));
          delete window.NekoMotion;
          return calls;
        }
        """
    )
    assert native_calls == [
        "hold:jukebox:1",
        "play:/dance.vrma",
        "stop",
        "release:jukebox:true",
    ]

    setup_jukebox_page(mock_page)
    web_result = mock_page.evaluate(
        """
        async () => {
          const calls = [];
          const J = window.Jukebox;
          J.getModelType = () => 'vrm';
          window.NekoMotion = {
            holdExternalPlayback: async (owner, options) => {
              calls.push(`hold:${owner}:${options.token}`);
              return true;
            },
            releaseExternalPlayback: async (owner, options) => {
              calls.push(`release:${owner}:${options.resume}`);
              return true;
            }
          };
          window.vrmManager = {
            playVRMAAnimation: async (url) => {
              calls.push('play:' + url);
              return true;
            },
            stopVRMAAnimation: () => calls.push('stop')
          };

          const started = await J.playVRMA('/dance.vrma', { requestId: J.State.playRequestId });
          const debtWhileDancing = J.State.idleRestorePending;
          J.stopVMD(false);
          await new Promise((resolve) => setTimeout(resolve, 0));
          const result = {
            started,
            debtWhileDancing,
            debtAfterStop: J.State.idleRestorePending,
            calls
          };
          delete window.NekoMotion;
          return result;
        }
        """
    )
    assert web_result == {
        "started": True,
        "debtWhileDancing": True,
        "debtAfterStop": False,
        "calls": [
            "hold:jukebox:0",
            "play:/dance.vrma",
            "stop",
            "release:jukebox:true",
        ],
    }


@pytest.mark.frontend
def test_jukebox_vrma_replacement_atomically_replaces_runtime_token(mock_page: Page):
    mock_page.set_content(
        """
        <script>
          window.__nekoJukeboxToggle = function() {};
          window.t = (key, fallback) => fallback || key;
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    native_result = mock_page.evaluate(
        """
        async () => {
          const calls = [];
          let finishRelease = null;
          window.lanlan_config = { model_type: 'live3d', live3d_sub_type: 'vrm' };
          window.NekoMotion = {
            holdExternalPlayback: async (owner, options) => {
              calls.push(`hold:${owner}:${options.token}`);
              return true;
            },
            releaseExternalPlayback: (owner, options) => {
              calls.push(`release:${owner}:${options.token}:${options.resume}`);
              return new Promise((resolve) => { finishRelease = resolve; });
            }
          };
          window.vrmManager = {
            playVRMAAnimation: async (url) => {
              calls.push('play:' + url);
              return true;
            },
            stopVRMAAnimation: () => calls.push('stop')
          };

          await window.Jukebox.playVRMA('/first.vrma');
          const replacement = window.Jukebox.playVRMA('/second.vrma');
          await new Promise((resolve) => setTimeout(resolve, 0));
          const callsBeforeAnyRelease = calls.slice();
          const blockedOnRelease = finishRelease !== null;
          if (finishRelease) finishRelease(true);
          await replacement;
          return {
            callsBeforeAnyRelease,
            blockedOnRelease,
            token: window.Jukebox.State.vrmMotionRuntimeToken
          };
        }
        """
    )
    assert native_result == {
        "callsBeforeAnyRelease": [
            "hold:jukebox:1",
            "play:/first.vrma",
            "stop",
            "hold:jukebox:2",
            "play:/second.vrma",
        ],
        "blockedOnRelease": False,
        "token": 2,
    }

    setup_jukebox_page(mock_page)
    web_result = mock_page.evaluate(
        """
        async () => {
          const calls = [];
          let finishRelease = null;
          const J = window.Jukebox;
          J.getModelType = () => 'vrm';
          window.NekoMotion = {
            holdExternalPlayback: async (owner, options) => {
              calls.push(`hold:${owner}:${options.token}`);
              return true;
            },
            releaseExternalPlayback: (owner, options) => {
              calls.push(`release:${owner}:${options.token}:${options.resume}`);
              return new Promise((resolve) => { finishRelease = resolve; });
            }
          };
          window.vrmManager = {
            playVRMAAnimation: async (url) => {
              calls.push('play:' + url);
              return true;
            },
            stopVRMAAnimation: () => calls.push('stop')
          };

          await J.playVRMA('/first.vrma', { requestId: J.State.playRequestId });
          J.State.playRequestId += 1;
          const requestId = J.State.playRequestId;
          const replacement = J.playVRMA('/second.vrma', { requestId });
          await new Promise((resolve) => setTimeout(resolve, 0));
          const callsBeforeAnyRelease = calls.slice();
          const blockedOnRelease = finishRelease !== null;
          if (finishRelease) finishRelease(true);
          const started = await replacement;
          return {
            callsBeforeAnyRelease,
            blockedOnRelease,
            started,
            token: J.State.vrmMotionRuntimeToken
          };
        }
        """
    )
    assert web_result == {
        "callsBeforeAnyRelease": [
            "hold:jukebox:0",
            "play:/first.vrma",
            "stop",
            "hold:jukebox:1",
            "play:/second.vrma",
        ],
        "blockedOnRelease": False,
        "started": True,
        "token": 1,
    }


@pytest.mark.frontend
def test_jukebox_native_stop_cancels_vrma_while_runtime_hold_is_pending(mock_page: Page):
    mock_page.set_content(
        """
        <script>
          window.__nekoJukeboxToggle = function() {};
          window.t = (key, fallback) => fallback || key;
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    result = mock_page.evaluate(
        """
        async () => {
          const calls = [];
          let resolveHold;
          window.lanlan_config = { model_type: 'live3d', live3d_sub_type: 'vrm' };
          window.NekoMotion = {
            holdExternalPlayback: (owner, options) => {
              calls.push(`hold:${owner}:${options.token}`);
              return new Promise((resolve) => { resolveHold = resolve; });
            },
            releaseExternalPlayback: async (owner, options) => {
              calls.push(`release:${owner}:${options.token}:${options.resume}`);
              return true;
            }
          };
          window.vrmManager = {
            playVRMAAnimation: async (url, options) => {
              calls.push((options && options.isIdle ? 'idle:' : 'play:') + url);
              return true;
            },
            stopVRMAAnimation: () => calls.push('stop')
          };

          const pendingPlay = window.Jukebox.playVRMA('/late.vrma');
          const requestDuringHold = window.Jukebox.State.playRequestId;
          window.Jukebox.stopVMD(false);
          const requestAfterStop = window.Jukebox.State.playRequestId;
          resolveHold(true);
          await pendingPlay;

          const state = window.Jukebox.State;
          delete window.NekoMotion;
          return {
            calls,
            requestDuringHold,
            requestAfterStop,
            pendingRequest: state.pendingAnimationRequestId,
            isVMDPlaying: state.isVMDPlaying,
            isPlaying: state.isPlaying
          };
        }
        """
    )

    assert result == {
        "calls": [
            "hold:jukebox:1",
            "stop",
            "idle:/static/vrm/animation/wait03.vrma.gz",
            "release:jukebox:1:false",
        ],
        "requestDuringHold": 1,
        "requestAfterStop": 3,
        "pendingRequest": None,
        "isVMDPlaying": False,
        "isPlaying": False,
    }


@pytest.mark.frontend
def test_jukebox_native_stop_cancels_pending_vrma_load_and_restores_idle(mock_page: Page):
    mock_page.set_content(
        """
        <script>
          window.__nekoJukeboxToggle = function() {};
          window.t = (key, fallback) => fallback || key;
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    result = mock_page.evaluate(
        """
        async () => {
          const calls = [];
          let finishDance;
          let danceShouldStart;
          window.lanlan_config = { model_type: 'live3d', live3d_sub_type: 'vrm' };
          window.vrmManager = {
            playVRMAAnimation: (url, options) => {
              if (options.isIdle) {
                calls.push('idle:' + url);
                return Promise.resolve(true);
              }
              calls.push('dance:' + url);
              danceShouldStart = options.shouldStart;
              return new Promise((resolve) => {
                finishDance = () => resolve(danceShouldStart());
              });
            },
            stopVRMAAnimation: () => calls.push('stop')
          };

          const pendingPlay = window.Jukebox.playVRMA('/loading.vrma');
          window.Jukebox.stopVMD(false);
          const guardAfterStop = danceShouldStart();
          finishDance();
          await pendingPlay;

          return {
            calls,
            guardAfterStop,
            isVMDPlaying: window.Jukebox.State.isVMDPlaying,
            pendingRequest: window.Jukebox.State.pendingAnimationRequestId
          };
        }
        """
    )

    assert result == {
        "calls": [
            "dance:/loading.vrma",
            "stop",
            "idle:/static/vrm/animation/wait03.vrma.gz",
        ],
        "guardAfterStop": False,
        "isVMDPlaying": False,
        "pendingRequest": None,
    }


@pytest.mark.frontend
def test_jukebox_native_vrma_failure_without_runtime_restores_idle(mock_page: Page):
    mock_page.set_content(
        """
        <script>
          window.__nekoJukeboxToggle = function() {};
          window.t = (key, fallback) => fallback || key;
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    calls = mock_page.evaluate(
        """
        async () => {
          const calls = [];
          window.lanlan_config = { model_type: 'live3d', live3d_sub_type: 'vrm' };
          window.vrmManager = {
            playVRMAAnimation: async (url, options) => {
              calls.push((options.isIdle ? 'idle:' : 'dance:') + url);
              return options.isIdle === true;
            }
          };
          await window.Jukebox.playVRMA('/broken.vrma');
          return calls;
        }
        """
    )

    assert calls == [
        "dance:/broken.vrma",
        "idle:/static/vrm/animation/wait03.vrma.gz",
    ]


@pytest.mark.frontend
def test_jukebox_vrm_hold_releases_after_model_switch_or_manager_unload(mock_page: Page):
    mock_page.set_content(
        """
        <script>
          window.__nekoJukeboxToggle = function() {};
          window.t = (key, fallback) => fallback || key;
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    native_result = mock_page.evaluate(
        """
        async () => {
          const calls = [];
          window.lanlan_config = { model_type: 'live3d', live3d_sub_type: 'vrm' };
          window.NekoMotion = {
            holdExternalPlayback: async (owner, options) => {
              calls.push(`hold:${owner}:${options.token}`);
              return true;
            },
            releaseExternalPlayback: async (owner, options) => {
              calls.push(`release:${owner}:${options.token}:${options.resume}:${options.scheduleNext}`);
              return true;
            }
          };
          window.vrmManager = {
            playVRMAAnimation: async (url) => {
              calls.push('play:' + url);
              return true;
            }
          };

          await window.Jukebox.playVRMA('/dance.vrma');
          delete window.vrmManager;
          window.Jukebox.stopVMD(false);
          await new Promise((resolve) => setTimeout(resolve, 0));

          const token = window.Jukebox.State.vrmMotionRuntimeToken;
          delete window.NekoMotion;
          return { calls, token };
        }
        """
    )
    assert native_result == {
        "calls": [
            "hold:jukebox:1",
            "play:/dance.vrma",
            "release:jukebox:1:false:false",
        ],
        "token": None,
    }

    setup_jukebox_page(mock_page)
    web_result = mock_page.evaluate(
        """
        async () => {
          const calls = [];
          const J = window.Jukebox;
          J.getModelType = () => 'vrm';
          window.NekoMotion = {
            holdExternalPlayback: async (owner, options) => {
              calls.push(`hold:${owner}:${options.token}`);
              return true;
            },
            releaseExternalPlayback: async (owner, options) => {
              calls.push(`release:${owner}:${options.token}:${options.resume}:${options.scheduleNext}`);
              return true;
            }
          };
          window.vrmManager = {
            playVRMAAnimation: async (url) => {
              calls.push('play:' + url);
              return true;
            }
          };
          window.mmdManager = {
            animationModule: { stop: () => calls.push('mmd-stop') }
          };

          await J.playVRMA('/dance.vrma', { requestId: J.State.playRequestId });
          J.getModelType = () => 'mmd';
          J.stopVMD(false);
          await new Promise((resolve) => setTimeout(resolve, 0));

          const token = J.State.vrmMotionRuntimeToken;
          delete window.NekoMotion;
          return { calls, token };
        }
        """
    )
    assert web_result == {
        "calls": [
            "hold:jukebox:0",
            "play:/dance.vrma",
            "mmd-stop",
            "release:jukebox:0:false:false",
        ],
        "token": None,
    }


@pytest.mark.frontend
def test_jukebox_loader_normalizes_legacy_bundled_vrm_idle(mock_page: Page):
    mock_page.set_content(
        """
        <script>
          window.t = (key, fallback) => fallback || key;
          window.__nekoJukeboxToggle = function() {};
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    restored = mock_page.evaluate(
        """
        async () => {
          const calls = [];
          window.lanlan_config = {
            model_type: 'live3d',
            live3d_sub_type: 'vrm',
            vrmIdleAnimations: ['/static/vrm/animation/wait03.vrma?legacy=1']
          };
          window.vrmManager = {
            playVRMAAnimation: async (url) => calls.push(url)
          };
          await window.Jukebox.restoreIdleAnimation();
          window.lanlan_config.vrmIdleAnimations = [
            '/static/vrm/animation/custom-idle.vrma'
          ];
          await window.Jukebox.restoreIdleAnimation();
          return calls;
        }
        """
    )

    assert restored == [
        "/static/vrm/animation/wait03.vrma.gz?legacy=1",
        "/static/vrm/animation/custom-idle.vrma",
    ]


@pytest.mark.frontend
def test_jukebox_loader_rejects_stale_idle_restore(mock_page: Page):
    mock_page.set_content(
        """
        <script>
          window.t = (key, fallback) => fallback || key;
          window.__nekoJukeboxToggle = function() {};
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    result = mock_page.evaluate(
        """
        async () => {
          let finishRestore;
          let shouldApply;
          window.lanlan_config = { model_type: 'live3d', live3d_sub_type: 'vrm' };
          window.vrmManager = {
            playVRMAAnimation: (url, options) => {
              shouldApply = options.shouldApply;
              return new Promise((resolve) => { finishRestore = resolve; });
            }
          };
          const restore = window.Jukebox.restoreIdleAnimation();
          const currentBefore = shouldApply();
          window.Jukebox.State.playRequestId += 1;
          const currentAfter = shouldApply();
          finishRestore(false);
          await restore;
          return { currentBefore, currentAfter };
        }
        """
    )

    assert result == {"currentBefore": True, "currentAfter": False}


@pytest.mark.frontend
def test_jukebox_vrma_false_or_stale_result_does_not_mark_playback_active(mock_page: Page):
    mock_page.set_content(
        """
        <script>
          window.t = (key, fallback) => fallback || key;
          window.__nekoJukeboxToggle = function() {};
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    loader_result = mock_page.evaluate(
        """
        async () => {
          let finishStalePlay;
          window.vrmManager = {
            playVRMAAnimation: (url) => url === '/false.vrma'
              ? Promise.resolve(false)
              : new Promise((resolve) => { finishStalePlay = resolve; })
          };
          await window.Jukebox.playVRMA('/false.vrma');
          const afterFalse = { ...window.Jukebox.State };
          const stalePlay = window.Jukebox.playVRMA('/stale.vrma');
          window.Jukebox.State.playRequestId += 1;
          finishStalePlay(true);
          await stalePlay;
          return {
            afterFalse: {
              isPlaying: afterFalse.isPlaying,
              isVMDPlaying: afterFalse.isVMDPlaying,
              isPaused: afterFalse.isPaused
            },
            afterStale: {
              isPlaying: window.Jukebox.State.isPlaying,
              isVMDPlaying: window.Jukebox.State.isVMDPlaying,
              isPaused: window.Jukebox.State.isPaused
            }
          };
        }
        """
    )
    assert loader_result == {
        "afterFalse": {"isPlaying": False, "isVMDPlaying": False, "isPaused": False},
        "afterStale": {"isPlaying": False, "isVMDPlaying": False, "isPaused": False},
    }

    setup_jukebox_page(mock_page)
    transport_result = mock_page.evaluate(
        """
        async () => {
          let finishStalePlay;
          window.vrmManager = {
            playVRMAAnimation: (url) => url === '/false.vrma'
              ? Promise.resolve(false)
              : new Promise((resolve) => { finishStalePlay = resolve; })
          };
          await window.Jukebox.playVRMA('/false.vrma');
          const afterFalse = { ...window.Jukebox.State };
          const stalePlay = window.Jukebox.playVRMA('/stale.vrma');
          window.Jukebox.State.playRequestId += 1;
          finishStalePlay(true);
          await stalePlay;
          return {
            afterFalse: {
              isPlaying: afterFalse.isPlaying,
              isVMDPlaying: afterFalse.isVMDPlaying,
              isPaused: afterFalse.isPaused
            },
            afterStale: {
              isPlaying: window.Jukebox.State.isPlaying,
              isVMDPlaying: window.Jukebox.State.isVMDPlaying,
              isPaused: window.Jukebox.State.isPaused
            }
          };
        }
        """
    )
    assert transport_result == {
        "afterFalse": {"isPlaying": False, "isVMDPlaying": False, "isPaused": False},
        "afterStale": {"isPlaying": False, "isVMDPlaying": False, "isPaused": False},
    }


@pytest.mark.frontend
def test_jukebox_transport_normalizes_legacy_bundled_vrm_idle(mock_page: Page):
    setup_jukebox_page(mock_page)

    restored = mock_page.evaluate(
        """
        async () => {
          const calls = [];
          window.Jukebox.getModelType = () => 'vrm';
          window.lanlan_config = {
            vrmIdleAnimation: '/static/vrm/animation/wait03.vrma#saved'
          };
          window.vrmManager = {
            playVRMAAnimation: async (url) => calls.push(url)
          };
          await window.Jukebox.restoreIdleAnimation();
          window.lanlan_config.vrmIdleAnimation = '/static/vrm/animation/custom-idle.vrma';
          await window.Jukebox.restoreIdleAnimation();
          return calls;
        }
        """
    )

    assert restored == [
        "/static/vrm/animation/wait03.vrma.gz#saved",
        "/static/vrm/animation/custom-idle.vrma",
    ]


def test_jukebox_parts_are_loaded_in_directory_order():
    expected_paths = [f"/static/jukebox/jukebox/{part.name}" for part in JUKEBOX_PARTS]

    # 模板里的 src 带 ?v={{ static_asset_version }}，所以只能匹配到路径为止，
    # 不能带闭合引号。
    loader_positions = [JUKEBOX_LOADER_SCRIPT.find(f"'{part_path}'") for part_path in expected_paths]
    template_positions = [JUKEBOX_TEMPLATE.find(f'"{part_path}?') for part_path in expected_paths]
    manager_positions = [JUKEBOX_MANAGER_TEMPLATE.find(f'"{part_path}?') for part_path in expected_paths]

    assert all(position >= 0 for position in loader_positions)
    assert all(position >= 0 for position in template_positions)
    assert all(position >= 0 for position in manager_positions)
    assert loader_positions == sorted(loader_positions)
    assert template_positions == sorted(template_positions)
    assert manager_positions == sorted(manager_positions)


@pytest.mark.frontend
def test_jukebox_loader_fetches_all_parts_sequentially(mock_page: Page):
    loaded_parts = []

    def serve_jukebox_part(route):
        file_name = route.request.url.split("?", 1)[0].rsplit("/", 1)[-1]
        loaded_parts.append(file_name)
        route.fulfill(
            status=200,
            content_type="application/javascript",
            body=(JUKEBOX_PARTS_DIR / file_name).read_text(encoding="utf-8"),
        )

    # part URL 现在带 ?jukebox_control_api=N，glob 必须容纳 query。
    mock_page.route("**/static/jukebox/jukebox/*.js*", serve_jukebox_part)
    mock_page.set_content('<base href="http://jukebox.test/"><body></body>')
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    result = mock_page.evaluate(
        """
        async () => {
          const jukebox = await window.__nekoJukeboxLoader.load();
          return {
            keyCount: Object.keys(jukebox).length,
            hasLoadSongs: typeof jukebox.loadSongs === 'function',
            hasManager: typeof jukebox.SongActionManager === 'object',
            hasScriptTag: window.__nekoJukeboxLoader.getState().hasScriptTag
          };
        }
        """
    )

    assert loaded_parts == [part.name for part in JUKEBOX_PARTS]
    assert result == {
        "keyCount": 205,
        "hasLoadSongs": True,
        "hasManager": True,
        "hasScriptTag": True,
    }


def test_jukebox_action_column_reserves_space_for_two_buttons():
    assert ".jukebox-table col.jukebox-col-action {\n        width: 104px;" in JUKEBOX_SCRIPT
    assert ".jukebox-table td.song-action" in JUKEBOX_SCRIPT
    assert "justify-content: center;" in JUKEBOX_SCRIPT


def test_jukebox_sequence_column_reserves_lock_space_and_centers_numbers():
    assert ".jukebox-table col.jukebox-col-sequence {\n        width: 66px;" in JUKEBOX_SCRIPT
    assert ".jukebox-sort-lock-btn {\n        width: 22px;" in JUKEBOX_SCRIPT
    assert ".jukebox-sort-lock-btn svg {\n        width: 14px;" in JUKEBOX_SCRIPT
    assert ".jukebox-table td.song-index" in JUKEBOX_SCRIPT
    assert "text-align: center;" in JUKEBOX_SCRIPT
    assert ".song-index-number" in JUKEBOX_SCRIPT
    assert "justify-content: center;" in JUKEBOX_SCRIPT


def test_jukebox_header_owns_top_drag_region_instead_of_container_padding():
    container_match = re.search(r"\.jukebox-container\s*\{(?P<body>[\s\S]*?)\n\s*\}", JUKEBOX_SCRIPT)
    assert container_match is not None
    assert re.search(r"padding:\s*0;", container_match.group("body"))

    header_match = re.search(r"\.jukebox-header\s*\{(?P<body>[\s\S]*?)\n\s*\}", JUKEBOX_SCRIPT)
    assert header_match is not None
    assert re.search(r"padding:\s*20px 20px 10px;", header_match.group("body"))
    assert re.search(r"cursor:\s*grab;", header_match.group("body"))

    assert re.search(r"\.jukebox-content\s*\{[\s\S]*?margin:\s*0 20px;", JUKEBOX_SCRIPT)
    assert re.search(r"\.jukebox-controls-row\s*\{[\s\S]*?margin:\s*15px 20px 20px;", JUKEBOX_SCRIPT)


def test_jukebox_list_area_flexes_between_header_and_bottom_player():
    container_match = re.search(r"\.jukebox-container\s*\{(?P<body>[\s\S]*?)\n\s*\}", JUKEBOX_SCRIPT)
    assert container_match is not None
    container_body = container_match.group("body")
    assert re.search(r"display:\s*flex;", container_body)
    assert re.search(r"flex-direction:\s*column;", container_body)
    assert re.search(r"height:\s*calc\(100vh - 40px\);", container_body)
    assert re.search(r"max-height:\s*calc\(100vh - 40px\);", container_body)
    assert re.search(r"overflow:\s*hidden;", container_body)

    content_match = re.search(r"\.jukebox-content\s*\{(?P<body>[\s\S]*?)\n\s*\}", JUKEBOX_SCRIPT)
    assert content_match is not None
    content_body = content_match.group("body")
    assert re.search(r"flex:\s*1 1 auto;", content_body)
    assert re.search(r"overflow-y:\s*auto;", content_body)
    assert re.search(r"min-height:\s*0;", content_body)
    assert not re.search(r"max-height:\s*270px;", content_body)

    controls_match = re.search(r"\.jukebox-controls-row\s*\{(?P<body>[\s\S]*?)\n\s*\}", JUKEBOX_SCRIPT)
    assert controls_match is not None
    assert re.search(r"flex:\s*0 0 auto;", controls_match.group("body"))


def test_jukebox_injected_standalone_styles_disable_open_close_transform_transition():
    assert "html.neko-jukebox-standalone-host" in JUKEBOX_SCRIPT
    assert "html[data-theme=\"dark\"].neko-jukebox-standalone-host" in JUKEBOX_SCRIPT
    assert "body.neko-jukebox-standalone-page .jukebox-container.open" in JUKEBOX_SCRIPT
    assert "body.neko-jukebox-standalone-page .jukebox-container.hidden" in JUKEBOX_SCRIPT
    assert "body.neko-jukebox-standalone-page .jukebox-container.open" in JUKEBOX_TEMPLATE
    assert "body.neko-jukebox-standalone-page .jukebox-container.hidden" in JUKEBOX_TEMPLATE
    assert "transition: none !important;" in JUKEBOX_TEMPLATE
    assert "transform: none !important;" in JUKEBOX_TEMPLATE


@pytest.mark.frontend
def test_jukebox_web_window_size_is_saved_and_restored(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          const container = document.querySelector('.jukebox-container');
          container.style.width = '432px';
          container.style.height = '376px';
          J.saveWindowSize(container);

          container.style.width = '';
          container.style.height = '';
          J.applyStoredWindowSize(container);

          return {
            stored: JSON.parse(window.__jukeboxLocalStore['neko.jukebox.windowSize']),
            width: container.style.width,
            height: container.style.height
          };
        }
        """
    )

    assert result["stored"] == {"width": 432, "height": 376}
    assert result["width"] == "432px"
    assert result["height"] == "376px"


@pytest.mark.frontend
def test_jukebox_web_resize_click_without_delta_does_not_save_size(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          const container = document.querySelector('.jukebox-container');
          const handle = document.createElement('div');
          handle.className = 'jukebox-resize-handle';
          handle.dataset.dir = 'se';
          container.appendChild(handle);

          J.State.hasCustomWindowSize = false;
          J.bindResize(container);

          handle.dispatchEvent(new MouseEvent('mousedown', {
            bubbles: true,
            cancelable: true,
            clientX: 100,
            clientY: 100
          }));
          document.dispatchEvent(new MouseEvent('mouseup', {
            bubbles: true,
            cancelable: true,
            clientX: 100,
            clientY: 100
          }));

          return {
            hasCustomWindowSize: J.State.hasCustomWindowSize,
            stored: window.__jukeboxLocalStore['neko.jukebox.windowSize'] || null,
            resizingClass: document.body.classList.contains('jukebox-resizing')
          };
        }
        """
    )

    assert result == {
        "hasCustomWindowSize": False,
        "stored": None,
        "resizingClass": False,
    }


@pytest.mark.frontend
def test_jukebox_content_height_expands_while_bottom_player_stays_inside(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          J.State.songs = Array.from({ length: 30 }, (_, index) => ({
            id: `song-${index}`,
            name: `Song ${index}`,
            artist: 'Artist'
          }));
          J.State.songElements = {};
          J.renderList();

          const container = document.querySelector('.jukebox-container');
          const content = document.querySelector('.jukebox-content');
          const controls = document.querySelector('.jukebox-controls-row');

          const measure = (height) => {
            container.style.height = `${height}px`;
            const containerRect = container.getBoundingClientRect();
            const contentRect = content.getBoundingClientRect();
            const controlsRect = controls.getBoundingClientRect();
            return {
              contentHeight: contentRect.height,
              controlsBottomGap: containerRect.bottom - controlsRect.bottom,
              contentClientHeight: content.clientHeight,
              contentScrollHeight: content.scrollHeight
            };
          };

          return {
            compact: measure(360),
            roomy: measure(560),
            containerOverflow: getComputedStyle(container).overflow,
            contentOverflowY: getComputedStyle(content).overflowY,
            controlsFlex: getComputedStyle(controls).flex
          };
        }
        """
    )

    assert result["containerOverflow"] == "hidden"
    assert result["contentOverflowY"] == "auto"
    assert result["controlsFlex"] == "0 0 auto"
    assert result["compact"]["contentScrollHeight"] > result["compact"]["contentClientHeight"]
    assert result["roomy"]["contentHeight"] - result["compact"]["contentHeight"] > 150
    assert abs(result["compact"]["controlsBottomGap"] - 20) <= 1
    assert abs(result["roomy"]["controlsBottomGap"] - 20) <= 1


@pytest.mark.frontend
def test_jukebox_volume_wheel_adjusts_volume_without_scrolling_container(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          const calls = [];
          J.State.player = {
            audio: { volume: 0.5 },
            volume(value) {
              this.audio.volume = value;
              calls.push(value);
            }
          };
          J.State.isMuted = false;
          J.State.savedVolume = 0.5;
          J.initVolumeSlider();

          const container = document.querySelector('.jukebox-container');
          const slider = document.getElementById('jukebox-volume-slider');
          const value = document.getElementById('jukebox-volume-value');
          let containerWheelCount = 0;
          container.addEventListener('wheel', () => {
            containerWheelCount += 1;
          });

          const upEvent = new WheelEvent('wheel', { deltaY: -120, bubbles: true, cancelable: true });
          const upDispatchResult = slider.dispatchEvent(upEvent);
          const afterUp = { slider: slider.value, value: value.textContent, volume: J.State.player.audio.volume };

          const downEvent = new WheelEvent('wheel', { deltaY: 120, bubbles: true, cancelable: true });
          const downDispatchResult = slider.dispatchEvent(downEvent);

          return {
            calls,
            afterUp,
            finalSlider: slider.value,
            finalValue: value.textContent,
            finalVolume: J.State.player.audio.volume,
            upDefaultPrevented: upEvent.defaultPrevented,
            downDefaultPrevented: downEvent.defaultPrevented,
            upDispatchResult,
            downDispatchResult,
            containerWheelCount
          };
        }
        """
    )

    assert result["calls"] == [0.55, 0.5]
    assert result["afterUp"] == {"slider": "0.55", "value": "55%", "volume": 0.55}
    assert result["finalSlider"] == "0.5"
    assert result["finalValue"] == "50%"
    assert result["finalVolume"] == 0.5
    assert result["upDefaultPrevented"] is True
    assert result["downDefaultPrevented"] is True
    assert result["upDispatchResult"] is False
    assert result["downDispatchResult"] is False
    assert result["containerWheelCount"] == 0


@pytest.mark.frontend
def test_jukebox_builtin_paths_keep_resource_directories(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const playerUrls = [];
          const vrmaCalls = [];

          window.fetch = async (url, options = {}) => {
            if (options.method === 'HEAD') {
              return { ok: true, status: 200 };
            }
            if (url === '/api/jukebox/config') {
              return {
                ok: true,
                json: async () => ({
                  configRevision: 'rev-builtin-paths',
                  songs: {
                    song_001: {
                      name: '桃源恋歌',
                      artist: 'GARNiDELiA',
                      audio: 'songs/song_001.mp3',
                      visible: true,
                      isBuiltin: true,
                      defaultAction: 'action_001'
                    }
                  },
                  actions: {
                    action_001: {
                      name: '桃源恋歌',
                      file: 'actions/song_001.vrma',
                      format: 'vrma',
                      visible: true,
                      isBuiltin: true
                    }
                  },
                  bindings: {
                    song_001: { action_001: { offset: 0 } }
                  }
                })
              };
            }
            throw new Error(`unexpected fetch ${url}`);
          };

          await J.loadSongs();
          const song = J.State.songs[0];

          J.getModelType = () => 'vrm';
          J.stopPlayback = () => {};
          J.playVRMA = async (url) => { vrmaCalls.push(url); };
          J.getPlayer = () => ({
            list: {
              clear() {},
              add(items) {
                playerUrls.push(...items.map(item => item.url));
              }
            },
            options: {},
            on() {},
            play() {}
          });

          await J.playSong('song_001');

          return {
            audio: song.audio,
            playerUrls,
            vrmaCalls,
            legacyStaticVrma: J.resolveJukeboxFileUrl('/static/jukebox/actions/song_001.vrma'),
            legacyFlatVrma: J.resolveJukeboxFileUrl('static/jukebox/song_001.vrma')
          };
        }
        """
    )

    assert result == {
        "audio": "songs/song_001.mp3",
        "playerUrls": ["/api/jukebox/file/songs/song_001.mp3"],
        "vrmaCalls": ["/api/jukebox/file/actions/song_001.vrma"],
        "legacyStaticVrma": "/api/jukebox/file/actions/song_001.vrma",
        "legacyFlatVrma": "/api/jukebox/file/song_001.vrma",
    }


@pytest.mark.frontend
def test_jukebox_vrm_progress_seek_and_calibration_sync_animation(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          const audioSeekCalls = [];
          const vrmSeekCalls = [];
          const audio = { duration: 100, currentTime: 20 };

          J.getModelType = () => 'vrm';
          J.State.currentSong = {
            id: 'song-vrm',
            name: 'VRM Song',
            boundActions: [{ id: 'action-vrma', name: 'Dance', format: 'vrma', fps: 60 }],
            defaultAction: 'action-vrma'
          };
          J.SongActionManager.data = {
            bindings: {
              'song-vrm': {
                'action-vrma': { offset: 30 }
              }
            }
          };
          J.State.player = {
            audio,
            seek(time) {
              audio.currentTime = time;
              audioSeekCalls.push(time);
            }
          };
          window.vrmManager = {
            seekVRMAAnimation(time, options) {
              vrmSeekCalls.push({ time, paused: options && options.paused });
              return true;
            }
          };

          const slider = document.getElementById('jukebox-progress-slider');
          slider.value = '50';
          J._onProgressChange();
          const afterProgressChange = {
            audioCurrentTime: audio.currentTime,
            isSeeking: J.State.isSeeking,
            timeText: document.getElementById('jukebox-time-current').textContent
          };

          audio.currentTime = 12;
          J.syncAnimationToOffset(-30);

          return { audioSeekCalls, vrmSeekCalls, afterProgressChange };
        }
        """
    )

    assert result == {
        "audioSeekCalls": [50],
        "vrmSeekCalls": [
            {"time": 50.5, "paused": False},
            {"time": 11.5, "paused": False},
        ],
        "afterProgressChange": {
            "audioCurrentTime": 50,
            "isSeeking": False,
            "timeText": "0:50",
        },
    }


@pytest.mark.frontend
def test_jukebox_progress_seek_uses_loaded_config_offset_before_manager_load(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          const vrmSeekCalls = [];
          const audio = { duration: 100, currentTime: 0 };

          J.getModelType = () => 'vrm';
          J.State.config = {
            bindings: {
              'song-vrm': {
                'action-vrma': { offset: 6 }
              }
            }
          };
          J.State.currentSong = {
            id: 'song-vrm',
            name: 'VRM Song',
            boundActions: [{ id: 'action-vrma', name: 'Dance', format: 'vrma', fps: 60 }],
            defaultAction: 'action-vrma'
          };
          J.SongActionManager.data = { bindings: {} };
          J.State.player = {
            audio,
            seek(time) {
              audio.currentTime = time;
            }
          };
          window.vrmManager = {
            seekVRMAAnimation(time, options) {
              vrmSeekCalls.push({ time, paused: options && options.paused });
              return true;
            }
          };

          const slider = document.getElementById('jukebox-progress-slider');
          slider.value = '50';
          J._onProgressChange();

          return {
            currentOffset: J.getCurrentOffset(),
            audioCurrentTime: audio.currentTime,
            vrmSeekCalls
          };
        }
        """
    )

    assert result == {
        "currentOffset": 6,
        "audioCurrentTime": 50,
        "vrmSeekCalls": [{"time": 50.1, "paused": False}],
    }


@pytest.mark.frontend
def test_vrm_animation_seek_preserves_paused_state_and_refreshes_pose(mock_page: Page):
    mock_page.set_content("<html><body></body></html>")
    mock_page.evaluate("() => { window.THREE = {}; }")
    mock_page.add_script_tag(content=VRM_ANIMATION_SCRIPT)

    result = mock_page.evaluate(
        """
        () => {
          const events = [];
          const skinnedMesh = {
            isSkinnedMesh: true,
            skeleton: {
              update() {
                events.push('skeleton');
              }
            }
          };
          const scene = {
            uuid: 'scene-a',
            traverse(callback) {
              callback(skinnedMesh);
            },
            updateMatrixWorld(force) {
              events.push(`matrix:${force}`);
            }
          };
          const manager = { currentModel: { vrm: { scene } } };
          const anim = new window.VRMAnimation(manager);
          anim.vrmaMixer = {
            update(delta) {
              events.push(`mixer:${delta}`);
            },
            getRoot() {
              return scene;
            }
          };
          anim.currentAction = { time: 0, paused: true };

          const ok = anim.seekTo(3.25);
          const pausedAfterFirstSeek = anim.currentAction.paused;
          const okPlaying = anim.seekTo(1.5, { paused: false });

          return {
            ok,
            okPlaying,
            pausedAfterFirstSeek,
            actionTime: anim.currentAction.time,
            actionPaused: anim.currentAction.paused,
            cachedMeshes: anim._skinnedMeshes.length,
            events
          };
        }
        """
    )

    assert result == {
        "ok": True,
        "okPlaying": True,
        "pausedAfterFirstSeek": True,
        "actionTime": 1.5,
        "actionPaused": False,
        "cachedMeshes": 1,
        "events": [
            "mixer:0",
            "matrix:true",
            "skeleton",
            "mixer:0",
            "matrix:true",
            "skeleton",
        ],
    }


@pytest.mark.frontend
def test_jukebox_config_poll_fetches_full_config_only_after_revision_change(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const urls = [];
          const managerLoads = [];
          J.State.isOpen = true;
          J.State.configRevision = 'rev-a';
          J.loadSongs = async () => {
            urls.push('/api/jukebox/config');
            J.State.configRevision = 'rev-b';
          };
          J.SongActionManager.load = async () => {
            managerLoads.push('manager');
          };
          window.fetch = async (url) => {
            urls.push(String(url));
            return {
              ok: true,
              json: async () => ({ configRevision: 'rev-a', songCount: 2, visibleSongCount: 2 })
            };
          };

          await J.checkConfigUpdates();
          window.fetch = async (url) => {
            urls.push(String(url));
            return {
              ok: true,
              json: async () => ({ configRevision: 'rev-b', songCount: 3, visibleSongCount: 3 })
            };
          };
          await J.checkConfigUpdates();

          return { urls, revision: J.State.configRevision, managerLoads };
        }
        """
    )

    assert result == {
        "urls": [
            "/api/jukebox/config/summary",
            "/api/jukebox/config/summary",
            "/api/jukebox/config",
        ],
        "revision": "rev-b",
        "managerLoads": ["manager"],
    }


@pytest.mark.frontend
def test_jukebox_playback_mode_button_cycles_and_persists(mock_page: Page):
    setup_jukebox_page(mock_page)

    mode_button = mock_page.locator("#jukebox-mode-controls .jukebox-mode-btn")
    assert mode_button.count() == 1
    assert mode_button.get_attribute("data-mode") == "sequence"

    mode_button.click()
    assert mode_button.get_attribute("data-mode") == "single"
    assert mock_page.evaluate("window.__jukeboxLocalStore['neko.jukebox.playbackMode']") == '"single"'

    mode_button.click()
    assert mode_button.get_attribute("data-mode") == "random"
    assert mock_page.evaluate("window.__jukeboxLocalStore['neko.jukebox.playbackMode']") == '"random"'

    mode_button.click()
    assert mode_button.get_attribute("data-mode") == "none"
    assert mock_page.evaluate("window.__jukeboxLocalStore['neko.jukebox.playbackMode']") == '"none"'

    mode_button.click()
    assert mode_button.get_attribute("data-mode") == "sequence"
    assert mock_page.evaluate("window.__jukeboxLocalStore['neko.jukebox.playbackMode']") == '"sequence"'


@pytest.mark.frontend
def test_jukebox_playback_mode_tooltip_uses_current_mode(mock_page: Page):
    setup_jukebox_page(mock_page)

    mode_button = mock_page.locator("#jukebox-mode-controls .jukebox-mode-btn")
    assert mode_button.get_attribute("title") is None

    mode_button.hover()
    tooltip = mock_page.locator(".jukebox-tooltip")
    mock_page.wait_for_function(
        "() => {"
        " const el = document.querySelector('.jukebox-tooltip');"
        " return !!el && el.textContent.includes('顺序播放');"
        "}"
    )
    assert "顺序播放" in tooltip.inner_text()

    mode_button.click()
    assert mode_button.get_attribute("data-mode") == "single"
    assert mode_button.get_attribute("title") is None
    assert "单曲循环" in tooltip.inner_text()


@pytest.mark.frontend
def test_jukebox_next_song_respects_sequence_single_and_random(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          const ended = J.State.songs[0];
          const last = J.State.songs[2];

          J.State.playbackMode = 'sequence';
          const sequenceNext = J.getNextSongToPlay(ended)?.id;
          const sequenceEnd = J.getNextSongToPlay(last);

          J.State.playbackMode = 'none';
          const noneNext = J.getNextSongToPlay(ended);

          J.State.playbackMode = 'single';
          const singleNext = J.getNextSongToPlay(ended)?.id;
          const removedSingleNext = J.getNextSongToPlay({ id: 'removed-song' });

          const originalRandom = Math.random;
          Math.random = () => 0;
          J.State.playbackMode = 'random';
          const randomNext = J.getNextSongToPlay(ended)?.id;
          const randomQueue = [...J.State.randomQueue];
          const randomQueueIndex = J.State.randomQueueIndex;
          Math.random = originalRandom;

          return {
            sequenceNext,
            sequenceEnd,
            noneNext,
            singleNext,
            removedSingleNext,
            randomNext,
            randomQueue,
            randomQueueIndex
          };
        }
        """
    )

    assert result == {
        "sequenceNext": "song2",
        "sequenceEnd": None,
        "noneNext": None,
        "singleNext": "song1",
        "removedSingleNext": None,
        "randomNext": "song2",
        "randomQueue": ["song1", "song2"],
        "randomQueueIndex": 1,
    }


@pytest.mark.frontend
def test_jukebox_auto_next_skips_idle_restore_only_when_next_song_has_animation(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const stopArgs = [];
          const played = [];
          J.stopVMD = (skipIdleRestore) => {
            stopArgs.push(skipIdleRestore);
          };
          J.updateStoppedStatus = () => {};
          J.playSong = async (songId) => {
            played.push(songId);
          };
          J.getModelType = () => 'mmd';
          J.State.isOpen = true;
          J.State.playbackMode = 'sequence';
          J.State.songs[1].boundActions = [{ id: 'action-song2', name: 'Action', format: 'vmd' }];
          J.State.songs[1].defaultAction = 'action-song2';
          J.State.currentSong = J.State.songs[0];

          J.handleAudioEnded({ options: { loop: 'none' } });
          await new Promise((resolve) => setTimeout(resolve, 0));

          J.State.songs[1].boundActions = [];
          J.State.songs[1].defaultAction = '';
          J.State.currentSong = J.State.songs[0];
          J.handleAudioEnded({ options: { loop: 'none' } });
          await new Promise((resolve) => setTimeout(resolve, 0));

          J.State.songs[1].boundActions = [{ id: 'missing-song2', name: 'Missing', format: 'vmd', missing: true }];
          J.State.songs[1].defaultAction = 'missing-song2';
          J.State.currentSong = J.State.songs[0];
          const missingAction = J.getActionForModel(J.State.songs[1]);
          J.handleAudioEnded({ options: { loop: 'none' } });
          await new Promise((resolve) => setTimeout(resolve, 0));

          J.State.currentSong = J.State.songs[2];
          J.handleAudioEnded({ options: { loop: 'none' } });
          await new Promise((resolve) => setTimeout(resolve, 0));

          return { missingAction, stopArgs, played };
        }
        """
    )

    assert result == {
        "missingAction": None,
        "stopArgs": [True, False, False, False],
        "played": ["song2", "song2", "song2"],
    }


@pytest.mark.frontend
def test_jukebox_single_loop_removed_current_song_restores_idle(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const stopArgs = [];
          const played = [];
          J.stopVMD = (skipIdleRestore) => {
            stopArgs.push(skipIdleRestore);
          };
          J.updateStoppedStatus = () => {};
          J.playSong = async (songId) => {
            played.push(songId);
          };
          J.getActionForModel = () => ({ id: 'stale-action' });
          J.State.isOpen = true;
          J.State.playbackMode = 'single';
          J.State.songs = J.State.songs.filter(song => song.id !== 'song1');
          const removedSong = { id: 'song1', name: 'Removed Song' };
          J.State.currentSong = removedSong;
          const nextSong = J.getNextSongToPlay(removedSong);

          J.handleAudioEnded({ options: { loop: 'none' } });
          await new Promise((resolve) => setTimeout(resolve, 0));

          return { nextSong, stopArgs, played };
        }
        """
    )

    assert result == {
        "nextSong": None,
        "stopArgs": [False],
        "played": [],
    }


@pytest.mark.frontend
def test_jukebox_global_transport_controls_follow_sorted_playlist(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          const played = [];
          J.playSong = async (songId) => {
            played.push(songId);
            J.State.currentSong = J.State.songs.find((song) => song.id === songId) || null;
            J.State.isPlaying = true;
            J.State.isPaused = false;
            J.updateGlobalTransportControls();
          };

          J.State.songs = [J.State.songs[2], J.State.songs[0], J.State.songs[1]];
          J.renderList();
          J.State.currentSong = J.State.songs[1];
          J.State.isPlaying = true;
          J.State.isPaused = false;
          J.updateGlobalTransportControls();
          const pauseLabel = document.getElementById('jukebox-control-play-pause').getAttribute('aria-label');

          J.playAdjacentSong(-1);
          J.playAdjacentSong(1);

          J.State.isPlaying = false;
          J.State.isPaused = true;
          J.updateGlobalTransportControls();
          const resumeLabel = document.getElementById('jukebox-control-play-pause').getAttribute('aria-label');

          return { played, pauseLabel, resumeLabel };
        }
        """
    )

    assert result == {
        "played": ["song3", "song1"],
        "pauseLabel": "暂停",
        "resumeLabel": "继续",
    }


@pytest.mark.frontend
def test_jukebox_non_random_manual_previous_next_follow_sorted_playlist(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          const result = {};
          J.playSong = (songId) => {
            result.lastPlayed = songId;
            J.State.currentSong = J.State.songs.find((song) => song.id === songId) || null;
          };

          J.State.songs = [J.State.songs[2], J.State.songs[0], J.State.songs[1]];
          const song1 = J.State.songs[1];
          for (const mode of ['none', 'single', 'sequence']) {
            J.State.playbackMode = mode;
            J.State.currentSong = song1;
            J.playAdjacentSong(1);
            result[`${mode}Next`] = result.lastPlayed;

            J.State.currentSong = song1;
            J.playAdjacentSong(-1);
            result[`${mode}Previous`] = result.lastPlayed;
          }
          delete result.lastPlayed;
          return result;
        }
        """
    )

    assert result == {
        "noneNext": "song2",
        "nonePrevious": "song3",
        "singleNext": "song2",
        "singlePrevious": "song3",
        "sequenceNext": "song2",
        "sequencePrevious": "song3",
    }


@pytest.mark.frontend
def test_jukebox_random_mode_starts_queue_from_current_song(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          J.State.currentSong = J.State.songs[1];
          J.setPlaybackMode('random');
          const entered = {
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            pendingExit: J.State.randomQueueExitSongId
          };

          J.setPlaybackMode('sequence');
          const exited = {
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            pendingExit: J.State.randomQueueExitSongId
          };

          J.State.currentSong = J.State.songs[2];
          J.setPlaybackMode('random');
          const reentered = {
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            pendingExit: J.State.randomQueueExitSongId
          };

          return { entered, exited, reentered };
        }
        """
    )

    assert result == {
        "entered": {"queue": ["song2"], "index": 0, "pendingExit": None},
        "exited": {"queue": [], "index": -1, "pendingExit": None},
        "reentered": {"queue": ["song3"], "index": 0, "pendingExit": None},
    }


@pytest.mark.frontend
def test_jukebox_random_exit_is_delayed_until_current_song_ends(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          J.State.playbackMode = 'random';
          J.State.currentSong = J.State.songs[1];
          J.State.isPlaying = true;
          J.State.randomQueue = ['song1', 'song2'];
          J.State.randomQueueIndex = 1;

          J.setPlaybackMode('sequence');
          const afterExit = {
            mode: J.State.playbackMode,
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            pendingExit: J.State.randomQueueExitSongId
          };

          J.setPlaybackMode('random');
          const afterReturn = {
            mode: J.State.playbackMode,
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            pendingExit: J.State.randomQueueExitSongId
          };

          J.setPlaybackMode('sequence');
          const nextSong = J.getNextSongToPlay(J.State.songs[1])?.id;
          const afterEndedOutsideRandom = {
            nextSong,
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            pendingExit: J.State.randomQueueExitSongId
          };

          return { afterExit, afterReturn, afterEndedOutsideRandom };
        }
        """
    )

    assert result == {
        "afterExit": {
            "mode": "sequence",
            "queue": ["song1", "song2"],
            "index": 1,
            "pendingExit": "song2",
        },
        "afterReturn": {
            "mode": "random",
            "queue": ["song1", "song2"],
            "index": 1,
            "pendingExit": None,
        },
        "afterEndedOutsideRandom": {
            "nextSong": "song3",
            "queue": [],
            "index": -1,
            "pendingExit": None,
        },
    }


@pytest.mark.frontend
def test_jukebox_random_exit_uses_queued_anchor_without_current_song(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          J.State.playbackMode = 'random';
          J.State.currentSong = null;
          J.State.isPlaying = false;
          J.State.isPaused = false;
          J.State.randomQueue = ['song1', 'song2'];
          J.State.randomQueueIndex = 1;

          J.setPlaybackMode('sequence');
          const afterExit = {
            mode: J.State.playbackMode,
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            pendingExit: J.State.randomQueueExitSongId
          };

          J.setPlaybackMode('random');
          const afterReturn = {
            mode: J.State.playbackMode,
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            pendingExit: J.State.randomQueueExitSongId
          };

          J.State.currentSong = null;
          J.State.randomQueue = ['missing-song'];
          J.State.randomQueueIndex = 0;
          J.setPlaybackMode('sequence');

          return {
            afterExit,
            afterReturn,
            invalidQueuedAnchor: {
              queue: [...J.State.randomQueue],
              index: J.State.randomQueueIndex,
              pendingExit: J.State.randomQueueExitSongId
            }
          };
        }
        """
    )

    assert result == {
        "afterExit": {
            "mode": "sequence",
            "queue": ["song1", "song2"],
            "index": 1,
            "pendingExit": "song2",
        },
        "afterReturn": {
            "mode": "random",
            "queue": ["song1", "song2"],
            "index": 1,
            "pendingExit": None,
        },
        "invalidQueuedAnchor": {
            "queue": [],
            "index": -1,
            "pendingExit": None,
        },
    }


@pytest.mark.frontend
def test_jukebox_random_exit_prunes_removed_songs_while_preserving_anchor(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          J.State.playbackMode = 'sequence';
          J.State.currentSong = J.State.songs[1];
          J.State.randomQueue = ['song1', 'song2', 'song3'];
          J.State.randomQueueIndex = 1;
          J.State.randomQueueExitSongId = 'song2';

          J.State.songs = [
            { id: 'song2', name: 'Song 2', artist: 'B' },
            { id: 'song4', name: 'Song 4', artist: 'D' }
          ];
          J.syncRandomQueueWithSongs();

          return {
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            pendingExit: J.State.randomQueueExitSongId
          };
        }
        """
    )

    assert result == {
        "queue": ["song2"],
        "index": 0,
        "pendingExit": "song2",
    }


@pytest.mark.frontend
def test_jukebox_random_exit_sync_preserves_queued_pending_anchor(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          J.State.playbackMode = 'sequence';
          J.State.currentSong = null;
          J.State.randomQueue = ['song1', 'song2'];
          J.State.randomQueueIndex = 1;
          J.State.randomQueueExitSongId = 'song2';

          J.syncRandomQueueWithSongs();

          const retainedQueuedPending = {
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            pendingExit: J.State.randomQueueExitSongId
          };

          J.State.currentSong = null;
          J.State.randomQueue = ['song1', 'song2'];
          J.State.randomQueueIndex = 1;
          J.State.randomQueueExitSongId = 'song1';

          J.syncRandomQueueWithSongs();

          return {
            retainedQueuedPending,
            clearedMismatchedPending: {
              queue: [...J.State.randomQueue],
              index: J.State.randomQueueIndex,
              pendingExit: J.State.randomQueueExitSongId
            }
          };
        }
        """
    )

    assert result == {
        "retainedQueuedPending": {
            "queue": ["song1", "song2"],
            "index": 1,
            "pendingExit": "song2",
        },
        "clearedMismatchedPending": {
            "queue": [],
            "index": -1,
            "pendingExit": None,
        },
    }


@pytest.mark.frontend
def test_jukebox_random_exit_pending_song_start_preserves_queue(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          J.stopAudio = () => {};
          J.stopVMD = () => {};
          J.updateStoppedStatus = () => {};
          J.updatePlayingStatus = () => {};
          J.updateCalibrationDisplay = () => {};
          J.playAudio = async () => {};
          J.getActionForModel = () => null;

          J.State.playbackMode = 'sequence';
          J.State.currentSong = null;
          J.State.randomQueue = ['song1', 'song2'];
          J.State.randomQueueIndex = 1;
          J.State.randomQueueExitSongId = 'song2';

          await J.playSong('song2');

          return {
            currentSong: J.State.currentSong && J.State.currentSong.id,
            isPlaying: J.State.isPlaying,
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            pendingExit: J.State.randomQueueExitSongId
          };
        }
        """
    )

    assert result == {
        "currentSong": "song2",
        "isPlaying": True,
        "queue": ["song1", "song2"],
        "index": 1,
        "pendingExit": "song2",
    }


@pytest.mark.frontend
def test_jukebox_random_explicit_stop_clears_queue(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          J.stopAudio = () => {};
          J.stopVMD = () => {};
          J.updateStoppedStatus = () => {};

          J.State.playbackMode = 'random';
          J.State.currentSong = J.State.songs[1];
          J.State.isPlaying = true;
          J.State.randomQueue = ['song1', 'song2'];
          J.State.randomQueueIndex = 1;
          J.State.randomQueueExitSongId = null;

          J.stopPlayback();

          return {
            currentSong: J.State.currentSong && J.State.currentSong.id,
            isPlaying: J.State.isPlaying,
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            pendingExit: J.State.randomQueueExitSongId
          };
        }
        """
    )

    assert result == {
        "currentSong": None,
        "isPlaying": False,
        "queue": [],
        "index": -1,
        "pendingExit": None,
    }


@pytest.mark.frontend
def test_jukebox_random_song_start_preserves_reset_queue(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          J.stopAudio = () => {};
          J.stopVMD = () => {};
          J.updateStoppedStatus = () => {};
          J.updatePlayingStatus = () => {};
          J.updateCalibrationDisplay = () => {};
          J.playAudio = async () => {};
          J.getActionForModel = () => null;

          J.State.playbackMode = 'random';
          J.State.currentSong = J.State.songs[0];
          J.State.isPlaying = true;
          J.State.randomQueue = ['song1', 'song3'];
          J.State.randomQueueIndex = 1;

          await J.playSong('song2');

          return {
            currentSong: J.State.currentSong && J.State.currentSong.id,
            isPlaying: J.State.isPlaying,
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            pendingExit: J.State.randomQueueExitSongId
          };
        }
        """
    )

    assert result == {
        "currentSong": "song2",
        "isPlaying": True,
        "queue": ["song2"],
        "index": 0,
        "pendingExit": None,
    }


@pytest.mark.frontend
def test_jukebox_random_sync_preserves_current_duplicate_queue_entry(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          J.State.playbackMode = 'random';
          J.State.currentSong = J.State.songs[0];
          J.State.randomQueue = ['song1', 'song2', 'song1', 'song3'];
          J.State.randomQueueIndex = 0;

          J.syncRandomQueueWithSongs();

          const afterFirstDuplicate = {
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex
          };

          J.State.randomQueueIndex = 2;
          J.syncRandomQueueWithSongs();

          return {
            afterFirstDuplicate,
            afterSecondDuplicate: {
              queue: [...J.State.randomQueue],
              index: J.State.randomQueueIndex
            }
          };
        }
        """
    )

    assert result == {
        "afterFirstDuplicate": {
            "queue": ["song1", "song2", "song1", "song3"],
            "index": 0,
        },
        "afterSecondDuplicate": {
            "queue": ["song1", "song2", "song1", "song3"],
            "index": 2,
        },
    }


@pytest.mark.frontend
def test_jukebox_random_sync_preserves_queued_anchor_without_current_song(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          J.State.playbackMode = 'random';
          J.State.currentSong = null;
          J.State.randomQueue = ['song1', 'song2'];
          J.State.randomQueueIndex = 1;

          J.syncRandomQueueWithSongs();

          const retainedQueuedAnchor = {
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex
          };

          J.State.currentSong = null;
          J.State.randomQueue = ['missing-song'];
          J.State.randomQueueIndex = 0;

          J.syncRandomQueueWithSongs();

          return {
            retainedQueuedAnchor,
            clearedMissingAnchor: {
              queue: [...J.State.randomQueue],
              index: J.State.randomQueueIndex
            }
          };
        }
        """
    )

    assert result == {
        "retainedQueuedAnchor": {
            "queue": ["song1", "song2"],
            "index": 1,
        },
        "clearedMissingAnchor": {
            "queue": [],
            "index": -1,
        },
    }


@pytest.mark.frontend
def test_jukebox_random_next_appends_only_at_queue_end(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          const played = [];
          let randomCalls = 0;
          const originalRandom = Math.random;
          Math.random = () => {
            randomCalls += 1;
            return 0;
          };
          J.playSong = (songId, options = {}) => {
            played.push({ songId, fromQueue: options.fromQueue === true });
            J.State.currentSong = J.State.songs.find((song) => song.id === songId) || null;
          };

          J.State.playbackMode = 'random';
          J.State.currentSong = J.State.songs[0];
          J.State.randomQueue = ['song1'];
          J.State.randomQueueIndex = 0;
          J.playAdjacentSong(1);
          const appended = {
            played: [...played],
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            randomCalls
          };

          J.State.currentSong = J.State.songs[1];
          J.State.randomQueue = ['song1', 'song2', 'song3'];
          J.State.randomQueueIndex = 1;
          J.playAdjacentSong(1);
          Math.random = originalRandom;

          return {
            appended,
            finalPlayed: played,
            finalQueue: J.State.randomQueue,
            finalIndex: J.State.randomQueueIndex,
            finalRandomCalls: randomCalls
          };
        }
        """
    )

    assert result == {
        "appended": {
            "played": [{"songId": "song2", "fromQueue": True}],
            "queue": ["song1", "song2"],
            "index": 1,
            "randomCalls": 1,
        },
        "finalPlayed": [
            {"songId": "song2", "fromQueue": True},
            {"songId": "song3", "fromQueue": True},
        ],
        "finalQueue": ["song1", "song2", "song3"],
        "finalIndex": 2,
        "finalRandomCalls": 1,
    }


@pytest.mark.frontend
def test_jukebox_random_rapid_next_uses_advanced_queue_anchor(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          const played = [];
          let randomCalls = 0;
          const randomValues = [0, 0];
          const originalRandom = Math.random;
          Math.random = () => randomValues[randomCalls++] || 0;
          J.playSong = (songId, options = {}) => {
            played.push({ songId, fromQueue: options.fromQueue === true });
          };

          J.State.playbackMode = 'random';
          J.State.currentSong = J.State.songs[0];
          J.State.randomQueue = ['song1'];
          J.State.randomQueueIndex = 0;

          J.playAdjacentSong(1);
          J.playAdjacentSong(1);
          Math.random = originalRandom;

          return {
            currentSong: J.State.currentSong && J.State.currentSong.id,
            played,
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex,
            randomCalls
          };
        }
        """
    )

    assert result == {
        "currentSong": "song1",
        "played": [
            {"songId": "song2", "fromQueue": True},
            {"songId": "song3", "fromQueue": True},
        ],
        "queue": ["song1", "song2", "song3"],
        "index": 2,
        "randomCalls": 2,
    }


@pytest.mark.frontend
def test_jukebox_random_rapid_previous_does_not_stop_stale_current_song(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          let stopped = false;
          J.stopPlayback = () => {
            stopped = true;
            J.State.isPlaying = false;
          };

          J.State.playbackMode = 'random';
          J.State.currentSong = J.State.songs[0];
          J.State.isPlaying = true;
          J.State.isPaused = false;
          J.State.randomQueue = ['song1', 'song2'];
          J.State.randomQueueIndex = 1;

          J.playAdjacentSong(-1);

          return {
            stopped,
            currentSong: J.State.currentSong && J.State.currentSong.id,
            isPlaying: J.State.isPlaying,
            queue: [...J.State.randomQueue],
            index: J.State.randomQueueIndex
          };
        }
        """
    )

    assert result == {
        "stopped": False,
        "currentSong": "song1",
        "isPlaying": True,
        "queue": ["song1", "song2"],
        "index": 0,
    }


@pytest.mark.frontend
def test_jukebox_random_previous_uses_accumulated_queue(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          const played = [];
          J.playSong = (songId, options = {}) => {
            played.push({ songId, fromQueue: options.fromQueue === true });
            J.State.currentSong = J.State.songs.find((song) => song.id === songId) || null;
          };

          J.State.playbackMode = 'random';
          J.State.currentSong = J.State.songs[1];
          J.State.randomQueue = ['song1', 'song2', 'song3'];
          J.State.randomQueueIndex = 1;
          J.playAdjacentSong(-1);
          J.playAdjacentSong(-1);

          return {
            played,
            queue: J.State.randomQueue,
            index: J.State.randomQueueIndex
          };
        }
        """
    )

    assert result == {
        "played": [{"songId": "song1", "fromQueue": True}],
        "queue": ["song1", "song2", "song3"],
        "index": 0,
    }


@pytest.mark.frontend
def test_jukebox_random_audio_end_advances_queue_and_skips_idle_restore(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const stopArgs = [];
          const played = [];
          const originalRandom = Math.random;
          Math.random = () => 0;
          J.stopVMD = (skipIdleRestore) => {
            stopArgs.push(skipIdleRestore);
          };
          J.updateStoppedStatus = () => {};
          J.playSong = async (songId, options = {}) => {
            played.push({ songId, fromQueue: options.fromQueue === true });
            J.State.currentSong = J.State.songs.find((song) => song.id === songId) || null;
            // 真实的 playSong 成功时返回歌曲、失败返回 null，自动续播据此决定要不要
            // 回滚随机队列。桩必须照这个契约来，否则这里会被当成「这首没播成」。
            return J.State.currentSong;
          };
          J.getModelType = () => 'mmd';
          J.State.isOpen = true;
          J.State.playbackMode = 'random';
          J.State.songs[1].boundActions = [{ id: 'action-song2', name: 'Action', format: 'vmd' }];
          J.State.songs[1].defaultAction = 'action-song2';
          J.State.currentSong = J.State.songs[0];
          J.State.randomQueue = ['song1'];
          J.State.randomQueueIndex = 0;

          J.handleAudioEnded({ options: { loop: 'none' } });
          await new Promise((resolve) => setTimeout(resolve, 0));
          Math.random = originalRandom;

          return {
            stopArgs,
            played,
            queue: J.State.randomQueue,
            index: J.State.randomQueueIndex
          };
        }
        """
    )

    assert result == {
        "stopArgs": [True],
        "played": [{"songId": "song2", "fromQueue": True}],
        "queue": ["song1", "song2"],
        "index": 1,
    }


@pytest.mark.frontend
def test_jukebox_random_user_selected_song_resets_queue_anchor(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          J.stopPlayback = () => {};
          J.playAudio = async () => {};
          J.getActionForModel = () => null;
          J.updatePlayingStatus = () => {};
          J.updateCalibrationDisplay = () => {};
          J.State.playbackMode = 'random';
          J.State.currentSong = J.State.songs[0];
          J.State.randomQueue = ['song1', 'song3'];
          J.State.randomQueueIndex = 1;

          await J.playSong('song2');

          return {
            currentSong: J.State.currentSong && J.State.currentSong.id,
            queue: J.State.randomQueue,
            index: J.State.randomQueueIndex
          };
        }
        """
    )

    assert result == {
        "currentSong": "song2",
        "queue": ["song2"],
        "index": 0,
    }


@pytest.mark.frontend
def test_jukebox_manual_previous_uses_last_song_without_current_song(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          J.State.currentSong = null;
          const noCurrentPrevious = J.getManualAdjacentSong(-1)?.id;
          const noCurrentNext = J.getManualAdjacentSong(1)?.id;

          J.State.currentSong = { id: 'missing-song' };
          const missingCurrentPrevious = J.getManualAdjacentSong(-1)?.id;
          const missingCurrentNext = J.getManualAdjacentSong(1)?.id;

          return {
            noCurrentPrevious,
            noCurrentNext,
            missingCurrentPrevious,
            missingCurrentNext
          };
        }
        """
    )

    assert result == {
        "noCurrentPrevious": "song3",
        "noCurrentNext": "song1",
        "missingCurrentPrevious": "song3",
        "missingCurrentNext": "song1",
    }


@pytest.mark.frontend
def test_jukebox_drag_sort_requires_unlock_button(mock_page: Page):
    setup_jukebox_page(mock_page)

    lock_button = mock_page.locator(".jukebox-sort-lock-btn")
    first_row = mock_page.locator('#jukebox-song-list tr[data-song-id="song1"]')

    assert lock_button.get_attribute("aria-pressed") == "false"
    assert first_row.evaluate("(row) => row.draggable") is False

    lock_button.click()
    assert lock_button.get_attribute("aria-pressed") == "true"
    assert first_row.evaluate("(row) => row.draggable") is True

    lock_button.click()
    assert lock_button.get_attribute("aria-pressed") == "false"
    assert first_row.evaluate("(row) => row.draggable") is False


@pytest.mark.frontend
def test_jukebox_drag_sort_order_is_rendered_and_persisted(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          const moved = J.moveSongInPlaylist('song3', 'song1', false);
          const renderedOrder = Array.from(document.querySelectorAll('#jukebox-song-list tr'))
            .map((row) => row.dataset.songId);
          const saved = JSON.parse(window.__jukeboxLocalStore['neko.jukebox.songOrder']);
          const reapplied = J.applySavedSongOrder([
            { id: 'song1' },
            { id: 'song2' },
            { id: 'song3' },
            { id: 'song4' }
          ]).map((song) => song.id);
          return { moved, renderedOrder, saved, reapplied };
        }
        """
    )

    assert result == {
        "moved": True,
        "renderedOrder": ["song3", "song1", "song2"],
        "saved": ["song3", "song1", "song2"],
        "reapplied": ["song3", "song1", "song2", "song4"],
    }


def setup_headless_jukebox_page(mock_page: Page) -> None:
    mock_page.set_content("<!DOCTYPE html><html><body></body></html>")
    mock_page.evaluate(
        """
        () => {
          const store = {};
          Object.defineProperty(window, 'localStorage', {
            configurable: true,
            value: {
              getItem(key) {
                return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
              },
              setItem(key, value) {
                store[key] = String(value);
              },
              removeItem(key) {
                delete store[key];
              },
              clear() {
                Object.keys(store).forEach((key) => delete store[key]);
              }
            }
          });
          window.__jukeboxLocalStore = store;
          window.t = (key, fallback) => typeof fallback === 'string' ? fallback : key;
          window.fetch = async (url, options = {}) => {
            if (options.method === 'HEAD') {
              const available = !String(url).includes('missing');
              return { ok: available, status: available ? 200 : 404 };
            }
            if (url === '/api/jukebox/config') {
              return {
                ok: true,
                json: async () => ({
                  configRevision: 'rev-headless',
                  songs: {
                    song1: { name: 'Song 1', artist: 'A', audio: 'songs/song1.mp3', visible: true },
                    song2: { name: 'Song 2', artist: 'B', audio: 'songs/song2.mp3', visible: true },
                    song3: { name: 'Song 3', artist: 'C', audio: 'songs/song3.mp3', visible: true },
                    song4: { name: '桃源恋歌', artist: 'GARNiDELiA', audio: 'songs/tougen-renka.mp3', visible: true }
                  },
                  actions: {},
                  bindings: {}
                })
              };
            }
            throw new Error('Unexpected fetch: ' + url);
          };
          window.APlayer = class {
            constructor(options) {
              this.options = options;
              // 真实 APlayer 的 storage 层是 `data.volume = data.volume || options.volume`，
              // 也就是上一次会话存下的音量优先于构造参数。不照着建模的话，只改
              // 构造参数的「假修复」也能骗过测试。
              let stored = null;
              try {
                const raw = localStorage.getItem('aplayer-setting');
                stored = raw ? (JSON.parse(raw) || {}).volume : null;
              } catch (_) {}
              const effective = (typeof stored === 'number' ? stored : null)
                ?? (typeof options.volume === 'number' ? options.volume : 1);
              this.audio = { volume: effective, duration: 0, currentTime: 0, paused: true };
              this.events = {};
              this.list = {
                items: [],
                clear: () => { this.list.items = []; },
                add: (items) => { this.list.items = items; }
              };
              window.__lastAPlayer = this;
            }
            on(name, handler) { this.events[name] = handler; }
            play() { this.audio.paused = false; this.played = true; }
            pause() { this.audio.paused = true; }
            seek(value) { this.audio.currentTime = value; }
            destroy() { this.destroyed = true; }
          };
        }
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_SCRIPT)


@pytest.mark.frontend
def test_jukebox_execute_control_play_headless_loads_without_ui(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const result = await window.Jukebox.executeControl({ action: 'play', query: 'Song' });
          return {
            result,
            hasUi: !!document.querySelector('.jukebox-wrapper'),
            hasRuntimeHost: !!document.getElementById('neko-jukebox-runtime-host'),
            currentSong: window.Jukebox.State.currentSong && window.Jukebox.State.currentSong.id,
            isRuntimeReady: window.Jukebox.State.isRuntimeReady,
            playerItems: window.__lastAPlayer.list.items.map((item) => item.name),
            playerUrls: window.__lastAPlayer.list.items.map((item) => item.url)
          };
        }
        """
    )

    assert result == {
        "result": {
            "ok": True,
            "action": "play",
            "song": {"id": "song1", "name": "Song 1", "artist": "A"},
            "actionStatus": "no_action",
        },
        "hasUi": False,
        "hasRuntimeHost": True,
        "currentSong": "song1",
        "isRuntimeReady": True,
        "playerItems": ["Song 1"],
        "playerUrls": ["/api/jukebox/file/songs/song1.mp3"],
    }


@pytest.mark.frontend
def test_jukebox_execute_control_same_song_replays_instead_of_stopping(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const first = await window.Jukebox.executeControl({ action: 'play', query: 'Song 1' });
          const second = await window.Jukebox.executeControl({ action: 'play', query: 'Song 1' });
          return {
            first,
            second,
            currentSong: window.Jukebox.State.currentSong && window.Jukebox.State.currentSong.id,
            isPlaying: window.Jukebox.State.isPlaying,
            isPaused: window.Jukebox.State.isPaused,
            playerItems: window.__lastAPlayer.list.items.map((item) => item.name)
          };
        }
        """
    )

    assert result == {
        "first": {
            "ok": True,
            "action": "play",
            "song": {"id": "song1", "name": "Song 1", "artist": "A"},
            "actionStatus": "no_action",
        },
        "second": {
            "ok": True,
            "action": "play",
            "song": {"id": "song1", "name": "Song 1", "artist": "A"},
            "actionStatus": "no_action",
        },
        "currentSong": "song1",
        "isPlaying": True,
        "isPaused": False,
        "playerItems": ["Song 1"],
    }


@pytest.mark.frontend
def test_jukebox_execute_control_discards_stale_preflight_play(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const originalFetch = window.fetch;
          let releaseSong1Head;
          window.fetch = async (url, options = {}) => {
            if (options.method === 'HEAD' && String(url).includes('song1.mp3')) {
              return await new Promise((resolve) => {
                releaseSong1Head = () => resolve({ ok: true, status: 200 });
              });
            }
            return originalFetch(url, options);
          };

          const firstPromise = window.Jukebox.executeControl({ action: 'play', query: 'Song 1' });
          while (typeof releaseSong1Head !== 'function') {
            await new Promise((resolve) => setTimeout(resolve, 0));
          }
          const second = await window.Jukebox.executeControl({ action: 'play', query: 'Song 2' });
          releaseSong1Head();
          const first = await firstPromise;

          return {
            first,
            second,
            currentSong: window.Jukebox.State.currentSong && window.Jukebox.State.currentSong.id,
            playerItems: window.__lastAPlayer.list.items.map((item) => item.name)
          };
        }
        """
    )

    assert result == {
        "first": {
            "ok": False,
            "action": "play",
            "message": "play_superseded",
            "song": {"id": "song1", "name": "Song 1", "artist": "A"},
        },
        "second": {
            "ok": True,
            "action": "play",
            "song": {"id": "song2", "name": "Song 2", "artist": "B"},
            "actionStatus": "no_action",
        },
        "currentSong": "song2",
        "playerItems": ["Song 2"],
    }


@pytest.mark.frontend
def test_jukebox_execute_control_stop_discards_pending_play(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const originalFetch = window.fetch;
          let releaseSong1Head;
          window.fetch = async (url, options = {}) => {
            if (options.method === 'HEAD' && String(url).includes('song1.mp3')) {
              return await new Promise((resolve) => {
                releaseSong1Head = () => resolve({ ok: true, status: 200 });
              });
            }
            return originalFetch(url, options);
          };

          const playPromise = window.Jukebox.executeControl({ action: 'play', query: 'Song 1' });
          while (typeof releaseSong1Head !== 'function') {
            await new Promise((resolve) => setTimeout(resolve, 0));
          }
          const stop = await window.Jukebox.executeControl({ action: 'stop' });
          releaseSong1Head();
          const play = await playPromise;

          return {
            play,
            stop,
            currentSong: window.Jukebox.State.currentSong && window.Jukebox.State.currentSong.id,
            isPlaying: window.Jukebox.State.isPlaying,
            playerItems: window.__lastAPlayer.list.items.map((item) => item.name)
          };
        }
        """
    )

    assert result == {
        # stop 现在推进的是独立的取消世代，这条 play 在预检之前就被判定为
        # 「已取消」——比原来的 play_superseded 更准，也省掉一次 HEAD 预检。
        "play": {
            "ok": False,
            "action": "play",
            "message": "play_cancelled",
            "song": {"id": "song1", "name": "Song 1", "artist": "A"},
        },
        "stop": {"ok": True, "action": "stop"},
        "currentSong": None,
        "isPlaying": False,
        "playerItems": [],
    }


@pytest.mark.frontend
def test_jukebox_play_song_skips_stale_action_start(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          let releaseAction;
          const animationCalls = [];
          J.getModelType = () => 'vrm';
          J.playVRMA = async (url) => { animationCalls.push(url); };
          J.getActionAvailability = async (song) => {
            if (song.id === 'song1') {
              return await new Promise((resolve) => {
                releaseAction = () => resolve({
                  ok: true,
                  status: 'action_ready',
                  action: { id: 'action1', name: 'Dance 1', file: 'actions/song1.vrma' },
                  url: '/api/jukebox/file/actions/song1.vrma'
                });
              });
            }
            return { ok: true, status: 'no_action', action: null, url: '' };
          };

          const firstPromise = J.playSong('song1');
          while (typeof releaseAction !== 'function') {
            await new Promise((resolve) => setTimeout(resolve, 0));
          }
          const second = await J.playSong('song2');
          releaseAction();
          const first = await firstPromise;

          return {
            first: first && first.id,
            second: second && second.id,
            currentSong: J.State.currentSong && J.State.currentSong.id,
            animationCalls
          };
        }
        """
    )

    assert result == {
        "first": None,
        "second": "song2",
        "currentSong": "song2",
        "animationCalls": [],
    }


@pytest.mark.frontend
def test_jukebox_play_song_skips_stale_vrma_internal_start(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          let releaseAnimation;
          const animationStarts = [];
          J.getModelType = () => 'vrm';
          J.getActionAvailability = async (song) => {
            if (song.id === 'song1') {
              return {
                ok: true,
                status: 'action_ready',
                action: { id: 'action1', name: 'Dance 1', file: 'actions/song1.vrma' },
                url: '/api/jukebox/file/actions/song1.vrma'
              };
            }
            return { ok: true, status: 'no_action', action: null, url: '' };
          };
          window.vrmManager = {
            playVRMAAnimation: async (url, options = {}) => {
              return await new Promise((resolve) => {
                releaseAnimation = () => {
                  const shouldStart = typeof options.shouldStart === 'function' ? options.shouldStart() : true;
                  if (shouldStart) animationStarts.push(url);
                  resolve(shouldStart);
                };
              });
            }
          };

          const firstPromise = J.playSong('song1');
          while (typeof releaseAnimation !== 'function') {
            await new Promise((resolve) => setTimeout(resolve, 0));
          }
          const second = await J.playSong('song2');
          releaseAnimation();
          const first = await firstPromise;

          return {
            first: first && first.id,
            second: second && second.id,
            currentSong: J.State.currentSong && J.State.currentSong.id,
            animationStarts,
            isVMDPlaying: J.State.isVMDPlaying
          };
        }
        """
    )

    assert result == {
        "first": None,
        "second": "song2",
        "currentSong": "song2",
        "animationStarts": [],
        "isVMDPlaying": False,
    }


@pytest.mark.frontend
def test_jukebox_execute_control_play_uses_fuzzy_matching(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const result = await window.Jukebox.executeControl({ action: 'play', query: '桃园' });
          return {
            result,
            currentSong: window.Jukebox.State.currentSong && window.Jukebox.State.currentSong.id,
            playerItems: window.__lastAPlayer.list.items.map((item) => item.name)
          };
        }
        """
    )

    assert result == {
        "result": {
            "ok": True,
            "action": "play",
            "song": {"id": "song4", "name": "桃源恋歌", "artist": "GARNiDELiA"},
            "actionStatus": "no_action",
        },
        "currentSong": "song4",
        "playerItems": ["桃源恋歌"],
    }


@pytest.mark.frontend
def test_jukebox_execute_control_uses_canonical_control_keys(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const skipResult = await window.Jukebox.executeControl({ action: 'skip' });
          const cutResult = await window.Jukebox.executeControl({ action: 'cut' });
          const commandOnlyResult = await window.Jukebox.executeControl({ command: 'stop' });
          const legacyNameResult = await window.Jukebox.executeControl({ action: 'play', name: 'Song 2' });
          return {
            skipResult,
            cutResult,
            commandOnlyResult,
            legacyNameResult,
            currentSong: window.Jukebox.State.currentSong && window.Jukebox.State.currentSong.id
          };
        }
        """
    )

    assert result == {
        "skipResult": {
            "ok": False,
            "action": "skip",
            "message": "unsupported_jukebox_action",
        },
        "cutResult": {
            "ok": False,
            "action": "cut",
            "message": "unsupported_jukebox_action",
        },
        "commandOnlyResult": {
            "ok": False,
            "action": "",
            "message": "unsupported_jukebox_action",
        },
        "legacyNameResult": {
            "ok": True,
            "action": "play",
            "song": {"id": "song1", "name": "Song 1", "artist": "A"},
            "actionStatus": "no_action",
        },
        "currentSong": "song1",
    }


@pytest.mark.frontend
def test_jukebox_execute_control_sets_and_adjusts_volume_headless(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const setResult = await window.Jukebox.executeControl({ action: 'set_volume', value: 35 });
          const afterSet = window.__lastAPlayer.audio.volume;
          const adjustResult = await window.Jukebox.executeControl({ action: 'adjust_volume', value: 10 });
          const afterAdjust = window.__lastAPlayer.audio.volume;
          const invalidSet = await window.Jukebox.executeControl({ action: 'set_volume', value: 130 });
          const invalidAdjust = await window.Jukebox.executeControl({ action: 'adjust_volume', value: 'louder' });
          return {
            setResult,
            afterSet,
            adjustResult,
            afterAdjust,
            invalidSet,
            invalidAdjust,
            hasUi: !!document.querySelector('.jukebox-wrapper'),
            hasRuntimeHost: !!document.getElementById('neko-jukebox-runtime-host')
          };
        }
        """
    )

    assert result == {
        "setResult": {"ok": True, "action": "set_volume", "volume": 0.35},
        "afterSet": 0.35,
        "adjustResult": {"ok": True, "action": "adjust_volume", "volume": 0.45, "value": 0.1},
        "afterAdjust": 0.45,
        "invalidSet": {"ok": False, "action": "set_volume", "message": "invalid_volume"},
        "invalidAdjust": {"ok": False, "action": "adjust_volume", "message": "invalid_volume_delta"},
        "hasUi": False,
        "hasRuntimeHost": True,
    }


@pytest.mark.frontend
def test_jukebox_execute_control_sets_playback_mode_without_ui(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const randomResult = await window.Jukebox.executeControl({ action: 'set_mode', mode: 'random' });
          const invalidResult = await window.Jukebox.executeControl({ action: 'set_mode', mode: 'shuffle' });
          return {
            randomResult,
            invalidResult,
            playbackMode: window.Jukebox.State.playbackMode,
            storedMode: window.localStorage.getItem('neko.jukebox.playbackMode'),
            hasUi: !!document.querySelector('.jukebox-wrapper'),
            hasRuntimeHost: !!document.getElementById('neko-jukebox-runtime-host')
          };
        }
        """
    )

    assert result == {
        "randomResult": {"ok": True, "action": "set_mode", "mode": "random"},
        "invalidResult": {"ok": False, "action": "set_mode", "message": "invalid_playback_mode"},
        "playbackMode": "random",
        "storedMode": '"random"',
        "hasUi": False,
        "hasRuntimeHost": False,
    }


@pytest.mark.frontend
def test_jukebox_builtin_paths_keep_resource_directories_via_control(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          window.fetch = async (url, options = {}) => {
            if (options.method === 'HEAD') {
              return { ok: !String(url).includes('missing'), status: String(url).includes('missing') ? 404 : 200 };
            }
            if (url === '/api/jukebox/config') {
              return {
                ok: true,
                json: async () => ({
                  configRevision: 'rev-builtin-paths',
                  songs: {
                    song_001: {
                      name: '桃源恋歌',
                      artist: 'GARNiDELiA',
                      audio: 'songs/song_001.mp3',
                      visible: true,
                      isBuiltin: true,
                      defaultAction: 'action_001'
                    }
                  },
                  actions: {
                    action_001: {
                      name: '桃源恋歌',
                      file: 'actions/song_001.vrma',
                      format: 'vrma',
                      visible: true,
                      isBuiltin: true
                    }
                  },
                  bindings: {
                    song_001: { action_001: { offset: 0 } }
                  }
                })
              };
            }
            throw new Error('Unexpected fetch: ' + url);
          };
          window.lanlan_config = { model_type: 'live3d', live3d_sub_type: 'vrm' };
          const vrmaCalls = [];
          window.vrmManager = {
            playVRMAAnimation: async (url) => vrmaCalls.push(url)
          };

          await window.Jukebox.executeControl({ action: 'play', query: '桃园' });

          return {
            audio: window.Jukebox.State.songs[0].audio,
            audioUrl: window.__lastAPlayer.list.items[0].url,
            vrmaCalls
          };
        }
        """
    )

    assert result == {
        "audio": "songs/song_001.mp3",
        "audioUrl": "/api/jukebox/file/songs/song_001.mp3",
        "vrmaCalls": ["/api/jukebox/file/actions/song_001.vrma"],
    }


@pytest.mark.frontend
def test_jukebox_execute_control_does_not_play_when_audio_missing(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          window.fetch = async (url, options = {}) => {
            if (options.method === 'HEAD') {
              return { ok: !String(url).includes('missing'), status: String(url).includes('missing') ? 404 : 200 };
            }
            if (url === '/api/jukebox/config') {
              return {
                ok: true,
                json: async () => ({
                  configRevision: 'rev-missing-audio',
                  songs: {
                    missingSong: {
                      name: 'Missing Song',
                      artist: 'A',
                      audio: 'songs/missing.mp3',
                      visible: true
                    }
                  },
                  actions: {},
                  bindings: {}
                })
              };
            }
            throw new Error('Unexpected fetch: ' + url);
          };

          const result = await window.Jukebox.executeControl({ action: 'play', query: 'Missing' });
          return {
            result,
            playerItems: window.__lastAPlayer.list.items,
            played: window.__lastAPlayer.played === true,
            currentSong: window.Jukebox.State.currentSong
          };
        }
        """
    )

    assert result == {
        "result": {
            "ok": False,
            "action": "play",
            "message": "audio_not_found",
            "song": {"id": "missingSong", "name": "Missing Song", "artist": "A"},
        },
        "playerItems": [],
        "played": False,
        "currentSong": None,
    }


@pytest.mark.frontend
def test_jukebox_execute_control_skips_missing_action_but_plays_audio(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          window.fetch = async (url, options = {}) => {
            if (options.method === 'HEAD') {
              return { ok: !String(url).includes('missing-action'), status: String(url).includes('missing-action') ? 404 : 200 };
            }
            if (url === '/api/jukebox/config') {
              return {
                ok: true,
                json: async () => ({
                  configRevision: 'rev-missing-action',
                  songs: {
                    songWithMissingAction: {
                      name: 'Song With Missing Action',
                      artist: 'A',
                      audio: 'songs/song1.mp3',
                      visible: true,
                      defaultAction: 'missingAction'
                    }
                  },
                  actions: {
                    missingAction: {
                      name: 'Missing Action',
                      file: 'actions/missing-action.vrma',
                      format: 'vrma',
                      visible: true
                    }
                  },
                  bindings: {
                    songWithMissingAction: { missingAction: { offset: 0 } }
                  }
                })
              };
            }
            throw new Error('Unexpected fetch: ' + url);
          };
          window.lanlan_config = { model_type: 'live3d', live3d_sub_type: 'vrm' };
          const vrmaCalls = [];
          window.vrmManager = {
            playVRMAAnimation: async (url) => vrmaCalls.push(url)
          };

          const result = await window.Jukebox.executeControl({ action: 'play', query: 'Missing Action' });
          return {
            result,
            playerItems: window.__lastAPlayer.list.items.map((item) => item.name),
            played: window.__lastAPlayer.played === true,
            currentSong: window.Jukebox.State.currentSong && window.Jukebox.State.currentSong.id,
            vrmaCalls
          };
        }
        """
    )

    assert result == {
        "result": {
            "ok": True,
            "action": "play",
            "song": {"id": "songWithMissingAction", "name": "Song With Missing Action", "artist": "A"},
            "actionStatus": "action_not_found",
        },
        "playerItems": ["Song With Missing Action"],
        "played": True,
        "currentSong": "songWithMissingAction",
        "vrmaCalls": [],
    }


@pytest.mark.frontend
def test_jukebox_close_preserves_headless_runtime(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          let fullCloseEvents = 0;
          window.addEventListener('neko:jukebox-full-close', () => { fullCloseEvents += 1; });

          const wrapper = document.createElement('div');
          wrapper.className = 'jukebox-wrapper';
          wrapper.innerHTML = '<div class="jukebox-container"></div>';
          document.body.appendChild(wrapper);
          const style = document.createElement('style');
          document.head.appendChild(style);

          J.State.container = wrapper;
          J.State.styleElement = style;
          J.State.isOpen = true;
          J.State.isHidden = false;
          J._broadcastChannel = {
            onmessage: () => {},
            closed: false,
            close() { this.closed = true; }
          };
          const channel = J._broadcastChannel;

          J.close();

          return {
            fullCloseEvents,
            hasUi: !!document.querySelector('.jukebox-wrapper'),
            hasStyle: document.head.contains(style),
            hasRuntimeHost: !!document.getElementById('neko-jukebox-runtime-host'),
            isRuntimeReady: J.State.isRuntimeReady,
            headlessRuntimeRequested: J.State.headlessRuntimeRequested,
            playerDestroyed: window.__lastAPlayer.destroyed === true,
            currentSong: J.State.currentSong && J.State.currentSong.id,
            songCount: J.State.songs.length,
            channelClosed: channel.closed === true
          };
        }
        """
    )

    assert result == {
        "fullCloseEvents": 0,
        "hasUi": False,
        "hasStyle": False,
        "hasRuntimeHost": True,
        "isRuntimeReady": True,
        "headlessRuntimeRequested": True,
        "playerDestroyed": False,
        "currentSong": "song1",
        "songCount": 4,
        "channelClosed": True,
    }


@pytest.mark.frontend
def test_jukebox_execute_control_next_and_stop_headless(mock_page: Page):
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          await window.Jukebox.executeControl({ action: 'play', query: 'Song 1' });
          const nextResult = await window.Jukebox.executeControl({ action: 'next' });
          const previousResult = await window.Jukebox.executeControl({ action: 'previous' });
          window.Jukebox.State.playbackMode = 'random';
          window.Jukebox.State.randomQueue = ['song1', 'song2'];
          window.Jukebox.State.randomQueueIndex = 1;
          const stopResult = await window.Jukebox.executeControl({ action: 'stop' });
          return {
            nextResult,
            previousResult,
            stopResult,
            currentSong: window.Jukebox.State.currentSong,
            isPlaying: window.Jukebox.State.isPlaying,
            randomQueue: window.Jukebox.State.randomQueue,
            randomQueueIndex: window.Jukebox.State.randomQueueIndex,
            hasRuntimeHost: !!document.getElementById('neko-jukebox-runtime-host')
          };
        }
        """
    )

    assert result["nextResult"] == {
        "ok": True,
        "action": "next",
        "song": {"id": "song2", "name": "Song 2", "artist": "B"},
        "actionStatus": "no_action",
    }
    assert result["previousResult"] == {
        "ok": True,
        "action": "previous",
        "song": {"id": "song1", "name": "Song 1", "artist": "A"},
        "actionStatus": "no_action",
    }
    assert result["stopResult"] == {"ok": True, "action": "stop"}
    assert result["currentSong"] is None
    assert result["isPlaying"] is False
    assert result["randomQueue"] == []
    assert result["randomQueueIndex"] == -1
    assert result["hasRuntimeHost"] is True


@pytest.mark.frontend
def test_jukebox_loader_restores_native_facade_after_full_unload(mock_page: Page):
    mock_page.set_content(
        """
        <script>
          window.nativeToggleCount = 0;
          window.__nekoJukeboxToggle = function() {
            window.nativeToggleCount += 1;
          };
          window.t = (key, fallback) => typeof fallback === 'string' ? fallback : key;
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    result = mock_page.evaluate(
        """
        () => {
          const originalSetTimeout = window.setTimeout;
          window.setTimeout = (handler, delay) => {
            if (delay === 3000) {
              handler();
              return 1;
            }
            return originalSetTimeout(handler, delay);
          };

          window.__nekoJukeboxLoader.unload();
          window.Jukebox.toggle();

          return {
            hasFacade: window.Jukebox.__nativeBridgeFacade === true,
            hasExecuteControl: typeof window.Jukebox.executeControl === 'function',
            nativeToggleCount: window.nativeToggleCount,
            webLoaderToggle: !!window.__nekoJukeboxToggle.__nekoJukeboxWebLoader
          };
        }
        """
    )

    assert result == {
        "hasFacade": True,
        "hasExecuteControl": True,
        "nativeToggleCount": 1,
        "webLoaderToggle": False,
    }


@pytest.mark.frontend
def test_jukebox_loader_exposes_control_on_jukebox_key_only(mock_page: Page):
    mock_page.set_content(
        """
        <script>
          window.t = (key, fallback) => typeof fallback === 'string' ? fallback : key;
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    result = mock_page.evaluate(
        """
        () => ({
          hasJukeboxFacade: !!window.Jukebox && window.Jukebox.__nekoLazyFacade === true,
          hasExecuteControl: typeof window.Jukebox.executeControl === 'function',
          hasEnsureRuntime: typeof window.Jukebox.ensureRuntime === 'function',
          hasInit: typeof window.Jukebox.init === 'function',
          initReturns: window.Jukebox.init(),
          loaderHasControl: Object.prototype.hasOwnProperty.call(window.__nekoJukeboxLoader, 'control')
        })
        """
    )

    assert result == {
        "hasJukeboxFacade": True,
        "hasExecuteControl": True,
        "hasEnsureRuntime": True,
        "hasInit": True,
        "initReturns": None,
        "loaderHasControl": False,
    }


@pytest.mark.frontend
def test_jukebox_loader_reloads_stale_control_api_with_versioned_url(mock_page: Page):
    requested_urls = []

    def fulfill_loader(route):
        route.fulfill(
            status=200,
            content_type="application/javascript",
            body=JUKEBOX_LOADER_SCRIPT,
        )

    def fulfill_jukebox(route):
        requested_urls.append(route.request.url)
        # 多 part 加载：桩对象放在 bootstrap（真实 bootstrap.js 也只负责建 window.Jukebox），
        # 其余 part 返回空体，避免桩被后续 Object.assign 覆盖。
        if not route.request.url.split("?")[0].endswith("/bootstrap.js"):
            route.fulfill(status=200, content_type="application/javascript", body="")
            return
        route.fulfill(
            status=200,
            content_type="application/javascript",
            body="""
              window.Jukebox = {
                controlApiVersion: 3,
                supportedControlActions: ['play', 'next', 'previous', 'stop', 'set_volume', 'adjust_volume', 'set_mode'],
                init() { window.__jukeboxInitCalled = true; },
                executeControl: async (command) => ({
                  ok: true,
                  action: command.action,
                  controlApiVersion: window.Jukebox.controlApiVersion
                })
              };
            """,
        )

    mock_page.route("**/static/jukebox/jukebox-loader.js*", fulfill_loader)
    mock_page.route("**/static/jukebox/jukebox/*.js*", fulfill_jukebox)
    mock_page.set_content(
        """
        <!DOCTYPE html>
        <html>
        <head><base href="http://127.0.0.1:48911/"></head>
        <body>
          <script>
            window.t = (key, fallback) => typeof fallback === 'string' ? fallback : key;
            window.Jukebox = {
              controlApiVersion: 1,
              executeControl: async (command) => ({
                ok: false,
                action: command.action,
                message: 'stale-control-api'
              })
            };
          </script>
        </body>
        </html>
        """
    )
    mock_page.add_script_tag(url="http://127.0.0.1:48911/static/jukebox/jukebox-loader.js?v=test-assets")

    result = mock_page.evaluate(
        """
        async () => {
          const result = await window.Jukebox.executeControl({ action: 'adjust_volume', value: 20 });
          return {
            result,
            initCalled: window.__jukeboxInitCalled === true,
            controlApiVersion: window.Jukebox.controlApiVersion,
            supported: window.Jukebox.supportedControlActions
          };
        }
        """
    )

    assert result == {
        "result": {"ok": True, "action": "adjust_volume", "controlApiVersion": 3},
        "initCalled": True,
        "controlApiVersion": 3,
        "supported": ["play", "next", "previous", "stop", "set_volume", "adjust_volume", "set_mode"],
    }
    assert [url.split("/")[-1].split("?")[0] for url in requested_urls] == [
        "bootstrap.js",
        "core.js",
        "manager.js",
        "shell.js",
        "transport.js",
        "wiring.js",
    ]
    assert all("v=test-assets" in url for url in requested_urls)
    assert all("jukebox_control_api=3" in url for url in requested_urls)


def test_jukebox_control_api_declares_versioned_supported_actions():
    assert "controlApiVersion: 3" in JUKEBOX_SCRIPT
    assert "supportedControlActions: ['play', 'next', 'previous', 'stop', 'set_volume', 'adjust_volume', 'set_mode']" in JUKEBOX_SCRIPT
    assert "REQUIRED_CONTROL_API_VERSION = 3" in JUKEBOX_LOADER_SCRIPT
    assert "jukebox_control_api" in JUKEBOX_LOADER_SCRIPT


@pytest.mark.frontend
def test_jukebox_audio_end_queued_next_respects_request_generation(mock_page: Page):
    setup_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const played = [];
          J.stopVMD = () => {};
          J.updateStoppedStatus = () => {};
          J.playSong = async (songId, options = {}) => {
            played.push({ songId, requestId: options.requestId });
            J.State.currentSong = J.State.songs.find((song) => song.id === songId) || null;
          };

          J.State.isOpen = true;
          J.State.playbackMode = 'sequence';
          J.State.playRequestId = 7;
          J.State.currentSong = J.State.songs[0];

          J.handleAudioEnded({ options: { loop: 'none' } });
          J.State.playRequestId += 1;
          await new Promise((resolve) => setTimeout(resolve, 0));

          return {
            played,
            currentSong: J.State.currentSong && J.State.currentSong.id,
            playRequestId: J.State.playRequestId
          };
        }
        """
    )

    assert result == {
        "played": [],
        "currentSong": None,
        "playRequestId": 9,
    }


# ---------------------------------------------------------------------------
# 以下六条是 #2293 评审检出问题的回归护栏，每条都对应一个具体的失败场景。
# ---------------------------------------------------------------------------


def _single_song_fetch_override() -> str:
    """A library with exactly one visible song: next/previous wrap onto it."""
    return """
          window.fetch = async (url, options = {}) => {
            if (options.method === 'HEAD') return { ok: true, status: 200 };
            if (url === '/api/jukebox/config') {
              return {
                ok: true,
                json: async () => ({
                  configRevision: 'rev-single',
                  songs: {
                    only: { name: 'Only Song', artist: 'A', audio: 'songs/only.mp3', visible: true }
                  },
                  actions: {},
                  bindings: {}
                })
              };
            }
            throw new Error('Unexpected fetch: ' + url);
          };
    """


@pytest.mark.frontend
def test_jukebox_control_next_replays_the_only_song_instead_of_stopping(mock_page: Page):
    """#1: next/previous on a one-song library must not stop playback.

    They wrap onto the song already playing, which used to fall into playSong's
    "same song -> stopPlayback()" branch while still reporting ok:true.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          %s
          const J = window.Jukebox;
          const played = await J.executeControl({ action: 'play', query: 'Only', headless: true });
          const afterPlay = {
            ok: played.ok,
            isPlaying: J.State.isPlaying,
            currentSong: J.State.currentSong && J.State.currentSong.id
          };

          // 逐条取样：next 停播之后 currentSong 会被清空，紧接着的 previous 又会
          // 重新起播，只看终态两种实现都是「在放」，护栏会变哑。
          const sample = async (action) => {
            window.__lastAPlayer.played = false;
            const outcome = await J.executeControl({ action, headless: true });
            return {
              ok: outcome.ok,
              // ok:true 必须名副其实：音乐还在放，而不是被「同曲即停」分支停掉。
              isPlaying: J.State.isPlaying,
              currentSong: J.State.currentSong && J.State.currentSong.id,
              replayed: window.__lastAPlayer.played === true
            };
          };

          return {
            afterPlay,
            playbackMode: J.State.playbackMode,
            afterNext: await sample('next'),
            afterPrevious: await sample('previous')
          };
        }
        """ % _single_song_fetch_override()
    )

    assert result == {
        "afterPlay": {"ok": True, "isPlaying": True, "currentSong": "only"},
        # 非随机模式才会走 getManualAdjacentSong，也就是评审描述的那个场景。
        "playbackMode": "sequence",
        "afterNext": {"ok": True, "isPlaying": True, "currentSong": "only", "replayed": True},
        "afterPrevious": {"ok": True, "isPlaying": True, "currentSong": "only", "replayed": True},
    }


@pytest.mark.frontend
def test_jukebox_control_volume_scale_does_not_invert_between_one_and_two(mock_page: Page):
    """#2: the 0-1 / 0-100 volume scales must not invert between 1 and 2.

    ``value: 1`` used to mean full scale and ``value: 2`` only 2%, so a larger
    request produced a 50x smaller volume.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          const setTo = async (value) => {
            await J.executeControl({ action: 'set_volume', value, headless: true });
            return Number(J.getCurrentVolume().toFixed(4));
          };
          const adjustFrom = async (start, delta) => {
            await J.executeControl({ action: 'set_volume', value: start * 100, headless: true });
            await J.executeControl({ action: 'adjust_volume', value: delta, headless: true });
            return Number(J.getCurrentVolume().toFixed(4));
          };

          const ladder = [];
          for (const value of [1, 2, 3, 10, 50, 100]) {
            ladder.push(await setTo(value));
          }

          return {
            ladder,
            // 0-1 之间的小数仍按比例
            halfAsRatio: await setTo(0.5),
            // 「调大一点」最常给的 1，应当是 +1 个百分点而不是拉满
            adjustByOne: await adjustFrom(0.3, 1),
            adjustByTwenty: await adjustFrom(0.3, 20),
            adjustByHalf: await adjustFrom(0.3, 0.5),
            adjustDown: await adjustFrom(0.3, -10)
          };
        }
        """
    )

    assert result["ladder"] == [0.01, 0.02, 0.03, 0.1, 0.5, 1.0]
    # 单调：1 到 100 之间不再出现「更大的请求得到更小的音量」。
    assert result["ladder"] == sorted(result["ladder"])
    assert result["halfAsRatio"] == 0.5
    assert result["adjustByOne"] == 0.31
    assert result["adjustByTwenty"] == 0.5
    assert result["adjustByHalf"] == 0.8
    assert result["adjustDown"] == 0.2


@pytest.mark.frontend
def test_jukebox_fuzzy_search_runs_in_a_worker(mock_page: Page):
    """#3: fuzzy scoring runs in a worker; a direct hit must not spawn one."""
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const NativeWorker = window.Worker;
          let constructed = 0;
          window.Worker = class extends NativeWorker {
            constructor(url) {
              super(url);
              constructed += 1;
            }
          };

          await J.ensureRuntime({ headless: true });

          // 子串直接命中：不必开线程。
          const direct = await J.findSongForQuery('Song 2');
          const afterDirect = constructed;

          // 「桃园」不是「桃源恋歌」的子串，只能靠模糊匹配。
          const fuzzy = await J.findSongForQuery('桃园');
          const afterFuzzy = constructed;

          window.Worker = NativeWorker;
          return {
            directId: direct && direct.id,
            fuzzyId: fuzzy && fuzzy.id,
            afterDirect,
            afterFuzzy,
            workerReleased: J.State.fuzzySearchWorker === null
          };
        }
        """
    )

    assert result == {
        "directId": "song2",
        "fuzzyId": "song4",
        "afterDirect": 0,
        "afterFuzzy": 1,
        "workerReleased": True,
    }


@pytest.mark.frontend
def test_jukebox_fuzzy_distance_stays_linear_in_candidate_length(mock_page: Page):
    """#3, second half: the algorithm itself must stay linear in the candidate.

    The worst case is a query that is not in the library at all, where the old
    start x length enumeration had no bound to prune against.
    """
    setup_headless_jukebox_page(mock_page)

    measured = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          const make = (n, alphabet) => {
            let s = '';
            for (let i = 0; i < n; i += 1) s += alphabet[(i * 7 + 3) % alphabet.length];
            return s;
          };
          // 最坏情况是「查一首曲库里没有的歌」：字符集不相交，一个近似窗口都命
          // 不中，旧实现的 start x length 枚举全程没有可剪枝的上界。
          const query = make(50, 'abcdefghij');
          const target = make(120, '0123456789') + make(120, '0123456789');
          const started = performance.now();
          let result = 0;
          for (let i = 0; i < 20; i += 1) {
            result = J.getBestFuzzyDistance(query, target);
          }
          return { elapsed: performance.now() - started, result: result === Infinity ? 'inf' : result };
        }
        """
    )

    # 这个形状下必须判定为不匹配 —— 先确认量的确实是「未命中」这条最坏路径。
    assert measured["result"] == "inf"
    # 同一形状在 chromium 实测：旧的双层枚举 20 次约 420 ms，线性实现约 2 ms。
    # 阈值取 100 ms —— 比实现本身宽 50 倍，又比二次方实现低 4 倍。
    assert measured["elapsed"] < 100, (
        f"模糊匹配 20 次耗时 {measured['elapsed']:.0f} ms，疑似退回二次方实现"
    )


@pytest.mark.frontend
def test_jukebox_close_preserves_playback_when_panel_opened_first(mock_page: Page):
    """#4, second half: panel opened first, then the control API reuses its player.

    Closing the panel must adopt the player node into the headless host instead
    of removing it along with the container.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;

          // 先按「面板已打开」建出播放器：播放器节点落在面板容器里。
          const wrapper = document.createElement('div');
          wrapper.className = 'jukebox-wrapper';
          wrapper.innerHTML = '<div class="jukebox-container"></div>';
          document.body.appendChild(wrapper);
          J.State.container = wrapper;
          J.State.isOpen = true;
          J.initPlayer({ headless: false });
          const playerNode = document.getElementById('jukebox-player');
          const insideContainer = wrapper.contains(playerNode);

          // 再由 AI 借这个播放器起播。
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });

          let stopped = 0;
          const originalStop = J.stopPlayback;
          J.stopPlayback = function(...args) { stopped += 1; return originalStop.apply(this, args); };
          J.close();
          J.stopPlayback = originalStop;

          const host = document.getElementById('neko-jukebox-runtime-host');
          return {
            insideContainer,
            stopped,
            // 播放器节点被移进无头宿主，而不是随容器一起被 remove。
            adoptedIntoRuntimeHost: !!host && host.contains(document.getElementById('jukebox-player')),
            playerDestroyed: window.__lastAPlayer.destroyed === true,
            currentSong: J.State.currentSong && J.State.currentSong.id
          };
        }
        """
    )

    assert result == {
        "insideContainer": True,
        "stopped": 0,
        "adoptedIntoRuntimeHost": True,
        "playerDestroyed": False,
        "currentSong": "song1",
    }


@pytest.mark.frontend
def test_jukebox_runtime_is_memoized_across_commands(mock_page: Page):
    """#5: ensureRuntime must memoize instead of refetching the whole config.

    It used to be a concurrency guard only, so every command re-downloaded the
    config and rebuilt State.songs under the rendered rows.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          let configFetches = 0;
          const originalFetch = window.fetch;
          window.fetch = async (url, options = {}) => {
            if (url === '/api/jukebox/config') configFetches += 1;
            return originalFetch(url, options);
          };

          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          const afterPlay = configFetches;
          const songsAfterPlay = J.State.songs;

          await J.executeControl({ action: 'set_volume', value: 40, headless: true });
          await J.executeControl({ action: 'adjust_volume', value: 5, headless: true });
          await J.executeControl({ action: 'next', headless: true });

          return {
            afterPlay,
            afterEverything: configFetches,
            // 曲库数组没有被换掉，面板里已渲染的行还指着同一批对象。
            songsIdentity: J.State.songs === songsAfterPlay
          };
        }
        """
    )

    assert result == {"afterPlay": 1, "afterEverything": 1, "songsIdentity": True}


@pytest.mark.frontend
def test_jukebox_volume_slider_updates_before_the_player_exists(mock_page: Page):
    """#6: the volume slider must give feedback before the player exists.

    buildUI() is synchronous while initPlayer() runs behind a 100 ms timeout;
    dragging inside that window used to leave the percentage label stale.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          document.body.insertAdjacentHTML('beforeend',
            '<input id="jukebox-volume-slider" type="range" min="0" max="1" step="0.01" value="1">'
            + '<span id="jukebox-volume-value">100%</span>');

          // 播放器还没建出来。
          const hasPlayer = !!J.getPlayer();
          J.updateVolume(0.4);

          return {
            hasPlayer,
            label: document.getElementById('jukebox-volume-value').textContent,
            slider: document.getElementById('jukebox-volume-slider').value,
            savedVolume: J.State.savedVolume,
            isMuted: J.State.isMuted
          };
        }
        """
    )

    assert result == {
        "hasPlayer": False,
        "label": "40%",
        "slider": "0.4",
        "savedVolume": 0.4,
        "isMuted": False,
    }


@pytest.mark.frontend
def test_jukebox_switching_songs_while_dancing_starts_the_new_song(mock_page: Page):
    """Codex P2: the play generation must survive stopPlayback's idle restore.

    stopVMD's idle restoration bumps ``playRequestId`` as its own staleness
    token, which used to invalidate the very request that triggered it.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const vrmaCalls = [];
          const idleCalls = [];
          window.lanlan_config = {
            model_type: 'live3d',
            live3d_sub_type: 'vrm',
            vrmIdleAnimations: ['/static/vrm/animation/wait03.vrma']
          };
          window.vrmManager = {
            playVRMAAnimation: async (url, options = {}) => {
              if (options.isIdle) { idleCalls.push(url); return true; }
              vrmaCalls.push(url);
              return true;
            },
            stopVRMAAnimation: () => {}
          };
          window.fetch = async (url, options = {}) => {
            if (options.method === 'HEAD') return { ok: true, status: 200 };
            if (url === '/api/jukebox/config') {
              return {
                ok: true,
                json: async () => ({
                  configRevision: 'rev-dance',
                  songs: {
                    a: { name: 'Alpha', artist: 'A', audio: 'songs/a.mp3', visible: true, defaultAction: 'act_a' },
                    b: { name: 'Bravo', artist: 'B', audio: 'songs/b.mp3', visible: true, defaultAction: 'act_b' }
                  },
                  actions: {
                    act_a: { name: 'Dance A', file: 'actions/a.vrma', format: 'vrma', visible: true },
                    act_b: { name: 'Dance B', file: 'actions/b.vrma', format: 'vrma', visible: true }
                  },
                  bindings: { a: { act_a: { offset: 0 } }, b: { act_b: { offset: 0 } } }
                })
              };
            }
            throw new Error('Unexpected fetch: ' + url);
          };

          const first = await J.executeControl({ action: 'play', query: 'Alpha', headless: true });
          const afterFirst = {
            ok: first.ok,
            current: J.State.currentSong && J.State.currentSong.id,
            dancing: J.State.isVMDPlaying
          };

          // 换歌：此刻 A 的舞蹈动画正在播，stopPlayback 会走到待机恢复那条路。
          const second = await J.executeControl({ action: 'play', query: 'Bravo', headless: true });

          return {
            afterFirst,
            secondOk: second.ok,
            secondMessage: second.message || null,
            current: J.State.currentSong && J.State.currentSong.id,
            isPlaying: J.State.isPlaying,
            vrmaCalls
          };
        }
        """
    )

    assert result["afterFirst"] == {"ok": True, "current": "a", "dancing": True}
    assert result["secondOk"] is True
    assert result["secondMessage"] is None
    assert result["current"] == "b"
    assert result["isPlaying"] is True
    assert result["vrmaCalls"] == [
        "/api/jukebox/file/actions/a.vrma",
        "/api/jukebox/file/actions/b.vrma",
    ]


@pytest.mark.frontend
def test_jukebox_auto_advance_is_cancelled_when_mode_changes(mock_page: Page):
    """Greptile P1: a pending auto-advance must not outlive the mode it was picked under.

    ``set_mode`` does not move ``playRequestId``, so switching to ``none`` in
    the gap before the zero-delay callback used to still start the next track.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.executeControl({ action: 'set_mode', mode: 'sequence', headless: true });
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });

          const played = [];
          const originalPlaySong = J.playSong;
          J.playSong = async function(songId, options) {
            played.push(songId);
            return originalPlaySong.call(this, songId, options);
          };

          // 歌放完 -> 自动续播被挂到 setTimeout(0)；回调跑之前把模式改掉。
          J.handleAudioEnded(J.getPlayer());
          J.State.playbackMode = 'none';
          await new Promise(resolve => setTimeout(resolve, 30));

          J.playSong = originalPlaySong;
          return { played, current: J.State.currentSong && J.State.currentSong.id };
        }
        """
    )

    assert result["played"] == []
    assert result["current"] is None


@pytest.mark.frontend
def test_vrm_animation_stale_request_does_not_touch_shared_state(mock_page: Page):
    """Codex P2: shouldStart must gate the shared-state mutations, not only playback.

    A request superseded while its asset loads used to still set
    ``isIdleAnimation`` and build an action on the live mixer before the final
    gate rejected it.
    """
    mock_page.set_content("<html><body></body></html>")
    mock_page.evaluate("() => { window.THREE = {}; }")
    mock_page.add_script_tag(content=VRM_ANIMATION_SCRIPT)

    result = mock_page.evaluate(
        """
        async () => {
          const scene = { uuid: 'scene-a', traverse() {}, updateMatrixWorld() {} };
          const manager = { currentModel: { vrm: { scene, humanoid: {} } } };
          const anim = new window.VRMAnimation(manager);

          let created = 0;
          let released = 0;
          let played = 0;
          let superseded = false;

          anim._cacheSkinnedMeshes = () => {};
          anim._cleanupOldMixer = () => {};
          anim._initLoader = async () => ({});
          anim._loadVRMAGltf = async () => {
            // 资源加载期间，点唱机换了歌，这条动画请求已经作废。
            superseded = true;
            return { userData: { vrmAnimations: [{}] } };
          };
          anim._detectVRMVersion = () => 1;
          anim._ensureNormalizedRootInScene = () => {};
          anim._createLookAtProxy = async () => {};
          anim._createAndValidateAnimationClip = async () => ({});
          anim._processTracksForVersion = () => {};
          anim._normalizeQuaternionTrackSigns = () => {};
          anim._alignClipToCurrentPose = () => {};
          anim._findBestMixerRoot = () => ({});
          anim._createAndConfigureAction = () => { created += 1; return {}; };
          anim._releaseMixerAction = () => { released += 1; };
          anim._playAction = () => { played += 1; };
          anim._restorePhysics = () => {};
          anim.isIdleAnimation = 'untouched';

          const started = await anim.playVRMAAnimation('/x.vrma', {
            isIdle: true,
            shouldStart: () => !superseded
          });

          return { started, created, released, played, isIdleAnimation: anim.isIdleAnimation };
        }
        """
    )

    assert result["started"] is False
    assert result["played"] == 0
    # 作废的请求既不该改 isIdleAnimation，也不该在活着的 mixer 上建 action。
    assert result["isIdleAnimation"] == "untouched"
    assert result["created"] == 0
    assert result["released"] == 0


@pytest.mark.frontend
def test_jukebox_superseded_fuzzy_search_settles_instead_of_hanging(mock_page: Page):
    """CodeRabbit: terminating a worker fires neither onmessage nor onerror.

    A superseded search must be settled explicitly, or the ``findSongForQuery``
    awaiting it never returns and the serialized control queue wedges.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          // 两次模糊查询背靠背发出：第二次会 terminate 第一次的 worker。
          const firstPromise = J.findSongForQuery('桃园');
          const secondPromise = J.findSongForQuery('桃园');

          const timeout = new Promise(resolve => setTimeout(() => resolve('TIMEOUT'), 3000));
          const first = await Promise.race([firstPromise, timeout]);
          const second = await Promise.race([secondPromise, timeout]);

          return {
            firstSettled: first !== 'TIMEOUT',
            firstResult: first === 'TIMEOUT' ? 'TIMEOUT' : (first && first.id) || null,
            secondId: second === 'TIMEOUT' ? 'TIMEOUT' : (second && second.id) || null,
            settleReleased: J.State.fuzzySearchSettle === null
          };
        }
        """
    )

    # 被取代的那次必须 settle（返回 null 即可），不能永远悬着。
    assert result["firstSettled"] is True
    assert result["firstResult"] is None
    assert result["secondId"] == "song4"
    assert result["settleReleased"] is True


@pytest.mark.frontend
def test_jukebox_play_recovers_when_library_was_empty_at_startup(mock_page: Page):
    """CodeRabbit: a memoized runtime must not pin an empty library forever."""
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          let serveSongs = false;
          let configFetches = 0;
          window.fetch = async (url, options = {}) => {
            if (options.method === 'HEAD') return { ok: true, status: 200 };
            if (url === '/api/jukebox/config') {
              configFetches += 1;
              return {
                ok: true,
                json: async () => ({
                  configRevision: serveSongs ? 'rev-late' : 'rev-empty',
                  songs: serveSongs
                    ? { late: { name: 'Late Arrival', artist: 'A', audio: 'songs/late.mp3', visible: true } }
                    : {},
                  actions: {},
                  bindings: {}
                })
              };
            }
            throw new Error('Unexpected fetch: ' + url);
          };

          // 运行时在曲库还是空的时候就绪。
          await J.ensureRuntime({ headless: true });
          const songsAtStartup = J.State.songs.length;

          // 之后后端才有了歌。
          serveSongs = true;
          const played = await J.executeControl({ action: 'play', query: 'Late', headless: true });

          return {
            songsAtStartup,
            ok: played.ok,
            message: played.message || null,
            current: J.State.currentSong && J.State.currentSong.id,
            configFetches
          };
        }
        """
    )

    assert result["songsAtStartup"] == 0
    assert result["ok"] is True
    assert result["message"] is None
    assert result["current"] == "late"
    # 一次是运行时初始化，一次是找不到歌之后的兜底刷新。
    assert result["configFetches"] == 2


@pytest.mark.frontend
def test_jukebox_loader_control_entrypoints_cancel_pending_unload(mock_page: Page):
    """CodeRabbit: a control command inside the 3s unload window must cancel it."""
    mock_page.set_content(
        """
        <script>
          window.t = (key, fallback) => typeof fallback === 'string' ? fallback : key;
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    result = mock_page.evaluate(
        """
        async () => {
          const loader = window.__nekoJukeboxLoader;
          const before = [];
          const after = [];

          loader.unload();
          before.push(loader.getState().pendingUnload);
          // 门面的 executeControl 会去加载 parts；这里只关心它有没有先撤销卸载定时器。
          const controlPromise = window.Jukebox.executeControl({ action: 'stop' }).catch(() => 'failed');
          after.push(loader.getState().pendingUnload);

          loader.unload();
          const beforeRuntime = loader.getState().pendingUnload;
          const runtimePromise = window.Jukebox.ensureRuntime({ headless: true }).catch(() => 'failed');
          const afterRuntime = loader.getState().pendingUnload;

          await Promise.all([controlPromise, runtimePromise]);
          return {
            pendingBeforeControl: before[0],
            pendingAfterControl: after[0],
            pendingBeforeRuntime: beforeRuntime,
            pendingAfterRuntime: afterRuntime
          };
        }
        """
    )

    assert result == {
        "pendingBeforeControl": True,
        "pendingAfterControl": False,
        "pendingBeforeRuntime": True,
        "pendingAfterRuntime": False,
    }


@pytest.mark.frontend
def test_jukebox_stop_during_animation_load_actually_stops_audio(mock_page: Page):
    """Codex P2: stopAudio's player.pause() hangs off State.isPlaying.

    A stop arriving while the animation is still loading used to leave the
    audio running and still report success.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          let releaseAnimation;
          J.getModelType = () => 'vrm';
          J.getActionAvailability = async () => ({
            ok: true,
            status: 'action_ready',
            action: { id: 'act', name: 'Dance', file: 'actions/a.vrma' },
            url: '/api/jukebox/file/actions/a.vrma'
          });
          window.vrmManager = {
            playVRMAAnimation: () => new Promise((resolve) => { releaseAnimation = () => resolve(true); }),
            stopVRMAAnimation: () => {}
          };

          const playing = J.playSong('song1');
          while (typeof releaseAnimation !== 'function') {
            await new Promise(resolve => setTimeout(resolve, 0));
          }

          // 音频已经在放，动画还在加载 —— 这时候来一条 stop。
          const player = J.getPlayer();
          const pausedBeforeStop = player.audio.paused;
          const stopped = await J.executeControl({ action: 'stop', headless: true });
          const pausedAfterStop = player.audio.paused;

          releaseAnimation();
          await playing;

          return {
            stopOk: stopped.ok,
            pausedBeforeStop,
            pausedAfterStop,
            isPlaying: J.State.isPlaying
          };
        }
        """
    )

    assert result["stopOk"] is True
    assert result["pausedBeforeStop"] is False
    # ok:true 必须名副其实：声音真的停了。
    assert result["pausedAfterStop"] is True
    assert result["isPlaying"] is False


@pytest.mark.frontend
def test_jukebox_fuzzy_worker_source_has_no_missing_dependency(mock_page: Page):
    """The worker source is assembled from a hand-kept list of function names.

    Missing one is silent in production: the worker throws a ReferenceError,
    the error path reports "search failed", and the symptom is merely that a
    song cannot be found.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          const source = J.buildFuzzySearchWorkerSource();
          // worker 里唯一的全局对象就是这份 source 自己拼出来的 Jukebox。
          const declared = new Set(
            [...source.matchAll(/^const (\\w+) = /gm)].map(m => m[1])
          );
          const referenced = new Set(
            [...source.matchAll(/Jukebox\\.(\\w+)/g)].map(m => m[1])
          );
          const missing = [...referenced].filter(name => !declared.has(name));

          // 真跑一遍这份 source，语法/引用错误会直接抛出来。
          let executed = null;
          try {
            const factory = new Function('self', source + '; return self.onmessage;');
            const fakeSelf = { postMessage: (data) => { executed = data; } };
            const onmessage = factory(fakeSelf);
            onmessage({ data: { token: 7, query: 'song', songs: J.State.songs } });
          } catch (error) {
            return { missing, threw: String(error && error.message || error) };
          }
          return { missing, threw: null, executed };
        }
        """
    )

    assert result["missing"] == [], f"worker 源码缺少依赖: {result['missing']}"
    assert result["threw"] is None
    assert result["executed"]["token"] == 7
    assert "error" not in result["executed"]


@pytest.mark.frontend
def test_jukebox_exact_match_wins_past_the_thousandth_song(mock_page: Page):
    """Codex P2: tier scores must not be crossed by an unbounded playlist index.

    Tiers are 1000 apart, so subtracting the raw index made an exact match at
    index 1001 score below a prefix match at index 0.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          // index 0 是前缀命中（'alpha' 是 'alphabet' 的前缀），
          // index 1001 才是精确命中。
          const songs = [{ id: 'prefix', name: 'alphabet', artist: '', audio: '' }];
          for (let i = 1; i <= 1000; i += 1) {
            songs.push({ id: 'filler' + i, name: 'filler ' + i, artist: '', audio: '' });
          }
          songs.push({ id: 'exact', name: 'alpha', artist: '', audio: '' });
          J.State.songs = songs;

          const exactIndex = songs.findIndex(s => s.id === 'exact');
          const found = await J.findSongForQuery('alpha');
          return {
            exactIndex,
            foundId: found && found.id,
            exactScore: J.scoreSongForQuery(songs[exactIndex], 'alpha', exactIndex),
            prefixScore: J.scoreSongForQuery(songs[0], 'alpha', 0)
          };
        }
        """
    )

    assert result["exactIndex"] == 1001
    assert result["foundId"] == "exact"
    # 档间距 1000，位置只在档内并列时起作用，永远不能把精确档压到前缀档以下。
    assert result["exactScore"] > result["prefixScore"]
    assert result["exactScore"] > 9000


@pytest.mark.frontend
def test_jukebox_full_teardown_cancels_a_pending_control(mock_page: Page):
    """Codex P2: a command in flight must not resurrect the runtime after teardown."""
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          let releaseConfig;
          const originalLoad = J.loadSongData;
          J.loadSongData = async function() {
            await new Promise(resolve => { releaseConfig = resolve; });
            return originalLoad.call(this);
          };

          // 第一条无头指令卡在拉配置里。
          const pending = J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          while (typeof releaseConfig !== 'function') {
            await new Promise(resolve => setTimeout(resolve, 0));
          }

          // 用户此时把点歌台整个拆掉。
          J.prepareForUnload();
          const hostAfterTeardown = !!document.getElementById('neko-jukebox-runtime-host');

          releaseConfig();
          const outcome = await pending.catch(error => ({ ok: false, message: String(error && error.message) }));
          await new Promise(resolve => setTimeout(resolve, 30));

          J.loadSongData = originalLoad;
          return {
            hostAfterTeardown,
            ok: outcome.ok,
            // 拆除之后不该有复活的宿主，也不该在放。
            hostResurrected: !!document.getElementById('neko-jukebox-runtime-host'),
            isPlaying: J.State.isPlaying,
            runtimeInitPromise: J.State.runtimeInitPromise === null
          };
        }
        """
    )

    assert result["hostAfterTeardown"] is False
    assert result["ok"] is False
    assert result["hostResurrected"] is False
    assert result["isPlaying"] is False
    assert result["runtimeInitPromise"] is True


@pytest.mark.frontend
def test_jukebox_reports_failure_when_autoplay_is_blocked(mock_page: Page):
    """Codex P2: APlayer swallows NotAllowedError and play() returns no promise.

    The control must not report success when nothing is audible.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          const player = J.getPlayer();

          // 模拟自动播放被拦：play() 无返回值，音频回到 paused。
          player.play = function() { this.audio.paused = true; };

          const blocked = await J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          const isPlayingAfterBlocked = J.State.isPlaying;

          // 恢复正常起播行为再验一次，确认没有把正常情况误判成失败。
          player.play = function() { this.audio.paused = false; this.played = true; };
          const allowed = await J.executeControl({ action: 'play', query: 'Song 2', headless: true });

          return {
            blockedOk: blocked.ok,
            blockedMessage: blocked.message || null,
            isPlayingAfterBlocked,
            allowedOk: allowed.ok,
            current: J.State.currentSong && J.State.currentSong.id
          };
        }
        """
    )

    assert result["blockedOk"] is False
    assert result["isPlayingAfterBlocked"] is False
    assert result["allowedOk"] is True
    assert result["current"] == "song2"


@pytest.mark.frontend
def test_jukebox_standalone_window_serves_forwarded_controls(mock_page: Page):
    """#4 owner side: the standalone window answers discovery and runs commands.

    It owns the player the user can see, but loads neither app-websocket.js nor
    jukebox-loader.js, so it has to pick the command up off the channel.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          window.__NEKO_JUKEBOX_STANDALONE__ = true;
          const started = J.startControlOwnerService();
          // 归属宣告与「能执行」现在是两件事：窗口一起来就宣告，曲库和播放器
          // 就绪后才放行。这条用例测的是就绪之后的服务行为。
          J.markControlOwnerReady();

          // 扮演角色窗口：BroadcastChannel 不会把消息投回发送它的那个对象，
          // 但同页面里另一个 channel 对象收得到，足以驱动完整协议。
          const peer = new BroadcastChannel('neko-jukebox-control');
          const seen = [];
          peer.onmessage = (event) => seen.push(event.data);

          const waitFor = async (predicate, timeoutMs) => {
            const deadline = Date.now() + timeoutMs;
            while (Date.now() < deadline) {
              if (predicate()) return true;
              await new Promise(resolve => setTimeout(resolve, 10));
            }
            return false;
          };

          // 1) 探测：问一声就该有人应答
          peer.postMessage({ type: 'jukebox_owner_query' });
          const answered = await waitFor(
            () => seen.some(m => m && m.type === 'jukebox_owner_alive'), 1000);

          // 2) 代执行：转发一条指令，结果要原样回来
          peer.postMessage({
            type: 'jukebox_control_request',
            requestId: 'req-1',
            command: { action: 'set_mode', mode: 'random', headless: true }
          });
          const replied = await waitFor(
            () => seen.some(m => m && m.type === 'jukebox_control_result' && m.requestId === 'req-1'), 3000);
          const reply = seen.find(m => m && m.type === 'jukebox_control_result' && m.requestId === 'req-1');

          // 3) 退出时主动通告，别让对端干等 TTL
          J.stopControlOwnerService();
          const goneSeen = await waitFor(
            () => seen.some(m => m && m.type === 'jukebox_owner_gone'), 1000);

          peer.close();
          return {
            started,
            answered,
            replied,
            replyOk: reply && reply.result && reply.result.ok,
            replyAction: reply && reply.result && reply.result.action,
            modeApplied: J.State.playbackMode,
            goneSeen,
            channelReleased: J.State.controlOwnerChannel === null
          };
        }
        """
    )

    assert result == {
        "started": True,
        "answered": True,
        "replied": True,
        "replyOk": True,
        "replyAction": "set_mode",
        "modeApplied": "random",
        "goneSeen": True,
        "channelReleased": True,
    }


@pytest.mark.frontend
def test_jukebox_loader_discovers_and_forwards_to_the_owner(mock_page: Page):
    """#4 character-window side: discovery + forwarding without loading the parts.

    Answering "is there an owner?" must not drag the hidden runtime up, which is
    why this lives in the loader rather than in the jukebox parts.
    """
    mock_page.set_content(
        """
        <script>
          window.t = (key, fallback) => typeof fallback === 'string' ? fallback : key;
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    result = mock_page.evaluate(
        """
        async () => {
          const loader = window.__nekoJukeboxLoader;
          const ownerless = loader.hasControlOwner();

          // 扮演独立点唱机窗口
          const owner = new BroadcastChannel('neko-jukebox-control');
          const requests = [];
          owner.onmessage = (event) => {
            const data = event.data;
            if (!data) return;
            if (data.type === 'jukebox_owner_query') {
              owner.postMessage({ type: 'jukebox_owner_alive' });
              return;
            }
            if (data.type === 'jukebox_control_request') {
              requests.push(data.command);
              owner.postMessage({
                type: 'jukebox_control_result',
                requestId: data.requestId,
                result: { ok: true, action: data.command.action, via: 'owner' }
              });
            }
          };
          owner.postMessage({ type: 'jukebox_owner_alive' });
          await new Promise(resolve => setTimeout(resolve, 50));
          const discovered = loader.hasControlOwner();

          const forwarded = await loader.forwardControl({ action: 'next', headless: true });

          // 拥有者退出后要回落成「没有拥有者」
          owner.postMessage({ type: 'jukebox_owner_gone' });
          await new Promise(resolve => setTimeout(resolve, 50));
          const afterGone = loader.hasControlOwner();

          owner.close();
          return {
            ownerless,
            discovered,
            requests,
            forwarded,
            afterGone,
            // 全程没有把 parts 拉起来
            partsLoaded: !!document.querySelector('script[data-neko-jukebox-part="true"]')
          };
        }
        """
    )

    assert result["ownerless"] is False
    assert result["discovered"] is True
    assert result["requests"] == [{"action": "next", "headless": True}]
    assert result["forwarded"] == {"ok": True, "action": "next", "via": "owner"}
    assert result["afterGone"] is False
    assert result["partsLoaded"] is False


@pytest.mark.frontend
def test_jukebox_open_marks_the_control_owner_ready_only_when_standalone(mock_page: Page):
    """#4 call site: readiness is declared once the runtime can actually serve.

    Ownership itself is announced as soon as the window exists (otherwise the
    character window sees no owner and starts a hidden runtime); executing has
    to wait until the library and player are up, or the first command picks its
    action with the default model type.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const run = async (isStandalone) => {
            window.__NEKO_JUKEBOX_STANDALONE__ = isStandalone;
            J.State.controlOwnerChannel = null;
            let started = 0;
            // 标记就绪那一刻的状态：光数「有没有调用」挡不住提前放行。
            const stateAtAnnounce = [];
            const original = J.markControlOwnerReady;
            J.markControlOwnerReady = function() {
              const snapshot = {
                hasPlayer: !!J.getPlayer(),
                songCount: (J.State.songs || []).length
              };
              const ok = original.call(this);
              if (ok === true && J.State.controlOwnerReady) {
                started += 1;
                stateAtAnnounce.push(snapshot);
              }
              return ok;
            };
            try {
              J.open();
              // open() 的实际初始化挂在 rAF + 100ms 的 setTimeout 里，必须等过去。
              await new Promise(resolve => setTimeout(resolve, 400));
            } finally {
              J.markControlOwnerReady = original;
              J.stopControlOwnerService();
              J.close();
            }
            return { started, stateAtAnnounce };
          };

          return { standalone: await run(true), embedded: await run(false) };
        }
        """
    )

    assert result["standalone"]["started"] == 1
    # 宣告的那一刻，可见运行时必须真的就位：播放器建好、曲库拉完。
    assert result["standalone"]["stateAtAnnounce"] == [{"hasPlayer": True, "songCount": 4}]
    # 嵌在角色窗口里的点歌台不是拥有者，绝不能去抢这个身份。
    assert result["embedded"]["started"] == 0


@pytest.mark.frontend
def test_jukebox_restores_idle_when_the_replacement_animation_fails(mock_page: Page):
    """Codex P2: file availability is not proof the animation started.

    The HEAD preflight can pass while the load fails or no model manager is
    around; the previous dance was already stopped with idle restoration
    suppressed, so the avatar was left standing there.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const idleCalls = [];
          window.lanlan_config = {
            model_type: 'live3d',
            live3d_sub_type: 'vrm',
            vrmIdleAnimations: ['/static/vrm/animation/wait03.vrma']
          };
          window.vrmManager = {
            playVRMAAnimation: async (url, options = {}) => {
              if (options.isIdle) { idleCalls.push(url); return true; }
              return true;
            },
            stopVRMAAnimation: () => {}
          };
          J.getActionAvailability = async () => ({
            ok: true,
            status: 'action_ready',
            action: { id: 'act', name: 'Dance', file: 'actions/a.vrma' },
            url: '/api/jukebox/file/actions/a.vrma'
          });

          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          const dancing = J.State.isVMDPlaying;
          const idleAfterDance = idleCalls.length;

          // 换歌：文件预检照样通过，但动画真的起不来（返回 false）。
          J.playVRMA = async () => false;
          const switched = await J.executeControl({ action: 'play', query: 'Song 2', headless: true });
          await new Promise(resolve => setTimeout(resolve, 30));

          return {
            dancing,
            idleAfterDance,
            switchedOk: switched.ok,
            current: J.State.currentSong && J.State.currentSong.id,
            idleRestored: idleCalls.length > idleAfterDance
          };
        }
        """
    )

    assert result["dancing"] is True
    assert result["idleAfterDance"] == 0
    assert result["switchedOk"] is True
    assert result["current"] == "song2"
    # 动画没起来 -> 必须把待机接回去，不能让模型僵在原地。
    assert result["idleRestored"] is True


@pytest.mark.frontend
def test_jukebox_falls_back_to_main_thread_when_the_worker_fails(mock_page: Page):
    """Codex P2: a worker that dies after construction is not a miss.

    Creation-time failure already fell back; a host policy that kills the blob
    worker later resolved as index -1, and the control reported song_not_found
    even though the main-thread matcher would have found it.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          // worker 建得出来，但一投递就报错（宿主策略拦 blob worker 的表现）。
          const NativeWorker = window.Worker;
          window.Worker = class {
            constructor() {
              this.onmessage = null;
              this.onerror = null;
              setTimeout(() => {
                if (this.onerror) this.onerror({ message: 'blocked by policy' });
              }, 0);
            }
            postMessage() {}
            terminate() {}
          };

          // '桃园' 不是 '桃源恋歌' 的子串，只能靠模糊匹配。
          const found = await J.findSongForQuery('桃园');
          const played = await J.executeControl({ action: 'play', query: '桃园', headless: true });

          window.Worker = NativeWorker;
          return {
            foundId: found && found.id,
            ok: played.ok,
            message: played.message || null,
            current: J.State.currentSong && J.State.currentSong.id
          };
        }
        """
    )

    # worker 失败不等于没这首歌：主线程匹配得出来。
    assert result["foundId"] == "song4"
    assert result["ok"] is True
    assert result["message"] is None
    assert result["current"] == "song4"


@pytest.mark.frontend
def test_jukebox_abandoned_auto_advance_restores_idle(mock_page: Page):
    """Codex P2: the mode-change abort left the avatar with no animation.

    handleAudioEnded stops the old dance with idle restoration suppressed
    because a replacement is coming; abandoning that transition has to put the
    idle animation back.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const idleCalls = [];
          window.lanlan_config = {
            model_type: 'live3d',
            live3d_sub_type: 'vrm',
            vrmIdleAnimations: ['/static/vrm/animation/wait03.vrma']
          };
          window.vrmManager = {
            playVRMAAnimation: async (url, options = {}) => {
              if (options.isIdle) idleCalls.push(url);
              return true;
            },
            stopVRMAAnimation: () => {}
          };
          J.getActionForModel = () => ({ id: 'act', name: 'Dance', file: 'actions/a.vrma' });

          // 运行时必须真的就绪：否则回调会走「面板没开且运行时未就绪」那个提前
          // 返回，根本到不了模式变更那条判据，护栏就成了哑的。
          await J.ensureRuntime({ headless: true });
          await J.executeControl({ action: 'set_mode', mode: 'sequence', headless: true });
          J.State.currentSong = J.State.songs[0];
          J.State.isVMDPlaying = true;
          const idleBefore = idleCalls.length;
          const runtimeReady = J.State.isRuntimeReady;

          // 歌放完 -> 自动续播排上，旧舞蹈已被 stopVMD(true) 停掉且跳过待机恢复。
          J.handleAudioEnded(J.getPlayer());
          // 回调跑之前把模式改掉，这次续播作废。
          J.State.playbackMode = 'none';
          await new Promise(resolve => setTimeout(resolve, 30));

          return {
            idleBefore,
            runtimeReady,
            idleRestored: idleCalls.length > idleBefore,
            current: J.State.currentSong && J.State.currentSong.id
          };
        }
        """
    )

    assert result["idleBefore"] == 0
    # 钉住走的确实是模式变更那条出口，而不是「运行时未就绪」那条。
    assert result["runtimeReady"] is True
    # 续播作废了，待机必须接回去，模型不能僵在原地。
    assert result["idleRestored"] is True
    assert result["current"] is None


@pytest.mark.frontend
def test_jukebox_standalone_close_tears_down_playback(mock_page: Page):
    """Codex P2: preserving a runtime inside a window that is about to die.

    Every forwarded command carries headless:true, so even a volume change set
    the headless flag; close() then skipped stopPlayback and with it the IPC
    stopVMD, leaving the avatar dancing after the window was gone.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          window.__NEKO_JUKEBOX_STANDALONE__ = true;
          // 一条转发过来的音量指令就足以把 headlessRuntimeRequested 置真。
          await J.executeControl({ action: 'set_volume', value: 40, headless: true });
          const headlessFlag = J.State.headlessRuntimeRequested;

          let stopped = 0;
          const originalStop = J.stopPlayback;
          J.stopPlayback = function(...args) { stopped += 1; return originalStop.apply(this, args); };

          const wrapper = document.createElement('div');
          wrapper.className = 'jukebox-wrapper';
          wrapper.innerHTML = '<div class="jukebox-container"></div>';
          document.body.appendChild(wrapper);
          J.State.container = wrapper;
          J.State.isOpen = true;

          J.close();
          J.stopPlayback = originalStop;
          window.__NEKO_JUKEBOX_STANDALONE__ = false;

          return {
            headlessFlag,
            stopped,
            // 完整拆除：运行时标记清掉、宿主不留。
            headlessAfter: J.State.headlessRuntimeRequested,
            hostLeft: !!document.getElementById('neko-jukebox-runtime-host')
          };
        }
        """
    )

    assert result["headlessFlag"] is True
    # 独立窗口整个要销毁，不存在保活：必须走完整停播路径。
    assert result["stopped"] == 1
    assert result["headlessAfter"] is False
    assert result["hostLeft"] is False


@pytest.mark.frontend
def test_jukebox_auto_advance_restores_idle_when_the_successor_animation_fails(mock_page: Page):
    """Greptile P1: the interrupted-dance fact has to survive the transition.

    handleAudioEnded clears isVMDPlaying via stopVMD(true) before calling
    playSong, so playSong could not tell that a dance had just been cut; if the
    successor's animation then failed, nobody restored idle and the avatar
    stayed frozen while audio kept playing.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const idleCalls = [];
          window.lanlan_config = {
            model_type: 'live3d',
            live3d_sub_type: 'vrm',
            vrmIdleAnimations: ['/static/vrm/animation/wait03.vrma']
          };
          window.vrmManager = {
            playVRMAAnimation: async (url, options = {}) => {
              if (options.isIdle) idleCalls.push(url);
              return true;
            },
            stopVRMAAnimation: () => {}
          };
          J.getActionForModel = () => ({ id: 'act', name: 'Dance', file: 'actions/a.vrma' });
          J.getActionAvailability = async () => ({
            ok: true,
            status: 'action_ready',
            action: { id: 'act', name: 'Dance', file: 'actions/a.vrma' },
            url: '/api/jukebox/file/actions/a.vrma'
          });
          // 接班那首歌的动画起不来（文件在切换前被删掉之类）。
          J.playVRMA = async () => false;

          await J.ensureRuntime({ headless: true });
          await J.executeControl({ action: 'set_mode', mode: 'sequence', headless: true });
          J.State.currentSong = J.State.songs[0];
          J.State.isVMDPlaying = true;
          const idleBefore = idleCalls.length;
          const runtimeReady = J.State.isRuntimeReady;

          J.handleAudioEnded(J.getPlayer());
          await new Promise(resolve => setTimeout(resolve, 60));

          return {
            idleBefore,
            runtimeReady,
            advanced: J.State.currentSong && J.State.currentSong.id,
            idleRestored: idleCalls.length > idleBefore
          };
        }
        """
    )

    assert result["idleBefore"] == 0
    assert result["runtimeReady"] is True
    # 续播确实发生了（不是走了放弃分支）。
    assert result["advanced"] == "song2"
    # 接班动画没起来 -> 待机必须接回去。
    assert result["idleRestored"] is True


@pytest.mark.frontend
def test_jukebox_auto_advance_allocates_a_fresh_playback_request(mock_page: Page):
    """A successor must not share the ended track's VRMA hold token."""
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          J.State.playbackMode = 'sequence';
          J.State.currentSong = J.State.songs[0];
          J.State.isPlaying = true;
          J.State.isVMDPlaying = false;
          J.State.playRequestId = 73;

          let successorRequestId = null;
          const originalPlaySong = J.playSong;
          J.playSong = async (songId, options = {}) => {
            successorRequestId = options.requestId;
            return J.State.songs.find(song => song.id === songId) || null;
          };
          try {
            J.handleAudioEnded(J.getPlayer());
            await new Promise(resolve => setTimeout(resolve, 20));
          } finally {
            J.playSong = originalPlaySong;
          }

          return {
            successorRequestId,
            currentRequestId: J.State.playRequestId
          };
        }
        """
    )

    assert result == {
        "successorRequestId": 74,
        "currentRequestId": 74,
    }


@pytest.mark.frontend
def test_jukebox_standalone_teardown_delegates_idle_restore_to_the_pet_window(mock_page: Page):
    """Idle restoration is not this window's job when it runs standalone.

    ``restoreIdleAnimation`` returns immediately under
    ``__NEKO_JUKEBOX_STANDALONE__`` because the pet window restores idle when it
    receives the stop; the desktop shell also sends VMD_STOP with
    ``skipIdleRestore: false`` from ``jukeboxWindow.on('close')``. What matters
    here is that the local teardown neither duplicates that nor throws.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const idleCalls = [];
          window.lanlan_config = {
            model_type: 'live3d',
            live3d_sub_type: 'vrm',
            vrmIdleAnimations: ['/static/vrm/animation/wait03.vrma']
          };
          let stopCalls = 0;
          window.vrmManager = {
            playVRMAAnimation: async (url, options = {}) => {
              if (options.isIdle) idleCalls.push(url);
              return true;
            },
            stopVRMAAnimation: () => { stopCalls += 1; }
          };

          window.__NEKO_JUKEBOX_STANDALONE__ = true;
          // 没有这个桩的话 stopVMD 的独立分支会短路，测试其实走的是本地路径，
          // 跟 docstring 说的完全不是一回事。
          const bridgeCalls = [];
          window.nekoJukeboxBridge = {
            stopVMD: (skipIdleRestore) => bridgeCalls.push(skipIdleRestore)
          };
          await J.executeControl({ action: 'set_volume', value: 40, headless: true });
          J.State.isVMDPlaying = true;

          const wrapper = document.createElement('div');
          wrapper.className = 'jukebox-wrapper';
          wrapper.innerHTML = '<div class="jukebox-container"></div>';
          document.body.appendChild(wrapper);
          J.State.container = wrapper;
          J.State.isOpen = true;

          J.close();
          await new Promise(resolve => setTimeout(resolve, 30));
          const afterClose = {
            idle: idleCalls.length,
            stops: stopCalls,
            vmd: J.State.isVMDPlaying,
            bridge: bridgeCalls.slice()
          };

          // 桌面端随后从主进程发来的那一下（skipIdleRestore: false）。
          J.stopVMD(false);
          await new Promise(resolve => setTimeout(resolve, 30));

          window.__NEKO_JUKEBOX_STANDALONE__ = false;
          delete window.nekoJukeboxBridge;
          return {
            afterClose,
            bridgeAfterElectronStop: bridgeCalls.slice(),
            idleAfterElectronStop: idleCalls.length,
            stopsAfterElectronStop: stopCalls,
            pendingSettled: J.State.idleRestorePending === false
          };
        }
        """
    )

    # 走的确实是独立窗口分支：停止经 IPC 发给 Pet，本地既不动 vrmManager
    # 也不在本窗口恢复待机。
    assert result["afterClose"] == {"idle": 0, "stops": 0, "vmd": False, "bridge": [False]}
    # 桌面端补发的那一下：本地没有欠账，所以不会再多发一条。
    assert result["bridgeAfterElectronStop"] == [False]
    assert result["idleAfterElectronStop"] == 0
    assert result["stopsAfterElectronStop"] == 0
    # 欠账不会留在原地拖着。
    assert result["pendingSettled"] is True


@pytest.mark.frontend
def test_jukebox_player_adopts_the_volume_set_before_it_existed(mock_page: Page):
    """Codex P2: a drag during the pre-player window was recorded, then discarded.

    initPlayer built APlayer at volume 1 and initVolumeSlider then wrote that
    back over the slider and label, so the user's setting vanished.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          document.body.insertAdjacentHTML('beforeend',
            '<input id="jukebox-volume-slider" type="range" min="0" max="1" step="0.01" value="1">'
            + '<span id="jukebox-volume-value">100%</span>');

          // 上一次会话在 localStorage 里留下的音量——真实 APlayer 会用它覆盖
          // 构造参数，所以「只改构造参数」的修法在这里必须失败。
          localStorage.setItem('aplayer-setting', JSON.stringify({ volume: 0.9 }));

          // buildUI 之后、initPlayer 之前拖滑条。
          const hadPlayerWhenDragged = !!J.getPlayer();
          J.updateVolume(0.4);

          J.initPlayer({ headless: true });
          J.initVolumeSlider();

          const player = J.getPlayer();
          return {
            hadPlayerWhenDragged,
            // 构造参数与最终音量分别断言：只有建后补设的话，构造那一瞬间仍是
            // 上一次会话的音量；只给构造参数的话，storage 会把它覆盖掉。
            constructedWith: player && player.options && player.options.volume,
            playerVolume: player && player.audio.volume,
            slider: document.getElementById('jukebox-volume-slider').value,
            label: document.getElementById('jukebox-volume-value').textContent
          };
        }
        """
    )

    assert result["hadPlayerWhenDragged"] is False
    # 播放器按用户拖到的值建，滑条和标签也不会被 100% 反向覆盖。
    assert result["constructedWith"] == 0.4
    assert result["playerVolume"] == 0.4
    assert result["slider"] == "0.4"
    assert result["label"] == "40%"


@pytest.mark.frontend
def test_jukebox_stop_cancels_a_play_still_inside_its_awaits(mock_page: Page):
    """Codex + Greptile, same defect: the cancel was overtaken by the play itself.

    cancelActivePlayback only advanced playRequestId, and executePlayControl's
    first statement is ``++playRequestId`` — so a play that had not yet
    allocated its generation minted straight past the cancel and started audio
    after the stop had completed.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          const sample = async (action, query) => {
            // 让这条指令卡在 ensureRuntime 里（play 还没轮到分配播放世代）。
            let release;
            const gate = new Promise(resolve => { release = resolve; });
            const originalEnsure = J.ensureRuntime;
            J.ensureRuntime = async function(options) {
              await gate;
              return originalEnsure.call(this, options);
            };

            const player = J.getPlayer();
            player.played = false;
            const pending = J.executeControl({ action, query, headless: true });
            await new Promise(resolve => setTimeout(resolve, 10));

            // 卡住期间用户说「停」。
            const stopped = await J.executeControl({ action: 'stop', headless: true });
            release();
            const outcome = await pending;
            J.ensureRuntime = originalEnsure;
            await new Promise(resolve => setTimeout(resolve, 20));

            return {
              stopOk: stopped.ok,
              ok: outcome.ok,
              message: outcome.message || null,
              startedAudio: player.played === true,
              isPlaying: J.State.isPlaying,
              currentSong: J.State.currentSong && J.State.currentSong.id
            };
          };

          const play = await sample('play', 'Song 1');
          // 先放一首，好让 next 有个当前曲目
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          const next = await sample('next', '');

          return { play, next };
        }
        """
    )

    for label in ("play", "next"):
        outcome = result[label]
        assert outcome["stopOk"] is True, label
        assert outcome["ok"] is False, label
        assert outcome["message"] == "play_cancelled", label
        # 关键：stop 之后不许再有声音起来。
        assert outcome["startedAudio"] is False, label
        assert outcome["isPlaying"] is False, label
        assert outcome["currentSong"] is None, label


@pytest.mark.frontend
def test_jukebox_cancel_active_playback_stops_a_play_in_its_awaits(mock_page: Page):
    """The websocket route cancels through cancelActivePlayback, not through stop.

    app-websocket.js calls it out of band before queueing the stop, so it has
    to advance the cancel epoch on its own.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          const player = J.getPlayer();
          player.played = false;

          let release;
          const gate = new Promise(resolve => { release = resolve; });
          const originalFind = J.findSongForQuery;
          J.findSongForQuery = async function(query) {
            await gate;
            return originalFind.call(this, query);
          };

          const pending = J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          await new Promise(resolve => setTimeout(resolve, 10));

          // 这正是 app-websocket 收到 stop 时先做的那一步。
          J.cancelActivePlayback();
          release();
          const outcome = await pending;
          J.findSongForQuery = originalFind;
          await new Promise(resolve => setTimeout(resolve, 20));

          return {
            ok: outcome.ok,
            message: outcome.message || null,
            startedAudio: player.played === true,
            currentSong: J.State.currentSong && J.State.currentSong.id
          };
        }
        """
    )

    assert result["ok"] is False
    assert result["message"] == "play_cancelled"
    assert result["startedAudio"] is False
    assert result["currentSong"] is None


@pytest.mark.frontend
def test_jukebox_cancel_during_preflight_and_startup_stops_the_play(mock_page: Page):
    """The later gates cover cancels that land after the play took its generation.

    One during the preflight HEAD requests, one while the audio is starting —
    the latter must also stop the sound it just made.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          const runWithCancelDuring = async (hookName) => {
            const player = J.getPlayer();
            player.played = false;
            J.State.currentSong = null;
            J.State.isPlaying = false;

            let release;
            const gate = new Promise(resolve => { release = resolve; });
            const original = J[hookName];
            J[hookName] = async function(...a) {
              const result = await original.apply(this, a);
              await gate;
              return result;
            };

            const pending = J.executeControl({ action: 'play', query: 'Song 1', headless: true });
            await new Promise(resolve => setTimeout(resolve, 10));
            J.cancelActivePlayback();
            release();
            const outcome = await pending;
            J[hookName] = original;
            await new Promise(resolve => setTimeout(resolve, 20));

            return {
              ok: outcome.ok,
              message: outcome.message || null,
              isPlaying: J.State.isPlaying,
              currentSong: J.State.currentSong && J.State.currentSong.id
            };
          };

          return {
            duringPreflight: await runWithCancelDuring('preflightSongPlayback'),
            duringStartup: await runWithCancelDuring('playAudio')
          };
        }
        """
    )

    for label in ("duringPreflight", "duringStartup"):
        outcome = result[label]
        assert outcome["ok"] is False, label
        assert outcome["message"] == "play_cancelled", label
        # 起播中途被取消时，刚响起来的声音也必须停掉。
        assert outcome["isPlaying"] is False, label
        assert outcome["currentSong"] is None, label


@pytest.mark.frontend
def test_jukebox_cancelled_play_skips_the_preflight_requests(mock_page: Page):
    """The early short-circuits exist to skip work, and that is observable.

    A play cancelled while it waited on the runtime must not go on to issue the
    preflight HEAD requests for a song it will never start.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          let headRequests = 0;
          const originalFetch = window.fetch;
          window.fetch = async (url, options = {}) => {
            if (options.method === 'HEAD') headRequests += 1;
            return originalFetch(url, options);
          };

          let release;
          const gate = new Promise(resolve => { release = resolve; });
          const originalEnsure = J.ensureRuntime;
          J.ensureRuntime = async function(options) {
            await gate;
            return originalEnsure.call(this, options);
          };

          const pending = J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          await new Promise(resolve => setTimeout(resolve, 10));
          J.cancelActivePlayback();
          release();
          const outcome = await pending;
          J.ensureRuntime = originalEnsure;
          window.fetch = originalFetch;

          return { ok: outcome.ok, message: outcome.message || null, headRequests };
        }
        """
    )

    assert result["ok"] is False
    assert result["message"] == "play_cancelled"
    # 取消之后不该再为一首永远不会播的歌发预检请求。
    assert result["headRequests"] == 0


@pytest.mark.frontend
def test_jukebox_execute_play_control_refuses_a_stale_cancel_epoch(mock_page: Page):
    """executePlayControl owns the "never mint a generation past a cancel" rule.

    Its callers check first, so no current path reaches this gate — it is the
    contract every future caller relies on, so it is pinned directly.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          const song = J.State.songs[0];

          const staleEpoch = J.State.playCancelEpoch;
          J.cancelActivePlayback();          // 世代前进，手里的那个就过期了
          const requestIdBefore = J.State.playRequestId;

          const outcome = await J.executePlayControl('play', song, { cancelEpoch: staleEpoch });

          return {
            ok: outcome.ok,
            message: outcome.message || null,
            songId: outcome.song && outcome.song.id,
            // 关键不变量：过期的取消世代下，播放世代一格都不许铸出去。
            mintedGeneration: J.State.playRequestId !== requestIdBefore,
            currentSong: J.State.currentSong && J.State.currentSong.id
          };
        }
        """
    )

    assert result["ok"] is False
    assert result["message"] == "play_cancelled"
    assert result["songId"] == "song1"
    assert result["mintedGeneration"] is False
    assert result["currentSong"] is None


@pytest.mark.frontend
def test_jukebox_failed_startup_settles_the_idle_debt(mock_page: Page):
    """Codex + Greptile: playSong's catch never settled the deferred restoration.

    Switching away from a dance suppresses idle restoration because a
    replacement is coming; if startup then throws (blocked autoplay, a load
    error) nobody ever puts the idle animation back.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const idleCalls = [];
          window.lanlan_config = {
            model_type: 'live3d',
            live3d_sub_type: 'vrm',
            vrmIdleAnimations: ['/static/vrm/animation/wait03.vrma']
          };
          window.vrmManager = {
            playVRMAAnimation: async (url, options = {}) => {
              if (options.isIdle) idleCalls.push(url);
              return true;
            },
            stopVRMAAnimation: () => {}
          };

          await J.ensureRuntime({ headless: true });
          J.State.currentSong = J.State.songs[0];
          J.State.isVMDPlaying = true;
          const idleBefore = idleCalls.length;

          // 换歌：旧舞蹈被停掉且跳过待机恢复，然后起播失败。
          J.playAudio = async () => { throw new Error('autoplay_blocked'); };
          const played = await J.playSong('song2');
          await new Promise(resolve => setTimeout(resolve, 30));

          return {
            idleBefore,
            played,
            debtSettled: J.State.idleRestorePending === false,
            idleRestored: idleCalls.length > idleBefore
          };
        }
        """
    )

    assert result["idleBefore"] == 0
    assert result["played"] is None
    assert result["debtSettled"] is True
    # 起播失败之后模型必须回到待机，不能停在舞蹈最后一帧。
    assert result["idleRestored"] is True


@pytest.mark.frontend
def test_jukebox_standalone_forwards_the_idle_restore_to_the_pet(mock_page: Page):
    """Codex: settling the debt was a no-op in the standalone window.

    restoreIdleAnimation returns early there, so the ledger was cleared while
    the pet stayed on the dance's last frame. The restore has to be forwarded.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const bridgeCalls = [];
          window.__NEKO_JUKEBOX_STANDALONE__ = true;
          window.nekoJukeboxBridge = {
            stopVMD: (skipIdleRestore) => bridgeCalls.push({ stop: skipIdleRestore }),
            playVMD: (url) => bridgeCalls.push({ play: url })
          };

          await J.ensureRuntime({ headless: true });
          J.State.isVMDPlaying = true;

          // 换歌到一首没有可播动作的歌：先抑制恢复地停掉旧舞蹈……
          J.stopVMD(true);
          const afterSuppressedStop = {
            calls: bridgeCalls.slice(),
            debt: J.State.idleRestorePending
          };

          // ……然后没有动画接上，收尾处结账。
          J.settleIdleRestore();
          const afterSettle = bridgeCalls.slice();

          window.__NEKO_JUKEBOX_STANDALONE__ = false;
          delete window.nekoJukeboxBridge;
          return { afterSuppressedStop, afterSettle, debtAfter: J.State.idleRestorePending };
        }
        """
    )

    # 抑制恢复的那一下确实记了账。
    assert result["afterSuppressedStop"]["calls"] == [{"stop": True}]
    assert result["afterSuppressedStop"]["debt"] is True
    # 结账必须真的把恢复转发给 Pet，而不是清个标志了事。
    assert result["afterSettle"] == [{"stop": True}, {"stop": False}]
    assert result["debtAfter"] is False


@pytest.mark.frontend
def test_jukebox_compensating_stop_settles_an_outstanding_debt(mock_page: Page):
    """A stop that arrives with nothing playing is the compensating one.

    Both arms of stopVMD returned before the ledger, so the desktop shell's
    VMD_STOP after a failed switch could never settle the debt.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const idleCalls = [];
          window.lanlan_config = {
            model_type: 'live3d',
            live3d_sub_type: 'vrm',
            vrmIdleAnimations: ['/static/vrm/animation/wait03.vrma']
          };
          window.vrmManager = {
            playVRMAAnimation: async (url, options = {}) => {
              if (options.isIdle) idleCalls.push(url);
              return true;
            },
            stopVRMAAnimation: () => {}
          };

          // 本地路径：欠着账，且已经没有舞蹈在播。
          J.State.isVMDPlaying = false;
          J.State.idleRestorePending = true;
          J.stopVMD(false);
          await new Promise(resolve => setTimeout(resolve, 20));
          const local = { idle: idleCalls.length, debt: J.State.idleRestorePending };

          // 抑制恢复的那种停止不该结账 —— 它本来就是记账的那一方。
          J.State.idleRestorePending = true;
          J.stopVMD(true);
          await new Promise(resolve => setTimeout(resolve, 20));
          const suppressed = { idle: idleCalls.length, debt: J.State.idleRestorePending };

          return { local, suppressed };
        }
        """
    )

    # 补发的停止把账结了，待机接回去。
    assert result["local"] == {"idle": 1, "debt": False}
    # 抑制恢复的停止不结账，也不会多恢复一次。
    assert result["suppressed"] == {"idle": 1, "debt": True}


@pytest.mark.frontend
def test_jukebox_cancelled_play_does_not_stop_its_successor(mock_page: Page):
    """CodeRabbit: the cleanup on a cancelled play was unconditional.

    A stop followed immediately by a new play means the older request wakes up
    after the newer one is already audible; stopping "its" playback then kills
    the successor's.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          // 让 A 卡在 playSong 里。
          let release;
          const gate = new Promise(resolve => { release = resolve; });
          const originalPlaySong = J.playSong;
          let hooked = true;
          J.playSong = async function(songId, options) {
            if (hooked) {
              hooked = false;
              const played = await originalPlaySong.call(this, songId, options);
              await gate;
              return played;
            }
            return originalPlaySong.call(this, songId, options);
          };

          const first = J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          await new Promise(resolve => setTimeout(resolve, 10));

          // stop，紧接着一条新的 play —— B 起播并成为当前曲目。
          await J.executeControl({ action: 'stop', headless: true });
          const second = await J.executeControl({ action: 'play', query: 'Song 2', headless: true });
          const afterSecond = {
            ok: second.ok,
            current: J.State.currentSong && J.State.currentSong.id,
            isPlaying: J.State.isPlaying
          };

          // A 这时才醒过来收尾。
          release();
          const firstOutcome = await first;
          J.playSong = originalPlaySong;
          await new Promise(resolve => setTimeout(resolve, 20));

          return {
            afterSecond,
            firstOk: firstOutcome.ok,
            firstMessage: firstOutcome.message || null,
            // B 的播放不能被 A 的收尾停掉。
            current: J.State.currentSong && J.State.currentSong.id,
            isPlaying: J.State.isPlaying
          };
        }
        """
    )

    assert result["afterSecond"] == {"ok": True, "current": "song2", "isPlaying": True}
    assert result["firstOk"] is False
    assert result["firstMessage"] == "play_cancelled"
    assert result["current"] == "song2"
    assert result["isPlaying"] is True


@pytest.mark.frontend
def test_jukebox_teardown_during_startup_stops_the_orphaned_audio(mock_page: Page):
    """Teardown reached after the audio already started has to stop it.

    The existing teardown guard aborts earlier, inside ensureRuntime, so this
    pins the late branch: nothing else will ever stop that player.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          let release;
          const gate = new Promise(resolve => { release = resolve; });
          // hook 必须落在 playSong **内部**：包在外面的话 playSong 已经跑完并提交了
          // currentSong，拆除就走不到「起播中途」那条分支了。
          const originalPlayAudio = J.playAudio;
          J.playAudio = async function(song) {
            await originalPlayAudio.call(this, song);
            await gate;
          };

          const pending = J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          await new Promise(resolve => setTimeout(resolve, 10));
          const player = J.getPlayer();
          const audibleBeforeTeardown = player.audio.paused === false;

          // 音频已经在响，这时用户把点歌台整个拆掉。
          J.prepareForUnload();
          release();
          const outcome = await pending;
          J.playAudio = originalPlayAudio;
          await new Promise(resolve => setTimeout(resolve, 20));

          return {
            audibleBeforeTeardown,
            ok: outcome.ok,
            message: outcome.message || null,
            stillAudible: player.audio.paused === false,
            isPlaying: J.State.isPlaying
          };
        }
        """
    )

    assert result["audibleBeforeTeardown"] is True
    assert result["ok"] is False
    assert result["message"] == "jukebox_torn_down"
    # 拆除之后不许留下一份还在响、没人管的音频。
    assert result["stillAudible"] is False
    assert result["isPlaying"] is False


@pytest.mark.frontend
def test_jukebox_owner_queues_controls_that_arrive_before_it_is_ready(mock_page: Page):
    """Both reviewers, same race: the window was open but nobody owned it.

    Announcing after the deferred init left a gap in which the character window
    saw no owner and started a hidden runtime; later commands went to the
    visible player, orphaning that first stream. Ownership is claimed as soon
    as the window exists, and commands arriving before it can serve are queued.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          // peer 必须先建：BroadcastChannel 不补发历史消息，晚建就收不到开场
          // 那次宣告，只能等 2 秒后的心跳。
          const peer = new BroadcastChannel('neko-jukebox-control');
          const seen = [];
          peer.onmessage = (event) => seen.push(event.data);

          window.__NEKO_JUKEBOX_STANDALONE__ = true;
          J.startControlOwnerService();
          const waitFor = async (predicate, ms) => {
            const deadline = Date.now() + ms;
            while (Date.now() < deadline) {
              if (predicate()) return true;
              await new Promise(resolve => setTimeout(resolve, 10));
            }
            return false;
          };

          // 窗口刚起来：归属已经宣告，但还没就绪。
          const announcedBeforeReady = await waitFor(
            () => seen.some(m => m && m.type === 'jukebox_owner_alive'), 1000);
          const readyBefore = J.State.controlOwnerReady;

          peer.postMessage({
            type: 'jukebox_control_request',
            requestId: 'early-1',
            command: { action: 'set_mode', mode: 'random', headless: true }
          });
          await new Promise(resolve => setTimeout(resolve, 50));
          const servedWhileNotReady = seen.some(
            m => m && m.type === 'jukebox_control_result' && m.requestId === 'early-1');
          const queued = J.State.controlOwnerPending.length;
          const modeBeforeReady = J.State.playbackMode;

          // 曲库与播放器就绪，攒着的指令放出去。
          J.markControlOwnerReady();
          const servedAfterReady = await waitFor(
            () => seen.some(m => m && m.type === 'jukebox_control_result' && m.requestId === 'early-1'), 2000);

          J.stopControlOwnerService();
          peer.close();
          window.__NEKO_JUKEBOX_STANDALONE__ = false;
          return {
            announcedBeforeReady,
            readyBefore,
            servedWhileNotReady,
            queued,
            modeBeforeReady,
            servedAfterReady,
            modeAfterReady: J.State.playbackMode
          };
        }
        """
    )

    # 窗口一存在就有主，角色窗口不会以为没人管而自己起隐藏运行时。
    assert result["announcedBeforeReady"] is True
    assert result["readyBefore"] is False
    # 就绪之前只排队，不执行——否则会用默认模型类型选动作。
    assert result["servedWhileNotReady"] is False
    assert result["queued"] == 1
    assert result["modeBeforeReady"] == "sequence"
    # 就绪之后按到达顺序放出去。
    assert result["servedAfterReady"] is True
    assert result["modeAfterReady"] == "random"


@pytest.mark.frontend
def test_jukebox_standalone_bootstrap_claims_ownership_immediately(mock_page: Page):
    """The claim has to happen as the window loads, not after its init settles.

    A gap with no owner is what let the character window start a hidden runtime
    the visible player could never take over.
    """
    standalone_source = (REPO_ROOT / "static" / "jukebox" / "jukebox-standalone.js").read_text(
        encoding="utf-8"
    )

    result = mock_page.evaluate(
        """
        (source) => {
          const run = (isStandalone) => {
            const calls = [];
            window.__NEKO_JUKEBOX_STANDALONE__ = isStandalone;
            window.Jukebox = {
              startControlOwnerService: () => { calls.push('claimed'); return true; },
              // 宣告的那一刻绝不能顺带把「可以执行了」也标上。
              markControlOwnerReady: () => { calls.push('ready'); return true; }
            };
            try {
              new Function(source)();
            } catch (error) {
              return { calls, error: String(error && error.message || error) };
            }
            return { calls, error: null };
          };
          return { standalone: run(true), embedded: run(false) };
        }
        """,
        standalone_source,
    )

    assert result["standalone"]["error"] is None
    assert result["standalone"]["calls"] == ["claimed"]
    # 嵌在角色窗口里的点歌台不是拥有者，绝不能去抢这个身份。
    assert result["embedded"]["calls"] == []


@pytest.mark.frontend
def test_jukebox_stale_play_spares_a_successor_mid_startup(mock_page: Page):
    """Greptile P1: currentSong is cleared while the replacement is starting.

    That window — successor's audio already audible, its currentSong not yet
    committed — is exactly where a "nobody claimed this" predicate misfires and
    the stale request stops the successor's sound.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          const player = J.getPlayer();

          // A 卡在自己的 playAudio 里。
          let releaseA;
          const gateA = new Promise(resolve => { releaseA = resolve; });
          // B 卡在起播之后、提交 currentSong 之前。
          let releaseB;
          const gateB = new Promise(resolve => { releaseB = resolve; });

          // A 的 hold 点在它自己的 playAudio 之后。
          // B 的 hold 点要落在「认领音频之后、提交 currentSong 之前」。自从导航
          // 锚点提前到「音频一响就记」之后，这一段只剩 confirmAudioStarted 那个
          // await —— 动画那一段已经排在锚点提交之后了，挂在那里就测不到这个窗口。
          // 用一个显式开关切换，不靠调用顺序区分：A 的 confirm 反而排在 B 之后，
          // 按顺序判会互等死锁。
          let holdConfirm = false;
          const originalPlayAudio = J.playAudio;
          let firstPlay = true;
          J.playAudio = async function(song) {
            await originalPlayAudio.call(this, song);
            if (firstPlay) { firstPlay = false; await gateA; }
          };
          J.getActionForModel = () => ({ id: 'act', name: 'Dance', file: 'actions/a.vrma' });
          J.getActionAvailability = async () => ({
            ok: true,
            status: 'action_ready',
            action: { id: 'act', name: 'Dance', file: 'actions/a.vrma' },
            url: '/api/jukebox/file/actions/a.vrma'
          });
          J.getModelType = () => 'vrm';
          J.playVRMA = async function() { return true; };
          const originalConfirm = J.confirmAudioStarted;
          J.confirmAudioStarted = async function() {
            const started = await originalConfirm.apply(this, arguments);
            if (holdConfirm) { holdConfirm = false; await gateB; }
            return started;
          };

          const a = J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          await new Promise(resolve => setTimeout(resolve, 10));
          await J.executeControl({ action: 'stop', headless: true });

          holdConfirm = true;
          const b = J.executeControl({ action: 'play', query: 'Song 2', headless: true });
          await new Promise(resolve => setTimeout(resolve, 20));
          const midStartup = {
            audible: player.audio.paused === false,
            currentSong: J.State.currentSong,
            audioOwner: J.State.audioOwnerRequestId !== null
          };

          // A 正好在这个窗口里醒来。
          releaseA();
          const aOutcome = await a;
          await new Promise(resolve => setTimeout(resolve, 10));
          const afterStaleWoke = { audible: player.audio.paused === false };

          releaseB();
          const bOutcome = await b;
          J.playAudio = originalPlayAudio;
          J.confirmAudioStarted = originalConfirm;

          return {
            midStartup,
            aOk: aOutcome.ok,
            afterStaleWoke,
            bOk: bOutcome.ok,
            current: J.State.currentSong && J.State.currentSong.id
          };
        }
        """
    )

    # B 的音频已经在响、也已认领，但 currentSong 还没提交——正是「没人认领这份
    # 声音」这类判据最容易 misfire 的窗口。
    assert result["midStartup"]["audible"] is True
    assert result["midStartup"]["currentSong"] is None
    assert result["midStartup"]["audioOwner"] is True
    assert result["aOk"] is False
    # 过期的 A 醒来不许把 B 的声音停掉。
    assert result["afterStaleWoke"]["audible"] is True
    assert result["bOk"] is True
    assert result["current"] == "song2"


@pytest.mark.frontend
def test_jukebox_audio_claim_is_released_by_a_stop(mock_page: Page):
    """The claim has to be cleared when playback stops.

    A claim left behind by an earlier request blocks the next one from taking
    it, and then nothing cleans that request's audio up when it is cancelled.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          const player = J.getPlayer();

          // 第一条播放会认领这份音频。
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          const claimedByFirst = J.State.audioOwnerRequestId !== null;

          await J.executeControl({ action: 'stop', headless: true });
          const claimAfterStop = J.State.audioOwnerRequestId;

          // 第二条播放卡在起播里，然后被取消——它得能认领到，才收得掉自己的声音。
          let release;
          const gate = new Promise(resolve => { release = resolve; });
          const originalPlayAudio = J.playAudio;
          J.playAudio = async function(song) {
            await originalPlayAudio.call(this, song);
            await gate;
          };
          const pending = J.executeControl({ action: 'play', query: 'Song 2', headless: true });
          await new Promise(resolve => setTimeout(resolve, 10));
          J.cancelActivePlayback();
          release();
          const outcome = await pending;
          J.playAudio = originalPlayAudio;
          await new Promise(resolve => setTimeout(resolve, 20));

          return {
            claimedByFirst,
            claimAfterStop,
            secondOk: outcome.ok,
            stillAudible: player.audio.paused === false
          };
        }
        """
    )

    assert result["claimedByFirst"] is True
    # stop 必须把认领清掉，否则下一条播放永远认领不到。
    assert result["claimAfterStop"] is None
    assert result["secondOk"] is False
    assert result["stillAudible"] is False


@pytest.mark.frontend
def test_jukebox_owner_queue_keeps_order_and_spares_non_playback(mock_page: Page):
    """Three follow-ups on the owner queue, in one scenario.

    onmessage is async, so nothing serialised the served commands; a cancel
    discarded the whole backlog including volume/mode commands; and discarded
    requests were never answered, so the caller's forward sat until its timeout.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const peer = new BroadcastChannel('neko-jukebox-control');
          const replies = [];
          peer.onmessage = (event) => {
            const data = event.data;
            if (data && data.type === 'jukebox_control_result') replies.push(data);
          };

          window.__NEKO_JUKEBOX_STANDALONE__ = true;
          J.startControlOwnerService();

          const order = [];
          const originalExecute = J.executeControl;
          J.executeControl = async function(command) {
            order.push('start:' + command.action);
            await new Promise(resolve => setTimeout(resolve, 20));
            order.push('end:' + command.action);
            return { ok: true, action: command.action };
          };

          const send = (id, command) => peer.postMessage({
            type: 'jukebox_control_request', requestId: id, command, ttlMs: 5000
          });

          // 就绪之前攒下：一条音量、一条播放。
          send('q-vol', { action: 'set_volume', value: 40 });
          send('q-play', { action: 'play', query: 'Song 1' });
          await new Promise(resolve => setTimeout(resolve, 30));
          const queuedBeforeReady = J.State.controlOwnerPending.length;

          // 取消：只该丢掉播放那条，并给它回执；音量那条要留着。
          peer.postMessage({ type: 'jukebox_cancel_request' });
          await new Promise(resolve => setTimeout(resolve, 20));
          const afterCancel = {
            pending: J.State.controlOwnerPending.map(
              item => item.command.action),
            replied: replies.map(r => [r.requestId, r.result.message || 'ok'])
          };

          // 开闸；紧接着再来一条，它必须排在攒着的那条后面。
          J.markControlOwnerReady();
          send('late', { action: 'next' });
          await new Promise(resolve => setTimeout(resolve, 200));

          J.executeControl = originalExecute;
          J.stopControlOwnerService();
          peer.close();
          window.__NEKO_JUKEBOX_STANDALONE__ = false;
          return { queuedBeforeReady, afterCancel, order };
        }
        """
    )

    assert result["queuedBeforeReady"] == 2
    # 取消只丢播放类，音量那条留下；被丢的那条立刻有回执，调用方不必干等超时。
    assert result["afterCancel"]["pending"] == ["set_volume"]
    assert result["afterCancel"]["replied"] == [["q-play", "play_cancelled"]]
    # 串行：后到的 next 不许插到攒着的 set_volume 中间。
    assert result["order"] == [
        "start:set_volume", "end:set_volume", "start:next", "end:next",
    ]


@pytest.mark.frontend
def test_jukebox_owner_drops_a_request_the_caller_stopped_waiting_for(mock_page: Page):
    """A queued request outliving the caller's timeout must not act silently."""
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const peer = new BroadcastChannel('neko-jukebox-control');
          const replies = [];
          peer.onmessage = (event) => {
            const data = event.data;
            if (data && data.type === 'jukebox_control_result') replies.push(data);
          };

          window.__NEKO_JUKEBOX_STANDALONE__ = true;
          J.startControlOwnerService();

          let executed = 0;
          const originalExecute = J.executeControl;
          J.executeControl = async function(command) {
            executed += 1;
            return originalExecute.call(this, command);
          };

          // ttl 极短：等它过期之后才开闸。
          peer.postMessage({
            type: 'jukebox_control_request',
            requestId: 'stale',
            command: { action: 'set_mode', mode: 'random' },
            ttlMs: 20
          });
          await new Promise(resolve => setTimeout(resolve, 80));
          J.markControlOwnerReady();
          await new Promise(resolve => setTimeout(resolve, 60));

          J.executeControl = originalExecute;
          J.stopControlOwnerService();
          peer.close();
          window.__NEKO_JUKEBOX_STANDALONE__ = false;
          return {
            executed,
            replies: replies.map(r => [r.requestId, r.result.message]),
            mode: J.State.playbackMode
          };
        }
        """
    )

    # 调用方早就不等了：不执行，但要明确回执。
    assert result["executed"] == 0
    assert result["replies"] == [["stale", "jukebox_request_expired"]]
    assert result["mode"] == "sequence"


@pytest.mark.frontend
def test_jukebox_random_queue_rewinds_when_the_pick_never_plays(mock_page: Page):
    """Codex P2: the queue advanced before the song was known to be playable.

    getRandomAdjacentSong moves the index (or appends) up front, so a pick whose
    preflight fails left the queue claiming a song that never played, and the
    next skip started from the wrong place.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          await J.executeControl({ action: 'set_mode', mode: 'random', headless: true });
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });

          const before = {
            queue: J.State.randomQueue.slice(),
            index: J.State.randomQueueIndex
          };

          // 下一首的音频文件已经不在了。
          const originalPreflight = J.preflightSongPlayback;
          J.preflightSongPlayback = async () => ({ ok: false, message: 'audio_not_found', audioUrl: '' });
          const outcome = await J.executeControl({ action: 'next', headless: true });
          J.preflightSongPlayback = originalPreflight;

          return {
            before,
            ok: outcome.ok,
            message: outcome.message,
            after: { queue: J.State.randomQueue.slice(), index: J.State.randomQueueIndex },
            current: J.State.currentSong && J.State.currentSong.id
          };
        }
        """
    )

    assert result["ok"] is False
    assert result["message"] == "audio_not_found"
    # 没播起来就不许推进队列，否则下一次 next 会从错的位置继续、跳过一首。
    assert result["after"] == result["before"]
    assert result["current"] == "song1"


@pytest.mark.frontend
def test_jukebox_stale_match_refreshes_before_reporting_failure(mock_page: Page):
    """Codex P2: a cached hit whose file is gone never triggered a refresh.

    Refreshing only when the search found nothing meant a stale match failed
    preflight forever, because the search kept returning that same object.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          let configFetches = 0;
          let servePath = 'songs/old.mp3';
          window.fetch = async (url, options = {}) => {
            if (options.method === 'HEAD') {
              // 旧路径已经不在了，新路径还在。
              const ok = !String(url).includes('old.mp3');
              return { ok, status: ok ? 200 : 404 };
            }
            if (url === '/api/jukebox/config') {
              configFetches += 1;
              return {
                ok: true,
                json: async () => ({
                  configRevision: 'rev-' + servePath,
                  songs: {
                    moved: { name: 'Moved Song', artist: 'A', audio: servePath, visible: true }
                  },
                  actions: {},
                  bindings: {}
                })
              };
            }
            throw new Error('Unexpected fetch: ' + url);
          };

          await J.ensureRuntime({ headless: true });
          const cachedAudio = J.State.songs[0].audio;

          // 后端那边这首歌已经换了音频路径。
          servePath = 'songs/new.mp3';
          const outcome = await J.executeControl({ action: 'play', query: 'Moved', headless: true });

          return {
            cachedAudio,
            ok: outcome.ok,
            message: outcome.message || null,
            current: J.State.currentSong && J.State.currentSong.id,
            audioNow: J.State.songs[0].audio,
            configFetches
          };
        }
        """
    )

    assert result["cachedAudio"] == "songs/old.mp3"
    # 命中的是陈旧对象、预检失败 -> 刷新一次再试，这次能播。
    assert result["ok"] is True
    assert result["current"] == "moved"
    assert result["audioNow"] == "songs/new.mp3"
    # 一次运行时初始化 + 一次陈旧刷新。
    assert result["configFetches"] == 2


@pytest.mark.frontend
def test_jukebox_fbx_reports_that_no_animation_started(mock_page: Page):
    """Codex P2: playFBX returned true while its implementation is a TODO.

    playSong then cleared the idle debt as if a replacement animation had taken
    over, leaving the avatar on the interrupted dance's last frame.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          window.fbxManager = {};
          const started = await J.playFBX('/api/jukebox/file/actions/a.fbx', {});
          return { started };
        }
        """
    )

    # 一帧都没播，就不能报「起播了」——否则待机欠账会被错误地清掉。
    assert result["started"] is False


@pytest.mark.frontend
def test_jukebox_cold_start_keeps_the_persisted_volume(mock_page: Page):
    """CodeRabbit: State.savedVolume defaults to 1, which is not a user setting.

    Re-applying it on a cold start overwrote the volume APlayer had persisted
    from the previous session, resetting everyone to 100%.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        () => {
          const J = window.Jukebox;
          // 上次会话留下的音量。
          localStorage.setItem('aplayer-setting', JSON.stringify({ volume: 0.3 }));

          // 冷启动：用户这一轮没碰过滑条。
          const pendingBefore = J.State.pendingVolume;
          J.initPlayer({ headless: true });
          const player = J.getPlayer();

          return {
            pendingBefore,
            // 一个字都不该提音量，让 APlayer 自己恢复。
            constructedWith: Object.prototype.hasOwnProperty.call(player.options, 'volume'),
            playerVolume: player.audio.volume
          };
        }
        """
    )

    assert result["pendingBefore"] is None
    assert result["constructedWith"] is False
    # 上次会话的 30% 必须留住，不能被 savedVolume 的默认值 1 抹掉。
    assert result["playerVolume"] == 0.3


@pytest.mark.frontend
def test_jukebox_control_refresh_rerenders_the_open_panel(mock_page: Page):
    """Codex P2: the refresh replaced the songs but never repainted the panel.

    It also advances configRevision, so the 10-second poll then sees itself as
    current and never calls loadSongs — the panel stays on the old rows for good.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          let serveLate = false;
          window.fetch = async (url, options = {}) => {
            if (options.method === 'HEAD') return { ok: true, status: 200 };
            if (url === '/api/jukebox/config') {
              const songs = { early: { name: 'Early', artist: 'A', audio: 'songs/e.mp3', visible: true } };
              if (serveLate) songs.late = { name: 'Late Arrival', artist: 'B', audio: 'songs/l.mp3', visible: true };
              return {
                ok: true,
                json: async () => ({ configRevision: serveLate ? 'rev-2' : 'rev-1', songs, actions: {}, bindings: {} })
              };
            }
            throw new Error('Unexpected fetch: ' + url);
          };

          await J.ensureRuntime({ headless: true });

          // 面板开着（用最小的 DOM 骨架，renderList 只需要这个容器）。
          document.body.insertAdjacentHTML('beforeend', '<table><tbody id="jukebox-song-list"></tbody></table>');
          J.State.isOpen = true;
          J.renderList();
          const rowsBefore = document.querySelectorAll('#jukebox-song-list .song-row, #jukebox-song-list tr').length;

          // 用户上传了一首新歌，然后让 AI 播它——控制面会刷新曲库。
          serveLate = true;
          const played = await J.executeControl({ action: 'play', query: 'Late Arrival', headless: true });
          await new Promise(resolve => setTimeout(resolve, 20));

          const rowsAfter = document.querySelectorAll('#jukebox-song-list .song-row, #jukebox-song-list tr').length;
          J.State.isOpen = false;
          return {
            rowsBefore,
            ok: played.ok,
            current: J.State.currentSong && J.State.currentSong.id,
            songCount: J.State.songs.length,
            rowsAfter
          };
        }
        """
    )

    assert result["rowsBefore"] == 1
    assert result["ok"] is True
    assert result["current"] == "late"
    assert result["songCount"] == 2
    # 刷新之后面板必须跟着重画，否则它会永久停在旧的那批行上。
    assert result["rowsAfter"] == 2


@pytest.mark.frontend
def test_jukebox_owner_answers_the_forwards_it_abandons_on_shutdown(mock_page: Page):
    """Codex P2: clearing the pending queue left the caller waiting out the TTL.

    The character window's forwardControl awaits a reply, and its whole command
    queue sits behind that promise -- so discarding a queued forward silently
    stalls local execution for the full five-second timeout.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          window.__NEKO_JUKEBOX_STANDALONE__ = true;
          const posted = [];
          const channel = {
            postMessage: (message) => { posted.push(message); },
            close: () => {},
            onmessage: null
          };
          const OriginalBC = window.BroadcastChannel;
          window.BroadcastChannel = function() { return channel; };
          try {
            J.startControlOwnerService();
            // 还没就绪：转发进来的指令攒在队列里。
            channel.onmessage({ data: {
              type: 'jukebox_control_request',
              requestId: 'req-1',
              command: { action: 'play', query: 'Song 1' }
            } });
            channel.onmessage({ data: {
              type: 'jukebox_control_request',
              requestId: 'req-2',
              command: { action: 'set_volume', value: 40 }
            } });
            const queued = J.State.controlOwnerPending.length;
            J.stopControlOwnerService();
            return {
              queued,
              results: posted
                .filter(m => m.type === 'jukebox_control_result')
                .map(m => ({ requestId: m.requestId, message: m.result.message, ok: m.result.ok })),
              pendingAfter: J.State.controlOwnerPending.length
            };
          } finally {
            window.BroadcastChannel = OriginalBC;
            window.__NEKO_JUKEBOX_STANDALONE__ = false;
          }
        }
        """
    )

    assert result["queued"] == 2
    assert result["pendingAfter"] == 0
    # 每一条被丢掉的都要有回执，调用方才不用干等 TTL；而且回执要能分辨是哪一条。
    assert result["results"] == [
        {"requestId": "req-1", "message": "jukebox_owner_gone", "ok": False},
        {"requestId": "req-2", "message": "jukebox_owner_gone", "ok": False},
    ]


@pytest.mark.frontend
def test_jukebox_loader_settles_in_flight_forwards_when_the_owner_goes_away(mock_page: Page):
    """The owner can also vanish after it has taken the command.

    Zeroing the TTL was not enough: the promise the caller is awaiting only ever
    settled on a result message or the five-second timeout, so the command queue
    behind it stalled even though the owner had already announced it was gone.
    """
    mock_page.set_content(
        """
        <script>
          window.t = (key, fallback) => typeof fallback === 'string' ? fallback : key;
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    result = mock_page.evaluate(
        """
        async () => {
          const loader = window.__nekoJukeboxLoader;
          const owner = new BroadcastChannel('neko-jukebox-control');
          owner.onmessage = (event) => {
            const data = event && event.data;
            if (!data) return;
            if (data.type === 'jukebox_owner_query') {
              owner.postMessage({ type: 'jukebox_owner_alive' });
            }
            // 收下指令但永不回执：模拟拥有者接了活之后窗口就关了。
          };
          owner.postMessage({ type: 'jukebox_owner_alive' });
          await new Promise(resolve => setTimeout(resolve, 60));
          if (!loader.hasControlOwner()) return { ownerSeen: false };

          const started = Date.now();
          const forwarded = loader.forwardControl({ action: 'play', query: 'x' });
          await new Promise(resolve => setTimeout(resolve, 60));
          owner.postMessage({ type: 'jukebox_owner_gone' });
          const settled = await forwarded;
          const elapsed = Date.now() - started;
          owner.close();
          return { ownerSeen: true, settled, elapsed };
        }
        """
    )

    assert result["ownerSeen"] is True
    assert result["settled"]["ok"] is False
    assert result["settled"]["message"] == "jukebox_owner_gone"
    # 关键是「不必等满 TTL」：转发超时是 5000ms。
    assert result["elapsed"] < 2000, result["elapsed"]


@pytest.mark.frontend
def test_jukebox_runtime_init_slot_is_cleared_only_by_its_own_initialization(mock_page: Page):
    """Codex P2: the finally cleared a slot that already held a replacement.

    Teardown clears runtimeInitPromise while initialization A is still awaiting
    its configuration, so a later command starts B.  When A then resumed, its
    unconditional finally released the slot B was occupying, letting a third
    command start yet another concurrent load -- and the loads overwrite
    State.songs out of order while commands are searching it.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const originalLoad = J.loadSongData;
          const gates = [];
          let loads = 0;
          J.loadSongData = async function() {
            loads += 1;
            let release;
            const gate = new Promise(resolve => { release = resolve; });
            gates.push(release);
            await gate;
            return originalLoad.apply(this, arguments);
          };
          try {
            const a = J.ensureRuntime({ headless: true });
            await new Promise(resolve => setTimeout(resolve, 10));
            // 拆除：槽位被清掉，A 还卡在配置加载里。
            J.prepareForUnload();
            const b = J.ensureRuntime({ headless: true });
            await new Promise(resolve => setTimeout(resolve, 10));
            const loadsBeforeAResumes = loads;

            // A 收尾。它不能把 B 占着的槽位清掉。
            gates[0]();
            await a.catch(() => {});
            await new Promise(resolve => setTimeout(resolve, 10));
            const slotStillHeld = J.State.runtimeInitPromise !== null;

            // 第三条指令必须挂在 B 上，而不是再起一次并发的配置加载。
            const c = J.ensureRuntime({ headless: true });
            await new Promise(resolve => setTimeout(resolve, 10));
            const loadsAfterThirdCommand = loads;

            gates.slice(1).forEach(release => release());
            await b.catch(() => {});
            await c.catch(() => {});
            return { loadsBeforeAResumes, slotStillHeld, loadsAfterThirdCommand };
          } finally {
            J.loadSongData = originalLoad;
          }
        }
        """
    )

    assert result["loadsBeforeAResumes"] == 2
    # A 的 finally 只有在槽位还指向它自己时才该清。
    assert result["slotStillHeld"] is True
    # 第三条指令复用 B：没有第三次并发的配置加载去乱序覆盖 State.songs。
    assert result["loadsAfterThirdCommand"] == 2, result["loadsAfterThirdCommand"]


@pytest.mark.frontend
def test_jukebox_random_queue_rewinds_when_the_auto_advance_is_abandoned(mock_page: Page):
    """Codex P2: the dual of the next/previous rollback was missing.

    getNextSongToPlay advances the random position before the scheduled
    transition runs.  Abandoning that transition -- a mode change in the gap --
    left the queue anchored to a song that never played, and leaving random mode
    then records it as the exit anchor.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          await J.executeControl({ action: 'set_mode', mode: 'random', headless: true });
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });

          const before = {
            queue: J.State.randomQueue.slice(),
            index: J.State.randomQueueIndex
          };

          J.handleAudioEnded(J.getPlayer());
          // 定时器还没跑，模式在这个空档里变了：这次自动续播作废。
          J.State.playbackMode = 'none';
          await new Promise(resolve => setTimeout(resolve, 20));

          return {
            before,
            after: { queue: J.State.randomQueue.slice(), index: J.State.randomQueueIndex },
            currentSong: J.State.currentSong && J.State.currentSong.id
          };
        }
        """
    )

    # 那首从没播过，队列位置不许停在它身上。
    assert result["after"] == result["before"]
    assert result["currentSong"] is None


@pytest.mark.frontend
def test_jukebox_plays_the_song_revision_that_passed_preflight(mock_page: Page):
    """Codex P2: playSong re-resolved the id after preflight had validated it.

    The visible panel's configuration poll can refresh State.songs during the
    HEAD preflight, so a re-upload or a binding edit made playback load a
    different audio path than the one that was actually checked.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          const loaded = [];
          const originalPlayAudio = J.playAudio;
          J.playAudio = function(song) {
            loaded.push(song && song.audio);
            return originalPlayAudio.apply(this, arguments);
          };

          // 预检通过之后、起播之前，面板的轮询把同一个 id 换成了新一版。
          const originalPreflight = J.preflightSongPlayback;
          J.preflightSongPlayback = async function(song) {
            const outcome = await originalPreflight.apply(this, arguments);
            const replaced = J.State.songs.map(s => (
              s.id === song.id ? Object.assign({}, s, { audio: 'songs/reuploaded.mp3' }) : s
            ));
            J.State.songs = replaced;
            return outcome;
          };

          try {
            await J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          } finally {
            J.preflightSongPlayback = originalPreflight;
            J.playAudio = originalPlayAudio;
          }

          return { loaded, tableAudio: J.State.songs.find(s => s.id === 'song1').audio };
        }
        """
    )

    # 曲库里已经是新一版了，但播的必须是通过预检的那一版。
    assert result["tableAudio"] == "songs/reuploaded.mp3"
    assert result["loaded"] == ["songs/song1.mp3"], result["loaded"]


@pytest.mark.frontend
def test_jukebox_abandoned_auto_advance_keeps_the_anchor_repair(mock_page: Page):
    """The rollback must undo the speculative step, not the repair before it.

    getNextSongToPlay calls ensureRandomQueueAnchor first, which resets a queue
    that has drifted out of sync with the song that just ended -- a fact that
    holds whether or not the next song plays.  Restoring the raw snapshot put
    the stale queue back, so leaving random mode would anchor the exit on an
    entry the player had already moved past.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          await J.executeControl({ action: 'set_mode', mode: 'random', headless: true });
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });

          // 队列跟当前曲目脱节：正是 ensureRandomQueueAnchor 存在的理由。
          J.State.randomQueue = ['song3', 'song2'];
          J.State.randomQueueIndex = 1;
          const stale = { queue: J.State.randomQueue.slice(), index: J.State.randomQueueIndex };

          J.handleAudioEnded(J.getPlayer());
          J.State.playbackMode = 'none';
          await new Promise(resolve => setTimeout(resolve, 20));

          const queue = J.State.randomQueue.slice();
          const index = J.State.randomQueueIndex;
          return { stale, queue, index, anchored: queue[index] || null };
        }
        """
    )

    # 锚点修复保留：队列指向刚播完的那一首。
    assert result["anchored"] == "song1", result
    # 而那一步投机性的前进仍然被撤销了：队列没有停在从没播过的下一首上。
    assert result["queue"] == ["song1"], result["queue"]
    assert result["index"] == 0
    assert result["queue"] != result["stale"]["queue"]


@pytest.mark.frontend
def test_jukebox_owner_cancel_reaches_the_ready_state_queue_too(mock_page: Page):
    """Greptile P1: only the pre-ready queue could be cancelled.

    Once the owner is ready a request goes straight onto serveChain and can no
    longer be taken off it, so a queued play outlived the cancellation and then
    read the already-advanced epoch as current when its turn came.  Volume and
    mode commands must survive the same cancellation.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          window.__NEKO_JUKEBOX_STANDALONE__ = true;
          const posted = [];
          const channel = {
            postMessage: (message) => { posted.push(message); },
            close: () => {},
            onmessage: null
          };
          const OriginalBC = window.BroadcastChannel;
          window.BroadcastChannel = function() { return channel; };

          const executed = [];
          const originalExecute = J.executeControl;
          let releaseSlow;
          const slowGate = new Promise(resolve => { releaseSlow = resolve; });
          J.executeControl = async (command) => {
            executed.push(command.action + ':' + (command.query || ''));
            if (command.query === 'slow') await slowGate;
            return { ok: true, action: command.action };
          };

          try {
            J.startControlOwnerService();
            J.State.controlOwnerReady = true;

            // 第一条慢指令占住 serveChain。
            channel.onmessage({ data: {
              type: 'jukebox_control_request', requestId: 'r1',
              command: { action: 'play', query: 'slow' }
            } });
            await new Promise(resolve => setTimeout(resolve, 5));
            // 排在它后面的一条 play，以及一条与取消无关的音量。
            channel.onmessage({ data: {
              type: 'jukebox_control_request', requestId: 'r2',
              command: { action: 'play', query: 'queued' }
            } });
            channel.onmessage({ data: {
              type: 'jukebox_control_request', requestId: 'r3',
              command: { action: 'set_volume', value: 40 }
            } });
            // 独立的取消信号：它越过正在执行的那条，也必须作废排队的那条 play。
            channel.onmessage({ data: { type: 'jukebox_cancel_request' } });
            releaseSlow();
            await new Promise(resolve => setTimeout(resolve, 40));

            return {
              executed,
              results: posted
                .filter(m => m.type === 'jukebox_control_result')
                .map(m => ({ requestId: m.requestId, message: m.result.message, ok: m.result.ok }))
            };
          } finally {
            J.executeControl = originalExecute;
            J.stopControlOwnerService();
            window.BroadcastChannel = OriginalBC;
            window.__NEKO_JUKEBOX_STANDALONE__ = false;
          }
        }
        """
    )

    # 排队的那条 play 不许开跑；音量照常执行。
    assert result["executed"] == ["play:slow", "set_volume:"], result["executed"]
    # 而且被作废的那条要有回执，调用方才不用干等转发超时。
    assert {"requestId": "r2", "message": "play_cancelled", "ok": False} in result["results"]
    assert [r for r in result["results"] if r["requestId"] == "r3"][0]["ok"] is True


@pytest.mark.frontend
def test_jukebox_random_queue_rewinds_when_the_auto_advance_itself_fails(mock_page: Page):
    """CodeRabbit: the scheduled playSong is fire-and-forget and can fail.

    Audio load errors and a blocked autoplay both make playSong return null,
    but getNextSongToPlay had already advanced the position -- so the next skip
    started past a song that never played.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          await J.executeControl({ action: 'set_mode', mode: 'random', headless: true });
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });

          const before = {
            queue: J.State.randomQueue.slice(),
            index: J.State.randomQueueIndex
          };

          // 自动续播那一首起播失败。
          const originalPlaySong = J.playSong;
          J.playSong = async () => null;
          try {
            J.handleAudioEnded(J.getPlayer());
            await new Promise(resolve => setTimeout(resolve, 20));
          } finally {
            J.playSong = originalPlaySong;
          }

          return {
            before,
            after: { queue: J.State.randomQueue.slice(), index: J.State.randomQueueIndex }
          };
        }
        """
    )

    # 没播起来就不许把位置留在它身上，否则下一次随机导航跳过一首。
    assert result["after"] == result["before"], result


@pytest.mark.frontend
def test_jukebox_cancelling_playback_keeps_the_navigation_anchor(mock_page: Page):
    """Codex P1: making next/previous preempting cost them their anchor.

    cancelActivePlayback ran the plain stopPlayback, which clears currentSong
    and the random queue.  next and previous are computed relative to the song
    that was playing, so the queued command fell back to the first song and
    random navigation lost its history.  Cancellation exists to silence the
    audio and unwedge the in-flight command; deciding the playback position is
    the following command's job.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          // 顺序模式：next 相对当前曲目取下一首。
          await J.executeControl({ action: 'play', query: 'Song 2', headless: true });
          J.cancelActivePlayback();
          const anchorAfterCancel = J.State.currentSong && J.State.currentSong.id;
          const audibleAfterCancel = J.State.isPlaying;
          const next = await J.executeControl({ action: 'next', headless: true });

          // 随机模式：历史也要活下来。
          await J.executeControl({ action: 'set_mode', mode: 'random', headless: true });
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          await J.executeControl({ action: 'next', headless: true });
          const queueBefore = J.State.randomQueue.slice();
          J.cancelActivePlayback();

          return {
            anchorAfterCancel,
            audibleAfterCancel,
            nextSong: next.song && next.song.id,
            queueBefore,
            queueAfterCancel: J.State.randomQueue.slice()
          };
        }
        """
    )

    # 作废把声音停了，但没把「用户在哪儿」也一起抹掉。
    assert result["audibleAfterCancel"] is False
    assert result["anchorAfterCancel"] == "song2"
    # 于是排在后面的 next 走的是 song2 的下一首，而不是退回第一首。
    assert result["nextSong"] == "song3", result["nextSong"]
    # 随机历史同理：作废不该清空它。
    assert len(result["queueBefore"]) >= 2
    assert result["queueAfterCancel"] == result["queueBefore"]


@pytest.mark.frontend
def test_jukebox_auto_advance_rewind_survives_the_idle_settlement(mock_page: Page):
    """Codex P2: the rollback was blocked by the generation it advances itself.

    A failed auto-advance settles the pending idle restoration on its way out,
    and restoreIdleAnimation increments playRequestId (the VRM branch does this
    explicitly).  Keying the rollback on that counter therefore made it fail on
    the one path it was written for.  Ownership is now decided by the queue
    itself.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          await J.executeControl({ action: 'set_mode', mode: 'random', headless: true });
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });

          const before = {
            queue: J.State.randomQueue.slice(),
            index: J.State.randomQueueIndex
          };

          // 只替换动画层。要点是让世代**只**在 playSong 里被推进：
          // stopVMD 打成「抑制恢复、记一笔欠账」，它自己不推世代，否则
          // handleAudioEnded 里那一下就会让定时器走「被取代」出口，回滚由那里
          // 完成，这条用例就测不到 abandonAutoAdvance 了。
          const originalStopVMD = J.stopVMD;
          J.stopVMD = () => { J.State.idleRestorePending = true; };
          // 真实的 restoreIdleAnimation 在 VRM 分支里 ++playRequestId。
          const originalRestore = J.restoreIdleAnimation;
          J.restoreIdleAnimation = async () => {
            J.State.idleRestorePending = false;
            J.State.playRequestId += 1;
          };
          const originalPlaySong = J.playSong;
          // 起播失败，而且失败路上先结清了待机欠账 —— 正是 Codex 描述的形态。
          J.playSong = async () => { J.settleIdleRestore(); return null; };
          let generationMovedInPlaySong = false;
          try {
            const before = J.State.playRequestId;
            J.handleAudioEnded(J.getPlayer());
            await new Promise(resolve => setTimeout(resolve, 20));
            generationMovedInPlaySong = J.State.playRequestId !== before;
          } finally {
            J.playSong = originalPlaySong;
            J.restoreIdleAnimation = originalRestore;
            J.stopVMD = originalStopVMD;
          }

          return {
            before,
            generationMovedInPlaySong,
            after: { queue: J.State.randomQueue.slice(), index: J.State.randomQueueIndex }
          };
        }
        """
    )

    # 先确认这条用例真的走到了那个形态：世代是在 playSong 里被推进的。
    assert result["generationMovedInPlaySong"] is True
    # 结账推进了世代，但队列仍然归这次自动续播所有，所以回滚必须发生。
    assert result["after"] == result["before"], result


@pytest.mark.frontend
def test_jukebox_auto_advance_rewind_yields_to_a_replacement_play(mock_page: Page):
    """Codex P2: the other side of the same rule.

    If the user starts another song before the scheduled transition runs, that
    play has already reset the random queue around its own track.  Restoring
    the pre-advance snapshot over it would overwrite valid history, and the
    next random navigation would proceed from the song that ended long ago.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          await J.executeControl({ action: 'set_mode', mode: 'random', headless: true });
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });

          // 接手可能有几种形态，回滚器的三道比对各挡一种：
          //   reset —— 长度变了（用户点了另一首，队列围绕它重建）
          //   reindex —— 长度和内容都没变，只有位置动了（在同一条队列里往回导航）
          const shapes = {
            reset: () => { J.resetRandomQueue('song3'); },
            reindex: () => { J.State.randomQueueIndex = Math.max(0, J.State.randomQueueIndex - 1); }
          };
          const results = {};
          for (const name of Object.keys(shapes)) {
            await J.executeControl({ action: 'set_mode', mode: 'random', headless: true });
            await J.executeControl({ action: 'play', query: 'Song 1', headless: true });
            J.handleAudioEnded(J.getPlayer());
            const advanced = {
              queue: J.State.randomQueue.slice(),
              index: J.State.randomQueueIndex
            };
            // 定时器还没跑，接手的一方已经动了队列并推进了世代。
            shapes[name]();
            J.State.playRequestId += 1;
            const replacement = {
              queue: J.State.randomQueue.slice(),
              index: J.State.randomQueueIndex
            };
            await new Promise(resolve => setTimeout(resolve, 20));
            results[name] = {
              advanced,
              replacement,
              after: { queue: J.State.randomQueue.slice(), index: J.State.randomQueueIndex }
            };
          }
          return results;
        }
        """
    )

    # 队列已经归接手的那一方了，陈旧快照不许盖回去 —— 三种形态都要认得出。
    assert result["reset"]["after"] == result["reset"]["replacement"], result["reset"]
    assert result["reset"]["after"]["queue"] == ["song3"]
    # reindex 这一种长度和内容都没变，只有位置动了：只有位置比对能守住。
    reindex = result["reindex"]
    assert reindex["replacement"]["queue"] == reindex["advanced"]["queue"], reindex
    assert reindex["replacement"]["index"] != reindex["advanced"]["index"], reindex
    assert reindex["after"] == reindex["replacement"], reindex


@pytest.mark.frontend
def test_jukebox_owner_cancel_from_relative_navigation_spares_the_queued_play(mock_page: Page):
    """Codex P2: the owner's cancel rule disagreed with the sender's.

    next/previous send the same cancellation signal as stop and play, so the
    owner's unconditional bump superseded a queued play that the sender had
    deliberately left alone -- and the navigation then ran relative to the
    stale song instead of the queued one.  The signal now carries the action.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          window.__NEKO_JUKEBOX_STANDALONE__ = true;
          const OriginalBC = window.BroadcastChannel;

          const run = async (cancelAction) => {
            const posted = [];
            const channel = {
              postMessage: (m) => { posted.push(m); },
              close: () => {},
              onmessage: null
            };
            window.BroadcastChannel = function() { return channel; };
            const executed = [];
            const originalExecute = J.executeControl;
            let releaseSlow;
            const slowGate = new Promise(resolve => { releaseSlow = resolve; });
            J.executeControl = async (command) => {
              executed.push(command.action + ':' + (command.query || ''));
              if (command.query === 'slow') await slowGate;
              return { ok: true, action: command.action };
            };
            try {
              J.startControlOwnerService();
              J.State.controlOwnerReady = true;
              channel.onmessage({ data: {
                type: 'jukebox_control_request', requestId: 'r1',
                command: { action: 'play', query: 'slow' }
              } });
              await new Promise(resolve => setTimeout(resolve, 5));
              channel.onmessage({ data: {
                type: 'jukebox_control_request', requestId: 'r2',
                command: { action: 'play', query: 'queued' }
              } });
              channel.onmessage({ data: { type: 'jukebox_cancel_request', action: cancelAction } });
              releaseSlow();
              await new Promise(resolve => setTimeout(resolve, 40));
              return executed;
            } finally {
              J.executeControl = originalExecute;
              J.stopControlOwnerService();
            }
          };

          try {
            const relative = await run('next');
            const absolute = await run('stop');
            const unlabelled = await run('');
            return { relative, absolute, unlabelled };
          } finally {
            window.BroadcastChannel = OriginalBC;
            window.__NEKO_JUKEBOX_STANDALONE__ = false;
          }
        }
        """
    )

    # 相对导航不顶替排队中的 play：判据必须和发件侧一致。
    assert result["relative"] == ["play:slow", "play:queued"], result["relative"]
    # 绝对指令照旧顶替。
    assert result["absolute"] == ["play:slow"], result["absolute"]
    # 认不出动作时按绝对处理：漏顶替会让用户喊停之后声音又起来。
    assert result["unlabelled"] == ["play:slow"], result["unlabelled"]


@pytest.mark.frontend
def test_jukebox_navigation_anchor_is_recorded_when_the_audio_starts(mock_page: Page):
    """Codex P2: the preserved anchor was still empty during startup.

    currentSong used to be committed only after the animation awaits, so a
    next/previous arriving while the action was still loading cancelled the
    startup request and then found no anchor -- falling back to the first or
    last library entry instead of the audible song.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          // 动作还在加载：起播卡在动画那一步。
          let releaseAnimation;
          const animationGate = new Promise(resolve => { releaseAnimation = resolve; });
          J.getActionForModel = () => ({ id: 'act', name: 'Dance', file: 'actions/a.vrma' });
          J.getActionAvailability = async () => ({
            ok: true,
            status: 'action_ready',
            action: { id: 'act', name: 'Dance', file: 'actions/a.vrma' },
            url: '/api/jukebox/file/actions/a.vrma'
          });
          J.getModelType = () => 'vrm';
          J.playVRMA = async () => { await animationGate; return true; };

          const pending = J.executeControl({ action: 'play', query: 'Song 2', headless: true });
          await new Promise(resolve => setTimeout(resolve, 20));
          const midStartup = {
            audible: J.getPlayer().audio.paused === false,
            anchor: J.State.currentSong && J.State.currentSong.id
          };

          // 相对导航正好落在这个窗口里。
          J.cancelActivePlayback();
          const anchorAfterCancel = J.State.currentSong && J.State.currentSong.id;
          const adjacent = J.getManualAdjacentSong(1);

          releaseAnimation();
          await pending;
          return { midStartup, anchorAfterCancel, adjacent: adjacent && adjacent.id };
        }
        """
    )

    # 声音已经在响，锚点此刻就该记上了，不必等动画。
    assert result["midStartup"]["audible"] is True
    assert result["midStartup"]["anchor"] == "song2"
    assert result["anchorAfterCancel"] == "song2"
    # 于是 next 走的是 song2 的下一首，而不是退回第一首。
    assert result["adjacent"] == "song3", result["adjacent"]


@pytest.mark.frontend
def test_jukebox_manual_navigation_rollback_yields_to_a_replacement(mock_page: Page):
    """Codex P2: the manual rollback had no ownership check.

    The auto-advance rollback learned to ask whether the queue is still the one
    its own advance produced; the next/previous path -- which is the same
    speculative advance -- kept restoring unconditionally, so a song the user
    picked from the panel meanwhile had its fresh queue overwritten by the old
    snapshot.  Both now share one rollback.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          await J.executeControl({ action: 'set_mode', mode: 'random', headless: true });
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });

          // 远端的 next 卡在预检里，期间用户从面板点了另一首：那次播放围绕它
          // 重置了随机队列。
          let replacedQueue = null;
          let replacedIndex = -1;
          const originalPreflight = J.preflightSongPlayback;
          J.preflightSongPlayback = async function() {
            // 换成「长度相同、位置相同、内容不同」的一份：曲库刷新剪枝就是这个
            // 形态。只有逐项比对才认得出它已经不是我这次前进留下的队列了。
            const replaced = J.State.randomQueue.slice();
            const swapAt = replaced.length - 1;
            replaced[swapAt] = replaced[swapAt] === 'song4' ? 'song3' : 'song4';
            J.State.randomQueue = replaced;
            replacedQueue = replaced.slice();
            replacedIndex = J.State.randomQueueIndex;
            J.State.playRequestId += 1;
            return { ok: false, message: 'play_superseded', audioUrl: '' };
          };
          let outcome;
          try {
            outcome = await J.executeControl({ action: 'next', headless: true });
          } finally {
            J.preflightSongPlayback = originalPreflight;
          }

          return {
            ok: outcome.ok,
            queue: J.State.randomQueue.slice(),
            index: J.State.randomQueueIndex,
            replaced: replacedQueue,
            replacedIndex: replacedIndex
          };
        }
        """
    )

    assert result["ok"] is False
    # 队列已经归接手的那一方了，陈旧快照不许盖回去 —— 注意长度和位置都没变，
    # 只有内容变了，所以这条只有逐项比对能守住。
    assert result["queue"][-1] in ("song3", "song4")
    assert result["queue"] == result["replaced"], result
    assert result["index"] == result["replacedIndex"]


@pytest.mark.frontend
def test_jukebox_loader_labels_the_cancel_signal_with_its_action(mock_page: Page):
    """The owner's rule is only as good as what the sender tells it.

    The owner decides whether a cancellation supersedes queued playback from
    the action that initiated it.  If the loader drops that label, every cancel
    reads as absolute again and relative navigation goes back to swallowing the
    play queued in front of it.
    """
    mock_page.set_content(
        """
        <script>
          window.t = (key, fallback) => typeof fallback === 'string' ? fallback : key;
        </script>
        """
    )
    mock_page.add_script_tag(content=JUKEBOX_LOADER_SCRIPT)

    result = mock_page.evaluate(
        """
        async () => {
          const loader = window.__nekoJukeboxLoader;
          const seen = [];
          const owner = new BroadcastChannel('neko-jukebox-control');
          owner.onmessage = (event) => {
            const data = event && event.data;
            if (data && data.type === 'jukebox_cancel_request') seen.push(data.action);
          };
          loader.cancelOnOwner('next');
          loader.cancelOnOwner('STOP');
          loader.cancelOnOwner();
          await new Promise(resolve => setTimeout(resolve, 60));
          owner.close();
          return { seen };
        }
        """
    )

    # 动作原样带过去，并且归一化过大小写；没有动作时是空串（拥有者按绝对处理）。
    assert result["seen"] == ["next", "stop", ""], result["seen"]


@pytest.mark.frontend
def test_jukebox_stale_audio_ended_without_a_current_song_still_cleans_up(mock_page: Page):
    """CodeRabbit: a crash this PR introduced.

    The player can emit a stale `ended` after stopPlayback has already cleared
    currentSong.  getNextSongToPlay has always guarded that (`if (!endedSong)`),
    but the shared rollback hoisted `endedSong.id` above the guard and threw a
    TypeError, aborting the handler.  Returning early is not the fix either:
    the rest of the callback -- settling the idle debt, clearing the flags,
    refreshing the stopped status -- is still the right thing to do for a stale
    ended.  Only the queue advance is skipped.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          await J.executeControl({ action: 'set_mode', mode: 'random', headless: true });
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });

          const queueBefore = J.State.randomQueue.slice();
          const indexBefore = J.State.randomQueueIndex;

          // 陈旧的 ended：currentSong 已经被清掉了。
          J.State.currentSong = null;
          J.State.isPlaying = true;
          J.State.idleRestorePending = true;
          let stoppedStatusRefreshed = false;
          const originalStopped = J.updateStoppedStatus;
          J.updateStoppedStatus = function() {
            stoppedStatusRefreshed = true;
            return originalStopped.apply(this, arguments);
          };

          let threw = null;
          try {
            J.handleAudioEnded(J.getPlayer());
          } catch (error) {
            threw = String(error && error.message || error);
          } finally {
            J.updateStoppedStatus = originalStopped;
          }
          await new Promise(resolve => setTimeout(resolve, 20));

          return {
            threw,
            isPlaying: J.State.isPlaying,
            stoppedStatusRefreshed,
            idleRestorePending: J.State.idleRestorePending,
            queueUnchanged: JSON.stringify(J.State.randomQueue) === JSON.stringify(queueBefore)
              && J.State.randomQueueIndex === indexBefore
          };
        }
        """
    )

    assert result["threw"] is None, result["threw"]
    # 清理照做。
    assert result["isPlaying"] is False
    assert result["stoppedStatusRefreshed"] is True
    assert result["idleRestorePending"] is False
    # 但队列不该被推进 —— 没有「刚播完的那首」可言。
    assert result["queueUnchanged"] is True


@pytest.mark.frontend
def test_jukebox_torn_down_runtime_does_not_commit_its_library(mock_page: Page):
    """Codex P2: guarding the promise slot did not stop the in-flight load.

    Teardown clears runtimeInitPromise while initialization A is still fetching
    the configuration, so a later command starts B.  A then resumed and
    committed its own response over B's, and the replacement runtime kept a
    stale library until something else refreshed it.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          const originalFetch = window.fetch;
          let releaseStale;
          const staleGate = new Promise(resolve => { releaseStale = resolve; });
          let call = 0;
          window.fetch = async (url, options = {}) => {
            if (url === '/api/jukebox/config') {
              call += 1;
              const mine = call;
              if (mine === 1) await staleGate;
              return {
                ok: true,
                json: async () => ({
                  configRevision: 'rev-' + mine,
                  songs: mine === 1
                    ? { stale: { name: 'Stale', artist: 'A', audio: 'songs/stale.mp3', visible: true } }
                    : { fresh: { name: 'Fresh', artist: 'B', audio: 'songs/fresh.mp3', visible: true } },
                  actions: {},
                  bindings: {}
                })
              };
            }
            return originalFetch(url, options);
          };

          try {
            const stale = J.loadSongData();
            await new Promise(resolve => setTimeout(resolve, 10));
            // 运行时被拆掉，随后的指令重建了它并拉到了新的曲库。
            J.prepareForUnload();
            await J.loadSongData();
            const afterFresh = J.State.songs.map(s => s.id);

            // 上一份响应现在才到货。
            releaseStale();
            await stale;
            return {
              afterFresh,
              afterStaleArrived: J.State.songs.map(s => s.id),
              revision: J.State.configRevision
            };
          } finally {
            window.fetch = originalFetch;
          }
        }
        """
    )

    assert result["afterFresh"] == ["fresh"]
    # 属于已拆除运行时的那份响应不许落盘。
    assert result["afterStaleArrived"] == ["fresh"], result
    assert result["revision"] == "rev-2"


@pytest.mark.frontend
def test_jukebox_panel_selection_beats_a_pending_remote_play(mock_page: Page):
    """Codex P2: the user has to win, and playRequestId alone could not deliver it.

    A control-side play awaiting the fuzzy search has not allocated its
    generation yet, so bumping playRequestId from the panel did not mark it
    stale -- it allocated a newer one when the search returned and replaced the
    song the user had just picked.  A play with no explicit requestId is by
    definition a panel action, and now advances the cancellation epoch too.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });

          // 远端的 play 卡在检索里。
          let releaseSearch;
          const searchGate = new Promise(resolve => { releaseSearch = resolve; });
          const originalFind = J.findSongForQuery;
          J.findSongForQuery = async function(query) {
            await searchGate;
            return originalFind.call(this, query);
          };

          const remote = J.executeControl({ action: 'play', query: 'Song 3', headless: true });
          await new Promise(resolve => setTimeout(resolve, 10));

          // 用户在面板上点了另一首。
          await J.playSong('song1');
          const userChoice = J.State.currentSong && J.State.currentSong.id;

          releaseSearch();
          const outcome = await remote;
          J.findSongForQuery = originalFind;
          await new Promise(resolve => setTimeout(resolve, 10));

          return {
            userChoice,
            remoteOk: outcome.ok,
            remoteMessage: outcome.message,
            current: J.State.currentSong && J.State.currentSong.id
          };
        }
        """
    )

    assert result["userChoice"] == "song1"
    # 迟到的远端指令不许把用户刚选的这首顶掉。
    assert result["remoteOk"] is False
    assert result["remoteMessage"] == "play_cancelled", result["remoteMessage"]
    assert result["current"] == "song1", result["current"]


@pytest.mark.frontend
def test_jukebox_targetless_previous_leaves_the_music_playing(mock_page: Page):
    """Codex P2: preempting relative navigation stopped audio it never replaced.

    At the head of the random history `previous` has no target, so the command
    is a no-op -- but the arrival-time cancellation had already paused the
    audio and seeked it to zero, and returning `no_previous_song` restores
    nothing.  Relative navigation now invalidates the in-flight command without
    silencing what is already playing; the replacement song, when there is one,
    stops it through playSong as usual.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.ensureRuntime({ headless: true });
          await J.executeControl({ action: 'set_mode', mode: 'random', headless: true });
          await J.executeControl({ action: 'play', query: 'Song 1', headless: true });
          const before = {
            playing: J.State.isPlaying,
            song: J.State.currentSong && J.State.currentSong.id
          };

          // 随机历史的头部：previous 没有目标。
          J.cancelActivePlayback({ silenceAudio: false });
          const outcome = await J.executeControl({ action: 'previous', headless: true });

          return {
            before,
            ok: outcome.ok,
            message: outcome.message,
            afterPlaying: J.State.isPlaying,
            afterSong: J.State.currentSong && J.State.currentSong.id,
            audioPaused: J.getPlayer().audio.paused
          };
        }
        """
    )

    assert result["before"] == {"playing": True, "song": "song1"}
    assert result["ok"] is False
    assert result["message"] == "no_previous_song"
    # 空操作的导航不该把音乐停掉。
    assert result["afterPlaying"] is True, result
    assert result["afterSong"] == "song1"
    assert result["audioPaused"] is False, result


@pytest.mark.frontend
def test_jukebox_close_tears_down_when_only_the_panel_was_used(mock_page: Page):
    """A hand-driven panel must tear down on close, not be kept alive.

    Only ensureRuntime() sets isRuntimeReady, and the panel path in open() never
    calls it -- so a session the user drove by hand reaches close() with the
    runtime not ready, and the preserve branch must not fire.  Preserving it
    would leave music playing with no UI and the parts never unloading.

    Deliberately does NOT fabricate isRuntimeReady: an earlier version of this
    test set it to true so that hasHeadlessRuntime()'s headlessRuntimeRequested
    check would be reached, and thereby claimed coverage of a state the app
    cannot produce -- every caller that sets isRuntimeReady also sets the flag.
    """
    setup_headless_jukebox_page(mock_page)

    result = mock_page.evaluate(
        """
        async () => {
          const J = window.Jukebox;
          await J.loadSongData();

          // 纯面板使用：建面板、建面板播放器、用户自己点了一首。
          const wrapper = document.createElement('div');
          wrapper.className = 'jukebox-wrapper';
          wrapper.innerHTML = '<div class="jukebox-container"></div>';
          document.body.appendChild(wrapper);
          const style = document.createElement('style');
          document.head.appendChild(style);
          J.State.container = wrapper;
          J.State.styleElement = style;
          J.State.isOpen = true;
          J.initPlayer({ headless: false });
          await J.playSong('song1');

          const before = {
            // 全程没有任何 headless 指令，也没人调过 ensureRuntime：这就是
            // 「用户自己开面板放歌」的真实状态。
            headlessRuntimeRequested: J.State.headlessRuntimeRequested,
            isRuntimeReady: J.State.isRuntimeReady,
            playing: J.State.isPlaying
          };

          let fullCloseEvents = 0;
          window.addEventListener('neko:jukebox-full-close', () => { fullCloseEvents += 1; });
          let stopped = 0;
          const originalStop = J.stopPlayback;
          J.stopPlayback = function(...args) { stopped += 1; return originalStop.apply(this, args); };
          J.close();
          J.stopPlayback = originalStop;

          return {
            before,
            stopped,
            fullCloseEvents,
            isPlaying: J.State.isPlaying,
            currentSong: J.State.currentSong && J.State.currentSong.id,
            songCount: J.State.songs.length,
            playerDestroyed: window.__lastAPlayer.destroyed === true
          };
        }
        """
    )

    assert result["before"] == {
        "headlessRuntimeRequested": False,
        "isRuntimeReady": False,
        "playing": True,
    }
    # 手动开的面板：关闭就该停播、彻底拆除、通知卸载分片。
    assert result["stopped"] == 1, result
    assert result["fullCloseEvents"] == 1, result
    assert result["isPlaying"] is False
    assert result["currentSong"] is None
    assert result["songCount"] == 0
    # 这条不能省：上面几项在「只 stopPlayback、不 prepareForUnload」的回归下全都
    # 照样成立，播放器却还活着。要证明走的是彻底拆除那条分支，只能看它死没死。
    assert result["playerDestroyed"] is True
