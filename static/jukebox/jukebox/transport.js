var JUKEBOX_COMPRESSED_BUNDLED_VRM_IDLE_NAMES = new Set([
  'liked', 'wait01', 'wait02', 'wait03', 'wait04', 'wait05',
  '全身展示', '射击姿态', '屈伸运动', '旋转', '模特姿势', '比 V 手势', '致意问候'
]);

function normalizeJukeboxBundledVrmIdleUrl(url) {
  const value = String(url || '');
  const match = value.match(/^\/static\/vrm\/animation\/([^/?#]+)\.vrma(?:[?#]|$)/i);
  if (!match) return url;
  let assetName = match[1];
  try { assetName = decodeURIComponent(assetName); } catch (_) {}
  if (!JUKEBOX_COMPRESSED_BUNDLED_VRM_IDLE_NAMES.has(assetName)) return url;
  return value.replace(/\.vrma(?=[?#]|$)/i, '.vrma.gz');
}

Object.assign(window.Jukebox, {
  startConfigPolling: function() {
    Jukebox.stopConfigPolling();
    Jukebox.State.configPollTimer = setInterval(() => {
      Jukebox.checkConfigUpdates();
    }, 10000);
  },

  stopConfigPolling: function() {
    if (Jukebox.State.configPollTimer) {
      clearInterval(Jukebox.State.configPollTimer);
      Jukebox.State.configPollTimer = null;
    }
    Jukebox.State.configPollInFlight = false;
  },

  ensureRuntimeHost: function() {
    if (Jukebox.State.runtimeHost && document.body.contains(Jukebox.State.runtimeHost)) {
      return Jukebox.State.runtimeHost;
    }

    const host = document.createElement('div');
    host.id = 'neko-jukebox-runtime-host';
    host.setAttribute('aria-hidden', 'true');
    host.style.position = 'fixed';
    host.style.left = '-9999px';
    host.style.top = '-9999px';
    host.style.width = '1px';
    host.style.height = '1px';
    host.style.overflow = 'hidden';
    host.style.pointerEvents = 'none';
    document.body.appendChild(host);
    Jukebox.State.runtimeHost = host;
    return host;
  },

  destroyRuntimeHost: function() {
    Jukebox.terminateFuzzySearchWorker();
    if (Jukebox.State.runtimeHost) {
      try {
        Jukebox.State.runtimeHost.remove();
      } catch (_) {}
    }
    Jukebox.State.runtimeHost = null;
    Jukebox.State.isRuntimeReady = false;
  },

  ensureRuntime: async function(options = {}) {
    const Jukebox = window.Jukebox || this;
    const headless = options.headless === true;
    // 无头运行时的唯一判据。不能靠「播放器建在哪个宿主里」反推：复用 music_ui
    // 的共享播放器或面板播放器时根本不会新建无头宿主。
    if (headless) {
      Jukebox.State.headlessRuntimeRequested = true;
    }

    // 真正的记忆化。之前只在 finally 里清 runtimeInitPromise、从不看
    // isRuntimeReady，等于只有并发锁：每条指令（连 set_volume）都要重新拉一遍
    // 全量 /api/jukebox/config 并重建 State.songs，而面板里已渲染的行还指着上
    // 一个数组里的对象。曲库变更由 UI 侧的 config 轮询负责；无头下则由 play
    // 找不到歌时再刷新一次兜底。
    if (Jukebox.State.isRuntimeReady && Jukebox.getPlayer()) {
      return {
        songCount: Jukebox.State.songs.length,
        headless
      };
    }

    if (Jukebox.State.runtimeInitPromise) {
      return Jukebox.State.runtimeInitPromise;
    }

    const epoch = Jukebox.State.teardownEpoch;
    const runtimeInit = (async () => {
      Jukebox.loadPlaybackPreferences();
      await Jukebox.loadSongData();
      // 拉配置期间点歌台被整个拆掉了：绝不能再往下建隐藏宿主和播放器，否则用户
      // 明确销毁之后又冒出来一个没人负责销毁的实例。留给调用方的 epoch 闸门去报
      // jukebox_torn_down。
      if (Jukebox.State.teardownEpoch !== epoch) {
        return { songCount: Jukebox.State.songs.length, headless, tornDown: true };
      }
      const player = Jukebox.initPlayer({ headless });
      if (!player && !Jukebox.getPlayer()) {
        throw new Error(window.t('Jukebox.playError', '音乐播放器未初始化'));
      }
      Jukebox.State.isRuntimeReady = true;
      return {
        songCount: Jukebox.State.songs.length,
        headless
      };
    })();
    Jukebox.State.runtimeInitPromise = runtimeInit;

    try {
      return await runtimeInit;
    } finally {
      // 只有槽位还指向自己时才清。拆除会把槽位清掉，随后的指令可能已经起了第二次
      // 初始化；这里无条件清的话就把那一次也解锁了，两次配置加载会乱序覆盖
      // State.songs，而此刻正有指令在里面检索。
      if (Jukebox.State.runtimeInitPromise === runtimeInit) {
        Jukebox.State.runtimeInitPromise = null;
      }
    }
  },

  normalizeSongQuery: function(value) {
    return String(value || '')
      .normalize('NFKC')
      .trim()
      .toLowerCase()
      .replace(/[\s\-_/\\|·・,，.。:：;；'"\[\]【】()（）{}<>《》]+/g, '');
  },

  getSongSearchValues: function(song) {
    const values = [
      song && song.name,
      song && song.artist,
      song && song.id,
      song && song.audio,
      song && song.defaultAction
    ];
    if (song && Array.isArray(song.aliases)) {
      values.push(...song.aliases);
    } else if (song && song.aliases) {
      values.push(song.aliases);
    }
    if (song && Array.isArray(song.boundActions)) {
      song.boundActions.forEach(action => {
        values.push(action && action.id, action && action.name, action && action.file);
      });
    }

    return [...new Set(values.map(value => Jukebox.normalizeSongQuery(value)).filter(Boolean))];
  },

  getLevenshteinDistance: function(a, b, maxDistance = Infinity) {
    if (a === b) return 0;
    if (!a) return b.length;
    if (!b) return a.length;
    if (Math.abs(a.length - b.length) > maxDistance) return maxDistance + 1;

    let previous = Array.from({ length: b.length + 1 }, (_, index) => index);
    for (let i = 1; i <= a.length; i += 1) {
      const current = [i];
      let rowMin = current[0];
      for (let j = 1; j <= b.length; j += 1) {
        const cost = a[i - 1] === b[j - 1] ? 0 : 1;
        const value = Math.min(
          previous[j] + 1,
          current[j - 1] + 1,
          previous[j - 1] + cost
        );
        current[j] = value;
        rowMin = Math.min(rowMin, value);
      }
      if (rowMin > maxDistance) return maxDistance + 1;
      previous = current;
    }
    return previous[b.length];
  },

  getFuzzyDistanceBudget: function(query) {
    return query.length <= 3 ? 1 : Math.max(1, Math.floor(query.length * 0.3));
  },

  getBestFuzzyDistance: function(query, target) {
    if (!query || !target || query.length < 2 || target.length < 2) return Infinity;

    const maxDistance = Jukebox.getFuzzyDistanceBudget(query);

    // 近似子串匹配（Sellers）：首行全 0 表示「子串可以从 target 任意位置起」，
    // 末行最小值就是 query 到 target 任一子串的最小编辑距离。语义与旧的
    // start×length 双层枚举完全一致（长度差超过 maxDistance 的窗口距离必然
    // 也超过 maxDistance），但代价从 O(|q|*|t|^2) 降到 O(|q|*|t|)：旧实现在
    // 300 首 / 50 字查询 / 120 字候选下实测要 35.6s。
    let previous = new Array(target.length + 1).fill(0);
    let current = new Array(target.length + 1).fill(0);
    for (let i = 1; i <= query.length; i += 1) {
      current[0] = i;
      let rowMin = current[0];
      for (let j = 1; j <= target.length; j += 1) {
        const cost = query[i - 1] === target[j - 1] ? 0 : 1;
        const value = Math.min(
          previous[j] + 1,
          current[j - 1] + 1,
          previous[j - 1] + cost
        );
        current[j] = value;
        if (value < rowMin) rowMin = value;
      }
      // 每一行的最小值只会随行数单调不减，整行都超预算就不可能再降回来。
      if (rowMin > maxDistance) return Infinity;
      const swap = previous;
      previous = current;
      current = swap;
    }

    let best = Infinity;
    for (let j = 0; j <= target.length; j += 1) {
      if (previous[j] < best) best = previous[j];
    }
    return best <= maxDistance ? best : Infinity;
  },

  // 曲库位置只能当「同档并列时谁排前面」的次序，不能直接从档分里减：档间距是
  // 1000，而 index 没有上界，曲库超过 1000 首之后精确命中会掉进前缀档 —— 精确
  // 命中落在 index=1001 得 8999，输给 index=0 的前缀命中 9000，用户报准歌名反而
  // 播到别的歌。压成 [0,1) 的小数，档次序就跟曲库长度无关了。
  getSongOrderPenalty: function(index) {
    const position = Number.isFinite(index) && index > 0 ? index : 0;
    return position / (position + 1);
  },

  scoreSongForQuery: function(song, normalizedQuery, index, options = {}) {
    const allowFuzzy = options.fuzzy !== false;
    const orderPenalty = Jukebox.getSongOrderPenalty(index);
    let bestScore = -Infinity;
    const values = Jukebox.getSongSearchValues(song);

    values.forEach((value) => {
      if (value === normalizedQuery) {
        bestScore = Math.max(bestScore, 10000 - orderPenalty);
        return;
      }
      if (value.startsWith(normalizedQuery)) {
        bestScore = Math.max(bestScore, 9000 - (value.length - normalizedQuery.length) - orderPenalty);
        return;
      }
      const includesIndex = value.indexOf(normalizedQuery);
      if (includesIndex >= 0) {
        bestScore = Math.max(bestScore, 8000 - includesIndex - (value.length - normalizedQuery.length) - orderPenalty);
        return;
      }

      if (!allowFuzzy) return;

      const fuzzyDistance = Jukebox.getBestFuzzyDistance(normalizedQuery, value);
      if (Number.isFinite(fuzzyDistance)) {
        bestScore = Math.max(bestScore, 7000 - fuzzyDistance * 100 - Math.abs(value.length - normalizedQuery.length) - orderPenalty);
      }
    });

    return Number.isFinite(bestScore) ? bestScore : null;
  },

  findBestSongIndex: function(songs, normalizedQuery, options = {}) {
    let bestIndex = -1;
    let bestScore = -Infinity;
    for (let index = 0; index < songs.length; index += 1) {
      const score = Jukebox.scoreSongForQuery(songs[index], normalizedQuery, index, options);
      if (score !== null && score > bestScore) {
        bestIndex = index;
        bestScore = score;
      }
    }
    return bestIndex;
  },

  buildFuzzySearchWorkerSource: function() {
    // Worker 里跑的就是这几个函数本体（toString 序列化），不另抄一份实现，
    // 免得主线程与 worker 的匹配规则各自漂移。
    // 这几个函数是按名字序列化进 worker 的，漏一个就在 worker 里 ReferenceError，
    // 而错误被 catch 成「搜索失败」，表面只是搜不到歌。改动它们的依赖时必须同步
    // 这份清单，test_jukebox_fuzzy_worker_source_has_no_missing_dependency 会守住。
    const names = [
      'normalizeSongQuery',
      'getSongSearchValues',
      'getLevenshteinDistance',
      'getFuzzyDistanceBudget',
      'getBestFuzzyDistance',
      'getSongOrderPenalty',
      'scoreSongForQuery',
      'findBestSongIndex'
    ];
    const declarations = names
      .map(name => 'const ' + name + ' = ' + Jukebox[name].toString() + ';')
      .join('\n');
    return [
      declarations,
      'const Jukebox = { ' + names.join(', ') + ' };',
      'self.onmessage = function(event) {',
      '  const data = event.data || {};',
      '  let index = -1;',
      '  try {',
      '    index = Jukebox.findBestSongIndex(data.songs || [], data.query || \'\');',
      '  } catch (error) {',
      '    self.postMessage({ token: data.token, error: String(error && error.message || error) });',
      '    return;',
      '  }',
      '  self.postMessage({ token: data.token, index: index });',
      '};'
    ].join('\n');
  },

  terminateFuzzySearchWorker: function() {
    const state = Jukebox.State;
    // 先把上一次挂着的 Promise 结掉。terminate() 既不触发 onmessage 也不触发
    // onerror，不主动 settle 的话 await 它的 findSongForQuery 会永远悬着，
    // 前端那条串行的控制队列跟着一起再也不动。
    if (typeof state.fuzzySearchSettle === 'function') {
      const settle = state.fuzzySearchSettle;
      state.fuzzySearchSettle = null;
      // 被取代 ≠ 失败：新查询接手了，这一次直接作废，不要再去跑一遍主线程匹配。
      settle({ superseded: true });
    }
    if (state.fuzzySearchWorker) {
      try { state.fuzzySearchWorker.terminate(); } catch (_) {}
      state.fuzzySearchWorker = null;
    }
    if (state.fuzzySearchWorkerUrl) {
      try { URL.revokeObjectURL(state.fuzzySearchWorkerUrl); } catch (_) {}
      state.fuzzySearchWorkerUrl = null;
    }
  },

  createFuzzySearchWorker: function() {
    if (typeof Worker !== 'function' || typeof Blob !== 'function'
      || typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') {
      return null;
    }
    try {
      const blob = new Blob([Jukebox.buildFuzzySearchWorkerSource()], { type: 'text/javascript' });
      const url = URL.createObjectURL(blob);
      const worker = new Worker(url);
      Jukebox.State.fuzzySearchWorker = worker;
      Jukebox.State.fuzzySearchWorkerUrl = url;
      return worker;
    } catch (error) {
      console.warn('[Jukebox] 模糊搜索 worker 创建失败，退回主线程:', error);
      Jukebox.terminateFuzzySearchWorker();
      return null;
    }
  },

  findSongByFuzzyWorker: function(songs, normalizedQuery) {
    // 前一次模糊搜索直接作废：曲库大时它可能还在跑，留着只会拖慢这一次。
    Jukebox.terminateFuzzySearchWorker();
    const worker = Jukebox.createFuzzySearchWorker();
    if (!worker) return null;

    const token = (Jukebox.State.fuzzySearchToken || 0) + 1;
    Jukebox.State.fuzzySearchToken = token;

    return new Promise((resolve) => {
      let settled = false;
      const finish = (index) => {
        if (settled) return;
        settled = true;
        if (Jukebox.State.fuzzySearchSettle === finish) {
          Jukebox.State.fuzzySearchSettle = null;
        }
        if (Jukebox.State.fuzzySearchWorker === worker) {
          Jukebox.terminateFuzzySearchWorker();
        }
        resolve(index);
      };
      Jukebox.State.fuzzySearchSettle = finish;
      worker.onmessage = (event) => {
        const data = event.data || {};
        if (data.token !== token) return;
        if (data.error) {
          console.warn('[Jukebox] 模糊搜索 worker 报错:', data.error);
          finish({ failed: true });
          return;
        }
        finish(Number.isInteger(data.index) ? { index: data.index } : { failed: true });
      };
      worker.onerror = (error) => {
        console.warn('[Jukebox] 模糊搜索 worker 异常:', error && error.message);
        finish({ failed: true });
      };
      try {
        worker.postMessage({ token, query: normalizedQuery, songs });
      } catch (error) {
        console.warn('[Jukebox] 模糊搜索 worker 投递失败:', error);
        finish({ failed: true });
      }
    });
  },

  findSongForQuery: async function(query) {
    const songs = Jukebox.State.songs || [];
    const normalizedQuery = Jukebox.normalizeSongQuery(query);
    if (!normalizedQuery) {
      return songs[0] || null;
    }

    // 精确 / 前缀 / 子串三档只是字符串比较，主线程跑得起；而且这三档的分数
    // （8000-10000）永远高于模糊档（<=7000），命中就已经是全局最优，不必再
    // 开线程。
    const directIndex = Jukebox.findBestSongIndex(songs, normalizedQuery, { fuzzy: false });
    if (directIndex >= 0) return songs[directIndex];

    const workerResult = Jukebox.findSongByFuzzyWorker(songs, normalizedQuery);
    if (workerResult) {
      const outcome = await workerResult;
      // 被新查询取代：本次作废。
      if (outcome && outcome.superseded) return null;
      // worker 真的算出了结果才用它。建成之后才失败（比如宿主策略禁 blob worker）
      // 不能当成「没这首歌」，否则控制面会报 song_not_found，而主线程明明能匹配。
      if (outcome && Number.isInteger(outcome.index)) {
        return outcome.index >= 0 ? songs[outcome.index] : null;
      }
    }

    // 没有 Worker（老宿主 / 创建失败），或 worker 起来之后出错：退回主线程，
    // 此时算法已是 O(|q|*|t|)。
    const index = Jukebox.findBestSongIndex(songs, normalizedQuery);
    return index >= 0 ? songs[index] : null;
  },

  // ===== 跨窗口控制归属 =====
  // Electron 分发形态下同一条 jukebox_control 会被 RAW_MESSAGE 转发给多个窗口
  // （pet + chat），而独立点唱机窗口（templates/jukebox.html）另有自己的 Jukebox
  // 实例和 APlayer，且既不加载 app-websocket.js 也不加载 jukebox-loader.js。
  // 不做归属判定的话：可见的那个播放器收不到 stop/next/音量/模式，play 还会在
  // 隐藏窗口里另起一条音轨。
  //
  // 通道用 BroadcastChannel：点唱机窗口在 Electron 里没有单独 partition（只有
  // full-chat 用 persist:neko-full-chat），与 pet 窗口同 session，能互通。
  CONTROL_OWNER_CHANNEL: 'neko-jukebox-control',
  CONTROL_OWNER_HEARTBEAT_MS: 2000,

  startControlOwnerService: function() {
    // 只有真正持有可见播放器的独立窗口才当拥有者。
    if (!window.__NEKO_JUKEBOX_STANDALONE__) return false;
    if (Jukebox.State.controlOwnerChannel) return true;
    if (typeof BroadcastChannel === 'undefined') return false;

    let channel;
    try {
      channel = new BroadcastChannel(Jukebox.CONTROL_OWNER_CHANNEL);
    } catch (error) {
      console.warn('[Jukebox] 控制通道不可用，跨窗口归属失效:', error);
      return false;
    }
    Jukebox.State.controlOwnerChannel = channel;

    const announce = () => {
      try {
        channel.postMessage({ type: 'jukebox_owner_alive' });
      } catch (_) {}
    };

    const reply = (data, result) => {
      try {
        channel.postMessage({
          type: 'jukebox_control_result',
          requestId: data.requestId,
          result: result
        });
      } catch (_) {}
    };

    const serve = async (data) => {
      // 排队期间被顶替了：后来的 stop / play，或者一条独立的取消信号。挂上
      // serveChain 之后就没法把它摘下来，所以由它自己在开跑前退出。
      if (Jukebox.isControlOwnerRequestSuperseded(data)) {
        reply(data, {
          ok: false,
          action: (data.command && data.command.action) || '',
          message: 'play_cancelled'
        });
        return;
      }
      // 调用方已经不等了（它那边 forwardControl 有超时预算）：别再让一条过期指令
      // 静默生效。ttlMs 由请求方随消息带来，这里不另存一份常量。
      const ttl = Number(data.ttlMs);
      if (Number.isFinite(ttl) && ttl > 0 && Number.isFinite(data.queuedAt)
          && Date.now() - data.queuedAt > ttl) {
        reply(data, {
          ok: false,
          action: (data.command && data.command.action) || '',
          message: 'jukebox_request_expired'
        });
        return;
      }
      let result;
      try {
        result = await Jukebox.executeControl(data.command || {});
      } catch (error) {
        result = {
          ok: false,
          action: (data.command && data.command.action) || '',
          message: String((error && error.message) || error)
        };
      }
      reply(data, result);
    };

    // onmessage 是 async 的，浏览器不会替你串行化：drain 里每条都 await serve，
    // 这段等待期间新到的请求会直接执行、插到攒着的那些前面。所有指令统一走这条
    // promise 链，到达顺序才作数。
    let serveChain = Promise.resolve();
    const enqueueServe = (data) => {
      serveChain = serveChain.then(() => serve(data), () => serve(data));
      return serveChain;
    };

    channel.onmessage = async (event) => {
      const data = event && event.data;
      if (!data || typeof data !== 'object') return;
      if (data.type === 'jukebox_owner_query') {
        announce();
        return;
      }
      if (data.type === 'jukebox_cancel_request') {
        // 独立的取消信号：它必须能越过正在执行的那条指令，所以不走
        // jukebox_control_request 的路径，直接就地作废在途播放。
        //
        // 排队中的播放指令也要一并作废，两条队列都得覆盖：还没就绪时攒在
        // controlOwnerPending 里的，以及就绪后已经挂上 serveChain、再也摘不下来
        // 的。后者由 serve 自己在开跑前退出，靠的就是这个世代。
        //
        // set_volume / set_mode 从不记世代，所以两条路上都不会被顺手丢掉 ——
        // 它们跟这次取消无关，整队丢掉等于把用户先发的音量调整也吞了。
        // 攒着的那些还要逐条回执，否则调用方的 forwardControl 会一直占着队列
        // 干等超时。
        // 只有绝对指令（stop / play）才顶替排队中的播放指令。next / previous
        // 同样会发这个信号来作废「在途」的那条，但它们是相对当前曲目算的，把排
        // 在前面还没开跑的那条吞掉，它们算的就是旧位置了 —— 判据必须和发件侧
        // 一模一样。
        //
        // 「相对」就是「可被顶替但自己不顶替」，由那两张表推出来，不另立第三张。
        // 认不出发起动作时按绝对取消处理：漏顶替的代价是用户喊停之后声音又起来，
        // 比多顶替一次严重得多。
        const cancelAction = String(data.action || '').trim().toLowerCase();
        const cancelIsRelative = Jukebox.SUPERSEDABLE_CONTROL_ACTIONS.includes(cancelAction)
          && !Jukebox.SUPERSEDING_CONTROL_ACTIONS.includes(cancelAction);
        if (!cancelIsRelative) {
          Jukebox.State.controlOwnerSupersedeGeneration += 1;
        }
        const keep = [];
        for (const queued of Jukebox.State.controlOwnerPending) {
          if (Jukebox.isControlOwnerRequestSuperseded(queued)) {
            reply(queued, {
              ok: false,
              action: String((queued.command && queued.command.action) || '').trim().toLowerCase(),
              message: 'play_cancelled'
            });
          } else {
            keep.push(queued);
          }
        }
        Jukebox.State.controlOwnerPending = keep;
        Jukebox.cancelActivePlayback({ silenceAudio: !cancelIsRelative });
        return;
      }
      if (data.type !== 'jukebox_control_request') return;

      data.queuedAt = Date.now();
      Jukebox.stampControlOwnerRequest(data);
      if (!Jukebox.State.controlOwnerReady) {
        // 窗口已经宣告归属，但曲库还没拉完、播放器还没建好。直接执行会用默认的
        // live2d 去选动作、把该跳的舞跳过去；直接不接又会让角色窗口自己起一个
        // 隐藏运行时。先攒着，就绪时按到达顺序放出去。
        Jukebox.State.controlOwnerPending.push(data);
        return;
      }
      enqueueServe(data);
    };

    Jukebox.drainControlOwnerQueue = function() {
      // 同步地把攒着的按顺序挂上链子：此后到达的新请求只会排在它们后面。
      const queued = Jukebox.State.controlOwnerPending;
      Jukebox.State.controlOwnerPending = [];
      for (const data of queued) {
        enqueueServe(data);
      }
      return serveChain;
    };

    announce();
    Jukebox.State.controlOwnerHeartbeatTimer = setInterval(announce, Jukebox.CONTROL_OWNER_HEARTBEAT_MS);
    window.addEventListener('beforeunload', Jukebox.stopControlOwnerService);
    return true;
  },

  // 拥有者侧的顶替语义，与发件侧（app-websocket.js）同一套「绝对 / 相对」：
  //   绝对——stop 要静音、play 点名要这首，排在它们前面还没开跑的播放指令作废；
  //   相对——next / previous 是相对当前曲目算的，只被顶替、不顶替别人，否则它算
  //   的就是被吞掉那条本该替换掉的旧位置。
  // 两张表都不含 set_volume / set_mode：它们既不顶替也不被顶替。
  SUPERSEDING_CONTROL_ACTIONS: ['stop', 'play'],
  SUPERSEDABLE_CONTROL_ACTIONS: ['play', 'next', 'previous'],

  stampControlOwnerRequest: function(data) {
    const action = String((data.command && data.command.action) || '').trim().toLowerCase();
    // 自增在前、取号在后：顶替者自己记的是新世代，不会被自己顶掉。
    if (Jukebox.SUPERSEDING_CONTROL_ACTIONS.includes(action)) {
      Jukebox.State.controlOwnerSupersedeGeneration += 1;
    }
    if (Jukebox.SUPERSEDABLE_CONTROL_ACTIONS.includes(action)) {
      data.supersedeGeneration = Jukebox.State.controlOwnerSupersedeGeneration;
    }
  },

  // 随机队列「投机性前进」的回滚器。选下一首这一步就已经推进了队列位置（或往
  // 队尾追加），可这首未必播得起来；没播起来就得把位置放回去，否则下一次导航从
  // 错的地方继续、跳过一首。
  //
  // 归属判据是队列本身：只有它仍然停在「这次前进留下的样子」时才回滚。不能用
  // playRequestId ——
  //   起播失败时会先结清待机欠账，而结账里的 restoreIdleAnimation 自己就
  //   ++playRequestId，用它当判据的话回滚在唯一该生效的路径上永远不生效；
  //   而接手的那条 play 已经围绕新曲目重置了队列，硬把旧快照盖回去会抹掉它的
  //   历史。别的请求接手一定会重置队列，那才是「谁拥有它」的事实。
  //
  // anchorSongId 只有自动续播那条要传：getNextSongToPlay 在前进之前会先做一次
  // 锚点修复（队列与刚播完那首脱节时重置到它），那是既成事实，回滚不该连它一起
  // 撤掉，所以回滚之后再幂等地重做一次。
  beginRandomQueueRollback: function(options = {}) {
    const noop = { markAdvanced: function() {}, restore: function() {} };
    if (Jukebox.State.playbackMode !== 'random') return noop;

    const anchorSongId = options.anchorSongId || null;
    const before = {
      queue: (Jukebox.State.randomQueue || []).slice(),
      index: Jukebox.State.randomQueueIndex
    };
    let advanced = null;

    return {
      markAdvanced: function() {
        advanced = {
          queue: (Jukebox.State.randomQueue || []).slice(),
          index: Jukebox.State.randomQueueIndex
        };
      },
      restore: function() {
        if (!advanced) return;
        const queue = Jukebox.State.randomQueue || [];
        if (Jukebox.State.randomQueueIndex !== advanced.index) return;
        if (queue.length !== advanced.queue.length) return;
        if (!queue.every(function(songId, i) { return songId === advanced.queue[i]; })) return;
        Jukebox.State.randomQueue = before.queue;
        Jukebox.State.randomQueueIndex = before.index;
        if (anchorSongId) Jukebox.ensureRandomQueueAnchor(anchorSongId);
      }
    };
  },

  isControlOwnerRequestSuperseded: function(data) {
    return !!data
      && data.supersedeGeneration != null
      && data.supersedeGeneration !== Jukebox.State.controlOwnerSupersedeGeneration;
  },

  markControlOwnerReady: function() {
    if (!window.__NEKO_JUKEBOX_STANDALONE__) return false;
    if (Jukebox.State.controlOwnerReady) return false;
    // 顺序保证来自 drain 是同步入链的：它把攒着的按序挂上 serveChain，之后到达
    // 的请求只能排在后面。这两句谁先谁后其实不产生差别（中间没有 await），
    // 这么写只是把「先接管旧的、再接受新的」摆明；真正的不变量在 serveChain。
    if (typeof Jukebox.drainControlOwnerQueue === 'function') {
      Jukebox.drainControlOwnerQueue();
    }
    Jukebox.State.controlOwnerReady = true;
    return true;
  },

  stopControlOwnerService: function() {
    const state = Jukebox.State;
    state.controlOwnerReady = false;
    // 攒着还没服务的那些必须逐条回执再清空：调用方的 forwardControl 在等 promise，
    // 而它的整条指令队列都排在那个 promise 后面 —— 静默丢弃等于让本地执行也停摆
    // 到 TTL 到期为止。
    const abandoned = state.controlOwnerPending;
    state.controlOwnerPending = [];
    if (state.controlOwnerChannel && abandoned.length) {
      for (const queued of abandoned) {
        try {
          state.controlOwnerChannel.postMessage({
            type: 'jukebox_control_result',
            requestId: queued.requestId,
            result: {
              ok: false,
              action: String((queued.command && queued.command.action) || ''),
              message: 'jukebox_owner_gone'
            }
          });
        } catch (_) {}
      }
    }
    if (state.controlOwnerHeartbeatTimer) {
      clearInterval(state.controlOwnerHeartbeatTimer);
      state.controlOwnerHeartbeatTimer = null;
    }
    if (state.controlOwnerChannel) {
      try {
        // 主动告知对端我退出了，免得它们等 TTL 到期才把指令转回本地。
        state.controlOwnerChannel.postMessage({ type: 'jukebox_owner_gone' });
      } catch (_) {}
      try {
        state.controlOwnerChannel.onmessage = null;
        state.controlOwnerChannel.close();
      } catch (_) {}
      state.controlOwnerChannel = null;
    }
  },

  // 控制面刷新曲库之后，面板开着就得跟着重渲染：refresh 会换掉 State.songs 并
  // 推进 configRevision，而 10 秒轮询的 checkConfigUpdates 之后只会看到「已经是
  // 最新版本」，再也不会自己去 loadSongs —— 面板就永久停在旧的那批行上。
  refreshLibraryForControl: async function() {
    await Jukebox.loadSongData();
    if (Jukebox.State.isOpen) {
      Jukebox.renderList();
    }
  },

  isControlEpochCurrent: function(epoch) {
    return epoch === Jukebox.State.teardownEpoch;
  },

  executeControl: async function(command = {}) {
    const normalizedAction = String(command.action || '').trim().toLowerCase();
    // 用户可能在这条指令的任何一个 await 里把点歌台整个拆掉。
    const epoch = Jukebox.State.teardownEpoch;
    // 取消世代要在**进入函数时**就取快照：本条指令后面的每一个 await
    // （ensureRuntime、曲目检索、曲库刷新）都可能跨过一次 stop。
    const cancelEpoch = Jukebox.State.playCancelEpoch;

    if (!Jukebox.supportedControlActions.includes(normalizedAction)) {
      return {
        ok: false,
        action: normalizedAction,
        message: 'unsupported_jukebox_action'
      };
    }

    if (normalizedAction === 'stop') {
      // 拥有者一侧的 stop 直接走到这里（没有前置的 cancelActivePlayback），
      // 所以取消世代必须在这里也推进，否则那边的 stop 拦不住在途的 play。
      Jukebox.State.playCancelEpoch += 1;
      Jukebox.State.playRequestId += 1;
      Jukebox.stopPlayback();
      return { ok: true, action: 'stop' };
    }

    if (normalizedAction === 'set_mode') {
      return Jukebox.executeSetModeControl(command.mode);
    }

    if (normalizedAction === 'set_volume') {
      await Jukebox.ensureRuntime({ headless: command.headless !== false });
      if (!Jukebox.isControlEpochCurrent(epoch)) return Jukebox.tornDownResult(normalizedAction);
      return Jukebox.executeSetVolumeControl(command.value);
    }

    if (normalizedAction === 'adjust_volume') {
      await Jukebox.ensureRuntime({ headless: command.headless !== false });
      if (!Jukebox.isControlEpochCurrent(epoch)) return Jukebox.tornDownResult(normalizedAction);
      return Jukebox.executeAdjustVolumeControl(command.value);
    }

    await Jukebox.ensureRuntime({ headless: command.headless !== false });
    if (!Jukebox.isControlEpochCurrent(epoch)) return Jukebox.tornDownResult(normalizedAction);
    // 这里刻意不再单独查一次取消世代：下面 play 路径查完才走 executePlayControl，
    // next/previous 则由 executePlayControl 自己在铸世代之前查，两条路都覆盖到了。
    // 多一道查不出任何行为差异的分支，只会变成没人钉得住的死代码。

    if (normalizedAction === 'next' || normalizedAction === 'previous') {
      const direction = normalizedAction === 'previous' ? -1 : 1;
      const rollback = Jukebox.beginRandomQueueRollback();

      const adjacentSong = Jukebox.State.playbackMode === 'random'
        ? Jukebox.getRandomAdjacentSong(direction)
        : Jukebox.getManualAdjacentSong(direction);
      rollback.markAdvanced();
      if (!adjacentSong) {
        rollback.restore();
        return {
          ok: false,
          action: normalizedAction,
          message: normalizedAction === 'previous' ? 'no_previous_song' : 'no_next_song'
        };
      }
      const outcome = await Jukebox.executePlayControl(normalizedAction, adjacentSong, {
        fromQueue: Jukebox.State.playbackMode === 'random',
        epoch,
        cancelEpoch
      });
      if (!outcome || outcome.ok !== true) {
        rollback.restore();
      }
      return outcome;
    }

    let refreshedLibrary = false;
    let song = await Jukebox.findSongForQuery(command.query || '');
    if (!song) {
      // 运行时是记忆化的，无头会话里可能一直用着开机时那份曲库；只有在真的
      // 找不到时才多花一次请求重新拉，避免每条指令都重拉。这里不能再加
      // 「songs 非空」前置条件：运行时初始化那一刻曲库为空的话，那个条件会让
      // 之后每一条 play 都直接 song_not_found，永远等不到刷新。
      await Jukebox.refreshLibraryForControl();
      refreshedLibrary = true;
      song = await Jukebox.findSongForQuery(command.query || '');
    }
    if (!Jukebox.isControlEpochCurrent(epoch)) return Jukebox.tornDownResult('play');
    // 检索/刷新期间来的 stop 由 executePlayControl 的契约闸拦下（它到那里之间
    // 没有 await），这里不再重复一道钉不住的检查。
    if (!song) {
      return { ok: false, action: 'play', message: 'song_not_found' };
    }

    let outcome = await Jukebox.executePlayControl('play', song, { epoch, cancelEpoch });
    // 「搜到了」不等于「还在」：缓存里那首可能已经被删掉或换了音频路径，预检
    // 因此失败。只在没搜到时刷新是不够的——陈旧的命中会让之后每一条 play 都
    // 卡在同一个 audio_not_found 上，永远等不到刷新。
    const staleMatch = outcome && outcome.ok !== true
      && (outcome.message === 'audio_not_found' || outcome.message === 'audio_missing');
    if (staleMatch && !refreshedLibrary) {
      await Jukebox.refreshLibraryForControl();
      if (!Jukebox.isControlEpochCurrent(epoch)) return Jukebox.tornDownResult('play');
      const refreshedSong = await Jukebox.findSongForQuery(command.query || '');
      if (refreshedSong) {
        outcome = await Jukebox.executePlayControl('play', refreshedSong, { epoch, cancelEpoch });
      } else {
        outcome = { ok: false, action: 'play', message: 'song_not_found' };
      }
    }
    return outcome;
  },

  // 0-1 和 0-100 两套量纲共存时，判据不能是「是否大于 1」：那样 value:1 会被
  // 当成满量程（100%），value:2 却只有 2%，请求更大反而结果小 50 倍，而 1 恰恰
  // 是模型说「调大一点」时最常给的数。改成按区间分：(0,1) 之间是比例，其余
  // 一律按百分点，于是 1→1%、2→2%、0.5→50%，1 到 100 之间单调。
  toVolumeRatio: function(numberValue) {
    const magnitude = Math.abs(numberValue);
    return magnitude > 0 && magnitude < 1 ? numberValue : numberValue / 100;
  },

  normalizeControlVolume: function(value) {
    if (value === null || value === undefined || value === '') return null;
    const numberValue = Number(value);
    if (!Number.isFinite(numberValue)) return null;
    if (numberValue < 0 || numberValue > 100) return null;
    return Math.max(0, Math.min(1, Jukebox.toVolumeRatio(numberValue)));
  },

  normalizeControlVolumeDelta: function(value) {
    if (value === null || value === undefined || value === '') return null;
    const numberValue = Number(value);
    if (!Number.isFinite(numberValue)) return null;
    if (numberValue < -100 || numberValue > 100) return null;
    return Math.max(-1, Math.min(1, Jukebox.toVolumeRatio(numberValue)));
  },

  getCurrentVolume: function() {
    const player = Jukebox.getPlayer();
    const playerVolume = player && player.audio ? Number(player.audio.volume) : NaN;
    if (Number.isFinite(playerVolume)) {
      return Math.max(0, Math.min(1, playerVolume));
    }
    if (Jukebox.State.isMuted) return 0;
    const savedVolume = Number(Jukebox.State.savedVolume);
    return Number.isFinite(savedVolume) ? Math.max(0, Math.min(1, savedVolume)) : 1;
  },

  setRuntimeVolume: function(volume) {
    const normalizedVolume = Math.max(0, Math.min(1, Number(volume)));
    if (!Number.isFinite(normalizedVolume)) return false;

    const player = Jukebox.getPlayer();
    if (player && typeof player.volume === 'function') {
      player.volume(normalizedVolume);
    } else if (player && player.audio) {
      player.audio.volume = normalizedVolume;
    } else {
      return false;
    }

    const volumeSlider = document.getElementById('jukebox-volume-slider');
    if (volumeSlider) {
      volumeSlider.value = normalizedVolume;
    }

    if (normalizedVolume > 0) {
      Jukebox.State.isMuted = false;
      Jukebox.State.savedVolume = normalizedVolume;
    } else {
      Jukebox.State.isMuted = true;
    }

    Jukebox.updateVolumeDisplay(normalizedVolume);
    return true;
  },

  executeSetVolumeControl: function(value) {
    const volume = Jukebox.normalizeControlVolume(value);
    if (volume === null) {
      return { ok: false, action: 'set_volume', message: 'invalid_volume' };
    }
    if (!Jukebox.setRuntimeVolume(volume)) {
      return { ok: false, action: 'set_volume', message: 'volume_control_unavailable' };
    }
    return { ok: true, action: 'set_volume', volume };
  },

  executeAdjustVolumeControl: function(value) {
    const delta = Jukebox.normalizeControlVolumeDelta(value);
    if (delta === null) {
      return { ok: false, action: 'adjust_volume', message: 'invalid_volume_delta' };
    }
    const volume = Math.max(0, Math.min(1, Math.round((Jukebox.getCurrentVolume() + delta) * 100) / 100));
    if (!Jukebox.setRuntimeVolume(volume)) {
      return { ok: false, action: 'adjust_volume', message: 'volume_control_unavailable' };
    }
    return { ok: true, action: 'adjust_volume', volume, value: delta };
  },

  executeSetModeControl: function(mode) {
    const normalizedMode = String(mode || '').trim().toLowerCase();
    if (!Jukebox.getPlaybackModeOrder().includes(normalizedMode)) {
      return { ok: false, action: 'set_mode', message: 'invalid_playback_mode' };
    }
    Jukebox.setPlaybackMode(normalizedMode);
    return {
      ok: true,
      action: 'set_mode',
      mode: normalizedMode
    };
  },

  formatControlSong: function(song) {
    return song ? { id: song.id, name: song.name, artist: song.artist } : null;
  },

  isPlaybackRequestCurrent: function(requestId) {
    return !Number.isInteger(requestId) || requestId === Jukebox.State.playRequestId;
  },

  // 结清「欠着的待机恢复」：走到静止状态却还欠着账，就把待机接回去。
  // 所有放弃/失败/取消的路径最终都会经过 stopPlayback 或 playSong 的收尾，
  // 所以不需要每个提前返回各自记得这件事。
  settleIdleRestore: function() {
    if (!Jukebox.State.idleRestorePending) return false;
    Jukebox.restoreIdleAnimation();
    return true;
  },

  // 就地作废在途播放：推进世代让卡在 await 里的 play 在下一个检查处解开，
  // 同时把已经响起来的声音立刻停掉。stop 指令本身仍会按顺序再执行一次（幂等）。
  cancelActivePlayback: function(options = {}) {
    Jukebox.State.playCancelEpoch += 1;
    Jukebox.State.playRequestId += 1;
    // 相对导航（next / previous）只作废在途的那条，不动已经在响的声音：目标
    // 可能根本不存在 —— 随机历史的头部、空的播放列表 —— 那时这条指令是空操作，
    // 却已经把音乐停了。真选出了下一首时 playSong 自己会停掉当前这首。
    if (options.silenceAudio === false) return;
    // 保留导航锚点：随后那条指令自己会处置它 —— stop 走 stopPlayback 全清，
    // play 会围绕新曲目重置随机队列，而 next / previous 正需要它。
    Jukebox.stopPlayback({ preserveNavigationAnchor: true });
  },

  isPlayCancelEpochCurrent: function(cancelEpoch) {
    return cancelEpoch === Jukebox.State.playCancelEpoch;
  },

  cancelledResult: function(action, song) {
    const result = { ok: false, action, message: 'play_cancelled' };
    // 与 play_superseded 的形状保持一致：拿得到歌就带上，调用方（前端日志、
    // 插件返回值）不必为这两种同类结局分开处理。
    if (song) result.song = Jukebox.formatControlSong(song);
    return result;
  },

  tornDownResult: function(action) {
    return { ok: false, action, message: 'jukebox_torn_down' };
  },

  executePlayControl: async function(action, song, playOptions = {}) {
    const epoch = Number.isInteger(playOptions.epoch) ? playOptions.epoch : Jukebox.State.teardownEpoch;
    const cancelEpoch = Number.isInteger(playOptions.cancelEpoch)
      ? playOptions.cancelEpoch
      : Jukebox.State.playCancelEpoch;
    // 必须在 ++playRequestId 之前查：那一句会把取消推进的世代盖过去，之后
    // 的等值检查就成了自己跟自己比，取消形同没发生。
    if (!Jukebox.isPlayCancelEpochCurrent(cancelEpoch)) return Jukebox.cancelledResult(action, song);
    const requestId = ++Jukebox.State.playRequestId;
    const preflight = await Jukebox.preflightSongPlayback(song);
    if (!Jukebox.isControlEpochCurrent(epoch)) return Jukebox.tornDownResult(action);
    if (!Jukebox.isPlayCancelEpochCurrent(cancelEpoch)) return Jukebox.cancelledResult(action, song);
    if (requestId !== Jukebox.State.playRequestId) {
      return {
        ok: false,
        action,
        message: 'play_superseded',
        song: Jukebox.formatControlSong(song)
      };
    }
    if (!preflight.ok) {
      return {
        ok: false,
        action,
        message: preflight.message,
        song: Jukebox.formatControlSong(song)
      };
    }

    const playedSong = await Jukebox.playSong(song.id, {
      ...playOptions,
      epoch: undefined,
      // 把通过预检的那一版原样交下去，别让 playSong 拿 id 再解析一次。
      song,
      actionAvailability: preflight.actionAvailability,
      // next / previous 在单曲曲库下会绕回当前这首，走进 playSong 的「同曲即停」
      // 分支：音乐停了，却因为拿到了 song 对象而报 ok:true，猫娘照样说「已切歌」。
      // 「同曲即停」是面板上双击的交互，不是控制面的语义 —— 控制面来的三种动作
      // 都该无条件重新起播。
      forceReplay: true,
      requestId
    });
    // 收尾清理只能停「我自己起的那份声音」。这条请求在 await 里的时候可能已经有
    // 更新的一条接手并起播了（stop 之后紧跟一条新的 play 就是这个形态），那时
    // 无条件 stopPlayback() 停掉的是别人的播放。
    //
    // 判据试过两版都不对：
    //   requestId === playRequestId —— 取消本身也推进那个计数器，于是「没有接班者、
    //   只是被取消」会被误判成「别人接手了」，孤儿音频反而没人停；
    //   !State.currentSong —— 接班者的 playSong 起播期间会先把 currentSong 清空，
    //   旧请求这时醒来同样会误判成无人认领，照样停掉接班者。
    // 真正稳的是「这份音频是谁起的」：playAudio 一返回就认领，stopPlayback 清掉。
    const ownsCurrentAudio = Jukebox.State.audioOwnerRequestId === requestId;
    if (!Jukebox.isControlEpochCurrent(epoch)) {
      // 起播过程中点歌台被拆了：停掉刚起来的声音，别留下一个没人管的播放器。
      if (ownsCurrentAudio) Jukebox.stopPlayback();
      return Jukebox.tornDownResult(action);
    }
    if (!Jukebox.isPlayCancelEpochCurrent(cancelEpoch)) {
      // 起播过程中来过 stop：同样要把刚响起来的声音停掉。
      if (ownsCurrentAudio) Jukebox.stopPlayback();
      return Jukebox.cancelledResult(action, song);
    }
    if (!playedSong) {
      return {
        ok: false,
        action,
        message: 'play_failed',
        song: Jukebox.formatControlSong(song)
      };
    }

    return {
      ok: true,
      action,
      song: Jukebox.formatControlSong(song),
      actionStatus: preflight.actionAvailability.status
    };
  },

  shouldPreflightJukeboxUrl: function(url) {
    const value = String(url || '');
    if (!value) return false;
    if (/^(?:data:|blob:)/i.test(value)) return false;
    const jukeboxFilePrefix = '/api/jukebox/file' + '/';
    if (value.startsWith(jukeboxFilePrefix) || value.startsWith('/static/') || value.startsWith('/user_')) {
      return true;
    }
    try {
      const parsed = new URL(value, window.location.href);
      return parsed.origin === window.location.origin
        && (
          parsed.pathname.startsWith(jukeboxFilePrefix)
          || parsed.pathname.startsWith('/static/')
          || parsed.pathname.startsWith('/user_')
        );
    } catch (_) {
      return false;
    }
  },

  checkJukeboxFileAvailable: async function(url) {
    if (!url) return false;
    if (!Jukebox.shouldPreflightJukeboxUrl(url)) return true;

    try {
      const response = await fetch(url, { method: 'HEAD', cache: 'no-store' });
      return !!(response && response.ok);
    } catch (_) {
      return false;
    }
  },

  getActionAvailability: async function(song) {
    const action = Jukebox.getActionForModel(song);
    if (!action) {
      return { ok: true, status: 'no_action', action: null, url: '' };
    }

    const url = Jukebox.resolveJukeboxFileUrl(action.file || '');
    if (!url) {
      return { ok: false, status: 'action_missing', action, url: '' };
    }

    const available = await Jukebox.checkJukeboxFileAvailable(url);
    return {
      ok: available,
      status: available ? 'action_ready' : 'action_not_found',
      action,
      url
    };
  },

  preflightSongPlayback: async function(song) {
    const audioUrl = Jukebox.resolveJukeboxFileUrl(song && song.audio);
    if (!audioUrl) {
      return { ok: false, message: 'audio_missing', audioUrl: '' };
    }

    const audioAvailable = await Jukebox.checkJukeboxFileAvailable(audioUrl);
    if (!audioAvailable) {
      return { ok: false, message: 'audio_not_found', audioUrl };
    }

    const actionAvailability = await Jukebox.getActionAvailability(song);
    return { ok: true, audioUrl, actionAvailability };
  },

  checkConfigUpdates: async function() {
    const Jukebox = window.Jukebox || this;
    if (!Jukebox.State.isOpen || Jukebox.State.configPollInFlight) return;

    Jukebox.State.configPollInFlight = true;
    try {
      const response = await fetch('/api/jukebox/config/summary', { cache: 'no-store' });
      if (!response.ok) return;

      const summary = await response.json();
      const nextRevision = summary && summary.configRevision;
      if (!nextRevision) return;

      const currentRevision = Jukebox.State.configRevision;
      if (currentRevision && currentRevision !== nextRevision) {
        console.log('[Jukebox] 检测到歌单配置更新，重新加载歌曲');
        await Jukebox.loadSongs();
        if (Jukebox.SongActionManager && typeof Jukebox.SongActionManager.load === 'function') {
          await Jukebox.SongActionManager.load();
        }
      } else if (!currentRevision) {
        Jukebox.State.configRevision = nextRevision;
      }
    } catch (error) {
      console.warn('[Jukebox] 检查歌单更新失败:', error);
    } finally {
      Jukebox.State.configPollInFlight = false;
    }
  },

  loadSongData: async function() {
    const Jukebox = window.Jukebox || this;
    // 拉配置期间点歌台可能被整个拆掉、随后又被下一条指令重建。那种情况下这份
    // 响应属于上一个运行时：提交它会把接班运行时刚拉好的曲库覆盖成旧的，而且
    // 要等到下一次刷新才纠正得回来。守住槽位（只清自己那次初始化）挡得住第三次
    // 并发初始化，挡不住已经在飞的这一次把结果落盘。
    const epoch = Jukebox.State.teardownEpoch;
    // 从后端API加载配置
    const response = await fetch('/api/jukebox/config');
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const data = await response.json();

    if (Jukebox.State.teardownEpoch !== epoch) {
      console.log('[Jukebox] 曲库响应属于已拆除的运行时，丢弃');
      return Jukebox.State.songs;
    }

    // 保存完整的配置数据
    Jukebox.State.config = data;
    Jukebox.State.configRevision = data.configRevision || Jukebox.State.configRevision || null;

    // 将后端的歌曲对象转换为数组格式
    const songs = data.songs || {};
    const actions = data.actions || {};
    const bindings = data.bindings || {};

    Jukebox.State.songs = Jukebox.applySavedSongOrder(Object.entries(songs).map(([id, song]) => {
      // 获取该歌曲绑定的动画
      const songBindings = bindings[id] || {};
      const boundActions = Object.keys(songBindings)
        .filter(actionId => actions[actionId] && actions[actionId].visible !== false)
        .map(actionId => ({
          id: actionId,
          ...actions[actionId]
        })); // 过滤掉不存在或已隐藏的动画

      return {
        id: id,
        name: song.name || '未知',
        artist: song.artist || '未知',
        audio: song.audio || '',
        vmd: song.vmd || '',
        duration: song.duration || 0,
        visible: song.visible !== false, // 默认可见
        defaultAction: song.defaultAction || '',
        isBuiltin: song.isBuiltin || false, // 传递自带资源标记
        boundActions: boundActions // 绑定的动画列表
      };
    }).filter(song => song.visible)); // 只显示可见的歌曲

    console.log('[Jukebox]', window.t('Jukebox.songsLoaded', '歌曲列表已加载'), Jukebox.State.songs.length, '首歌曲');

    Jukebox.syncRandomQueueWithSongs();
    return Jukebox.State.songs;
  },

  loadSongs: async function() {
    const Jukebox = window.Jukebox || this;
    try {
      await Jukebox.loadSongData();
      Jukebox.renderList();
    } catch (error) {
      console.error('[Jukebox]', window.t('Jukebox.loadFailed', '加载歌曲列表失败'), error);
      Jukebox.showError(window.t('Jukebox.loadFailed', '加载歌曲列表失败') + ': ' + error.message);
    }
  },

  resolveJukeboxFileUrl: function(filePath) {
    const rawPath = String(filePath || '').trim();
    if (!rawPath) return '';
    if (/^(?:https?:|data:|blob:)/i.test(rawPath)) return rawPath;
    if (/^\/?static\/jukebox\//.test(rawPath)) {
      return '/api/jukebox/file/' + rawPath.replace(/^\/?static\/jukebox\//, '');
    }
    if (rawPath.startsWith('/api/') || rawPath.startsWith('/static/') || rawPath.startsWith('/user_')) {
      return rawPath;
    }
    return '/api/jukebox/file' + '/' + rawPath.replace(/^\/+/, '');
  },

  renderList: function() {
    const tbody = document.getElementById('jukebox-song-list');
    if (!tbody) {
      console.debug('[Jukebox]', window.t('Jukebox.listContainerNotFound', '歌曲列表容器不存在'));
      return;
    }

    if (Jukebox.State.songs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="loading">' + window.t('Jukebox.noSongs', '暂无歌曲') + '</td></tr>';
      Jukebox.State.songElements = {};
      return;
    }

    // 增量更新：只更新变化的歌曲，不重新创建正在播放的歌曲行
    const currentIds = new Set(Jukebox.State.songs.map(s => s.id));
    const existingIds = new Set(Object.keys(Jukebox.State.songElements));

    // 删除已经不存在的歌曲行
    for (const id of existingIds) {
      if (!currentIds.has(id)) {
        const row = Jukebox.State.songElements[id];
        if (row && row.parentNode) {
          row.remove();
        }
        delete Jukebox.State.songElements[id];
      }
    }

    // 删除"加载中..."提示行（如果有的话）
    const loadingRow = tbody.querySelector('tr .loading');
    if (loadingRow) {
      const loadingTr = loadingRow.closest('tr');
      if (loadingTr) {
        loadingTr.remove();
      }
    }

    // 创建、更新并按当前排序重新排列歌曲行
    Jukebox.State.songs.forEach((song, index) => {
      const existingRow = Jukebox.State.songElements[song.id];
      let row;

      if (existingRow) {
        // 更新现有行（只更新非播放状态的内容）
        Jukebox.updateSongRow(existingRow, song, index);
        row = existingRow;
      } else {
        // 创建新行
        row = Jukebox.createSongRow(song, index);
        Jukebox.State.songElements[song.id] = row;
      }
      tbody.appendChild(row);
    });

    console.log('[Jukebox]', window.t('Jukebox.songsRendered', '歌曲列表已渲染'));
    Jukebox.updatePlaybackModeButtons();
    Jukebox.updateSongSortLockControls();
    Jukebox.bindTextTooltips(tbody);
    Jukebox.scheduleMarqueeTextUpdate(tbody);
  },

  // 创建歌曲行
  createSongRow: function(song, index) {
    const tr = document.createElement('tr');
    tr.dataset.songId = song.id;
    tr.draggable = Jukebox.isSongSortUnlocked();
    tr.innerHTML = `
      <td class="song-index"><span class="song-index-number">${index + 1}</span></td>
      <td class="song-name" data-neko-marquee data-tooltip="${Jukebox.escapeAttr(song.name)}">${Jukebox.escapeHtml(song.name)}</td>
      <td class="song-artist" data-neko-marquee data-tooltip="${Jukebox.escapeAttr(song.artist)}">${Jukebox.escapeHtml(song.artist)}</td>
      <td class="song-action">
        <button class="play-btn" data-song-id="${Jukebox.escapeAttr(song.id)}" data-tooltip="${Jukebox.escapeAttr(window.t('Jukebox.play', '播放'))}">
          <svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>
        </button>
      </td>
    `;

    const btn = tr.querySelector('.play-btn');
    Jukebox.setupTooltip(btn, btn.dataset.tooltip);
    btn.addEventListener('click', () => {
      Jukebox_playSong(song.id);
    });
    Jukebox.bindSongRowDragEvents(tr);

    return tr;
  },

  // 更新歌曲行（只更新基本信息，不触碰播放按钮）
  updateSongRow: function(row, song, index) {
    // 更新序号
    const indexCell = row.querySelector('.song-index');
    if (indexCell) {
      const indexNumber = indexCell.querySelector('.song-index-number');
      if (indexNumber) {
        indexNumber.textContent = index + 1;
      } else {
        indexCell.textContent = index + 1;
      }
    }

    // 更新歌名
    const nameCell = row.querySelector('.song-name');
    if (nameCell) {
      nameCell.textContent = song.name;
      nameCell.dataset.tooltip = song.name;
      nameCell.removeAttribute('title');
    }

    // 更新歌手
    const artistCell = row.querySelector('.song-artist');
    if (artistCell) {
      artistCell.textContent = song.artist;
      artistCell.dataset.tooltip = song.artist;
      artistCell.removeAttribute('title');
    }

    Jukebox.scheduleMarqueeTextUpdate(row);

    // 注意：不更新播放按钮，以保持播放状态
  },

  bindSongRowDragEvents: function(row) {
    row.addEventListener('dragstart', (event) => {
      if (!Jukebox.isSongSortUnlocked()) {
        event.preventDefault();
        return;
      }
      if (event.target && event.target.closest('button, input, a, select, textarea')) {
        event.preventDefault();
        return;
      }
      Jukebox.State.draggedSongId = row.dataset.songId;
      row.classList.add('jukebox-row-dragging');
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', row.dataset.songId);
      }
    });

    row.addEventListener('dragover', (event) => {
      if (!Jukebox.isSongSortUnlocked()) return;
      if (!Jukebox.State.draggedSongId || Jukebox.State.draggedSongId === row.dataset.songId) return;
      event.preventDefault();
      const rect = row.getBoundingClientRect();
      const placeAfter = event.clientY > rect.top + rect.height / 2;
      row.classList.toggle('jukebox-row-drop-before', !placeAfter);
      row.classList.toggle('jukebox-row-drop-after', placeAfter);
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
    });

    row.addEventListener('dragleave', () => {
      row.classList.remove('jukebox-row-drop-before', 'jukebox-row-drop-after');
    });

    row.addEventListener('drop', (event) => {
      if (!Jukebox.isSongSortUnlocked()) return;
      if (!Jukebox.State.draggedSongId) return;
      event.preventDefault();
      const rect = row.getBoundingClientRect();
      const placeAfter = event.clientY > rect.top + rect.height / 2;
      const moved = Jukebox.moveSongInPlaylist(Jukebox.State.draggedSongId, row.dataset.songId, placeAfter);
      if (!moved) Jukebox.clearSongDragState();
    });

    row.addEventListener('dragend', () => {
      Jukebox.clearSongDragState();
    });
  },

  getNextSongToPlay: function(endedSong) {
    const songs = Jukebox.State.songs || [];
    if (!endedSong || songs.length === 0) return null;

    if (Jukebox.State.playbackMode === 'none') {
      Jukebox.expireRandomQueueIfPendingSongEnded(endedSong.id);
      return null;
    }

    if (Jukebox.State.playbackMode === 'single') {
      Jukebox.expireRandomQueueIfPendingSongEnded(endedSong.id);
      return songs.find(song => song.id === endedSong.id) || null;
    }

    if (Jukebox.State.playbackMode === 'random') {
      Jukebox.ensureRandomQueueAnchor(endedSong.id);
      return Jukebox.getRandomAdjacentSong(1, endedSong.id);
    }

    const currentIndex = songs.findIndex(song => song.id === endedSong.id);
    Jukebox.expireRandomQueueIfPendingSongEnded(endedSong.id);
    if (currentIndex >= 0 && currentIndex < songs.length - 1) {
      return songs[currentIndex + 1];
    }
    return null;
  },

  handleAudioEnded: function(player) {
    const endedSong = Jukebox.State.currentSong;
    console.log('[Jukebox]', window.t('Jukebox.audioEnded', '音频播放结束'), {
      isPlaying: Jukebox.State.isPlaying,
      currentSong: endedSong,
      playerLoop: player && player.options ? player.options.loop : undefined,
      playbackMode: Jukebox.State.playbackMode
    });

    // 与 next / previous 那条路径同一个回滚器：定时器有几个放弃出口，任何一个
    // 走掉都意味着这首从没播过，位置却已经落在了它身上 —— 退出随机模式时它会被
    // 记成 randomQueueExitSongId，再切回来就把这首没播过的当成当前曲目。
    // endedSong 可能为空：stopPlayback 清掉 currentSong 之后，播放器仍可能补发
    // 一次陈旧的 ended。这里不能整条早退 —— 下面的结账、状态清理和 UI 刷新在这
    // 种情况下照样该做（getNextSongToPlay 自己也是返回 null 而不是抛错）；不做
    // 的只是「推进队列」这件事，所以锚点传空即可，回滚器也就没什么可回滚的。
    const rollback = Jukebox.beginRandomQueueRollback({
      anchorSongId: endedSong && endedSong.id
    });
    const restoreRandomQueue = rollback.restore;

    const nextSong = Jukebox.getNextSongToPlay(endedSong);
    rollback.markAdvanced();
    const nextAction = nextSong ? Jukebox.getActionForModel(nextSong) : null;
    // 自动续播也是一条独立播放请求：先作废旧歌曲仍在加载的动作。没有接班动作时
    // stopVMD(false) 可能再推进一次世代来保护异步待机恢复，下面会取它的最终值。
    if (nextSong) Jukebox.State.playRequestId += 1;
    Jukebox.stopVMD(!!nextAction);
    Jukebox.State.isPlaying = false;
    Jukebox.State.isPaused = false;

    Jukebox.State.currentSong = null;
    Jukebox.updateStoppedStatus();

    if (nextSong) {
      // 旧歌曲与接班歌曲不能复用 runtime hold token，否则旧请求收尾时会误释放
      // 接班者；若上面的待机恢复又推进了世代，以推进后的值为准。
      const requestId = Jukebox.State.playRequestId;
      const scheduledMode = Jukebox.State.playbackMode;
      const fromQueue = scheduledMode === 'random';
      // 上面的 stopVMD(!!nextAction) 若跳过了待机恢复，那笔账已经记在
      // State.idleRestorePending 上。下面每个放弃的出口都只管 return：接不上动画
      // 的话，账由随后回到静止的那一方（stopPlayback / playSong 收尾）结清。
      setTimeout(() => {
        if (!Jukebox.State.isOpen && !Jukebox.State.isRuntimeReady && !window.__NEKO_JUKEBOX_STANDALONE__) {
          restoreRandomQueue();
          Jukebox.settleIdleRestore();
          return;
        }
        if (requestId !== Jukebox.State.playRequestId) {
          // 被新请求取代。接手的如果是另一次播放，它自己会接上动画并清账；
          // 如果是 stop，stopPlayback 已经把账结了。这里不必也不该抢着恢复。
          restoreRandomQueue();
          return;
        }
        // nextSong 是按排队时的模式选出来的，而 set_mode 不动 playRequestId。
        // 模式在这一个宏任务的空档里变了，这次自动续播就作废 —— 尤其是改成
        // none 之后不该再自动播下一首。
        if (Jukebox.State.playbackMode !== scheduledMode) {
          restoreRandomQueue();
          Jukebox.settleIdleRestore();
          return;
        }
        // playSong 是即发即忘的，它自己也可能失败（音频加载报错、自动播放被拦、
        // 曲目已不在），而队列位置早在 getNextSongToPlay 那一步就推进了。失败同样
        // 是「这首没播」，判据与上面三个放弃出口一致：只有这次自动续播仍然作数时
        // 才回滚，否则接手的那一方自己会安排。
        const abandonAutoAdvance = () => {
          // 队列的归属由 restoreRandomQueue 自己判定，所以它不受下面这道闸门
          // 约束 —— playSong 失败时先结清的待机欠账会推进 playRequestId，用它
          // 当判据的话，回滚在它唯一该生效的路径上永远不生效。
          restoreRandomQueue();
          if (requestId !== Jukebox.State.playRequestId) return;
          if (Jukebox.State.playbackMode !== scheduledMode) return;
          Jukebox.settleIdleRestore();
        };
        Promise.resolve(
          Jukebox.playSong(nextSong.id, { fromQueue, requestId, song: nextSong })
        ).then(
          playedSong => { if (!playedSong) abandonAutoAdvance(); },
          abandonAutoAdvance
        );
      }, 0);
    }
  },

  playSong: async function(songId, options = {}) {
    // 调用方已经拿着一份校验过的曲目对象时，用它，不要拿 id 去重新解析：面板的
    // 配置轮询会在预检的那几个 await 里刷掉 State.songs，重传或改绑定之后同一个
    // id 指向的已经是新一版，于是拿旧版的预检结论去加载新版的音频/动作。
    const song = (options.song && options.song.id === songId)
      ? options.song
      : Jukebox.State.songs.find(s => s.id === songId);
    if (!song) {
      console.error('[Jukebox]', window.t('Jukebox.notFound', '找不到歌曲'), songId);
      return null;
    }

    const forceReplay = options.forceReplay === true;
    if (Jukebox.State.currentSong && Jukebox.State.currentSong.id === songId && !forceReplay) {
      if (Jukebox.State.isPaused) {
        console.log('[Jukebox] 恢复暂停的歌曲:', song.name);
        Jukebox.togglePause();
        return song;
      }
      if (Jukebox.State.isPlaying) {
        if (options.fromQueue === true) {
          return song;
        }
        console.log('[Jukebox] 停止当前播放的歌曲:', song.name);
        Jukebox.stopPlayback();
        return song;
      }
    }

    if (Jukebox.State.playbackMode === 'random') {
      if (options.fromQueue === true) {
        Jukebox.ensureRandomQueueAnchor(songId);
      } else {
        Jukebox.resetRandomQueue(songId);
      }
    } else if (Jukebox.State.randomQueueExitSongId && Jukebox.State.randomQueueExitSongId !== songId) {
      Jukebox.clearRandomQueue();
    }

    // 没带 requestId 就是「用户在面板上按了什么」——行内播放键、上一首/下一首、
    // 播放键都走这条。这时要连取消世代一起推进：远端来的那条 play 可能正卡在
    // 模糊检索里还没取号，光推进 playRequestId 拦不住它，它醒来后会取一个更新的
    // 号，反过来把用户刚选的这首顶掉。用户永远赢。
    const isUserInitiated = !Number.isInteger(options.requestId);
    if (isUserInitiated) {
      Jukebox.State.playCancelEpoch += 1;
    }
    const requestId = isUserInitiated ? ++Jukebox.State.playRequestId : options.requestId;
    if (requestId !== Jukebox.State.playRequestId) {
      console.log('[Jukebox] 播放请求已被新请求取代，取消播放');
      return null;
    }

    console.log('[Jukebox] 播放歌曲:', song.name);

    const preserveRandomQueue = Jukebox.State.playbackMode === 'random'
      || (
        Jukebox.State.randomQueueExitSongId
        && Jukebox.State.randomQueueExitSongId === songId
      );
    // stopVMD 的待机恢复会自己 ++playRequestId 当作废令牌。换歌时那一下会把本次
    // 播放的世代顶掉，于是新歌起播后自己判定「已被取代」：动画不启动，控制面还报
    // play_failed。所以换歌一律跳过这次恢复 —— 它变成一笔欠账，由下面「接上了动画」
    // 或「收尾时还没接上」来结清。
    Jukebox.stopPlayback({ preserveRandomQueue, skipIdleRestore: true });

    try {
      await Jukebox.playAudio(song);
      // 音频此刻应该已经在响了。isPlaying 必须立刻为真：stopAudio 的 player.pause()
      // 挂在这个标志上，等到 playSong 末尾再置的话，动画加载期间来的 stop 会
      // 停不掉声音，还报成功。
      Jukebox.State.isPlaying = true;
      // 认领这份声音：收尾清理靠它区分「我起的」和「接班者起的」。
      // 判据是「还没人认领」，不是「我仍是当前世代」：取消和拆除也会推进
      // playRequestId，那两种情形下这份音频确实是我起的、得由我来收；而真有
      // 接班者时它已经先认领过了，这里就不能把它覆盖掉——否则收尾反而去停
      // 接班者的声音。stopPlayback 会把认领清空，所以两边都自洽。
      if (Jukebox.State.audioOwnerRequestId === null) {
        Jukebox.State.audioOwnerRequestId = requestId;
      }

      // APlayer 的 play() 不返回 promise，自动播放被拦时它把 NotAllowedError
      // 内部吞掉，await 照样立刻 resolve —— 于是没有声音却报成功。
      //
      // 决策：不等 play/playing 事件。那样必须配一个超时，而超时值在慢磁盘、
      // 长音频、冷缓存上都可能把正常起播误判成失败，代价比它要防的问题更大。
      // 这里只做一次「下一个 tick 回看播放器还停着吗」的判定：自动播放被拦时
      // 浏览器是同步拒绝、微任务里回到 paused，这一拍足够看见；正常起播则早已
      // paused === false。判不准时一律按成功放行，宁可漏报不要误报。
      if (!(await Jukebox.confirmAudioStarted())) {
        Jukebox.State.isPlaying = false;
        throw new Error('autoplay_blocked');
      }

      if (requestId !== Jukebox.State.playRequestId) {
        console.log('[Jukebox] 播放请求已被新请求取代，取消状态更新');
        return null;
      }

      // 声音已经在响了，这一刻起它就是「用户听到的那首」，也就是 next / previous
      // 的导航锚点。等动画那几个 await 走完再记的话，加载期间到达的相对导航会拿
      // 到一个空锚点，只能退回第一首 / 最后一首。
      Jukebox.State.currentSong = song;

      // 根据模型类型播放对应格式的动画；动作缺失时只跳过动作，不阻断歌曲播放。
      const actionAvailability = options.actionAvailability || await Jukebox.getActionAvailability(song);
      if (requestId !== Jukebox.State.playRequestId) {
        console.log('[Jukebox] 播放请求已被新请求取代，取消动画启动');
        return null;
      }
      const action = actionAvailability.action;
      // 文件可用不等于动画真的起来了：HEAD 预检过了，加载仍可能失败，或者
      // 模型管理器根本不在。要看播放方法的返回值，否则「动画没起来」也会被当成
      // 「接了动画」，收尾处就不会恢复待机，模型停在原地。
      let startsAnimation = false;
      if (action && actionAvailability.ok) {
        const actionUrl = actionAvailability.url;
        console.log('[Jukebox] 播放动画:', action.name, '格式:', action.format || 'vmd', '路径:', actionUrl);

        const modelType = Jukebox.getModelType();
        if (modelType === 'mmd' || modelType === 'live3d') {
          startsAnimation = (await Jukebox.playVMD(actionUrl, { requestId })) !== false;
        } else if (modelType === 'vrm') {
          startsAnimation = (await Jukebox.playVRMA(actionUrl, { requestId })) !== false;
        } else if (modelType === 'fbx') {
          startsAnimation = (await Jukebox.playFBX(actionUrl, { requestId })) !== false;
        }
      } else if (action) {
        console.warn('[Jukebox] 动作文件不可用，跳过动作:', actionAvailability.status, action.file || action.id);
      }

      if (requestId !== Jukebox.State.playRequestId) {
        console.log('[Jukebox] 播放请求已被新请求取代，取消状态更新');
        return null;
      }

      Jukebox.State.isPlaying = true;
      Jukebox.State.lastPlaybackReport = {
        song: Jukebox.formatControlSong(song),
        actionStatus: actionAvailability.status
      };

      Jukebox.updatePlayingStatus(song);
      Jukebox.updateCalibrationDisplay();
      if (startsAnimation) {
        // 新动画接上了，欠账两清。
        Jukebox.State.idleRestorePending = false;
      } else {
        // 没接上（这首没动作，或动画起不来）。所有世代检查都过完了，此时再恢复
        // 待机，它的 ++playRequestId 不会反过来作废本次播放。
        Jukebox.settleIdleRestore();
      }
      return song;
    } catch (error) {
      // 这次换歌把旧舞蹈停掉时跳过了待机恢复，起播却失败了（自动播放被拦、
      // 音频加载报错……）。没人会再接上动画，账必须在这里结掉，否则模型僵在
      // 舞蹈最后一帧。放在世代检查之前：被取代的情况下接手的那条自己会安排。
      if (requestId === Jukebox.State.playRequestId) {
        Jukebox.settleIdleRestore();
      }
      if (requestId !== Jukebox.State.playRequestId) {
        return null;
      }
      console.error('[Jukebox]', window.t('Jukebox.playFailed', '播放失败'), error);
      Jukebox.showError(window.t('Jukebox.playFailed', '播放失败') + ': ' + error.message);
      return null;
    }
  },

  confirmAudioStarted: async function() {
    const player = Jukebox.getPlayer();
    const audio = player && player.audio;
    // 拿不到 audio 元素就没法判断，按成功放行。
    if (!audio) return true;
    await new Promise(resolve => setTimeout(resolve, 0));
    return audio.paused !== true;
  },

  playAudio: async function(song) {
    const player = Jukebox.getPlayer();
    if (!player) {
      console.error('[Jukebox]', window.t('Jukebox.playError', '音乐播放器未初始化'));
      throw new Error(window.t('Jukebox.playError', '音乐播放器未初始化'));
    }

    player.list.clear();

    console.log('[Jukebox]', window.t('Jukebox.useAPlayer', '使用APlayer播放音频文件'));

    const audioUrl = Jukebox.resolveJukeboxFileUrl(song.audio);

    player.list.add([{
      name: song.name,
      artist: song.artist,
      url: audioUrl,
      cover: ''
    }]);

    player.options.loop = 'none';

    if (Jukebox.State.boundPlayer !== player) {
      player.on('ended', () => {
        Jukebox.handleAudioEnded(player);
      });
      Jukebox.State.boundPlayer = player;
    }

    player.play();

    console.log('[Jukebox]', window.t('Jukebox.startPlay', '开始播放音频'), song.audio);
  },

  playVMD: async function(vmdPath, options = {}) {
    const requestId = options.requestId;
    if (!Jukebox.isPlaybackRequestCurrent(requestId)) return false;

    // 独立窗口模式：通过 IPC 桥接到 Pet 窗口执行
    if (window.__NEKO_JUKEBOX_STANDALONE__ && window.nekoJukeboxBridge) {
      window.nekoJukeboxBridge.playVMD(vmdPath);
      Jukebox.State.isVMDPlaying = true;
      console.log('[Jukebox]', window.t('Jukebox.vmdPlayed', 'VMD 动画已播放'), '(IPC)', vmdPath);
      return true;
    }

    if (!window.mmdManager || !window.mmdManager.animationModule) {
      console.warn('[Jukebox]', window.t('Jukebox.vmdNotInit', 'MMD Manager 未初始化，跳过动画'));
      return false;
    }

    try {
      // 保存当前待机动画 URL（用于停止后恢复）
      // 只在未保存过待机动画 URL 时保存，避免被舞蹈 VMD 覆盖
      if (!Jukebox.State.savedIdleAnimationUrl && window.mmdManager.currentAnimationUrl) {
        Jukebox.State.savedIdleAnimationUrl = window.mmdManager.currentAnimationUrl;
      }

      Jukebox.stopVMD(true); // skipIdleRestore = true

      if (Jukebox.State.vrmMotionRuntimeToken !== null) {
        const releaseResult = Jukebox.releaseVrmMotionRuntime({
          resume: false,
          scheduleNext: false
        });
        if (releaseResult !== false) await releaseResult;
        if (!Jukebox.isPlaybackRequestCurrent(requestId)) return false;
      }

      await window.mmdManager.loadAnimation(vmdPath);
      if (!Jukebox.isPlaybackRequestCurrent(requestId)) return false;
      window.mmdManager.playAnimation('dance');

      Jukebox.State.isVMDPlaying = true;

      console.log('[Jukebox]', window.t('Jukebox.vmdPlayed', 'VMD 动画已播放'), vmdPath);
      return true;
    } catch (error) {
      console.error('[Jukebox]', window.t('Jukebox.vmdPlayFailed', 'VMD 播放失败'), error);
      return false;
    }
  },

  holdVrmMotionRuntime: function(requestId) {
    const runtime = window.NekoMotion;
    if (!runtime || typeof runtime.holdExternalPlayback !== 'function') return false;
    let holdRequest;
    try {
      holdRequest = runtime.holdExternalPlayback('jukebox', { token: requestId });
    } catch (error) {
      console.warn('[Jukebox] VRM 动作运行时占用失败，继续使用底层播放器:', error);
      return false;
    }
    return Promise.resolve(holdRequest).then(async function(held) {
      if (!Jukebox.isPlaybackRequestCurrent(requestId)) {
        // 旧加载请求晚到时，只能释放自己的 token。若新歌已经接管，同 owner
        // 的 token 已被替换，这次释放会成为无操作，不会误恢复待机。
        if (held === true && typeof runtime.releaseExternalPlayback === 'function') {
          await runtime.releaseExternalPlayback('jukebox', {
            token: requestId,
            resume: true
          });
        }
        return false;
      }
      if (held === true) {
        Jukebox.State.idleRestorePending = true;
        Jukebox.State.vrmMotionRuntimeToken = requestId;
      }
      return held === true;
    }).catch(function(error) {
      console.warn('[Jukebox] VRM 动作运行时占用失败，继续使用底层播放器:', error);
      return false;
    });
  },

  releaseVrmMotionRuntime: function(options = {}) {
    const runtime = window.NekoMotion;
    if (!runtime || typeof runtime.releaseExternalPlayback !== 'function') return false;
    const releaseOptions = { ...options };
    if (!Object.prototype.hasOwnProperty.call(releaseOptions, 'token') &&
        Jukebox.State.vrmMotionRuntimeToken !== null) {
      releaseOptions.token = Jukebox.State.vrmMotionRuntimeToken;
    }
    const releaseToken = Object.prototype.hasOwnProperty.call(releaseOptions, 'token')
      ? releaseOptions.token
      : null;
    let releaseRequest;
    try {
      releaseRequest = runtime.releaseExternalPlayback('jukebox', releaseOptions);
    } catch (error) {
      console.warn('[Jukebox] VRM 动作运行时释放失败，回退直接恢复待机:', error);
      return false;
    }
    return Promise.resolve(releaseRequest).then(function(released) {
      if (released === true && releaseToken !== null &&
          Jukebox.State.vrmMotionRuntimeToken === releaseToken) {
        Jukebox.State.vrmMotionRuntimeToken = null;
      }
      return released === true;
    }).catch(function(error) {
      console.warn('[Jukebox] VRM 动作运行时释放失败，回退直接恢复待机:', error);
      return false;
    });
  },

  // 播放 VRMA 动画（VRM 模型）
  playVRMA: async function(vrmaPath, options = {}) {
    const requestId = options.requestId;
    if (!Jukebox.isPlaybackRequestCurrent(requestId)) return false;

    // 独立窗口模式：复用 VMD 桥接通道发送到 Pet（Pet 侧根据模型类型分发）
    if (window.__NEKO_JUKEBOX_STANDALONE__ && window.nekoJukeboxBridge) {
      window.nekoJukeboxBridge.playVMD(vrmaPath);
      Jukebox.State.isVMDPlaying = true;
      console.log('[Jukebox] VRMA 动画已发送 (IPC):', vrmaPath);
      return true;
    }
    if (!window.vrmManager) {
      console.warn('[Jukebox] VRM Manager 未初始化，跳过动画');
      return false;
    }

    let motionRuntimeToken = null;
    let motionRuntimeHeld = false;
    try {
      console.log('[Jukebox] 播放 VRMA 动画:', vrmaPath);

      Jukebox.stopVMD(true); // 停止之前的舞蹈动画
      const playRequestId = Jukebox.State.playRequestId;
      motionRuntimeToken = playRequestId;
      // 同一个 owner 的新 token 会在运行时内原子替换旧 token。先释放再占用会在
      // 冷启动时等待完整初始化，让接班舞蹈落后于已经开始的音频。
      const holdResult = Jukebox.holdVrmMotionRuntime(playRequestId);
      motionRuntimeHeld = holdResult === false ? false : await holdResult;
      if (!Jukebox.isPlaybackRequestCurrent(requestId)) return false;

      const releaseFailedStart = async function() {
        if (!motionRuntimeHeld) return false;
        const released = await Jukebox.releaseVrmMotionRuntime({
          token: playRequestId,
          resume: true
        });
        if (released === true && Jukebox.isPlaybackRequestCurrent(requestId)) {
          Jukebox.State.idleRestorePending = false;
        }
        return released;
      };

      // 使用 VRMManager 播放 VRMA（manager 内部会确保 animation 模块已初始化）
      const animationStarted = await window.vrmManager.playVRMAAnimation(vrmaPath, {
        loop: false,
        fadeInDuration: 0.5,
        fadeOutDuration: 0.5,
        shouldStart: () => Jukebox.isPlaybackRequestCurrent(requestId)
      });
      if (animationStarted !== true) {
        await releaseFailedStart();
        return false;
      }
      if (playRequestId !== Jukebox.State.playRequestId || !Jukebox.isPlaybackRequestCurrent(requestId)) {
        await releaseFailedStart();
        return false;
      }
      Jukebox.State.isVMDPlaying = true;
      console.log('[Jukebox] VRMA 动画已播放:', vrmaPath);
      return true;
    } catch (error) {
      console.error('[Jukebox] VRMA 播放失败:', error);
      // playVRMA 可能在 holdExternalPlayback 之后的加载/解析阶段失败。对应 token
      // 仍属于本请求时释放它，避免待机轮换被永久暂停。
      if (motionRuntimeHeld) {
        const released = await Jukebox.releaseVrmMotionRuntime({
          token: motionRuntimeToken,
          resume: true
        });
        if (released === true && Jukebox.isPlaybackRequestCurrent(requestId)) {
          Jukebox.State.idleRestorePending = false;
        }
      }
      return false;
    }
  },

  // 播放 FBX 动画（FBX 模型）
  playFBX: async function(fbxPath, options = {}) {
    const requestId = options.requestId;
    if (!Jukebox.isPlaybackRequestCurrent(requestId)) return false;

    if (!window.fbxManager) {
      console.warn('[Jukebox] FBX Manager 未初始化，跳过动画');
      return false;
    }

    try {
      console.log('[Jukebox] 播放 FBX 动画:', fbxPath);
      // TODO: 实现 FBX 模型的动画播放
      // 这里需要根据 FBXManager 的实际 API 来实现
      // await window.fbxManager.loadAnimation(fbxPath);
      // window.fbxManager.playAnimation();
      console.warn('[Jukebox] FBX 动画播放尚未实现');
      // 返回 false：这里一帧都没播。报 true 的话 playSong 会当成「新动画接上了」
      // 并把待机欠账清掉，于是打断了旧舞蹈、又没有新动画，模型僵在原地。
      return false;
    } catch (error) {
      console.error('[Jukebox] FBX 播放失败:', error);
      return false;
    }
  },

  updateVolume: function(value) {
    const volume = parseFloat(value);
    if (!Number.isFinite(volume)) return;
    if (Jukebox.setRuntimeVolume(volume)) return;

    // 面板的 buildUI 是同步建好的，initPlayer 却在 open() 里 100ms 的 setTimeout
    // 之后才跑。这段窗口里拖滑条 setRuntimeVolume 会因为没有 player 直接返回
    // false，百分比标签就此僵在旧值。播放器还没来之前也要给反馈，并把音量记下来。
    const clampedVolume = Math.max(0, Math.min(1, volume));
    if (clampedVolume > 0) {
      Jukebox.State.isMuted = false;
      Jukebox.State.savedVolume = clampedVolume;
    } else {
      Jukebox.State.isMuted = true;
    }
    // 记成「待应用」：播放器建出来时按它来，冷启动没拖过就别去动 APlayer 自己
    // 持久化的音量。
    Jukebox.State.pendingVolume = clampedVolume;
    const volumeSlider = document.getElementById('jukebox-volume-slider');
    if (volumeSlider) {
      volumeSlider.value = clampedVolume;
    }
    Jukebox.updateVolumeDisplay(clampedVolume);
  },

  logVolumeChange: function(value) {
    const volume = parseFloat(value);
    console.log('[Jukebox]', window.t('Jukebox.volumeSet', '音量已设置为'), volume, '(' + Math.round(volume * 100) + '%)');
  },

  initVolumeSlider: function() {
    const player = Jukebox.getPlayer();
    const volumeSlider = document.getElementById('jukebox-volume-slider');

    if (player && volumeSlider) {
      volumeSlider.value = player.audio.volume;
      const volumeValue = document.getElementById('jukebox-volume-value');
      if (volumeValue) {
        volumeValue.textContent = Math.round(player.audio.volume * 100) + '%';
      }
      console.log('[Jukebox] 音量滑条已初始化，当前音量:', player.audio.volume);
    }

    const speakerBtn = document.getElementById('jukebox-speaker-btn');
    if (speakerBtn) {
      speakerBtn.addEventListener('click', Jukebox.toggleMute);
    }

    const volumeValueEl = document.getElementById('jukebox-volume-value');
    if (volumeValueEl) {
      volumeValueEl.addEventListener('click', Jukebox.startVolumeEdit);
    }

    Jukebox.bindVolumeWheel();
  },

  bindVolumeWheel: function() {
    const volumeWrapper = document.querySelector('.jukebox-volume-wrapper');
    if (!volumeWrapper || volumeWrapper.dataset.wheelBound === 'true') return;

    volumeWrapper.dataset.wheelBound = 'true';
    volumeWrapper.addEventListener('wheel', Jukebox.handleVolumeWheel, { passive: false });
  },

  handleVolumeWheel: function(e) {
    if (!e) return;

    e.preventDefault();
    e.stopPropagation();

    if (e.deltaY === 0) return;

    const volumeSlider = document.getElementById('jukebox-volume-slider');
    const player = Jukebox.getPlayer();
    const sliderVolume = volumeSlider ? parseFloat(volumeSlider.value) : NaN;
    const playerVolume = player && player.audio ? parseFloat(player.audio.volume) : NaN;
    const fallbackVolume = Jukebox.State.isMuted ? 0 : (Jukebox.State.savedVolume || 1);
    const currentVolume = Number.isFinite(sliderVolume)
      ? sliderVolume
      : (Number.isFinite(playerVolume) ? playerVolume : fallbackVolume);
    const wheelStep = 0.05;
    const nextVolume = Math.max(0, Math.min(1, Math.round((currentVolume + (e.deltaY < 0 ? wheelStep : -wheelStep)) * 100) / 100));

    if (volumeSlider) {
      volumeSlider.value = nextVolume;
    }

    Jukebox.updateVolume(nextVolume);
  },

  startVolumeEdit: function() {
    const volumeValueEl = document.getElementById('jukebox-volume-value');
    if (!volumeValueEl || volumeValueEl.dataset.editing === 'true') return;

    const currentVolume = Math.round((Jukebox.State.isMuted ? Jukebox.State.savedVolume : (Jukebox.getPlayer()?.audio?.volume || 1)) * 100);

    volumeValueEl.dataset.editing = 'true';
    volumeValueEl.innerHTML = `<input type="text" class="jukebox-volume-input" value="${currentVolume}" maxlength="3">`;

    const input = volumeValueEl.querySelector('.jukebox-volume-input');
    if (input) {
      input.focus();
      input.select();

      input.addEventListener('keydown', Jukebox.handleVolumeInputKeydown);
      input.addEventListener('blur', Jukebox.confirmVolumeEdit);
      input.addEventListener('input', Jukebox.filterVolumeInput);
    }
  },

  filterVolumeInput: function(e) {
    const input = e.target;
    input.value = input.value.replace(/[^0-9]/g, '');
  },

  handleVolumeInputKeydown: function(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      e.target.blur();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      Jukebox.cancelVolumeEdit();
    }
  },

  confirmVolumeEdit: function(e) {
    const volumeValueEl = document.getElementById('jukebox-volume-value');
    if (!volumeValueEl || volumeValueEl.dataset.editing !== 'true') return;

    const input = e.target;
    const inputValue = input.value.trim();

    if (inputValue === '') {
      Jukebox.cancelVolumeEdit();
      return;
    }

    let newVolume = parseInt(inputValue, 10);
    if (isNaN(newVolume)) {
      Jukebox.cancelVolumeEdit();
      return;
    }

    newVolume = Math.max(0, Math.min(100, newVolume));
    const normalizedVolume = newVolume / 100;

    const player = Jukebox.getPlayer();
    if (player) {
      player.volume(normalizedVolume);
    }

    const volumeSlider = document.getElementById('jukebox-volume-slider');
    if (volumeSlider) {
      volumeSlider.value = normalizedVolume;
    }

    if (normalizedVolume > 0 && Jukebox.State.isMuted) {
      Jukebox.State.isMuted = false;
      Jukebox.State.savedVolume = normalizedVolume;
    }

    volumeValueEl.dataset.editing = 'false';
    volumeValueEl.textContent = newVolume + '%';
    Jukebox.updateSpeakerIcon(normalizedVolume === 0);
  },

  cancelVolumeEdit: function() {
    const volumeValueEl = document.getElementById('jukebox-volume-value');
    if (!volumeValueEl) return;

    const currentVolume = Math.round((Jukebox.State.isMuted ? Jukebox.State.savedVolume : (Jukebox.getPlayer()?.audio?.volume || 1)) * 100);
    volumeValueEl.dataset.editing = 'false';
    volumeValueEl.textContent = currentVolume + '%';
  },

  toggleMute: function() {
    const player = Jukebox.getPlayer();
    const volumeSlider = document.getElementById('jukebox-volume-slider');

    if (Jukebox.State.isMuted) {
      Jukebox.State.isMuted = false;
      if (player && player.audio) {
        player.audio.volume = Jukebox.State.savedVolume;
      }
      if (volumeSlider) {
        volumeSlider.value = Jukebox.State.savedVolume;
      }
      Jukebox.updateVolumeDisplay(Jukebox.State.savedVolume);
      Jukebox.updateSpeakerIcon(false);
    } else {
      Jukebox.State.savedVolume = player && player.audio ? player.audio.volume : 1;
      Jukebox.State.isMuted = true;
      if (player && player.audio) {
        player.audio.volume = 0;
      }
      if (volumeSlider) {
        volumeSlider.value = 0;
      }
      Jukebox.updateVolumeDisplay(0);
      Jukebox.updateSpeakerIcon(true);
    }
  },

  updateSpeakerIcon: function(isMuted) {
    const speakerIcon = document.querySelector('.speaker-icon');
    const mutedIcon = document.querySelector('.speaker-muted-icon');
    if (speakerIcon && mutedIcon) {
      speakerIcon.style.display = isMuted ? 'none' : 'block';
      mutedIcon.style.display = isMuted ? 'block' : 'none';
    }
  },

  updateVolumeDisplay: function(volume) {
    const volumeValue = document.getElementById('jukebox-volume-value');
    if (volumeValue && volumeValue.dataset.editing !== 'true') {
      volumeValue.textContent = Math.round(volume * 100) + '%';
    }
    Jukebox.updateSpeakerIcon(volume === 0);
  },

  stopPlayback: function(options = {}) {
    // 「作废在途播放」和「用户要停」不是一回事。前者只负责让声音停下、让卡在
    // await 里的那条解开，不该替排在后面的那条指令决定播放位置 —— next 与
    // previous 是相对当前曲目算的，把 currentSong 和随机历史一起清掉，它们就
    // 只能退回第一首 / 最后一首。
    const preserveAnchor = options.preserveNavigationAnchor === true;
    const preserveRandomQueue = options.preserveRandomQueue === true || preserveAnchor;
    Jukebox.stopAudio();
    Jukebox.stopVMD(options.skipIdleRestore === true);

    if (!preserveAnchor) {
      Jukebox.State.currentSong = null;
    }
    Jukebox.State.isPlaying = false;
    Jukebox.State.isPaused = false;
    Jukebox.State.isVMDPlaying = false;
    Jukebox.State.audioOwnerRequestId = null;
    if (!preserveRandomQueue) {
      Jukebox.clearRandomQueue();
    }

    Jukebox.updateStoppedStatus();
    // 只有「停到底」才结账。skipIdleRestore 意味着调用方马上要接一段新动画
    // （换歌路径），这时结账等于 restoreIdleAnimation 去 ++playRequestId，
    // 反手把调用它的那次播放作废掉 —— 这个坑前面已经踩过一次。
    if (options.skipIdleRestore !== true) {
      Jukebox.settleIdleRestore();
    }
  },

  stopAudio: function() {
    if (Jukebox.State.audioElement) {
      Jukebox.State.audioElement.pause();
      Jukebox.State.audioElement.currentTime = 0;
      Jukebox.State.audioElement = null;
    }

    const player = Jukebox.getPlayer();
    if (player && Jukebox.State.isPlaying) {
      player.pause();
      player.seek(0);
    }
  },

  stopVMD: function(skipIdleRestore) {
    // 独立窗口模式：通过 IPC 桥接到 Pet 窗口执行
    if (window.__NEKO_JUKEBOX_STANDALONE__ && window.nekoJukeboxBridge) {
      if (Jukebox.State.isVMDPlaying) {
        window.nekoJukeboxBridge.stopVMD(skipIdleRestore);
        Jukebox.State.isVMDPlaying = false;
        Jukebox.State.isPaused = false;
        // 记账口径与本地路径同构：抑制了 Pet 侧的待机恢复，就欠一笔。
        Jukebox.State.idleRestorePending = skipIdleRestore === true;
        return;
      }
      // 没在跳舞却收到一条不抑制恢复的停止 —— 这正是「补发」的形态
      // （桌面端关点唱机窗口时会发一条）。欠着账就在这里结掉。
      if (!skipIdleRestore) Jukebox.settleIdleRestore();
      return;
    }

    // 没有在播放舞蹈动画时，不要停止当前动画（可能是 idle 待机）
    if (!Jukebox.State.isVMDPlaying) {
      // 同上：本地路径的补发停止也要能结账。纯 pet 窗口、完全不涉及独立窗口时
      // 同样走这里 —— 换歌起播失败留下的欠账原本会永远挂着。
      if (!skipIdleRestore) Jukebox.settleIdleRestore();
      return;
    }

    // 根据模型类型停止对应的动画模块
    var modelType = Jukebox.getModelType();
    if (modelType === 'vrm') {
      if (window.vrmManager) window.vrmManager.stopVRMAAnimation();
    } else {
      if (window.mmdManager?.animationModule) {
        // 直接停止动画模块，不通过 stopAnimation()
        // 避免在 idle 加载完成前改变 cursor follow 状态
        window.mmdManager.animationModule.stop();
      }
    }

    Jukebox.State.isVMDPlaying = false;
    Jukebox.State.isPaused = false;

    if (skipIdleRestore) {
      // 这一下把舞蹈停了却没恢复待机，因为「马上要接一段新动画」。记成欠账：
      // 接上了就由起播方清掉，接不上（换歌失败 / 模式变了 / 被 stop 取代 /
      // 运行时没就绪）就由回到静止的那一方补上。逐个出口去记得这件事已经漏过四次。
      Jukebox.State.idleRestorePending = true;
    } else {
      Jukebox.restoreIdleAnimation();
    }
  },

  _resetToNoneMode: function() {
    if (window.__NEKO_JUKEBOX_STANDALONE__) return;
    const mesh = window.mmdManager.currentModel?.mesh;
    if (mesh?.skeleton) {
      mesh.skeleton.pose();
    }
    if (window.mmdManager.cursorFollow) {
      window.mmdManager.cursorFollow.setAnimationMode('none');
    }
  },

  restoreIdleAnimation: async function() {
    const Jukebox = window.Jukebox || this;
    // 无论谁触发的，账到此为止。
    Jukebox.State.idleRestorePending = false;
    // 独立窗口模式：模型在 Pet 窗口里，本窗口自己恢复不了任何东西。
    // 直接 return 等于「账清了却什么都没发生」，Pet 会停在舞蹈最后一帧。
    // 补发一条不抑制恢复的停止过去，让 Pet 自己回到待机。
    if (window.__NEKO_JUKEBOX_STANDALONE__) {
      try {
        if (window.nekoJukeboxBridge && typeof window.nekoJukeboxBridge.stopVMD === 'function') {
          window.nekoJukeboxBridge.stopVMD(false);
        }
      } catch (error) {
        console.warn('[Jukebox] 转发待机恢复失败:', error);
      }
      return;
    }

    var modelType = Jukebox.getModelType();
    var canResumeVrm = modelType === 'vrm' && !!window.vrmManager;
    var heldRuntimeToken = Jukebox.State.vrmMotionRuntimeToken;
    const restoreRequestId = (heldRuntimeToken !== null || canResumeVrm)
      ? ++Jukebox.State.playRequestId
      : Jukebox.State.playRequestId;
    var motionRuntimeRestored = false;

    // 模型切换是同页热切换。即使当前已经是 MMD/Live2D，仍需释放先前 VRM
    // 舞蹈取得的 token；此时只解锁，不唤起旧 VRM 的待机动作。
    if (heldRuntimeToken !== null) {
      const releaseResult = Jukebox.releaseVrmMotionRuntime({
        token: heldRuntimeToken,
        resume: canResumeVrm,
        scheduleNext: canResumeVrm
      });
      motionRuntimeRestored = releaseResult === false ? false : await releaseResult;
      if (restoreRequestId !== Jukebox.State.playRequestId) return;
    }

    // VRM 模式：恢复 VRM 待机动画
    if (canResumeVrm) {
      try {
        if (motionRuntimeRestored === true) {
          console.log('[Jukebox] VRM 待机动画已由动作运行时恢复');
          return;
        }
        var vrmIdleList = window.lanlan_config?.vrmIdleAnimations;
        var vrmIdleUrl = (Array.isArray(vrmIdleList) && vrmIdleList.length > 0) ? vrmIdleList[0] : null;
        if (!vrmIdleUrl) {
          vrmIdleUrl = window.lanlan_config?.vrmIdleAnimation || '/static/vrm/animation/wait03.vrma.gz';
        }
        vrmIdleUrl = normalizeJukeboxBundledVrmIdleUrl(vrmIdleUrl);
        await window.vrmManager.playVRMAAnimation(vrmIdleUrl, {
          loop: true,
          isIdle: true,
          shouldApply: function() {
            return restoreRequestId === Jukebox.State.playRequestId;
          }
        });
        if (restoreRequestId !== Jukebox.State.playRequestId) return;
        console.log('[Jukebox] VRM 待机动画已恢复');
      } catch (error) {
        console.warn('[Jukebox] VRM 待机动画恢复失败:', error);
      }
      return;
    }

    if (modelType === 'vrm') return;

    if (!window.mmdManager) return;

    let idleUrl = Jukebox.State.savedIdleAnimationUrl;

    // 如果保存的是点歌台舞蹈 VMD（不是真正的待机动画），则忽略
    if (idleUrl && idleUrl.includes('/jukebox/song_')) {
      idleUrl = null;
    }

    // 如果没有保存的待机动画 URL，从角色配置获取
    if (!idleUrl) {
      try {
        const catgirlName = window.lanlan_config?.catgirl_name;
        if (catgirlName) {
          const charRes = await fetch('/api/characters');
          if (charRes.ok) {
            const charData = await charRes.json();
            idleUrl = charData?.['猫娘']?.[catgirlName]?.mmd_idle_animation;
          }
        }
      } catch (_) { /* ignore */ }
    }

    if (restoreRequestId !== Jukebox.State.playRequestId) return;

    if (!idleUrl) {
      Jukebox._resetToNoneMode();
      return;
    }

    try {
      await window.mmdManager.loadAnimation(idleUrl);
      if (restoreRequestId !== Jukebox.State.playRequestId) return;
      window.mmdManager.playAnimation('idle');
      console.log('[Jukebox]', window.t('Jukebox.idleRestored', '已恢复待机动画'));
    } catch (error) {
      console.warn('[Jukebox]', window.t('Jukebox.idleRestoreFailed', '恢复待机动画失败'), error);
      if (restoreRequestId !== Jukebox.State.playRequestId) return;
      Jukebox._resetToNoneMode();
    }
  },

  togglePause: function() {
    // Pet 窗口通过 IPC 调用时 currentSong 为 null，用 isVMDPlaying 兜底
    if (!Jukebox.State.currentSong && !Jukebox.State.isVMDPlaying) return;

    const player = Jukebox.getPlayer();
    var isStandalone = window.__NEKO_JUKEBOX_STANDALONE__ && window.nekoJukeboxBridge;
    var modelType = Jukebox.getModelType();

    if (Jukebox.State.isPaused) {
      // 恢复播放
      if (player) player.play();
      if (isStandalone) {
        window.nekoJukeboxBridge.resumeVMD();
      } else if (modelType === 'vrm') {
        var vrmAnim = window.vrmManager?.animationModule || window.vrmManager?.animation;
        if (vrmAnim?.currentAction) vrmAnim.currentAction.paused = false;
      } else if (window.mmdManager?.animationModule) {
        // 直接恢复动画模块（不通过 playAnimation 避免重置动画进度）
        window.mmdManager.animationModule.play();
        if (window.mmdManager.cursorFollow) {
          window.mmdManager.cursorFollow.setAnimationMode('dance');
        }
      }
      Jukebox.State.isPaused = false;
      Jukebox.State.isPlaying = true;
      if (Jukebox.State.currentSong) Jukebox.updatePlayingStatus(Jukebox.State.currentSong);
      console.log('[Jukebox]', window.t('Jukebox.resumed', '已恢复播放'));
    } else if (Jukebox.State.isPlaying || Jukebox.State.isVMDPlaying) {
      // 暂停
      if (player) player.pause();
      if (isStandalone) {
        window.nekoJukeboxBridge.pauseVMD();
      } else if (modelType === 'vrm') {
        var vrmAnim = window.vrmManager?.animationModule || window.vrmManager?.animation;
        if (vrmAnim?.currentAction) vrmAnim.currentAction.paused = true;
      } else if (window.mmdManager?.animationModule) {
        window.mmdManager.animationModule.pause();
        // 暂停时提升跟踪权重，让视线追踪更明显
        if (window.mmdManager.cursorFollow) {
          window.mmdManager.cursorFollow.setAnimationMode('idle');
        }
      }
      Jukebox.State.isPaused = true;
      Jukebox.State.isPlaying = false;
      if (Jukebox.State.currentSong) Jukebox.updatePausedStatus(Jukebox.State.currentSong);
      console.log('[Jukebox]', window.t('Jukebox.paused', '已暂停'));
    }
  },

  // ═══════════════════ 进度条 ═══════════════════

  startProgressUpdate: function() {
    Jukebox.stopProgressUpdate();

    const slider = document.getElementById('jukebox-progress-slider');
    if (slider) {
      // 始终允许拖动进度条
      slider.classList.add('seekable');
      // 绑定 seek 事件
      if (!slider._jukeboxBound) {
        slider.addEventListener('input', Jukebox._onProgressInput);
        slider.addEventListener('change', Jukebox._onProgressChange);
        slider._jukeboxBound = true;
      }
    }

    Jukebox.State.progressTimer = setInterval(() => {
      if (!Jukebox.State.isSeeking) {
        Jukebox._updateProgressDisplay();
      }
    }, 250);
  },

  stopProgressUpdate: function() {
    if (Jukebox.State.progressTimer) {
      clearInterval(Jukebox.State.progressTimer);
      Jukebox.State.progressTimer = null;
    }
  },

  _updateProgressDisplay: function() {
    const player = Jukebox.getPlayer();
    if (!player || !player.audio) return;

    const currentTime = player.audio.currentTime || 0;
    const duration = player.audio.duration || 0;

    const slider = document.getElementById('jukebox-progress-slider');
    const timeCurrent = document.getElementById('jukebox-time-current');
    const timeTotal = document.getElementById('jukebox-time-total');

    if (slider && duration > 0) {
      slider.value = (currentTime / duration) * 100;
    }
    if (timeCurrent) timeCurrent.textContent = Jukebox.formatDuration(Math.floor(currentTime));
    if (timeTotal) timeTotal.textContent = Jukebox.formatDuration(Math.floor(duration));
  },

  _onProgressInput: function() {
    Jukebox.State.isSeeking = true;
    // 拖动时只更新显示，不实际跳转
    Jukebox._updateProgressDisplayFromSlider();
  },

  getAnimationTimeForMusicTime: function(musicTime, offset) {
    const song = Jukebox.State.currentSong;
    const action = song ? Jukebox.getActionForModel(song) : null;
    const fps = Jukebox.getAnimationFps(action);
    const frameOffset = Number.isFinite(Number(offset)) ? Number(offset) : Jukebox.getCurrentOffset();
    const animFrame = (Number(musicTime) || 0) * fps + frameOffset;
    return Math.max(0, animFrame / fps);
  },

  _seekMmdAnimationToTime: function(animTime, requireClip) {
    const anim = window.mmdManager?.animationModule;
    if (!anim || !anim.mixer || (requireClip && !anim.currentClip)) return false;

    anim.mixer.setTime(animTime);
    const mesh = window.mmdManager.currentModel?.mesh;
    if (typeof anim._restoreBones === 'function') anim._restoreBones(mesh);
    if (anim.mixer.update) anim.mixer.update(0);
    if (typeof anim._saveBones === 'function') anim._saveBones(mesh);
    if (mesh) mesh.updateMatrixWorld(true);
    if (anim.ikSolver) anim.ikSolver.update();
    if (anim.grantSolver) anim.grantSolver.update();
    return true;
  },

  _seekVrmAnimationToTime: function(animTime) {
    const manager = window.vrmManager;
    const seekOptions = { paused: Jukebox.State.isPaused === true };
    if (manager && typeof manager.seekVRMAAnimation === 'function') {
      return manager.seekVRMAAnimation(animTime, seekOptions);
    }
    const anim = manager?.animationModule || manager?.animation;
    if (anim && typeof anim.seekTo === 'function') {
      return anim.seekTo(animTime, seekOptions);
    }
    console.warn('[Jukebox] VRM动画同步入口不可用，跳过 seek:', animTime);
    return false;
  },

  syncCurrentAnimationToTime: function(animTime, options = {}) {
    if (window.__NEKO_JUKEBOX_STANDALONE__) return false;

    const modelType = Jukebox.getModelType();
    if (modelType === 'mmd' || modelType === 'live3d') {
      return Jukebox._seekMmdAnimationToTime(animTime, options.requireClipForMmd === true);
    }
    if (modelType === 'vrm') {
      return Jukebox._seekVrmAnimationToTime(animTime);
    }
    if (modelType === 'fbx') {
      console.log('[Jukebox] FBX动画同步:', animTime);
    }
    return false;
  },

  _onProgressChange: function() {
    const slider = document.getElementById('jukebox-progress-slider');
    if (!slider) {
      Jukebox.State.isSeeking = false;
      return;
    }

    const player = Jukebox.getPlayer();
    if (!player || !player.audio) {
      Jukebox.State.isSeeking = false;
      return;
    }

    const duration = player.audio.duration || 0;
    const seekTime = (parseFloat(slider.value) / 100) * duration;

    // 同步音频
    player.seek(seekTime);

    // 同步动画（考虑 offset）—— 独立窗口无法直接操作动画模块
    Jukebox.syncCurrentAnimationToTime(
      Jukebox.getAnimationTimeForMusicTime(seekTime),
      { requireClipForMmd: true }
    );

    Jukebox.State.isSeeking = false;
    Jukebox._updateProgressDisplay();
  },

  // 根据滑块值更新显示（不实际跳转）
  _updateProgressDisplayFromSlider: function() {
    const slider = document.getElementById('jukebox-progress-slider');
    const timeCurrent = document.getElementById('jukebox-time-current');
    if (!slider || !timeCurrent) return;

    const player = Jukebox.getPlayer();
    if (!player || !player.audio) return;

    const duration = player.audio.duration || 0;
    const previewTime = (parseFloat(slider.value) / 100) * duration;
    timeCurrent.textContent = Jukebox.formatDuration(Math.floor(previewTime));
  },

  _setProgressSeekable: function(seekable) {
    const slider = document.getElementById('jukebox-progress-slider');
    if (slider) {
      if (seekable) {
        slider.classList.add('seekable');
      } else {
        slider.classList.remove('seekable');
      }
    }
  },

  getPlayer: function() {
    if (window.music_ui && window.music_ui.getMusicPlayerInstance) {
      const sharedPlayer = window.music_ui.getMusicPlayerInstance();
      if (sharedPlayer) {
        return sharedPlayer;
      }
    }

    return Jukebox.State.player;
  },

  initPlayer: function(options = {}) {
    const Jukebox = window.Jukebox || this;
    const isHeadless = options.headless === true;
    if (window.music_ui && window.music_ui.getMusicPlayerInstance) {
      const existingPlayer = window.music_ui.getMusicPlayerInstance();
      if (existingPlayer) {
        console.log('[Jukebox] 使用现有的音乐播放器');
        return existingPlayer;
      }
      console.log('[Jukebox] music_ui 存在但播放器未初始化，创建新播放器');
    }

    if (Jukebox.State.player) {
      return Jukebox.State.player;
    }

    const host = isHeadless
      ? Jukebox.ensureRuntimeHost()
      : Jukebox.State.container;

    if (!host) {
      console.warn('[Jukebox] 容器不存在，取消播放器初始化');
      return null;
    }

    console.log('[Jukebox] 创建新的音乐播放器');

    if (typeof APlayer === 'undefined') {
      console.warn('[Jukebox] APlayer 未加载，等待加载...');
      if (options.headless === true) {
        return null;
      }
      setTimeout(() => Jukebox.initPlayer(), 500);
      return null;
    }

    const playerContainer = document.createElement('div');
    playerContainer.id = 'jukebox-player';
    playerContainer.style.display = 'none';
    host.appendChild(playerContainer);

    // 面板建好到这里之间用户可能已经拖过滑条：那时还没有 player，值只落在
    // State.pendingVolume 上。有的话必须拿它来构造，否则紧接着的 initVolumeSlider
    // 会用 player 的音量反向覆盖滑条和标签，用户的设置就丢了。
    //
    // 没拖过就一个字都别提音量：APlayer 自己会从 localStorage 恢复上次会话的值，
    // 而 State.savedVolume 的默认是 1 —— 拿它去「恢复」等于把用户存的音量抹成 100%。
    const pendingVolume = Jukebox.State.pendingVolume;
    const hasPendingVolume = typeof pendingVolume === 'number' && Number.isFinite(pendingVolume);
    const playerOptions = {
      container: playerContainer,
      autoplay: false,
      theme: Jukebox.Config.container.background,
      preload: 'auto',
      listFolded: true,
      audio: []
    };
    if (hasPendingVolume) playerOptions.volume = pendingVolume;
    Jukebox.State.player = new APlayer(playerOptions);
    // 光给构造参数不够：APlayer 会用 localStorage['aplayer-setting'] 里存的音量
    // 覆盖 options.volume（`this.data.volume = this.data.volume || options.volume`），
    // 所以建好之后必须再显式设一次，才压得住上一次会话留下的值。
    if (hasPendingVolume) {
      Jukebox.setRuntimeVolume(pendingVolume);
      Jukebox.State.pendingVolume = null;
    }
    console.log('[Jukebox] APlayer已创建，音量:', Jukebox.State.player.audio.volume);
    return Jukebox.State.player;
  },

  // 获取当前模型类型（拆分 live3d 子类型，返回 'mmd' / 'vrm' / 'live2d'）
  getModelType: function() {
    var mt = window.lanlan_config?.model_type || 'live2d';
    if (mt === 'live3d') {
      var sub = (window.lanlan_config?.live3d_sub_type || '').toLowerCase();
      if (sub === 'vrm') return 'vrm';
      return 'mmd'; // live3d 默认走 MMD
    }
    return mt;
  },

  // 检查当前模型是否支持动画
  isAnimationSupported: function() {
    const modelType = Jukebox.getModelType();
    return ['mmd', 'live3d', 'vrm', 'fbx'].includes(modelType);
  },

  // 显示/隐藏校准区域
  updateCalibrationVisibility: function() {
    const section = document.getElementById('jukebox-calibration-section');
    if (section) {
      section.style.display = Jukebox.isAnimationSupported() ? 'block' : 'none';
    }
  },

  // 切换校准面板显示
  toggleCalibrationPanel: function() {
    const panel = document.getElementById('jukebox-calibration-panel');
    if (panel) {
      const isVisible = panel.style.display !== 'none';
      panel.style.display = isVisible ? 'none' : 'block';
    }
  },

  // 获取当前歌曲和动画的offset
  getCurrentOffset: function() {
    const song = Jukebox.State.currentSong;
    if (!song) return 0;

    const action = Jukebox.getActionForModel(song);
    if (!action) return 0;

    // 当前会话编辑过的值优先；普通播放路径未打开管理器时回退到 loadSongs 已加载的配置。
    const managerBinding = Jukebox.SongActionManager.data.bindings?.[song.id]?.[action.id];
    const configBinding = Jukebox.State.config?.bindings?.[song.id]?.[action.id];
    const offset = managerBinding?.offset ?? configBinding?.offset ?? 0;
    return Number.isFinite(Number(offset)) ? Number(offset) : 0;
  },

  // 更新校准显示值
  updateCalibrationDisplay: function() {
    const valueEl = document.getElementById('jukebox-calibration-value');
    const fpsEl = document.getElementById('jukebox-calibration-fps');

    if (valueEl) {
      const offset = Jukebox.getCurrentOffset();
      valueEl.textContent = offset + window.t('Jukebox.frames', '帧');
    }

    if (fpsEl) {
      const song = Jukebox.State.currentSong;
      const action = song ? Jukebox.getActionForModel(song) : null;
      const fps = Jukebox.getAnimationFps(action);
      fpsEl.textContent = '(' + fps + ' FPS)';
    }
  },

  // 调整offset
  adjustOffset: async function(delta) {
    const song = Jukebox.State.currentSong;
    if (!song) {
      Jukebox.showError(window.t('Jukebox.noSongPlaying', '没有正在播放的歌曲'));
      return;
    }

    const action = Jukebox.getActionForModel(song);
    if (!action) {
      Jukebox.showError(window.t('Jukebox.noActionBound', '当前歌曲没有绑定动画'));
      return;
    }

    const currentOffset = Jukebox.getCurrentOffset();
    const newOffset = currentOffset + delta;

    try {
      // 保存到后端
      await Jukebox.SongActionManager.api.updateOffset(song.id, action.id, newOffset);

      // 更新本地状态 (保存到 SongActionManager.data)
      if (!Jukebox.SongActionManager.data.bindings[song.id]) {
        Jukebox.SongActionManager.data.bindings[song.id] = {};
      }
      Jukebox.SongActionManager.data.bindings[song.id][action.id] = { offset: newOffset };

      // 更新显示
      Jukebox.updateCalibrationDisplay();

      // 如果正在播放，实时调整动画
      if (Jukebox.State.isPlaying && !Jukebox.State.isPaused) {
        Jukebox.syncAnimationToOffset(newOffset);
      }

      console.log('[Jukebox] Offset已调整:', currentOffset, '->', newOffset);
    } catch (error) {
      console.error('[Jukebox] 调整offset失败:', error);
      Jukebox.showError(window.t('Jukebox.adjustOffsetFailed', '调整偏移失败'));
    }
  },

  // 重置offset
  resetOffset: async function() {
    await Jukebox.adjustOffset(-Jukebox.getCurrentOffset());
  },

  // 获取动画的FPS
  getAnimationFps: function(action) {
    if (!action) return 30;

    // MMD/VMD 固定30fps
    const format = (action.format || 'vmd').toLowerCase();
    if (format === 'vmd') return 30;

    // 其他格式从配置读取，默认30
    return action.fps || 30;
  },

  // 根据offset同步动画
  syncAnimationToOffset: function(offset) {
    // 独立窗口模式：校准需要直接访问动画模块，无法通过 IPC 操作
    if (window.__NEKO_JUKEBOX_STANDALONE__) return;

    const player = Jukebox.getPlayer();
    if (!player || !player.audio) return;

    const musicTime = player.audio.currentTime;
    const animTime = Jukebox.getAnimationTimeForMusicTime(musicTime, offset);
    Jukebox.syncCurrentAnimationToTime(animTime);
  },

  // 根据模型类型获取对应格式的动画
  // 没有默认动画本身也是合理的状态，可以通过点击已设置的默认动画来取消它
  getActionForModel: function(song) {
    const modelType = Jukebox.getModelType();

    // 模型类型到动画格式的映射
    const formatMap = {
      'mmd': 'vmd',
      'live3d': 'vmd',
      'vrm': 'vrma',
      'fbx': 'fbx'
    };

    const targetFormat = formatMap[modelType];
    if (!targetFormat) {
      console.log('[Jukebox] 当前模型类型不支持动画:', modelType);
      return null;
    }

    // 获取绑定的动画中对应格式的动画
    const boundActions = song.boundActions || [];
    const availableActions = boundActions.filter(a => a.missing !== true);
    const formatActions = availableActions.filter(a =>
      (a.format || 'vmd').toLowerCase() === targetFormat
    );

    if (formatActions.length === 0) {
      console.log('[Jukebox] 歌曲没有绑定', targetFormat.toUpperCase(), '格式的动画');
      return null;
    }

    // 如果用户设置了默认动画，优先使用它
    if (song.defaultAction) {
      const defaultAction = formatActions.find(a => a.id === song.defaultAction);
      if (defaultAction) {
        return defaultAction;
      }
      // defaultAction 是其他格式（如 VMD），当前格式（如 VRMA）有可用动画则 fallback
      if (formatActions.length > 0) {
        console.log('[Jukebox] 默认动画格式不匹配，使用该格式的第一个动画:', formatActions[0].name);
        return formatActions[0];
      }
      // 该格式无可用动画
      console.log('[Jukebox] 默认动画格式不匹配且无可用动画');
      return null;
    }

    // 没有设置默认动画，不播放动画
    return null;
  },

  updatePlayingStatus: function(song) {
    const statusText = document.getElementById('jukebox-status-text');
    if (statusText) {
      statusText.textContent = window.t('Jukebox.playing', { name: song.name, artist: song.artist }) || `正在播放: ${song.name} - ${song.artist}`;
    }

    Jukebox._resetAllButtons();
    Jukebox.startProgressUpdate();
    Jukebox.updateGlobalTransportControls();

    const currentRow = document.querySelector(`tr[data-song-id="${CSS.escape(song.id)}"]`);
    if (currentRow) {
      const td = currentRow.querySelector('td:last-child');
      if (td) {
        td.innerHTML = '';

        const pauseBtn = document.createElement('button');
        pauseBtn.className = 'play-btn pause-btn';
        pauseBtn.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
        Jukebox.setupTooltip(pauseBtn, window.t('Jukebox.pause', '暂停'));
        pauseBtn.addEventListener('click', () => Jukebox.togglePause());

        const stopBtn = document.createElement('button');
        stopBtn.className = 'play-btn playing';
        stopBtn.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M6 6h12v12H6z"/></svg>';
        Jukebox.setupTooltip(stopBtn, window.t('Jukebox.stop', '停止'));
        stopBtn.addEventListener('click', () => Jukebox.stopPlayback());

        td.appendChild(pauseBtn);
        td.appendChild(stopBtn);
      }
    }
  },

  updatePausedStatus: function(song) {
    const statusText = document.getElementById('jukebox-status-text');
    if (statusText) {
      statusText.textContent = window.t('Jukebox.pausedStatus', { name: song.name }) || `已暂停: ${song.name}`;
    }

    Jukebox._resetAllButtons();
    Jukebox.updateGlobalTransportControls();

    const currentRow = document.querySelector(`tr[data-song-id="${CSS.escape(song.id)}"]`);
    if (currentRow) {
      const td = currentRow.querySelector('td:last-child');
      if (td) {
        td.innerHTML = '';

        const resumeBtn = document.createElement('button');
        resumeBtn.className = 'play-btn resume-btn';
        resumeBtn.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>';
        Jukebox.setupTooltip(resumeBtn, window.t('Jukebox.resume', '继续'));
        resumeBtn.addEventListener('click', () => Jukebox.togglePause());

        const stopBtn = document.createElement('button');
        stopBtn.className = 'play-btn playing';
        stopBtn.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M6 6h12v12H6z"/></svg>';
        Jukebox.setupTooltip(stopBtn, window.t('Jukebox.stop', '停止'));
        stopBtn.addEventListener('click', () => Jukebox.stopPlayback());

        td.appendChild(resumeBtn);
        td.appendChild(stopBtn);
      }
    }
  },

  _resetAllButtons: function() {
    document.querySelectorAll('#jukebox-song-list td:last-child').forEach(td => {
      const songId = td.parentElement?.dataset?.songId;
      if (!songId) return;
      td.innerHTML = '';
      const btn = document.createElement('button');
      btn.className = 'play-btn';
      btn.dataset.songId = songId;
      btn.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>';
      Jukebox.setupTooltip(btn, window.t('Jukebox.play', '播放'));
      btn.addEventListener('click', () => Jukebox_playSong(songId));
      td.appendChild(btn);
    });
  },

  updateStoppedStatus: function() {
    const statusText = document.getElementById('jukebox-status-text');
    if (statusText) {
      statusText.textContent = window.t('Jukebox.ready', '准备就绪');
    }

    Jukebox.stopProgressUpdate();
    Jukebox._resetAllButtons();
    Jukebox.updateGlobalTransportControls();
  },

  showError: function(message) {
    const statusText = document.getElementById('jukebox-status-text');
    if (statusText) {
      statusText.textContent = (window.t('Jukebox.error', { message }) || '错误: ' + message);
      statusText.style.color = '#ff6b6b';
    }
  },

  formatDuration: function(seconds) {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  },

  escapeHtml: function(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  },

  escapeAttr: function(text) {
    return Jukebox.escapeHtml(text).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  },

  escapeJsAttr: function(text) {
    const jsText = String(text)
      .replace(/\\/g, '\\\\')
      .replace(/'/g, "\\'")
      .replace(/\r/g, '\\r')
      .replace(/\n/g, '\\n')
      .replace(/\u2028/g, '\\u2028')
      .replace(/\u2029/g, '\\u2029');
    return Jukebox.escapeAttr(jsText);
  },

  /**
   * 语言切换后刷新 Jukebox UI 文本
   * 独立窗口模式直接重载页面；嵌入模式逐一更新 DOM 元素
   */
  refreshLocale: function() {
    // 独立窗口（N.E.K.O.-PC）：重载最干净
    if (window.__NEKO_JUKEBOX_STANDALONE__) {
      location.reload();
      return;
    }

    // 嵌入模式：逐一刷新已渲染的静态文本
    const c = Jukebox.State.container;
    if (!c) return;

    // --- Header ---
    var h3 = c.querySelector('.jukebox-header h3');
    if (h3) h3.textContent = window.t('Jukebox.title', '点歌台');
    var settingsBtn = c.querySelector('.jukebox-settings');
    if (settingsBtn) {
      settingsBtn.dataset.tooltip = window.t('Jukebox.manager', '点歌台管理与导入');
      settingsBtn.removeAttribute('title');
      settingsBtn.setAttribute('aria-label', settingsBtn.dataset.tooltip);
      Jukebox.refreshTooltip(settingsBtn);
      var settingsLabel = settingsBtn.querySelector('.jukebox-settings-label');
      if (settingsLabel) settingsLabel.textContent = window.t('Jukebox.settingsShort', '管理/导入');
    }
    var minBtn = c.querySelector('.jukebox-minimize');
    if (minBtn) {
      minBtn.dataset.tooltip = window.t('Jukebox.minimize', '最小化');
      minBtn.removeAttribute('title');
      minBtn.setAttribute('aria-label', minBtn.dataset.tooltip);
      Jukebox.refreshTooltip(minBtn);
    }
    var closeBtn = c.querySelector('.jukebox-close');
    if (closeBtn) {
      closeBtn.dataset.tooltip = window.t('Jukebox.close', '关闭');
      closeBtn.removeAttribute('title');
      closeBtn.setAttribute('aria-label', closeBtn.dataset.tooltip);
      Jukebox.refreshTooltip(closeBtn);
    }

    // --- Calibration ---
    var calToggle = c.querySelector('#jukebox-calibration-toggle');
    if (calToggle) calToggle.textContent = window.t('Jukebox.calibrateAnimation', '校准动画');
    var calClose = c.querySelector('.jukebox-calibration-close');
    if (calClose) calClose.textContent = window.t('Jukebox.closeCalibration', '关闭校准控制台');
    var calReset = c.querySelector('.jukebox-calibration-reset');
    if (calReset) { calReset.textContent = window.t('Jukebox.reset', '重置'); calReset.title = window.t('Jukebox.reset', '重置'); }
    var calTitle = c.querySelector('.jukebox-calibration-title');
    if (calTitle) {
      var fpsSpan = calTitle.querySelector('#jukebox-calibration-fps');
      var fpsHtml = fpsSpan ? fpsSpan.outerHTML : '';
      calTitle.innerHTML = window.t('Jukebox.animationCalibration', '动画校准') + ' ' + fpsHtml;
    }

    // --- Notice ---
    var notices = c.querySelectorAll('.jukebox-notice-item');
    if (notices[0]) notices[0].textContent = window.t('Jukebox.noticeDance', '💃 伴舞服务目前仅在载入 MMD 形象时可用，后续会增加更多互动');
    if (notices[1]) notices[1].textContent = window.t('Jukebox.noticeMusic', '⚠️ 当前歌曲仅供测试，后续版本将清除版权音乐，请自行导入');

    // --- Table headers ---
    var ths = c.querySelectorAll('.jukebox-table thead th');
    if (ths.length >= 4) {
      var sequenceLabel = ths[0].querySelector('span');
      if (sequenceLabel) {
        sequenceLabel.textContent = window.t('Jukebox.sequence', '序号');
      } else {
        ths[0].textContent = window.t('Jukebox.sequence', '序号');
      }
      ths[1].textContent = window.t('Jukebox.song', '歌曲');
      ths[2].textContent = window.t('Jukebox.artist', '艺术家');
      ths[3].textContent = window.t('Jukebox.action', '操作');
    }

    // --- Mute button ---
    var speakerBtn = c.querySelector('#jukebox-speaker-btn');
    if (speakerBtn) {
      speakerBtn.removeAttribute('title');
      speakerBtn.setAttribute('aria-label', window.t('Jukebox.mute', '静音'));
    }
    var prevBtn = c.querySelector('#jukebox-control-prev');
    if (prevBtn) {
      prevBtn.removeAttribute('title');
      prevBtn.setAttribute('aria-label', window.t('Jukebox.previousSong', '上一首'));
    }
    var nextBtn = c.querySelector('#jukebox-control-next');
    if (nextBtn) {
      nextBtn.removeAttribute('title');
      nextBtn.setAttribute('aria-label', window.t('Jukebox.nextSong', '下一首'));
    }
    Jukebox.renderPlaybackControls();
    Jukebox.updateSongSortLockControls(c);

    // --- Re-render song list (preserves playback state) ---
    if (Jukebox.State.songs && Jukebox.State.songs.length) {
      Jukebox.renderList();
    }

    // --- Re-render SongActionManager (if visible) ---
    try {
      if (Jukebox.SongActionManager && Jukebox.SongActionManager.element) {
        // Rebuild panel to refresh tab titles and static text
        var panel = Jukebox.SongActionManager.element;
        var titleEl = panel.querySelector('.sam-title');
        if (titleEl) titleEl.textContent = window.t('Jukebox.managerTitle', '点歌台管理');
        var tabs = panel.querySelectorAll('.sam-tab');
        var tabKeys = ['Jukebox.songs', 'Jukebox.actions', 'Jukebox.bindings'];
        var tabDefaults = ['歌曲库', '舞蹈动作', '歌曲绑定'];
        tabs.forEach(function(tab, i) {
          if (tabKeys[i]) tab.textContent = window.t(tabKeys[i], tabDefaults[i]);
        });
        var samCloseBtn = panel.querySelector('.sam-close-btn');
        if (samCloseBtn) {
          samCloseBtn.dataset.tooltip = window.t('Jukebox.close', '关闭');
          samCloseBtn.removeAttribute('title');
          samCloseBtn.setAttribute('aria-label', samCloseBtn.dataset.tooltip);
          Jukebox.refreshTooltip(samCloseBtn);
        }
        // Re-render active tab content
        Jukebox.SongActionManager.render();
      }
    } catch (e) { console.warn('[Jukebox] refreshLocale SongActionManager error:', e); }

    console.log('[Jukebox] UI 文本已刷新');
  }
});
