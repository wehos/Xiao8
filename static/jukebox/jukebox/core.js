Object.assign(window.Jukebox, {

  controlApiVersion: 3,
  supportedControlActions: ['play', 'next', 'previous', 'stop', 'set_volume', 'adjust_volume', 'set_mode'],



  Config: {
    width: '500px',
    container: {
      background: 'linear-gradient(160deg, rgba(255,255,255,.92), rgba(232,247,255,.86))', // 容器背景色
      boxShadow: '0 18px 48px rgba(78, 153, 190, 0.28), 0 4px 18px rgba(255, 159, 189, 0.16)', // 容器阴影
      color: 'rgba(28, 48, 68, 0.94)' // 容器文字颜色
    },
    header: {
      borderBottom: '1px solid rgba(116, 190, 224, 0.28)', // 标题栏下边框
      btnHoverBg: 'rgba(99, 199, 232, 0.16)' // 标题栏按钮悬停背景
    },
    notice: {
      background: 'rgba(255, 255, 255, 0.62)', // 提示区域背景
      border: '1px solid rgba(120, 203, 232, 0.24)' // 提示区域边框
    },
    table: {
      headerBg: 'rgba(255, 255, 255, 0.58)', // 表头背景
      headerColor: 'rgba(45, 78, 104, 0.86)', // 表头文字颜色
      bodyBg: 'rgba(255, 255, 255, 0.52)', // 表格背景
      rowHoverBg: 'rgba(255, 255, 255, 0.86)', // 表格行悬停背景
      rowBorder: '1px solid rgba(116, 190, 224, 0.16)', // 表格行边框
      loadingColor: 'rgba(38, 118, 148, 0.82)' // 加载中文字颜色
    },
    button: {
      playBg: '#35a9c9', // 播放按钮背景
      playHoverBg: '#63c7e8', // 播放按钮悬停背景
      playingBg: '#d94b61', // 播放中按钮背景
      playingHoverBg: '#ec6a7c', // 播放中按钮悬停背景
      pauseBg: '#ff9fbd', // 暂停按钮背景
      pauseHoverBg: '#ffb3ca', // 暂停按钮悬停背景
      resumeBg: '#63c7e8', // 恢复按钮背景
      resumeHoverBg: '#8dd9ef', // 恢复按钮悬停背景
      color: 'white' // 按钮文字颜色
    },
    progress: {
      containerBg: 'rgba(255, 255, 255, 0.58)', // 进度条容器背景
      trackBg: 'rgba(99, 199, 232, 0.22)', // 进度条轨道背景
      sliderBg: 'rgba(99, 199, 232, 0.7)', // 进度条滑块背景
      sliderSeekableBg: '#35a9c9', // 进度条滑块可拖动时背景
      textColor: 'rgba(45, 78, 104, 0.8)' // 进度条文字颜色
    },
    volume: {
      iconColor: 'rgba(45, 78, 104, 0.86)', // 喇叭图标颜色
      iconHoverColor: 'rgba(28, 48, 68, 0.94)', // 喇叭悬停颜色
      iconHoverBg: 'rgba(99, 199, 232, 0.14)', // 喇叭悬停背景
      popupBg: 'rgba(255, 255, 255, 0.95)', // 弹出窗口背景
      popupShadow: '0 4px 12px rgba(0, 0, 0, 0.15)', // 弹出窗口阴影
      trackColor: 'rgba(0, 100, 150, 0.3)', // 音量轨道颜色
      textColor: 'rgba(0, 60, 100, 0.85)', // 文字颜色
      textHoverBg: 'rgba(0, 100, 150, 0.15)', // 文字悬停背景
      inputBg: 'rgba(0, 100, 150, 0.1)', // 输入框背景
      inputBorder: 'rgba(0, 100, 150, 0.3)', // 输入框边框
      inputFocusBorder: '#35a9c9', // 输入框聚焦边框
      inputFocusBg: 'rgba(0, 100, 150, 0.15)', // 输入框聚焦背景
      sliderColor: '#35a9c9', // 滑块颜色
      sliderHoverColor: '#63c7e8' // 滑块悬停颜色
    },
    buttonActive: {
      background: 'rgba(30, 60, 114, 0.3)' // 点歌台按钮激活状态背景
    },
    // 校准面板颜色
    calibration: {
      toggleBg: 'linear-gradient(135deg, #6695ea 0%, #6695ea 100%)', // 校准按钮背景
      toggleShadow: '0 4px 12px rgba(102, 126, 234, 0.4)', // 校准按钮悬停阴影
      panelBg: 'rgba(0, 0, 0, 0.2)', // 校准面板背景
      titleColor: 'rgba(255, 255, 255, 0.9)', // 标题颜色
      fpsColor: 'rgba(255, 255, 255, 0.6)', // FPS显示颜色
      closeBg: 'rgba(255, 255, 255, 0.1)', // 关闭按钮背景
      closeHoverBg: 'rgba(255, 255, 255, 0.2)', // 关闭按钮悬停背景
      closeColor: 'rgba(255, 255, 255, 0.8)', // 关闭按钮颜色
      btnBg: 'rgba(255, 255, 255, 0.1)', // 校准按钮背景
      btnBorder: 'rgba(255, 255, 255, 0.2)', // 校准按钮边框
      btnHoverBg: 'rgba(255, 255, 255, 0.2)', // 校准按钮悬停背景
      btnHoverBorder: 'rgba(255, 255, 255, 0.4)', // 校准按钮悬停边框
      valueColor: '#ffffffff', // 数值颜色
      resetBg: 'rgba(244, 67, 54, 0.2)', // 重置按钮背景
      resetBorder: 'rgba(244, 67, 54, 0.4)', // 重置按钮边框
      resetColor: '#f44336', // 重置按钮颜色
      resetHoverBg: 'rgba(244, 67, 54, 0.3)', // 重置按钮悬停背景
      resetHoverBorder: 'rgba(244, 67, 54, 0.6)' // 重置按钮悬停边框
    },
    // 状态文字
    status: {
      color: 'rgba(38, 118, 148, 0.88)', // 状态文字颜色
      bg: 'rgba(232,247,255,0.72)' // 状态文字背景
    }
  },

  State: {
    songs: [],
    currentSong: null,
    isPlaying: false,
    isVMDPlaying: false,
    player: null,
    audioElement: null,
    mp3EndedListenerAdded: false,
    boundPlayer: null,
    playRequestId: 0,
    isPaused: false,
    savedIdleAnimationUrl: null,
    savedVolume: 1,
    // 面板建好、播放器还没建出来的窗口里用户拖过的音量。只有它非空时才在建
    // 播放器后强行覆盖 —— savedVolume 的默认值是 1，拿它当「用户设过」会在冷
    // 启动时把 APlayer 存在 localStorage 里的音量抹成 100%。
    pendingVolume: null,
    isMuted: false,
    progressTimer: null,
    isSeeking: false,
    playbackMode: 'sequence',
    randomQueue: [],
    randomQueueIndex: -1,
    randomQueueExitSongId: null,
    songSortUnlocked: false,
    draggedSongId: null,
    configRevision: null,
    configPollTimer: null,
    configPollInFlight: false,
    runtimeHost: null,
    runtimeInitPromise: null,
    isRuntimeReady: false,
    headlessRuntimeRequested: false,
    teardownEpoch: 0,
    // 取消专用世代。不能复用 playRequestId：一条还没轮到分配世代的 play
    // （正卡在 ensureRuntime / 曲目检索的 await 里）会在恢复后自己 ++，
    // 把取消推进的那一格盖过去，等值检查照样通过。
    playCancelEpoch: 0,
    // 当前这份音频是哪条播放请求起的。收尾清理只认它 —— currentSong 在接班者
    // 起播期间会被临时清空，拿它当判据会把接班者的声音误停。
    audioOwnerRequestId: null,
    // 「欠着一次待机恢复」：stopVMD(true) 把舞蹈停掉却跳过恢复时置真，
    // 由真正接上动画或真正回到静止的那一方清账。见 transport.js 的 settleIdleRestore。
    idleRestorePending: false,
    // 记录实际取得的 NekoMotion owner token；模型热切换不会刷新页面，释放时不能
    // 依赖当前 modelType 判断这段占用最初属于谁。
    vrmMotionRuntimeToken: null,
    controlOwnerChannel: null,
    controlOwnerHeartbeatTimer: null,
    // 拥有者是否已经能真的执行指令（曲库拉完、播放器建好）。宣告归属要尽早，
    // 执行却要等就绪——中间到达的指令先攒在 controlOwnerPending 里。
    controlOwnerReady: false,
    controlOwnerPending: [],
    // 拥有者侧的顶替世代。就绪之后请求直接挂上 serveChain，就再也拿不下来了，
    // 只能让它自己在开跑前发现「我已经过期了」。
    controlOwnerSupersedeGeneration: 0,
    fuzzySearchWorker: null,
    fuzzySearchWorkerUrl: null,
    fuzzySearchToken: 0,
    fuzzySearchSettle: null,
    lastPlaybackReport: null,
    isOpen: false,
    isHidden: false,
    container: null,
    styleElement: null,
    observer: null,
    closeListenerButton: null,
    closeListenerHandler: null,
    songElements: {},
    tooltipElement: null,
    tooltipTimeout: null,
    tooltipTarget: null,
    tooltipTextProvider: null,
    marqueeItems: new Map(),
    marqueeRaf: null,
    hasCustomWindowSize: false,
    // 窗口拖拽状态
    isDragging: false,
    dragOffset: { x: 0, y: 0 }
  },

  positionTooltip: function(target, tooltip) {
    if (!target || !tooltip) return;

    const rect = target.getBoundingClientRect();
    const viewportWidth = document.documentElement.clientWidth || window.innerWidth || 0;
    const viewportHeight = document.documentElement.clientHeight || window.innerHeight || 0;
    const edgePadding = 8;
    const gap = 6;
    const tooltipWidth = tooltip.offsetWidth;
    const tooltipHeight = tooltip.offsetHeight;
    let left = rect.left + rect.width / 2 - tooltipWidth / 2;
    let top = rect.bottom + gap;

    if (top + tooltipHeight + edgePadding > viewportHeight) {
      top = rect.top - tooltipHeight - gap;
    }

    const maxLeft = Math.max(edgePadding, viewportWidth - tooltipWidth - edgePadding);
    const maxTop = Math.max(edgePadding, viewportHeight - tooltipHeight - edgePadding);
    left = Math.min(Math.max(edgePadding, left), maxLeft);
    top = Math.min(Math.max(edgePadding, top), maxTop);

    tooltip.style.left = Math.round(left) + 'px';
    tooltip.style.top = Math.round(top) + 'px';
  },

  showTooltip: function(element, text) {
    Jukebox.hideTooltip();
    Jukebox.State.tooltipTarget = element;
    Jukebox.State.tooltipTextProvider = text;

    Jukebox.State.tooltipTimeout = setTimeout(() => {
      const tooltipText = typeof text === 'function' ? text() : text;
      if (!Jukebox.State.tooltipElement) {
        const tooltip = document.createElement('div');
        tooltip.className = 'jukebox-tooltip';
        tooltip.textContent = tooltipText;
        document.body.appendChild(tooltip);
        Jukebox.State.tooltipElement = tooltip;
      }

      const tooltip = Jukebox.State.tooltipElement;
      tooltip.textContent = tooltipText;

      Jukebox.positionTooltip(element, tooltip);

      requestAnimationFrame(() => {
        tooltip.classList.add('visible');
      });
    }, 400);
  },

  hideTooltip: function() {
    if (Jukebox.State.tooltipTimeout) {
      clearTimeout(Jukebox.State.tooltipTimeout);
      Jukebox.State.tooltipTimeout = null;
    }

    if (Jukebox.State.tooltipElement) {
      Jukebox.State.tooltipElement.remove();
      Jukebox.State.tooltipElement = null;
    }
    Jukebox.State.tooltipTarget = null;
    Jukebox.State.tooltipTextProvider = null;
  },

  setupTooltip: function(element, text) {
    element.addEventListener('mouseenter', () => Jukebox.showTooltip(element, text));
    element.addEventListener('mouseleave', () => Jukebox.hideTooltip());
  },

  setupTooltipOnce: function(element, text) {
    if (!element) return;
    element.removeAttribute('title');
    if (element.dataset.nekoTooltipBound !== 'true') {
      Jukebox.setupTooltip(element, text);
      element.dataset.nekoTooltipBound = 'true';
    }
  },

  bindTextTooltips: function(root) {
    const scope = root || document;
    scope.querySelectorAll('[data-neko-marquee], [data-tooltip]').forEach((element) => {
      const titleText = element.getAttribute('title');
      if (titleText && !element.dataset.tooltip) {
        element.dataset.tooltip = titleText;
      }
      Jukebox.setupTooltipOnce(element, () => element.dataset.tooltip || element.textContent.trim());
    });
  },

  refreshTooltip: function(element) {
    if (!Jukebox.State.tooltipElement) return;
    if (element && Jukebox.State.tooltipTarget !== element) return;
    const target = Jukebox.State.tooltipTarget;
    if (!target) return;
    const provider = Jukebox.State.tooltipTextProvider;
    const tooltipText = typeof provider === 'function' ? provider() : provider;
    Jukebox.State.tooltipElement.textContent = tooltipText || '';
    Jukebox.positionTooltip(target, Jukebox.State.tooltipElement);
  },

  getStorageKey: function(name) {
    return 'neko.jukebox.' + name;
  },

  getStoredJson: function(name, fallback) {
    try {
      const raw = window.localStorage ? window.localStorage.getItem(Jukebox.getStorageKey(name)) : null;
      if (!raw) return fallback;
      return JSON.parse(raw);
    } catch (error) {
      console.warn('[Jukebox] 读取本地偏好失败:', name, error);
      return fallback;
    }
  },

  setStoredJson: function(name, value) {
    try {
      if (window.localStorage) {
        window.localStorage.setItem(Jukebox.getStorageKey(name), JSON.stringify(value));
      }
    } catch (error) {
      console.warn('[Jukebox] 保存本地偏好失败:', name, error);
    }
  },

  applyStoredWindowSize: function(container) {
    if (window.__NEKO_JUKEBOX_STANDALONE__ || !container) return;

    const saved = Jukebox.getStoredJson('windowSize', null);
    if (!saved || typeof saved !== 'object') return;

    const minWidth = 360;
    const minHeight = 300;
    const maxWidth = Math.max(minWidth, (window.innerWidth || minWidth) - 16);
    const maxHeight = Math.max(minHeight, (window.innerHeight || minHeight) - 16);
    const width = Number(saved.width);
    const height = Number(saved.height);
    let applied = false;

    if (Number.isFinite(width) && width >= minWidth) {
      container.style.width = Math.min(Math.max(width, minWidth), maxWidth) + 'px';
      applied = true;
    }
    if (Number.isFinite(height) && height >= minHeight) {
      container.style.height = Math.min(Math.max(height, minHeight), maxHeight) + 'px';
      applied = true;
    }

    if (applied) {
      Jukebox.State.hasCustomWindowSize = true;
    }
  },

  saveWindowSize: function(container) {
    if (window.__NEKO_JUKEBOX_STANDALONE__ || !container) return;

    const rect = container.getBoundingClientRect();
    if (!rect || rect.width <= 0 || rect.height <= 0) return;
    const computedStyle = window.getComputedStyle(container);
    const contentWidth = parseFloat(computedStyle.width);
    const contentHeight = parseFloat(computedStyle.height);
    const width = Number.isFinite(contentWidth) && contentWidth > 0 ? contentWidth : rect.width;
    const height = Number.isFinite(contentHeight) && contentHeight > 0 ? contentHeight : rect.height;

    Jukebox.setStoredJson('windowSize', {
      width: Math.round(width),
      height: Math.round(height)
    });
  },

  loadPlaybackPreferences: function() {
    const savedMode = Jukebox.getStoredJson('playbackMode', 'sequence');
    Jukebox.State.playbackMode = Jukebox.getPlaybackModeOrder().includes(savedMode) ? savedMode : 'sequence';
  },

  getPlaybackModeOrder: function() {
    return ['none', 'sequence', 'single', 'random'];
  },

  setPlaybackMode: function(mode) {
    const nextMode = Jukebox.getPlaybackModeOrder().includes(mode) ? mode : 'sequence';
    const previousMode = Jukebox.State.playbackMode;
    Jukebox.State.playbackMode = nextMode;
    if (nextMode === 'random' && previousMode !== 'random') {
      const currentSongId = (Jukebox.State.currentSong ? Jukebox.State.currentSong.id : null)
        || Jukebox.getCurrentRandomQueueSongId();
      if (Jukebox.State.randomQueueExitSongId === currentSongId && Jukebox.State.randomQueue.length) {
        Jukebox.State.randomQueueExitSongId = null;
        Jukebox.ensureRandomQueueAnchor(currentSongId);
      } else {
        Jukebox.resetRandomQueue(currentSongId);
      }
    } else if (previousMode === 'random' && nextMode !== 'random') {
      const activeSongId = Jukebox.State.currentSong ? Jukebox.State.currentSong.id : null;
      const queuedSongId = !activeSongId ? Jukebox.getCurrentRandomQueueSongId() : null;
      if (activeSongId && (Jukebox.State.isPlaying || Jukebox.State.isPaused) && Jukebox.State.randomQueue.length) {
        Jukebox.State.randomQueueExitSongId = activeSongId;
      } else if (queuedSongId && Jukebox.State.randomQueue.length) {
        Jukebox.State.randomQueueExitSongId = queuedSongId;
      } else {
        Jukebox.clearRandomQueue();
      }
    }
    Jukebox.setStoredJson('playbackMode', nextMode);
    Jukebox.updatePlaybackModeButtons();
  },

  cyclePlaybackMode: function() {
    const modeOrder = Jukebox.getPlaybackModeOrder();
    const currentIndex = modeOrder.indexOf(Jukebox.State.playbackMode);
    const nextMode = modeOrder[(currentIndex + 1 + modeOrder.length) % modeOrder.length] || 'sequence';
    Jukebox.setPlaybackMode(nextMode);
  },

  getPlaybackModeLabel: function(mode) {
    if (mode === 'none') return window.t('Jukebox.noAutoNext', '不自动下一首');
    if (mode === 'single') return window.t('Jukebox.singleLoop', '单曲循环');
    if (mode === 'random') return window.t('Jukebox.randomPlay', '随机播放');
    return window.t('Jukebox.sequencePlay', '顺序播放');
  },

  getPlaybackModeButtonLabel: function() {
    return `${window.t('Jukebox.switchPlaybackMode', '切换播放模式')}: ${Jukebox.getPlaybackModeLabel(Jukebox.State.playbackMode)}`;
  },

  getPlaybackModeIcon: function(mode) {
    if (mode === 'none') {
      return '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M6.4 5 5 6.4 10.6 12 5 17.6 6.4 19l5.6-5.6 5.6 5.6 1.4-1.4-5.6-5.6L19 6.4 17.6 5 12 10.6 6.4 5z"/></svg>';
    }
    if (mode === 'single') {
      return '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/><path fill="currentColor" d="M11 9h2v6h-2z"/></svg>';
    }
    if (mode === 'sequence') {
      return '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M4 7h10v2H4V7zm0 4h12v2H4v-2zm0 4h10v2H4v-2zm13.5-8.5L21 10l-3.5 3.5V11H16V9h1.5V6.5z"/></svg>';
    }
    return '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M10.6 5.6 9.2 7 6.4 4.2C5.6 3.4 4.6 3 3.5 3H2v2h1.5c.6 0 1.1.2 1.5.6L7.8 8.4 5 11.2c-.4.4-.9.6-1.5.6H2v2h1.5c1.1 0 2.1-.4 2.9-1.2l2.8-2.8 1.4 1.4L8.8 13l5.2 5.2c.8.8 1.8 1.2 2.9 1.2H18v3l4-4-4-4v3h-1.1c-.6 0-1.1-.2-1.5-.6L10.2 9.6l1.8-1.8L10.6 5.6zM18 3v3h-1.1c-1.1 0-2.1.4-2.9 1.2l-1.2 1.2 1.4 1.4 1.2-1.2c.4-.4.9-.6 1.5-.6H18v3l4-4-4-4z"/></svg>';
  },

  createPlaybackModeButton: function() {
    const button = document.createElement('button');
    const mode = Jukebox.State.playbackMode;
    const label = Jukebox.getPlaybackModeButtonLabel();
    button.type = 'button';
    button.className = 'play-btn jukebox-mode-btn active';
    button.dataset.mode = mode;
    button.setAttribute('aria-label', label);
    button.setAttribute('aria-live', 'polite');
    button.innerHTML = Jukebox.getPlaybackModeIcon(mode);
    button.removeAttribute('title');
    Jukebox.setupTooltip(button, () => Jukebox.getPlaybackModeButtonLabel());
    button.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      Jukebox.cyclePlaybackMode();
    });
    return button;
  },

  appendPlaybackModeButtons: function(container) {
    container.appendChild(Jukebox.createPlaybackModeButton());
  },

  renderPlaybackControls: function() {
    const modeContainer = document.getElementById('jukebox-mode-controls');
    if (modeContainer) {
      modeContainer.innerHTML = '';
      Jukebox.appendPlaybackModeButtons(modeContainer);
    }
    Jukebox.bindMainControlTooltips();
    Jukebox.updateGlobalTransportControls();
  },

  bindMainControlTooltips: function() {
    const prevBtn = document.getElementById('jukebox-control-prev');
    Jukebox.setupTooltipOnce(prevBtn, () => window.t('Jukebox.previousSong', '上一首'));

    const nextBtn = document.getElementById('jukebox-control-next');
    Jukebox.setupTooltipOnce(nextBtn, () => window.t('Jukebox.nextSong', '下一首'));

    const playPauseBtn = document.getElementById('jukebox-control-play-pause');
    Jukebox.setupTooltipOnce(playPauseBtn, () => playPauseBtn?.dataset.tooltip || window.t('Jukebox.play', '播放'));

    const speakerBtn = document.getElementById('jukebox-speaker-btn');
    Jukebox.setupTooltipOnce(speakerBtn, () => window.t('Jukebox.mute', '静音'));
  },

  getGlobalPlayPauseIcon: function() {
    if (Jukebox.State.isPlaying && !Jukebox.State.isPaused) {
      return '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
    }
    return '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>';
  },

  updateGlobalTransportControls: function() {
    const playPauseBtn = document.getElementById('jukebox-control-play-pause');
    if (playPauseBtn) {
      const isPauseAction = Jukebox.State.isPlaying && !Jukebox.State.isPaused;
      const label = isPauseAction
        ? window.t('Jukebox.pause', '暂停')
        : (Jukebox.State.currentSong && Jukebox.State.isPaused
          ? window.t('Jukebox.resume', '继续')
          : window.t('Jukebox.play', '播放'));
      playPauseBtn.innerHTML = Jukebox.getGlobalPlayPauseIcon();
      playPauseBtn.setAttribute('aria-label', label);
      playPauseBtn.removeAttribute('title');
      playPauseBtn.dataset.tooltip = label;
      playPauseBtn.classList.toggle('pause-btn', isPauseAction);
      playPauseBtn.classList.toggle('resume-btn', !isPauseAction && Jukebox.State.currentSong && Jukebox.State.isPaused);
      Jukebox.refreshTooltip(playPauseBtn);
    }
  },

  getManualAdjacentSong: function(direction) {
    const songs = Jukebox.State.songs || [];
    if (!songs.length) return null;
    if (!Jukebox.State.currentSong) {
      return direction === -1 ? songs[songs.length - 1] : songs[0];
    }
    const currentIndex = songs.findIndex(song => song.id === Jukebox.State.currentSong.id);
    if (currentIndex < 0) return direction === -1 ? songs[songs.length - 1] : songs[0];
    const nextIndex = (currentIndex + direction + songs.length) % songs.length;
    return songs[nextIndex];
  },

  findSongById: function(songId) {
    if (!songId) return null;
    return (Jukebox.State.songs || []).find(song => song.id === songId) || null;
  },

  getCurrentRandomQueueSongId: function() {
    const queue = Jukebox.State.randomQueue || [];
    const queuedSongId = queue[Jukebox.State.randomQueueIndex];
    return Jukebox.findSongById(queuedSongId) ? queuedSongId : null;
  },

  clearRandomQueue: function() {
    Jukebox.State.randomQueue = [];
    Jukebox.State.randomQueueIndex = -1;
    Jukebox.State.randomQueueExitSongId = null;
  },

  resetRandomQueue: function(anchorSongId) {
    const anchorSong = Jukebox.findSongById(anchorSongId);
    if (anchorSong) {
      Jukebox.State.randomQueue = [anchorSong.id];
      Jukebox.State.randomQueueIndex = 0;
      Jukebox.State.randomQueueExitSongId = null;
      return;
    }
    Jukebox.clearRandomQueue();
  },

  pruneRandomQueue: function(anchorSongId) {
    const validIds = new Set((Jukebox.State.songs || []).map(song => song.id));
    if (anchorSongId && !validIds.has(anchorSongId)) {
      Jukebox.clearRandomQueue();
      return false;
    }

    const queue = Jukebox.State.randomQueue || [];
    const currentQueueIndex = Jukebox.State.randomQueueIndex;
    const filteredQueue = [];
    let retainedQueueIndex = -1;
    queue.forEach((songId, queueIndex) => {
      if (!validIds.has(songId)) return;
      if (queueIndex === currentQueueIndex) {
        retainedQueueIndex = filteredQueue.length;
      }
      filteredQueue.push(songId);
    });
    Jukebox.State.randomQueue = filteredQueue;

    if (!anchorSongId) {
      if (!filteredQueue.length) {
        Jukebox.State.randomQueueIndex = -1;
        return false;
      }
      if (retainedQueueIndex !== -1) {
        Jukebox.State.randomQueueIndex = retainedQueueIndex;
      } else if (currentQueueIndex < 0 || currentQueueIndex >= filteredQueue.length) {
        Jukebox.State.randomQueueIndex = filteredQueue.length - 1;
      } else {
        Jukebox.State.randomQueueIndex = currentQueueIndex;
      }
      return true;
    }

    if (retainedQueueIndex !== -1 && filteredQueue[retainedQueueIndex] === anchorSongId) {
      Jukebox.State.randomQueueIndex = retainedQueueIndex;
      return true;
    }

    const anchorIndex = filteredQueue.lastIndexOf(anchorSongId);
    if (anchorIndex === -1) {
      Jukebox.State.randomQueue = [anchorSongId];
      Jukebox.State.randomQueueIndex = 0;
      return true;
    }

    Jukebox.State.randomQueueIndex = anchorIndex;
    return true;
  },

  syncRandomQueueWithSongs: function() {
    if (Jukebox.State.playbackMode !== 'random') {
      const pendingSongId = Jukebox.State.randomQueueExitSongId;
      const currentSongId = (Jukebox.State.currentSong ? Jukebox.State.currentSong.id : null)
        || Jukebox.getCurrentRandomQueueSongId();
      if (!pendingSongId || pendingSongId !== currentSongId) {
        Jukebox.clearRandomQueue();
      } else {
        Jukebox.pruneRandomQueue(pendingSongId);
      }
      return;
    }

    const currentSongId = (Jukebox.State.currentSong ? Jukebox.State.currentSong.id : null)
      || Jukebox.getCurrentRandomQueueSongId()
      || null;
    if (!currentSongId || !Jukebox.pruneRandomQueue(currentSongId)) {
      Jukebox.clearRandomQueue();
      return;
    }

    Jukebox.ensureRandomQueueAnchor(currentSongId);
  },

  ensureRandomQueueAnchor: function(songId) {
    if (!Jukebox.findSongById(songId)) {
      Jukebox.clearRandomQueue();
      return;
    }

    const queue = Jukebox.State.randomQueue || [];
    const index = Jukebox.State.randomQueueIndex;
    if (!queue.length || index < 0 || index >= queue.length || queue[index] !== songId) {
      Jukebox.resetRandomQueue(songId);
    }
  },

  expireRandomQueueIfPendingSongEnded: function(endedSongId) {
    if (Jukebox.State.playbackMode === 'random') return;
    if (Jukebox.State.randomQueueExitSongId && Jukebox.State.randomQueueExitSongId === endedSongId) {
      Jukebox.clearRandomQueue();
    }
  },

  pickRandomSongExcluding: function(excludedIds) {
    const songs = Jukebox.State.songs || [];
    if (!songs.length) return null;
    const excludedSet = new Set((Array.isArray(excludedIds) ? excludedIds : [excludedIds]).filter(Boolean));
    let candidates = songs.filter(song => !excludedSet.has(song.id));
    if (!candidates.length && songs.length === 1) {
      candidates = songs;
    }
    if (!candidates.length) return null;
    return candidates[Math.floor(Math.random() * candidates.length)];
  },

  getRandomAdjacentSong: function(direction, anchorSongId) {
    const songs = Jukebox.State.songs || [];
    if (!songs.length) {
      Jukebox.clearRandomQueue();
      return null;
    }

    const queue = Jukebox.State.randomQueue || [];
    const queuedSongId = queue[Jukebox.State.randomQueueIndex];
    const currentSongId = anchorSongId
      || (Jukebox.findSongById(queuedSongId) ? queuedSongId : null)
      || (Jukebox.State.currentSong ? Jukebox.State.currentSong.id : null)
      || null;

    if (currentSongId) {
      Jukebox.ensureRandomQueueAnchor(currentSongId);
    } else if (!Jukebox.State.randomQueue.length) {
      if (direction < 0) return null;
      const firstRandomSong = Jukebox.pickRandomSongExcluding(null);
      if (!firstRandomSong) return null;
      Jukebox.State.randomQueue = [firstRandomSong.id];
      Jukebox.State.randomQueueIndex = 0;
      return firstRandomSong;
    }

    if (direction < 0) {
      if (Jukebox.State.randomQueueIndex <= 0) return null;
      Jukebox.State.randomQueueIndex -= 1;
      return Jukebox.findSongById(Jukebox.State.randomQueue[Jukebox.State.randomQueueIndex]);
    }

    if (Jukebox.State.randomQueueIndex < Jukebox.State.randomQueue.length - 1) {
      Jukebox.State.randomQueueIndex += 1;
      return Jukebox.findSongById(Jukebox.State.randomQueue[Jukebox.State.randomQueueIndex]);
    }

    const staleCurrentSongId = Jukebox.State.currentSong ? Jukebox.State.currentSong.id : null;
    const excludedIds = [currentSongId];
    if (staleCurrentSongId && staleCurrentSongId !== currentSongId) {
      excludedIds.push(staleCurrentSongId);
    }
    const nextRandomSong = Jukebox.pickRandomSongExcluding(excludedIds);
    if (!nextRandomSong) return null;
    Jukebox.State.randomQueue.push(nextRandomSong.id);
    Jukebox.State.randomQueueIndex = Jukebox.State.randomQueue.length - 1;
    return nextRandomSong;
  },

  playAdjacentSong: function(direction) {
    const nextSong = Jukebox.State.playbackMode === 'random'
      ? Jukebox.getRandomAdjacentSong(direction)
      : Jukebox.getManualAdjacentSong(direction);
    if (nextSong) {
      return Jukebox.playSong(nextSong.id, { fromQueue: Jukebox.State.playbackMode === 'random' });
    }
    return null;
  },

  toggleGlobalPlayPause: function() {
    if (Jukebox.State.currentSong && (Jukebox.State.isPlaying || Jukebox.State.isPaused || Jukebox.State.isVMDPlaying)) {
      Jukebox.togglePause();
      return;
    }
    const firstSong = (Jukebox.State.songs || [])[0];
    if (firstSong) {
      Jukebox.playSong(firstSong.id);
    }
  },

  updatePlaybackModeButtons: function(root) {
    const scope = root || document;
    scope.querySelectorAll('.jukebox-mode-btn').forEach((button) => {
      const mode = Jukebox.State.playbackMode;
      const label = Jukebox.getPlaybackModeButtonLabel();
      button.dataset.mode = mode;
      button.classList.add('active');
      button.setAttribute('aria-label', label);
      button.removeAttribute('title');
      button.innerHTML = Jukebox.getPlaybackModeIcon(mode);
      Jukebox.refreshTooltip(button);
    });
  },

  getSavedSongOrder: function() {
    const saved = Jukebox.getStoredJson('songOrder', []);
    return Array.isArray(saved) ? saved.filter(id => typeof id === 'string') : [];
  },

  applySavedSongOrder: function(songs) {
    const savedOrder = Jukebox.getSavedSongOrder();
    if (!savedOrder.length) return songs;
    const byId = new Map(songs.map(song => [song.id, song]));
    const ordered = [];
    savedOrder.forEach((id) => {
      if (byId.has(id)) {
        ordered.push(byId.get(id));
        byId.delete(id);
      }
    });
    return ordered.concat(Array.from(byId.values()));
  },

  saveSongOrder: function() {
    Jukebox.setStoredJson('songOrder', Jukebox.State.songs.map(song => song.id));
  },

  moveSongInPlaylist: function(draggedId, targetId, placeAfter) {
    if (!draggedId || !targetId || draggedId === targetId) return false;
    const fromIndex = Jukebox.State.songs.findIndex(song => song.id === draggedId);
    const targetIndex = Jukebox.State.songs.findIndex(song => song.id === targetId);
    if (fromIndex < 0 || targetIndex < 0) return false;

    const [moved] = Jukebox.State.songs.splice(fromIndex, 1);
    let insertIndex = Jukebox.State.songs.findIndex(song => song.id === targetId);
    if (insertIndex < 0) insertIndex = Jukebox.State.songs.length;
    if (placeAfter) insertIndex += 1;
    Jukebox.State.songs.splice(insertIndex, 0, moved);
    Jukebox.saveSongOrder();
    Jukebox.renderList();
    return true;
  },

  isSongSortUnlocked: function() {
    return Jukebox.State.songSortUnlocked === true;
  },

  getSongSortLockLabel: function() {
    return Jukebox.isSongSortUnlocked()
      ? window.t('Jukebox.lockSongSort', '锁定歌曲排序')
      : window.t('Jukebox.unlockSongSort', '解锁歌曲排序');
  },

  getSongSortLockTooltip: function() {
    return Jukebox.isSongSortUnlocked()
      ? window.t('Jukebox.songSortLockTooltipUnlocked', '歌曲排序已解锁：可以拖动歌曲调整顺序，点击后锁定防误拖')
      : window.t('Jukebox.songSortLockTooltipLocked', '歌曲排序已锁定：防止误拖，点击解锁后可拖动歌曲调整顺序');
  },

  getSongSortLockIcon: function() {
    if (Jukebox.isSongSortUnlocked()) {
      return '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path fill="currentColor" d="M7 10V7a5 5 0 0 1 9.6-1.9l-1.9.8A3 3 0 0 0 9 7v3h8a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2zm0 2v7h10v-7H7zm4 2h2v3h-2v-3z"/></svg>';
    }
    return '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path fill="currentColor" d="M7 10V7a5 5 0 0 1 10 0v3h1a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h1zm2 0h6V7a3 3 0 0 0-6 0v3zm-3 2v7h12v-7H6zm5 2h2v3h-2v-3z"/></svg>';
  },

  toggleSongSortLock: function(event) {
    if (event) {
      event.preventDefault();
      event.stopPropagation();
    }
    Jukebox.State.songSortUnlocked = !Jukebox.State.songSortUnlocked;
    if (!Jukebox.State.songSortUnlocked) {
      Jukebox.clearSongDragState();
    }
    Jukebox.updateSongSortLockControls();
  },

  updateSongSortLockControls: function(root) {
    const scope = root || document;
    const unlocked = Jukebox.isSongSortUnlocked();
    const label = Jukebox.getSongSortLockLabel();
    const tooltip = Jukebox.getSongSortLockTooltip();
    scope.querySelectorAll('.jukebox-sort-lock-btn').forEach((button) => {
      button.classList.toggle('unlocked', unlocked);
      button.setAttribute('aria-pressed', unlocked ? 'true' : 'false');
      button.setAttribute('aria-label', label);
      button.removeAttribute('title');
      button.dataset.tooltip = tooltip;
      button.innerHTML = Jukebox.getSongSortLockIcon();
    });
    scope.querySelectorAll('#jukebox-song-list tr[data-song-id]').forEach((row) => {
      row.draggable = unlocked;
      row.classList.toggle('jukebox-row-sort-unlocked', unlocked);
    });
  },

  clearSongDragState: function() {
    document.querySelectorAll('#jukebox-song-list tr').forEach((row) => {
      row.classList.remove('jukebox-row-dragging', 'jukebox-row-drop-before', 'jukebox-row-drop-after');
    });
    Jukebox.State.draggedSongId = null;
  },

  easeInOutMarquee: function(t) {
    return 0.5 - Math.cos(Math.PI * t) / 2;
  },

  getMarqueeDuration: function(maxScroll) {
    return Math.max(3000, Math.min(60000, maxScroll * 100));
  },

  updateMarqueeText: function(root) {
    const scope = root || document;
    const nodes = Array.from(scope.querySelectorAll('[data-neko-marquee]'));

    nodes.forEach((el) => {
      const maxScroll = Math.max(0, el.scrollWidth - el.clientWidth);
      if (maxScroll <= 1) {
        el.classList.remove('neko-marquee-active');
        el.scrollLeft = 0;
        Jukebox.State.marqueeItems.delete(el);
        return;
      }

      el.classList.add('neko-marquee-active');
      const existing = Jukebox.State.marqueeItems.get(el);
      const duration = Jukebox.getMarqueeDuration(maxScroll);
      if (!existing || Math.abs((existing.maxScroll || 0) - maxScroll) > 1) {
        Jukebox.State.marqueeItems.set(el, {
          phase: 'pauseStart',
          phaseStart: performance.now(),
          duration,
          maxScroll
        });
        el.scrollLeft = 0;
      } else {
        existing.duration = duration;
        existing.maxScroll = maxScroll;
      }
    });

    for (const el of Array.from(Jukebox.State.marqueeItems.keys())) {
      if (!document.contains(el)) {
        Jukebox.State.marqueeItems.delete(el);
      }
    }

    if (Jukebox.State.marqueeItems.size > 0 && !Jukebox.State.marqueeRaf) {
      Jukebox.State.marqueeRaf = requestAnimationFrame(Jukebox.tickMarqueeText);
    }
  },

  tickMarqueeText: function(now) {
    Jukebox.State.marqueeRaf = null;

    for (const [el, item] of Array.from(Jukebox.State.marqueeItems.entries())) {
      if (!document.contains(el)) {
        Jukebox.State.marqueeItems.delete(el);
        continue;
      }

      const maxScroll = Math.max(0, el.scrollWidth - el.clientWidth);
      if (maxScroll <= 1) {
        el.classList.remove('neko-marquee-active');
        el.scrollLeft = 0;
        Jukebox.State.marqueeItems.delete(el);
        continue;
      }

      if (document.activeElement === el || el.contains(document.activeElement)) {
        el.classList.add('neko-marquee-editing');
        item.phase = 'pauseStart';
        item.phaseStart = now;
        continue;
      }
      el.classList.remove('neko-marquee-editing');

      item.maxScroll = maxScroll;
      item.duration = Jukebox.getMarqueeDuration(maxScroll);
      const elapsed = now - item.phaseStart;

      if (item.phase === 'pauseStart') {
        el.scrollLeft = 0;
        if (elapsed >= 1000) {
          item.phase = 'forward';
          item.phaseStart = now;
        }
      } else if (item.phase === 'forward') {
        const t = Math.min(1, elapsed / item.duration);
        el.scrollLeft = Math.round(maxScroll * Jukebox.easeInOutMarquee(t));
        if (t >= 1) {
          el.scrollLeft = 0;
          item.phase = 'pauseStart';
          item.phaseStart = now;
        }
      }
    }

    if (Jukebox.State.marqueeItems.size > 0) {
      Jukebox.State.marqueeRaf = requestAnimationFrame(Jukebox.tickMarqueeText);
    }
  },

  scheduleMarqueeTextUpdate: function(root) {
    requestAnimationFrame(() => Jukebox.updateMarqueeText(root || document));
  },

});
