(function() {
  'use strict';

  var COMPRESSED_BUNDLED_VRM_IDLE_NAMES = new Set([
    'liked', 'wait01', 'wait02', 'wait03', 'wait04', 'wait05',
    '全身展示', '射击姿态', '屈伸运动', '旋转', '模特姿势', '比 V 手势', '致意问候'
  ]);

  function normalizeBundledVrmIdleUrl(url) {
    var value = String(url || '');
    var match = value.match(/^\/static\/vrm\/animation\/([^/?#]+)\.vrma(?:[?#]|$)/i);
    if (!match) return url;
    var assetName = match[1];
    try { assetName = decodeURIComponent(assetName); } catch (_) {}
    if (!COMPRESSED_BUNDLED_VRM_IDLE_NAMES.has(assetName)) return url;
    return value.replace(/\.vrma(?=[?#]|$)/i, '.vrma.gz');
  }

  function holdNekoMotionPlayback(token) {
    var runtime = window.NekoMotion;
    if (!runtime || typeof runtime.holdExternalPlayback !== 'function') return false;
    var holdRequest;
    try {
      holdRequest = runtime.holdExternalPlayback('jukebox', { token: token });
    } catch (error) {
      console.warn('[Jukebox] VRM 动作运行时占用失败，继续使用底层播放器:', error);
      return false;
    }
    return Promise.resolve(holdRequest).then(function(held) {
      return held === true;
    }).catch(function(error) {
      console.warn('[Jukebox] VRM 动作运行时占用失败，继续使用底层播放器:', error);
      return false;
    });
  }

  function releaseNekoMotionPlayback(options) {
    var runtime = window.NekoMotion;
    if (!runtime || typeof runtime.releaseExternalPlayback !== 'function') return false;
    var releaseRequest;
    try {
      releaseRequest = runtime.releaseExternalPlayback('jukebox', options || {});
    } catch (error) {
      console.warn('[Jukebox] VRM 动作运行时释放失败，回退直接恢复待机:', error);
      return false;
    }
    return Promise.resolve(releaseRequest).then(function(released) {
      return released === true;
    }).catch(function(error) {
      console.warn('[Jukebox] VRM 动作运行时释放失败，回退直接恢复待机:', error);
      return false;
    });
  }

  function releaseOwnedNekoMotionPlayback(state, options) {
    var releaseOptions = Object.assign({}, options || {});
    if (!Object.prototype.hasOwnProperty.call(releaseOptions, 'token') &&
        state.vrmMotionRuntimeToken !== null) {
      releaseOptions.token = state.vrmMotionRuntimeToken;
    }
    var releaseToken = Object.prototype.hasOwnProperty.call(releaseOptions, 'token')
      ? releaseOptions.token
      : null;
    var releaseResult = releaseNekoMotionPlayback(releaseOptions);
    if (releaseResult === false) return false;
    return Promise.resolve(releaseResult).then(function(released) {
      if (released === true && releaseToken !== null &&
          state.vrmMotionRuntimeToken === releaseToken) {
        state.vrmMotionRuntimeToken = null;
      }
      return released === true;
    });
  }

  function beginNativeAnimationPlayback(facade) {
    var state = facade.State;
    // 先终止上一段动画（也会作废它仍在途的加载），再给新请求取号；否则
    // stopVMD 推进世代时会把刚创建的这条请求一起误伤。
    facade.stopVMD(true);
    state.playRequestId += 1;
    state.pendingAnimationRequestId = state.playRequestId;
    return state.playRequestId;
  }

  function finishNativeAnimationPlayback(state, requestId) {
    if (state.pendingAnimationRequestId === requestId) {
      state.pendingAnimationRequestId = null;
    }
  }

  function ensureNativeJukeboxFacade() {
    if (window.Jukebox) return;

    var facade = {
      __nativeBridgeFacade: true,
      State: {
        currentSong: null,
        isPlaying: false,
        isVMDPlaying: false,
        isPaused: false,
        savedIdleAnimationUrl: null,
        playRequestId: 0,
        pendingAnimationRequestId: null,
        vrmMotionRuntimeToken: null
      },

      toggle: function() {
        if (typeof window.__nekoJukeboxToggle === 'function') {
          window.__nekoJukeboxToggle();
        }
      },

      init: function() {},

      getPlayer: function() {
        return null;
      },

      getModelType: function() {
        var config = window.lanlan_config || {};
        var modelType = config.model_type || 'live2d';
        if (modelType === 'live3d') {
          var subType = String(config.live3d_sub_type || '').toLowerCase();
          return subType === 'vrm' ? 'vrm' : 'mmd';
        }
        return modelType;
      },

      playVMD: async function(vmdPath) {
        if (!vmdPath) return;
        if (!window.mmdManager || !window.mmdManager.animationModule) {
          console.warn('[Jukebox]', translate('Jukebox.vmdNotInit', 'MMD Manager 未初始化，跳过动画'));
          return;
        }

        var state = facade.State;
        if (!state.savedIdleAnimationUrl && window.mmdManager.currentAnimationUrl) {
          state.savedIdleAnimationUrl = window.mmdManager.currentAnimationUrl;
        }
        var playRequestId = beginNativeAnimationPlayback(facade);

        try {
          if (state.vrmMotionRuntimeToken !== null) {
            var releaseResult = releaseOwnedNekoMotionPlayback(state, {
              resume: false,
              scheduleNext: false
            });
            if (releaseResult !== false) await releaseResult;
            if (playRequestId !== state.playRequestId) return;
          }
          if (typeof window.mmdManager.loadAnimation === 'function') {
            await window.mmdManager.loadAnimation(vmdPath);
          }
          if (playRequestId !== state.playRequestId) return;
          if (typeof window.mmdManager.playAnimation === 'function') {
            window.mmdManager.playAnimation('dance');
          } else if (window.mmdManager.animationModule && typeof window.mmdManager.animationModule.play === 'function') {
            window.mmdManager.animationModule.play();
          }
          state.isVMDPlaying = true;
          state.isPaused = false;
          state.isPlaying = true;
        } catch (error) {
          console.error('[Jukebox]', translate('Jukebox.vmdPlayFailed', 'VMD 播放失败'), error);
          if (playRequestId === state.playRequestId) {
            await facade.restoreIdleAnimation();
          }
        } finally {
          finishNativeAnimationPlayback(state, playRequestId);
        }
      },

      playVRMA: async function(vrmaPath) {
        if (!vrmaPath) return;
        if (!window.vrmManager || typeof window.vrmManager.playVRMAAnimation !== 'function') {
          console.warn('[Jukebox] VRM Manager 未初始化，跳过动画');
          return;
        }

        var state = facade.State;
        var playRequestId = beginNativeAnimationPlayback(facade);
        var motionRuntimeHeld = false;

        try {
          // 同一个 owner 的新 token 会在运行时内原子替换旧 token。先释放再占用会在
          // 冷启动时等待完整初始化，让接班舞蹈落后于已经开始的音频。
          var holdResult = holdNekoMotionPlayback(playRequestId);
          motionRuntimeHeld = holdResult === false ? false : await holdResult;
          if (playRequestId !== state.playRequestId) {
            if (motionRuntimeHeld) {
              // stop/new play 已经决定后续姿势；旧请求这里只解锁，不能再抢着恢复。
              await releaseNekoMotionPlayback({ token: playRequestId, resume: false });
            }
            return;
          }
          if (motionRuntimeHeld) state.vrmMotionRuntimeToken = playRequestId;
          var played = await window.vrmManager.playVRMAAnimation(vrmaPath, {
            loop: false,
            fadeInDuration: 0.5,
            fadeOutDuration: 0.5,
            shouldStart: function() {
              return playRequestId === state.playRequestId;
            }
          });
          if (played !== true || playRequestId !== state.playRequestId) {
            if (motionRuntimeHeld) {
              await releaseOwnedNekoMotionPlayback(state, { token: playRequestId, resume: true });
            } else if (playRequestId === state.playRequestId) {
              await facade.restoreIdleAnimation();
            }
            return;
          }
          state.isVMDPlaying = true;
          state.isPaused = false;
          state.isPlaying = true;
        } catch (error) {
          console.error('[Jukebox] VRMA 播放失败:', error);
          if (motionRuntimeHeld) {
            await releaseOwnedNekoMotionPlayback(state, { token: playRequestId, resume: true });
          } else if (playRequestId === state.playRequestId) {
            await facade.restoreIdleAnimation();
          }
        } finally {
          finishNativeAnimationPlayback(state, playRequestId);
        }
      },

      stopVMD: function(skipIdleRestore) {
        var state = facade.State;
        var pendingRequestId = state.pendingAnimationRequestId;
        var cancelledPendingRequest = false;
        if (pendingRequestId !== null) {
          // isVMDPlaying 只在真正起播后才会置位；等待 loadAnimation 或
          // holdExternalPlayback 时也必须能被停止。只有当前世代仍属于这条
          // 在途请求时才推进，避免旧 finally 误取消后来接手的新请求。
          if (pendingRequestId === state.playRequestId) {
            state.playRequestId += 1;
            cancelledPendingRequest = true;
          }
          state.pendingAnimationRequestId = null;
        }
        if (!state.isVMDPlaying) {
          if (cancelledPendingRequest && facade.getModelType() === 'vrm' &&
              window.vrmManager && typeof window.vrmManager.stopVRMAAnimation === 'function') {
            // 底层 stop 会推进 VRMA 自己的加载世代，和 shouldStart 组成双重取消闸门。
            window.vrmManager.stopVRMAAnimation();
          }
          if (cancelledPendingRequest && !skipIdleRestore) {
            facade.restoreIdleAnimation();
          }
          return;
        }

        var modelType = facade.getModelType();
        if (modelType === 'vrm') {
          if (window.vrmManager && typeof window.vrmManager.stopVRMAAnimation === 'function') {
            window.vrmManager.stopVRMAAnimation();
          }
        } else if (window.mmdManager && window.mmdManager.animationModule &&
            typeof window.mmdManager.animationModule.stop === 'function') {
          window.mmdManager.animationModule.stop();
        }

        state.isVMDPlaying = false;
        state.isPaused = false;
        state.isPlaying = false;

        if (!skipIdleRestore) {
          facade.restoreIdleAnimation();
        }
      },

      restoreIdleAnimation: async function() {
        var state = facade.State;
        state.playRequestId += 1;
        var restoreRequestId = state.playRequestId;
        var modelType = facade.getModelType();
        var canResumeVrm = modelType === 'vrm' && window.vrmManager &&
          typeof window.vrmManager.playVRMAAnimation === 'function';
        var heldRuntimeToken = state.vrmMotionRuntimeToken;
        var motionRuntimeRestored = false;

        if (heldRuntimeToken !== null) {
          var releaseResult = releaseOwnedNekoMotionPlayback(state, {
            token: heldRuntimeToken,
            resume: !!canResumeVrm,
            scheduleNext: !!canResumeVrm
          });
          motionRuntimeRestored = releaseResult === false ? false : await releaseResult;
          if (restoreRequestId !== state.playRequestId) return;
        }

        if (canResumeVrm) {
          try {
            if (motionRuntimeRestored) return;
            var vrmIdleList = window.lanlan_config && window.lanlan_config.vrmIdleAnimations;
            var vrmIdleUrl = Array.isArray(vrmIdleList) && vrmIdleList.length > 0 ? vrmIdleList[0] : null;
            if (!vrmIdleUrl) {
              vrmIdleUrl = window.lanlan_config && window.lanlan_config.vrmIdleAnimation;
            }
            vrmIdleUrl = normalizeBundledVrmIdleUrl(
              vrmIdleUrl || '/static/vrm/animation/wait03.vrma.gz'
            );
            await window.vrmManager.playVRMAAnimation(vrmIdleUrl, {
              loop: true,
              isIdle: true,
              shouldApply: function() {
                return restoreRequestId === state.playRequestId;
              }
            });
          } catch (error) {
            console.warn('[Jukebox] VRM 待机动画恢复失败:', error);
          }
          return;
        }

        if (modelType === 'vrm') return;

        if (!window.mmdManager) return;

        var idleUrl = state.savedIdleAnimationUrl;
        if (idleUrl && idleUrl.indexOf('/jukebox/song_') >= 0) {
          idleUrl = null;
        }
        if (!idleUrl) {
          facade._resetToNoneMode();
          return;
        }

        try {
          if (typeof window.mmdManager.loadAnimation === 'function') {
            await window.mmdManager.loadAnimation(idleUrl);
          }
          if (restoreRequestId !== state.playRequestId) return;
          if (typeof window.mmdManager.playAnimation === 'function') {
            window.mmdManager.playAnimation('idle');
          }
        } catch (error) {
          console.warn('[Jukebox]', translate('Jukebox.idleRestoreFailed', '恢复待机动画失败'), error);
          if (restoreRequestId !== state.playRequestId) return;
          facade._resetToNoneMode();
        }
      },

      _resetToNoneMode: function() {
        if (!window.mmdManager) return;
        var mesh = window.mmdManager.currentModel && window.mmdManager.currentModel.mesh;
        if (mesh && mesh.skeleton && typeof mesh.skeleton.pose === 'function') {
          mesh.skeleton.pose();
        }
        if (window.mmdManager.cursorFollow && typeof window.mmdManager.cursorFollow.setAnimationMode === 'function') {
          window.mmdManager.cursorFollow.setAnimationMode('none');
        }
      },

      togglePause: function() {
        var state = facade.State;
        if (!state.currentSong && !state.isVMDPlaying) return;

        var modelType = facade.getModelType();
        if (state.isPaused) {
          if (modelType === 'vrm') {
            var resumeVrmAnim = window.vrmManager &&
              (window.vrmManager.animationModule || window.vrmManager.animation);
            if (resumeVrmAnim && resumeVrmAnim.currentAction) {
              resumeVrmAnim.currentAction.paused = false;
            }
          } else if (window.mmdManager && window.mmdManager.animationModule) {
            if (typeof window.mmdManager.animationModule.play === 'function') {
              window.mmdManager.animationModule.play();
            }
            if (window.mmdManager.cursorFollow && typeof window.mmdManager.cursorFollow.setAnimationMode === 'function') {
              window.mmdManager.cursorFollow.setAnimationMode('dance');
            }
          }
          state.isPaused = false;
          state.isPlaying = true;
        } else if (state.isPlaying || state.isVMDPlaying) {
          if (modelType === 'vrm') {
            var pauseVrmAnim = window.vrmManager &&
              (window.vrmManager.animationModule || window.vrmManager.animation);
            if (pauseVrmAnim && pauseVrmAnim.currentAction) {
              pauseVrmAnim.currentAction.paused = true;
            }
          } else if (window.mmdManager && window.mmdManager.animationModule) {
            if (typeof window.mmdManager.animationModule.pause === 'function') {
              window.mmdManager.animationModule.pause();
            }
            if (window.mmdManager.cursorFollow && typeof window.mmdManager.cursorFollow.setAnimationMode === 'function') {
              window.mmdManager.cursorFollow.setAnimationMode('idle');
            }
          }
          state.isPaused = true;
          state.isPlaying = false;
        }
      }
    };

    window.Jukebox = facade;
    window.Jukebox_togglePause = facade.togglePause;
  }

  // 原生桥（Electron）下不再提前 return：AI 控制面需要惰性门面在任何形态下都存在，
  // 文件尾部只有在 __nekoJukeboxToggle 缺席时才会接管它。
  var SCRIPT_ID_PREFIX = 'neko-jukebox-part-';
  var SCRIPT_PATHS = [
    '/static/jukebox/jukebox/bootstrap.js',
    '/static/jukebox/jukebox/core.js',
    '/static/jukebox/jukebox/manager.js',
    '/static/jukebox/jukebox/shell.js',
    '/static/jukebox/jukebox/transport.js',
    '/static/jukebox/jukebox/wiring.js'
  ];
  var TOAST_ID = 'neko-jukebox-loader-toast';
  var STYLE_ID = 'neko-jukebox-loader-style';
  var REQUIRED_CONTROL_API_VERSION = 3;
  var currentScript = document.currentScript;
  var assetQuery = getAssetQuery(currentScript && currentScript.src);
  var loadPromise = null;
  var toggleInFlight = false;
  var toastTimer = null;
  var unloadTimer = null;
  var toastShownAt = 0;
  var MIN_INITIALIZING_TOAST_MS = 650;

  window.__NEKO_JUKEBOX_LAZY_LOADER__ = true;

  function hasRequiredControlApi(jukebox) {
    if (!jukebox || typeof jukebox.executeControl !== 'function') return false;
    var version = Number(jukebox.controlApiVersion || jukebox.__controlApiVersion || 0);
    if (version >= REQUIRED_CONTROL_API_VERSION) return true;
    return Array.isArray(jukebox.supportedControlActions)
      && jukebox.supportedControlActions.indexOf('adjust_volume') >= 0
      && jukebox.supportedControlActions.indexOf('set_mode') >= 0
      && jukebox.supportedControlActions.indexOf('previous') >= 0;
  }

  function isLoadedJukebox(jukebox) {
    return !!(
      jukebox
      && !jukebox.__nekoLazyFacade
      && !jukebox.__nativeBridgeFacade
      && hasRequiredControlApi(jukebox)
    );
  }

  function getAssetQuery(src) {
    if (!src) return '';
    try {
      var url = new URL(src, window.location.href);
      return url.search || '';
    } catch (_) {
      var queryIndex = src.indexOf('?');
      return queryIndex >= 0 ? src.slice(queryIndex) : '';
    }
  }

  function buildJukeboxPartSrc(scriptPath) {
    try {
      var url = new URL(scriptPath, window.location.href);
      if (assetQuery) {
        var inherited = new URLSearchParams(assetQuery.replace(/^\?/, ''));
        inherited.forEach(function(value, key) {
          url.searchParams.set(key, value);
        });
      }
      url.searchParams.set('jukebox_control_api', String(REQUIRED_CONTROL_API_VERSION));
      return url.pathname + url.search;
    } catch (_) {
      var query = assetQuery || '';
      var separator = query ? '&' : '?';
      return scriptPath + query + separator + 'jukebox_control_api=' + encodeURIComponent(String(REQUIRED_CONTROL_API_VERSION));
    }
  }

  function translate(key, fallback) {
    try {
      if (typeof window.t === 'function') {
        return window.t(key, fallback) || fallback;
      }
    } catch (_) {}
    return fallback;
  }

  function ensureToastStyle() {
    if (document.getElementById(STYLE_ID)) return;

    var style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = [
      '.neko-jukebox-loader-toast {',
      '  position: fixed;',
      '  right: 22px;',
      '  bottom: 92px;',
      '  z-index: 10050;',
      '  display: inline-flex;',
      '  align-items: center;',
      '  gap: 10px;',
      '  max-width: min(320px, calc(100vw - 32px));',
      '  padding: 10px 14px;',
      '  border-radius: 8px;',
      '  color: rgba(28, 48, 68, 0.94);',
      '  background: rgba(255, 255, 255, 0.94);',
      '  border: 1px solid rgba(116, 190, 224, 0.28);',
      '  box-shadow: 0 12px 34px rgba(78, 153, 190, 0.24);',
      '  font-size: 14px;',
      '  line-height: 1.35;',
      '  opacity: 0;',
      '  transform: translateY(8px);',
      '  pointer-events: none;',
      '  transition: opacity 160ms ease, transform 160ms ease;',
      '}',
      '.neko-jukebox-loader-toast.visible {',
      '  opacity: 1;',
      '  transform: translateY(0);',
      '}',
      '.neko-jukebox-loader-toast.error {',
      '  color: #8f2230;',
      '  border-color: rgba(217, 75, 97, 0.32);',
      '  box-shadow: 0 12px 34px rgba(217, 75, 97, 0.18);',
      '}',
      '.neko-jukebox-loader-spinner {',
      '  width: 14px;',
      '  height: 14px;',
      '  flex: 0 0 auto;',
      '  border-radius: 50%;',
      '  border: 2px solid rgba(53, 169, 201, 0.24);',
      '  border-top-color: #35a9c9;',
      '  animation: neko-jukebox-loader-spin 800ms linear infinite;',
      '}',
      '.neko-jukebox-loader-toast.error .neko-jukebox-loader-spinner {',
      '  display: none;',
      '}',
      '@keyframes neko-jukebox-loader-spin {',
      '  to { transform: rotate(360deg); }',
      '}',
      'html[data-theme="dark"] .neko-jukebox-loader-toast {',
      '  color: rgba(230, 237, 243, 0.94);',
      '  background: rgba(15, 23, 42, 0.94);',
      '  border-color: rgba(124, 218, 244, 0.2);',
      '  box-shadow: 0 12px 34px rgba(2, 8, 23, 0.42);',
      '}',
      '@media (max-width: 640px) {',
      '  .neko-jukebox-loader-toast {',
      '    right: 16px;',
      '    bottom: 74px;',
      '  }',
      '}'
    ].join('\n');
    document.head.appendChild(style);
  }

  function getToast() {
    ensureToastStyle();
    var toast = document.getElementById(TOAST_ID);
    if (toast) return toast;

    toast = document.createElement('div');
    toast.id = TOAST_ID;
    toast.className = 'neko-jukebox-loader-toast';
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');
    toast.innerHTML = '<span class="neko-jukebox-loader-spinner" aria-hidden="true"></span><span class="neko-jukebox-loader-message"></span>';
    document.body.appendChild(toast);
    return toast;
  }

  function showToast(message, isError) {
    if (toastTimer) {
      clearTimeout(toastTimer);
      toastTimer = null;
    }
    toastShownAt = Date.now();

    var toast = getToast();
    var messageEl = toast.querySelector('.neko-jukebox-loader-message');
    if (messageEl) messageEl.textContent = message;
    toast.classList.toggle('error', !!isError);
    requestAnimationFrame(function() {
      toast.classList.add('visible');
    });
  }

  function hideToast(delay) {
    var toast = document.getElementById(TOAST_ID);
    if (!toast) return;

    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function() {
      toast.classList.remove('visible');
      toastTimer = setTimeout(function() {
        if (toast.parentNode && !toast.classList.contains('visible')) {
          toast.remove();
        }
        toastTimer = null;
      }, 180);
    }, delay || 0);
  }

  function getInitializingToastDelay(fallbackDelay) {
    var elapsed = Date.now() - toastShownAt;
    var remaining = MIN_INITIALIZING_TOAST_MS - elapsed;
    return Math.max(fallbackDelay || 0, remaining > 0 ? remaining : 0);
  }

  function showInitializing() {
    showToast(translate('Jukebox.initializing', '正在初始化点歌台...'), false);
  }

  function showInitializeFailed(error) {
    console.error('[JukeboxLoader] 初始化失败:', error);
    showToast(translate('Jukebox.initializeFailed', '点歌台初始化失败'), true);
    hideToast(2800);
  }

  // ===== 跨窗口控制归属（角色窗口一侧）=====
  // 独立点唱机窗口（templates/jukebox.html）持有用户看得见的播放器，但它既不加载
  // 本文件也不加载 app-websocket.js。它开着的时候，控制指令必须转发过去，而不是
  // 在角色窗口里另起一个隐藏运行时 —— 那会让 stop/next/音量/模式对可见播放器全部
  // 失效，play 还会同时响两条音轨。
  var CONTROL_OWNER_CHANNEL = 'neko-jukebox-control';
  var CONTROL_OWNER_TTL_MS = 6000;      // 拥有者心跳 2s 一次，容三拍
  var CONTROL_FORWARD_TIMEOUT_MS = 5000;
  var controlChannel = null;
  var controlOwnerExpiresAt = 0;
  var pendingForwards = new Map();
  var forwardSeq = 0;

  function ensureControlChannel() {
    if (controlChannel) return controlChannel;
    if (typeof BroadcastChannel === 'undefined') return null;
    try {
      controlChannel = new BroadcastChannel(CONTROL_OWNER_CHANNEL);
    } catch (_) {
      return null;
    }
    controlChannel.onmessage = function(event) {
      var data = event && event.data;
      if (!data || typeof data !== 'object') return;
      if (data.type === 'jukebox_owner_alive') {
        controlOwnerExpiresAt = Date.now() + CONTROL_OWNER_TTL_MS;
        return;
      }
      if (data.type === 'jukebox_owner_gone') {
        controlOwnerExpiresAt = 0;
        // 已经交出去的那些也要立刻了结：它们等的是拥有者的回执，而拥有者刚说自己
        // 没了。干等 TTL 的话，本地队列里排在后面的指令一并卡住。逐条结掉之后
        // 迟到的 jukebox_control_result 会因为已从表里删掉而成为空操作。
        var abandoned = Array.from(pendingForwards.values());
        pendingForwards.clear();
        abandoned.forEach(function(settle) {
          try {
            settle({ ok: false, action: '', message: 'jukebox_owner_gone' });
          } catch (_) {}
        });
        return;
      }
      if (data.type === 'jukebox_control_result') {
        var settle = pendingForwards.get(data.requestId);
        if (settle) {
          pendingForwards.delete(data.requestId);
          settle(data.result);
        }
      }
    };
    try {
      // 刚起来时主动问一声，不用干等第一个心跳。
      controlChannel.postMessage({ type: 'jukebox_owner_query' });
    } catch (_) {}
    return controlChannel;
  }

  function hasControlOwner() {
    ensureControlChannel();
    return Date.now() < controlOwnerExpiresAt;
  }

  function cancelControlOnOwner(action) {
    var channel = ensureControlChannel();
    if (!channel) return false;
    try {
      // 只是一个信号，不等回执：它要在拥有者那条在途指令还没结束时就生效。
      // 带上发起动作：拥有者据此决定要不要顶替它排队中的播放指令 —— 判据必须
      // 跟发件侧一致，否则 next / previous 在本地不顶替、转发出去却顶替。
      channel.postMessage({
        type: 'jukebox_cancel_request',
        action: String(action || '').trim().toLowerCase()
      });
      return true;
    } catch (_) {
      return false;
    }
  }

  function forwardControlToOwner(command) {
    var channel = ensureControlChannel();
    if (!channel) {
      return Promise.resolve({ ok: false, action: (command && command.action) || '', message: 'jukebox_owner_unreachable' });
    }
    forwardSeq += 1;
    var requestId = 'ctl-' + forwardSeq + '-' + Date.now();
    return new Promise(function(resolve) {
      var settled = false;
      var finish = function(result) {
        if (settled) return;
        settled = true;
        pendingForwards.delete(requestId);
        resolve(result);
      };
      pendingForwards.set(requestId, finish);
      setTimeout(function() {
        // 超时不回落本地执行：那样会在隐藏窗口里再起一条音轨，比失败更糟。
        finish({ ok: false, action: (command && command.action) || '', message: 'jukebox_owner_timeout' });
      }, CONTROL_FORWARD_TIMEOUT_MS);
      try {
        // 把超时预算随请求带过去：拥有者据此丢弃「调用方已经不等了」的陈旧请求。
        // 常量只在这里定义一份，不在两个文件各存一份。
        channel.postMessage({
          type: 'jukebox_control_request',
          requestId: requestId,
          command: command || {},
          ttlMs: CONTROL_FORWARD_TIMEOUT_MS
        });
      } catch (_) {
        finish({ ok: false, action: (command && command.action) || '', message: 'jukebox_owner_unreachable' });
      }
    });
  }

  function clearPendingUnload() {
    if (unloadTimer) {
      clearTimeout(unloadTimer);
      unloadTimer = null;
    }
  }

  function ensureLazyJukeboxFacade() {
    if (!window.Jukebox && typeof window.__nekoJukeboxToggle === 'function') {
      ensureNativeJukeboxFacade();
    }
    if (isLoadedJukebox(window.Jukebox)) return window.Jukebox;
    if (window.Jukebox && window.Jukebox.__nekoLazyFacade) return window.Jukebox;

    var facade = window.Jukebox || {
      State: {
        isOpen: false,
        isHidden: false,
        currentSong: null,
        isPlaying: false,
        isPaused: false,
        isVMDPlaying: false
      },
      toggle: toggleJukebox
    };
    if (!facade.__nativeBridgeFacade) {
      facade.__nekoLazyFacade = true;
    }
    if (typeof facade.toggle !== 'function') {
      facade.toggle = toggleJukebox;
    }
    if (typeof facade.init !== 'function') {
      facade.init = function() {};
    }
    facade.ensureRuntime = async function(options) {
      // unloadJukebox() 排的是 3 秒定时器；控制指令落在这个窗口里而不撤销它的话，
      // parts 刚加载完就会被 finalizeUnload 把 window.Jukebox 删掉。
      clearPendingUnload();
      var jukebox = await loadJukeboxScript();
      initJukebox(jukebox);
      if (!jukebox || typeof jukebox.ensureRuntime !== 'function') {
        throw new Error('Jukebox runtime API unavailable');
      }
      return jukebox.ensureRuntime(options || {});
    };
    facade.executeControl = async function(command) {
      clearPendingUnload();
      var jukebox = await loadJukeboxScript();
      initJukebox(jukebox);
      if (!jukebox || typeof jukebox.executeControl !== 'function') {
        throw new Error('Jukebox control API unavailable');
      }
      return jukebox.executeControl(command || {});
    };
    window.Jukebox = facade;
    return window.Jukebox;
  }

  function getJukeboxPartScriptId(scriptPath) {
    var fileName = scriptPath.split('/').pop() || 'part';
    return SCRIPT_ID_PREFIX + fileName.replace(/\.js$/, '');
  }

  function removeJukeboxScripts() {
    document.querySelectorAll('script[data-neko-jukebox-part="true"]').forEach(function(script) {
      script.remove();
    });
  }

  function loadJukeboxPart(scriptPath) {
    return new Promise(function(resolve, reject) {
      var script = document.createElement('script');
      script.id = getJukeboxPartScriptId(scriptPath);
      script.src = buildJukeboxPartSrc(scriptPath);
      script.async = false;
      script.dataset.nekoJukeboxPart = 'true';
      script.dataset.nekoJukeboxLazy = 'true';
      script.onload = function() {
        resolve();
      };
      script.onerror = function() {
        script.remove();
        reject(new Error('Failed to load Jukebox part: ' + scriptPath));
      };
      document.body.appendChild(script);
    });
  }

  function loadJukeboxScript() {
    if (isLoadedJukebox(window.Jukebox)) return Promise.resolve(window.Jukebox);
    if (loadPromise) return loadPromise;

    removeJukeboxScripts();
    console.log('[JukeboxLoader] 按需加载点歌台资源');
    loadPromise = SCRIPT_PATHS.reduce(function(sequence, scriptPath) {
      return sequence.then(function() {
        return loadJukeboxPart(scriptPath);
      });
    }, Promise.resolve()).then(function() {
      if (!isLoadedJukebox(window.Jukebox)) {
        throw new Error('Jukebox global missing after parts loaded');
      }
      console.log('[JukeboxLoader] 点歌台资源已加载');
      return window.Jukebox;
    }).catch(function(error) {
      removeJukeboxScripts();
      try {
        delete window.Jukebox;
      } catch (_) {
        window.Jukebox = undefined;
      }
      loadPromise = null;
      ensureLazyJukeboxFacade();
      throw error;
    });

    return loadPromise;
  }

  function initJukebox(jukebox) {
    if (!jukebox || jukebox.__nekoLazyLoaderInitialized) return;
    if (typeof jukebox.init === 'function') {
      jukebox.init();
    }
    if (typeof jukebox.executeControl === 'function' && !jukebox.__nekoLazyLoaderExecuteWrapped) {
      var originalExecuteControl = jukebox.executeControl;
      jukebox.executeControl = function(command) {
        clearPendingUnload();
        return originalExecuteControl.call(jukebox, command);
      };
      jukebox.__nekoLazyLoaderExecuteWrapped = true;
    }
    if (typeof jukebox.ensureRuntime === 'function' && !jukebox.__nekoLazyLoaderRuntimeWrapped) {
      var originalEnsureRuntime = jukebox.ensureRuntime;
      jukebox.ensureRuntime = function(options) {
        clearPendingUnload();
        return originalEnsureRuntime.call(jukebox, options);
      };
      jukebox.__nekoLazyLoaderRuntimeWrapped = true;
    }
    jukebox.__nekoLazyLoaderInitialized = true;
  }

  function showOrOpenJukebox(jukebox) {
    if (!jukebox || !jukebox.State) return;

    if (jukebox.State.isHidden && typeof jukebox.show === 'function') {
      jukebox.show();
    } else if (jukebox.State.isOpen && typeof jukebox.hide === 'function') {
      jukebox.hide();
    } else if (typeof jukebox.open === 'function') {
      jukebox.open();
    }
  }

  async function toggleJukebox() {
    clearPendingUnload();

    if (toggleInFlight) {
      showInitializing();
      return;
    }

    if (window.Jukebox && window.Jukebox.State && !window.Jukebox.__nekoLazyFacade) {
      showOrOpenJukebox(window.Jukebox);
      return;
    }

    toggleInFlight = true;
    showInitializing();

    try {
      var jukebox = await loadJukeboxScript();
      initJukebox(jukebox);
      showOrOpenJukebox(jukebox);
      hideToast(getInitializingToastDelay(180));
    } catch (error) {
      showInitializeFailed(error);
    } finally {
      toggleInFlight = false;
    }
  }

  function finalizeUnload() {
    unloadTimer = null;
    var jukebox = window.Jukebox;
    if (jukebox && typeof jukebox.cleanupCloseListener === 'function') {
      try {
        jukebox.cleanupCloseListener();
      } catch (_) {}
    }
    if (window.__JukeboxLocaleChangeHandler) {
      try {
        window.removeEventListener('localechange', window.__JukeboxLocaleChangeHandler);
      } catch (_) {}
      window.__JukeboxLocaleChangeHandler = null;
    }
    [
      'Jukebox',
      'Jukebox_playSong',
      'Jukebox_close',
      'Jukebox_hide',
      'Jukebox_updateVolume',
      'Jukebox_logVolumeChange',
      'Jukebox_togglePause',
      '__JukeboxLocaleChangeHandler'
    ].forEach(function(name) {
      try {
        delete window[name];
      } catch (_) {
        window[name] = undefined;
      }
    });
    ensureLazyJukeboxFacade();
    console.log('[JukeboxLoader] 点歌台资源已卸载');
  }

  function unloadJukebox() {
    loadPromise = null;
    toggleInFlight = false;
    console.log('[JukeboxLoader] 点歌台完全关闭，准备卸载资源');

    removeJukeboxScripts();

    if (unloadTimer) clearTimeout(unloadTimer);
    unloadTimer = setTimeout(finalizeUnload, 3000);
  }

  function getState() {
    var jukebox = window.Jukebox;
    return {
      hasJukeboxGlobal: !!jukebox,
      hasScriptTag: !!document.querySelector('script[data-neko-jukebox-part="true"]'),
      hasUi: !!document.querySelector('.jukebox-wrapper'),
      isOpen: !!(jukebox && jukebox.State && jukebox.State.isOpen),
      isHidden: !!(jukebox && jukebox.State && jukebox.State.isHidden),
      pendingUnload: !!unloadTimer
    };
  }

  window.addEventListener('neko:jukebox-full-close', unloadJukebox);

  if (typeof window.__nekoJukeboxToggle === 'function') {
    ensureNativeJukeboxFacade();
  }
  ensureLazyJukeboxFacade();

  if (typeof window.__nekoJukeboxToggle !== 'function') {
    window.__nekoJukeboxToggle = toggleJukebox;
    window.__nekoJukeboxToggle.__nekoJukeboxWebLoader = true;
  }
  ensureControlChannel();
  window.__nekoJukeboxLoader = {
    load: loadJukeboxScript,
    toggle: toggleJukebox,
    unload: unloadJukebox,
    getState: getState,
    hasControlOwner: hasControlOwner,
    forwardControl: forwardControlToOwner,
    cancelOnOwner: cancelControlOnOwner
  };
})();
