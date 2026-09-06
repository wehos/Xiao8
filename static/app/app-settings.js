/**
 * app-settings.js — 设置保存/加载模块
 * 负责 saveSettings / loadSettings、地区检测、设置迁移
 * 依赖: app-state.js (window.appState, window.appConst, window.appUtils)
 */
(function () {
    'use strict';

    const mod = {};
    const S = window.appState;
    const C = window.appConst;
    const U = window.appUtils;

    // ======================== 内部辅助 ========================

    // 定时同步到服务器的 timer ID
    let _syncTimerId = null;
    // 周期同步因「设置未水合」被跳过时只 log 一次，避免 GET 持续失败刷屏
    let _periodicSyncSkippedUnhydratedLogged = false;
    // Field-level user authority (Codex P2): _dirtySettingsKeys records which
    // conversation-settings keys the user explicitly changed since boot.
    // _pendingSettingsKeys is narrower: it contains only changes not yet
    // acknowledged by a successful POST. A delayed boot GET preserves pending
    // keys unconditionally and preserves acknowledged dirty keys only when the
    // returned revision does not supersede the POST that acknowledged them.
    const _dirtySettingsKeys = new Set();
    const _pendingSettingsKeys = new Set();
    let _crossWindowMutationVersion = 0;
    const _crossWindowKeyMutationVersions = Object.create(null);
    let _settingsBaseline = null;
    // Cross-window write metadata (Codex P2): saveSettings() writes EVERY
    // conversation key into the shared localStorage snapshot, so a receiving
    // window cannot tell a real independentAsrEnabled toggle from the
    // incidental copy that rides along with an unrelated preference save.
    // Inferring intent from a value difference alone let an UNHYDRATED
    // window's unrelated save (carrying a pre-merge boot default) look like an
    // explicit ASR flip to a window that had already merged the server value:
    // the receiver adopted the stale value, marked the key user-dirty and
    // stamped the wrong route on the next handshake. Every write now carries
    // the keys the writing user EXPLICITLY changed plus a monotonic write id,
    // and the listener grants ASR authority only on that evidence.
    const _SHARED_WRITE_META_KEY = '_sharedWriteMeta';
    const _ASR_WRITE_ID_MAX_FUTURE_SKEW_MS = 365 * 24 * 60 * 60 * 1000;
    let _lastSharedWriteId = 0;
    let _lastAppliedSharedWriteId = 0;
    // Latest explicit non-ASR write token this window has incorporated per
    // shared key. A server-merge snapshot gets a fresh envelope writeId, but
    // that does not prove its field values observed every earlier user edit.
    // Carrying these per-key source tokens lets receivers compare the data's
    // actual provenance instead of mistaking the envelope timestamp for it.
    const _knownSharedKeyWrites = Object.create(null);
    // Server revision floors are deliberately separate from per-key browser
    // write provenance. A GET/merge can prove that a server revision won, but
    // its freshly minted localStorage envelope did not produce that field and
    // must not become the field's source token.
    const _knownServerKeyRevisions = Object.create(null);
    // Window-unique second sort key for concurrent shared-settings writes. Per
    // document load; stability across reloads is not needed because every
    // comparison reads the id off the write itself, and sessionStorage would be
    // WORSE -- the browser copies it into a duplicated tab, destroying the
    // uniqueness this depends on.
    const _SHARED_WRITER_ID = (Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
    // (writeId, writerId, value) of the newest EXPLICIT independentAsrEnabled
    // write this window knows about, INCLUDING ITS OWN. _lastAppliedSharedWriteId
    // records only writes RECEIVED here, so it cannot order this window's own
    // pending toggle against a concurrent one from another window: without this,
    // two windows holding divergent values that both write before observing each
    // other each adopt the other and stay swapped forever. That needs no
    // millisecond tie at all -- a strictly older foreign write still wins.
    let _lastAsrDecision = null;
    let _asrDecisionWriteIdFloor = 0;
    function _isValidAsrWriteId(value, serverAuthoritative) {
        if (!Number.isSafeInteger(value) || value < 0
            || value > Number.MAX_SAFE_INTEGER - 1) return false;
        // The server is the clock authority for persisted decision tuples.
        // Rechecking its accepted ceiling against this browser's Date.now()
        // makes a lagging browser reject a valid winner. Keep the clock-relative
        // bound only for untrusted localStorage/write metadata.
        if (serverAuthoritative === true) return true;
        const maxAccepted = Math.min(
            Number.MAX_SAFE_INTEGER - 1,
            Date.now() + _ASR_WRITE_ID_MAX_FUTURE_SKEW_MS
        );
        return value <= maxAccepted;
    }
    function _noteSettingDecision(
        current,
        writeId,
        writerId,
        value,
        isFreshChoice
    ) {
        // A write that merely re-asserts the value already decided is not a new
        // choice: _dirtySettingsKeys is monotone, so every later save from a
        // window that once toggled declares the key explicit, and treating those
        // as fresh intent would shield this window from a genuinely newer toggle.
        if (
            current
            && current.value === value
            && isFreshChoice !== true
        ) return current;
        if (current
            && !(writeId > current.writeId
                || (writeId === current.writeId && writerId > current.writerId))) {
            return current;
        }
        return { writeId: writeId, writerId: writerId || '', value: value };
    }
    let _lastOptimizationDecision = null;
    // Durable write-ahead bit for the optimization handshake. A localStorage
    // decision can outlive the page that issued its POST; keep it authoritative
    // across reloads until the exact decision is acknowledged by the server.
    let _optimizationDecisionPendingSync = false;
    function _noteAsrDecision(
        writeId,
        writerId,
        value,
        isFreshChoice
    ) {
        const nextDecision = _noteSettingDecision(
            _lastAsrDecision,
            writeId,
            writerId,
            value,
            isFreshChoice
        );
        _lastAsrDecision = nextDecision;
        if (nextDecision) {
            _asrDecisionWriteIdFloor = Math.max(
                _asrDecisionWriteIdFloor,
                nextDecision.writeId
            );
        }
    }
    function _noteOptimizationDecision(
        writeId,
        writerId,
        value,
        isFreshChoice
    ) {
        _lastOptimizationDecision = _noteSettingDecision(
            _lastOptimizationDecision,
            writeId,
            writerId,
            value,
            isFreshChoice
        );
    }
    function _settingWriteOutranksLocalChoice(
        meta,
        settingKey,
        decisionKey,
        localDecision
    ) {
        if (!localDecision) return true;
        // Order on the DECISION that produced this value, not on the id of the
        // write that happens to carry it. _dirtySettingsKeys is monotone and
        // every save copies shared settings, so once a window has toggled once,
        // each later unrelated save re-declares the dirty key with a fresh id.
        const decision = meta[decisionKey]
            || (meta.changedKeys.indexOf(settingKey) !== -1 ? meta : null);
        // No tuple AND not declared explicit: an incidental copy of whatever
        // this writer happened to hold. It must never outrank an explicit local
        // choice. Previous-build snapshots that DO declare the key keep today's
        // writeId ordering through the fallback above.
        if (!decision) return false;
        if (decision.writeId > localDecision.writeId) return true;
        if (decision.writeId < localDecision.writeId) return false;
        // Equal ids are unordered in time; break on the window-unique key so
        // both windows pick the SAME winner. An absent writerId (previous
        // build) reads as '' and loses, keeping this window's own choice.
        return (decision.writerId || '') > localDecision.writerId;
    }
    function _asrWriteOutranksLocalChoice(meta) {
        return _settingWriteOutranksLocalChoice(
            meta,
            'independentAsrEnabled',
            'asrDecision',
            _lastAsrDecision
        );
    }
    function _optimizationWriteOutranksLocalChoice(meta) {
        return _settingWriteOutranksLocalChoice(
            meta,
            'voiceInputResourceOptimizationEnabled',
            'optimizationDecision',
            _lastOptimizationDecision
        );
    }
    function _normalizeServerAsrDecision(value, serverAuthoritative) {
        if (!value || typeof value !== 'object') return null;
        if (!_isValidAsrWriteId(value.writeId, serverAuthoritative)) return null;
        if (typeof value.writerId !== 'string'
            || !/^[\x20-\x7E]{1,128}$/.test(value.writerId)) return null;
        if (typeof value.value !== 'boolean') return null;
        return {
            writeId: value.writeId,
            writerId: value.writerId,
            value: value.value
        };
    }
    function _asrDecisionOutranks(candidate, current) {
        if (!candidate) return false;
        if (!current) return true;
        if (candidate.writeId > current.writeId) return true;
        if (candidate.writeId < current.writeId) return false;
        return candidate.writerId > current.writerId;
    }
    function _asrDecisionsEqual(left, right) {
        return !!left && !!right
            && left.writeId === right.writeId
            && left.writerId === right.writerId
            && left.value === right.value;
    }
    function _sharedWriteTokenOutranks(candidate, current) {
        if (!candidate) return false;
        if (!current) return true;
        if (candidate.writeId > current.writeId) return true;
        if (candidate.writeId < current.writeId) return false;
        return (candidate.writerId || '') > (current.writerId || '');
    }
    function _sharedWriteTokensEqual(left, right) {
        return !!left && !!right
            && left.writeId === right.writeId
            && (left.writerId || '') === (right.writerId || '');
    }
    function _rememberSharedKeyWrites(keys, token, acceptedSettings) {
        if (!Array.isArray(keys) || !token) return;
        for (const key of keys) {
            if (key === 'independentAsrEnabled') continue;
            if (_SHARED_SETTINGS_KEYS.indexOf(key) === -1) continue;
            if (acceptedSettings
                && !Object.prototype.hasOwnProperty.call(acceptedSettings, key)) continue;
            const candidate = {
                writeId: token.writeId,
                writerId: token.writerId || ''
            };
            if (Number.isInteger(token.confirmedRevision)
                && token.confirmedRevision >= 0) {
                candidate.confirmedRevision = token.confirmedRevision;
            }
            if (_sharedWriteTokenOutranks(candidate, _knownSharedKeyWrites[key])) {
                _knownSharedKeyWrites[key] = candidate;
            } else if (_sharedWriteTokensEqual(
                candidate,
                _knownSharedKeyWrites[key]
            ) && Number.isInteger(candidate.confirmedRevision)
                && (!Number.isInteger(
                    _knownSharedKeyWrites[key].confirmedRevision
                ) || candidate.confirmedRevision
                    > _knownSharedKeyWrites[key].confirmedRevision)) {
                _knownSharedKeyWrites[key].confirmedRevision =
                    candidate.confirmedRevision;
            }
        }
    }
    function _rememberKnownSharedKeyWrites(knownWrites, acceptedSettings) {
        if (!knownWrites) return;
        for (const key of Object.keys(knownWrites)) {
            _rememberSharedKeyWrites([key], knownWrites[key], acceptedSettings);
        }
    }
    function _knownSharedKeyWritesSnapshot() {
        const snapshot = {};
        for (const key of Object.keys(_knownSharedKeyWrites)) {
            snapshot[key] = {
                writeId: _knownSharedKeyWrites[key].writeId,
                writerId: _knownSharedKeyWrites[key].writerId
            };
            if (Number.isInteger(
                _knownSharedKeyWrites[key].confirmedRevision
            )) {
                snapshot[key].confirmedRevision =
                    _knownSharedKeyWrites[key].confirmedRevision;
            }
        }
        return snapshot;
    }
    function _rememberServerKeyRevisions(revisions, acceptedSettings) {
        if (!revisions || typeof revisions !== 'object') return;
        for (const key of Object.keys(revisions)) {
            if (_SHARED_SETTINGS_KEYS.indexOf(key) === -1) continue;
            if (acceptedSettings
                && !Object.prototype.hasOwnProperty.call(acceptedSettings, key)) continue;
            const revision = revisions[key];
            if (!Number.isInteger(revision) || revision < 0) continue;
            _knownServerKeyRevisions[key] = Math.max(
                Number.isInteger(_knownServerKeyRevisions[key])
                    ? _knownServerKeyRevisions[key]
                    : 0,
                revision
            );
        }
    }
    function _knownServerKeyRevisionsSnapshot() {
        return Object.assign({}, _knownServerKeyRevisions);
    }
    function _confirmSharedKeyWrites(
        payload,
        serverSettings,
        revision
    ) {
        if (!Number.isInteger(revision) || !serverSettings
            || typeof serverSettings !== 'object') return;
        const currentSettings = getConversationSettings();
        for (const key of Object.keys(payload || {})) {
            if (key === 'independentAsrEnabled') continue;
            if (payload[key] !== serverSettings[key]) continue;
            if (currentSettings[key] !== serverSettings[key]) continue;
            const currentToken = _knownSharedKeyWrites[key];
            if (!currentToken) continue;
            currentToken.confirmedRevision = Math.max(
                Number.isInteger(currentToken.confirmedRevision)
                    ? currentToken.confirmedRevision
                    : 0,
                revision
            );
        }
    }
    function _serverAsrDecision(data) {
        const decisions = data && data.decisions;
        return _normalizeServerAsrDecision(
            decisions && decisions.independentAsrEnabled,
            true
        );
    }
    function _adoptAsrDecisionTuple(value, serverAuthoritative) {
        const decision = _normalizeServerAsrDecision(value, serverAuthoritative);
        const alreadyCurrent = _asrDecisionsEqual(
            decision,
            _lastAsrDecision
        );
        if (!alreadyCurrent
            && !_asrDecisionOutranks(decision, _lastAsrDecision)) return false;
        if (!alreadyCurrent) {
            _lastAsrDecision = decision;
        }
        _asrDecisionWriteIdFloor = Math.max(
            _asrDecisionWriteIdFloor,
            decision.writeId
        );
        return true;
    }
    function _rebaseAsrDecisionForReset(serverDecision, resetValue) {
        let floor = _asrDecisionWriteIdFloor;
        if (_lastAsrDecision) {
            floor = Math.max(floor, _lastAsrDecision.writeId);
        }
        if (serverDecision) {
            floor = Math.max(floor, serverDecision.writeId);
        }
        if (floor >= Number.MAX_SAFE_INTEGER - 1) {
            _asrDecisionWriteIdFloor = floor;
            _lastAsrDecision = null;
            return;
        }
        const writeId = Math.max(Date.now(), floor + 1);
        // Materialize reset authority as a matching decision tuple. The reset
        // writeback sends this tuple instead of asking the server to mint a
        // potentially later legacy tuple; a user toggle made while that
        // request is in flight therefore mints strictly above the response.
        _lastAsrDecision = {
            writeId,
            writerId: _SHARED_WRITER_ID,
            value: resetValue
        };
        _asrDecisionWriteIdFloor = writeId;
    }
    function _responseEtag(response) {
        try {
            return response && response.headers && typeof response.headers.get === 'function'
                ? response.headers.get('ETag')
                : null;
        } catch (_) {
            return null;
        }
    }
    function _adoptServerAsrDecision(data) {
        const decision = _serverAsrDecision(data);
        if (!_adoptAsrDecisionTuple(decision, true)) return false;
        // Decision ordering is independent from localStorage envelope ordering.
        // In particular, a server clock ahead of this browser must not raise the
        // shared-write floor and make later envelopes invalid to sibling windows.
        const serverSettings = data && data.settings;
        if (serverSettings
            && typeof serverSettings.independentAsrEnabled === 'boolean'
            && serverSettings.independentAsrEnabled === decision.value) {
            applySharedRuntimeSettings({
                independentAsrEnabled: serverSettings.independentAsrEnabled
            });
            S.settingsHydrated = true;
            if (_settingsBaseline) {
                _settingsBaseline.independentAsrEnabled = S.independentAsrEnabled;
            }
        }
        return true;
    }
    function _mergeConversationSettingsSnapshot(data, preservedKeys) {
        const serverSettings = _serverSettingsForMerge(data);
        if (!serverSettings || typeof serverSettings !== 'object') return;
        const protectedKeys = preservedKeys || _pendingSettingsKeys;
        const resetSnapshot = !!(data && data.reset === true);
        const rawServerAsrDecision = _serverAsrDecision(data);
        const resetReplacesAsrDecision = resetSnapshot
            && !protectedKeys.has('independentAsrEnabled')
            && typeof serverSettings.independentAsrEnabled === 'boolean';
        if (resetReplacesAsrDecision) {
            _rebaseAsrDecisionForReset(
                rawServerAsrDecision,
                serverSettings.independentAsrEnabled
            );
        }
        // A reset tombstone invalidates the persisted tuple. Preserve a pending
        // local choice, or adopt the materialized reset default without
        // re-adopting the stale pre-reset tuple.
        const adoptedAsr = resetSnapshot
            ? false
            : _adoptServerAsrDecision(data);
        const serverAsrDecision = resetSnapshot ? null : rawServerAsrDecision;
        const preserveLocalAsrDecision = resetSnapshot
            ? !resetReplacesAsrDecision
            : !!(_lastAsrDecision
            && (!serverAsrDecision
                || _asrDecisionOutranks(_lastAsrDecision, serverAsrDecision)));
        const acceptedSettings = {};
        for (const key of Object.keys(serverSettings)) {
            if (key === 'independentAsrEnabled'
                && (adoptedAsr || preserveLocalAsrDecision)) continue;
            if (protectedKeys.has(key)) continue;
            if (serverSettings[key] !== undefined) {
                acceptedSettings[key] = serverSettings[key];
            }
        }
        let changed = applySharedRuntimeSettings(acceptedSettings);
        // Preserve server-only conversation keys that are intentionally not in
        // the cross-window shared-settings list.
        for (const key of Object.keys(acceptedSettings)) {
            if (_SHARED_SETTINGS_KEYS.indexOf(key) !== -1 || key === 'userLanguage') continue;
            if (S[key] !== acceptedSettings[key]) changed = true;
            S[key] = acceptedSettings[key];
            if (Object.prototype.hasOwnProperty.call(window, key)) {
                window[key] = S[key];
            }
        }
        _settingsMergedFromServer = true;
        S.settingsHydrated = true;
        S.independentAsrAuthoritative = true;
        if (_settingsBaseline) {
            for (const key of Object.keys(serverSettings)) {
                if (protectedKeys.has(key)) continue;
                if (serverSettings[key] !== undefined) {
                    _settingsBaseline[key] = S[key];
                }
            }
        }
        // Persist the reconciled runtime snapshot for offline restarts and
        // notify sibling windows, but do not advertise server winners as fresh
        // user intent or enqueue another POST.
        saveSettings({
            skipServerSync: true,
            serverMerged: true,
            serverAuthoritativeKeys: Object.keys(acceptedSettings).filter(
                (key) => _SHARED_SETTINGS_KEYS.indexOf(key) !== -1
            )
        });
        if (changed) {
            if (typeof window.appProactive !== 'undefined'
                && window.appProactive.scheduleProactiveChat) {
                window.appProactive.scheduleProactiveChat();
            } else if (typeof window.scheduleProactiveChat === 'function') {
                window.scheduleProactiveChat();
            }
        }
    }
    // Bounded gate for the boot settings GET: settings POST bodies are built
    // at send time AFTER awaiting this gate, so once the GET settled the merge
    // has already run and fields the user never touched carry server truth
    // instead of boot defaults. Starts resolved (no GET in flight yet) and
    // never rejects, so the sync chain can never stall on it.
    let _settingsGetGate = Promise.resolve();
    const SETTINGS_GET_GATE_TIMEOUT_MS = 3000;
    // Whether server values were actually MERGED into the local settings view.
    // Deliberately NOT "the boot GET attempt finished" (Codex P2, round 16):
    // those are different facts, and only the merge licenses a full-snapshot
    // write. The bound above only preserves liveness — it must never turn into
    // authority: while no merge has happened the local snapshot still holds
    // pre-merge boot/localStorage values for untouched keys, and a FULL write
    // would overwrite the server-persisted preferences (independentAsrEnabled
    // included). That is true whether the GET is still in flight OR resolved to
    // null (HTTP error, bad JSON, success:false, empty body) — a failed read
    // teaches this client nothing about server truth, so it must not re-enable
    // full writes. The backend also resolves telemetry BEFORE reading the
    // settings file (main_routers/config_router/preferences.py
    // get_conversation_settings), so a slow GET would resume by reading back
    // the file such a POST just overwrote and the field-level merge could no
    // longer restore the originals.
    // While this flag is false every POST carries ONLY the user-dirty keys
    // (see _pickDirtySettings), which loses nothing: the backend MERGES partial
    // payloads, so each user change is persisted per key and untouched keys
    // simply keep server truth. It starts false (nothing merged yet; loadSettings
    // arms the boot GET at module load, below) and flips to true only inside the
    // merge callback, i.e. exactly when a usable server result was applied —
    // late-settling boots therefore still resume full writes and converge the
    // server. A permanently failing GET keeps this window dirty-only for the
    // whole session on purpose: that mode never blocks persistence, and the
    // alternative (re-fetching from the periodic timer) would trade a
    // guaranteed-safe path for extra requests without persisting anything the
    // dirty-only path does not already persist. A fresh page load re-runs the
    // GET and restores full writes on its first successful merge.
    let _settingsMergedFromServer = false;
    // Serialization tail for conversation-settings POSTs (Codex P2): rapid
    // successive syncSettingsToServer calls used to issue concurrent POSTs,
    // and the backend saves each one in its own asyncio.to_thread, so the
    // OLDER request could finish LAST and persist a stale toggle value. Every
    // sync now queues behind this tail; runSync never rejects, so the tail
    // can never become a permanently rejected promise that stalls the chain.
    //
    // The tail remains per-JS-realm, so cross-window requests can overlap.
    // Their server persistence is ordered separately by the ETag CAS protocol
    // below plus the same ASR decision tuple localStorage already uses.
    let _syncChainTail = Promise.resolve();
    let _conversationSettingsEtag = null;
    let _conversationSettingsRevision = null;
    // Cross-window mutation version represented by the current ETag. A field
    // explicitly edited after this watermark is absent from that server
    // revision even when the edit arrived before the next CAS request began.
    // Preserve such fields across a 412; mutationVersionAtSend alone only sees
    // edits that arrive after the request snapshot.
    const _conversationSettingsEtagKeyMutationVersions = Object.create(null);
    const _CONVERSATION_SETTINGS_MAX_ATTEMPTS = 3;
    const _CONVERSATION_SETTINGS_REQUEST_TIMEOUT_MS = 15000;
    // 同步间隔（毫秒）：60秒
    const SYNC_INTERVAL_MS = 60000;
    // 「首启等 settings/telemetry 决议」专属 marker：只有 localStorage 走过首启分支才会写
    // 「1」，branch 决议后清掉。用 marker 在不在判断「是否仍在等待首次决议」，避免拿
    // 「没见过 branch 」当首启代名——升级用户也都没见过 branch，那个口径会误伤他们的
    // 既有偏好。offline 首启错过 branch 时 marker 留着，下次在线再补
    const _FIRST_LAUNCH_PENDING_KEY = '_neko_first_launch_branch_pending';
    const _SHARED_SETTINGS_KEYS = [
        'proactiveChatEnabled',
        'proactiveVisionEnabled',
        'proactiveVisionChatEnabled',
        'proactiveNewsChatEnabled',
        'proactiveCommunityChatEnabled',
        'proactiveVideoChatEnabled',
        'proactivePersonalChatEnabled',
        'proactiveMusicEnabled',
        'proactiveMemeEnabled',
        'proactiveMiniGameInviteEnabled',
        'mergeMessagesEnabled',
        'focusModeEnabled',
        'focusCognitionEnabled',
        'noiseReductionEnabled',
        'independentAsrEnabled',
        'voiceInputResourceOptimizationEnabled',
        'avatarReactionBubbleEnabled',
        'slopFilterEnabled',
        'proactiveChatInterval',
        'proactiveVisionInterval',
        'subtitleEnabled',
        'userLanguage',
        'textGuardMaxLength',
        'renderQuality',
        'targetFrameRate',
        'forgeDropEffectsEnabled'
    ];

    function _defaultConversationSettingsForReset() {
        return {
            proactiveChatEnabled: true,
            proactiveVisionEnabled: _isUserRegionChina(),
            proactiveVisionChatEnabled: true,
            proactiveNewsChatEnabled: false,
            proactiveCommunityChatEnabled: false,
            proactiveVideoChatEnabled: true,
            proactivePersonalChatEnabled: false,
            proactiveMusicEnabled: true,
            proactiveMemeEnabled: true,
            proactiveMiniGameInviteEnabled: true,
            mergeMessagesEnabled: false,
            focusModeEnabled: false,
            focusCognitionEnabled: true,
            noiseReductionEnabled: true,
            independentAsrEnabled: false,
            voiceInputResourceOptimizationEnabled: true,
            avatarReactionBubbleEnabled: true,
            slopFilterEnabled: true,
            proactiveChatInterval: Number.isFinite(C.DEFAULT_PROACTIVE_CHAT_INTERVAL)
                ? C.DEFAULT_PROACTIVE_CHAT_INTERVAL
                : 15,
            proactiveVisionInterval: Number.isFinite(C.DEFAULT_PROACTIVE_VISION_INTERVAL)
                ? C.DEFAULT_PROACTIVE_VISION_INTERVAL
                : 10,
            subtitleEnabled: false,
            userLanguage: null,
            textGuardMaxLength: 300
        };
    }

    function _serverSettingsForMerge(data) {
        const settings = data && data.settings;
        if (data && data.reset === true) {
            return Object.assign(
                _defaultConversationSettingsForReset(),
                settings && typeof settings === 'object' ? settings : {}
            );
        }
        return settings;
    }

    function getDefaultRenderQuality() {
        return S.renderQuality || 'medium';
    }

    function syncMouseTrackingRuntimeManagers() {
        const enabled = window.mouseTrackingEnabled !== false;
        [window.live2dManager, window.vrmManager, window.pngtuberManager].forEach((manager) => {
            if (manager && typeof manager.setMouseTrackingEnabled === 'function') {
                manager.setMouseTrackingEnabled(enabled);
            }
        });
    }

    /**
     * 获取对话相关设置（仅包含需要同步到服务器的设置）
     * 注意：不包含 renderQuality、targetFrameRate、mouseTrackingEnabled 等性能/外观设置
     */
    function getConversationSettings() {
        const settings = {
            proactiveChatEnabled: S.proactiveChatEnabled,
            proactiveVisionEnabled: S.proactiveVisionEnabled,
            proactiveVisionChatEnabled: S.proactiveVisionChatEnabled,
            proactiveNewsChatEnabled: S.proactiveNewsChatEnabled,
            proactiveCommunityChatEnabled: S.proactiveCommunityChatEnabled,
            proactiveVideoChatEnabled: S.proactiveVideoChatEnabled,
            proactivePersonalChatEnabled: S.proactivePersonalChatEnabled,
            proactiveMusicEnabled: S.proactiveMusicEnabled,
            proactiveMemeEnabled: S.proactiveMemeEnabled,
            proactiveMiniGameInviteEnabled: S.proactiveMiniGameInviteEnabled,
            mergeMessagesEnabled: S.mergeMessagesEnabled,
            focusModeEnabled: S.focusModeEnabled,
            focusCognitionEnabled: S.focusCognitionEnabled,
            noiseReductionEnabled: S.noiseReductionEnabled,
            independentAsrEnabled: S.independentAsrEnabled,
            voiceInputResourceOptimizationEnabled: S.voiceInputResourceOptimizationEnabled,
            avatarReactionBubbleEnabled: S.avatarReactionBubbleEnabled,
            slopFilterEnabled: S.slopFilterEnabled,
            proactiveChatInterval: S.proactiveChatInterval,
            proactiveVisionInterval: S.proactiveVisionInterval,
            subtitleEnabled: S.subtitleEnabled,
            textGuardMaxLength: S.textGuardMaxLength
        };
        // 只有在 S 上存在 userLanguage 属性时才包含（含 null，支持显式清除语义）
        if ('userLanguage' in S) {
            settings.userLanguage = S.userLanguage;
        }
        return settings;
    }

    /**
     * Record which conversation-settings keys the user explicitly changed by
     * diffing the current state against _settingsBaseline, then roll the
     * baseline forward. Runs synchronously inside every userInitiated sync,
     * so a toggle-and-back still leaves its key dirty (the set is monotone —
     * a key the user touched stays user-authoritative for the boot merge).
     */
    function _markUserDirtySettings() {
        const current = getConversationSettings();
        if (_settingsBaseline) {
            const keys = new Set(
                Object.keys(current).concat(Object.keys(_settingsBaseline))
            );
            keys.forEach((key) => {
                const cur = Object.prototype.hasOwnProperty.call(current, key)
                    ? current[key]
                    : undefined;
                const base = Object.prototype.hasOwnProperty.call(_settingsBaseline, key)
                    ? _settingsBaseline[key]
                    : undefined;
                if (cur !== base) {
                    _dirtySettingsKeys.add(key);
                    _pendingSettingsKeys.add(key);
                }
            });
        }
        _settingsBaseline = current;
    }

    /**
     * Monotonic id for shared-settings writes. The wall clock keeps ids
     * comparable across windows (same browser profile, one clock) and the
     * max()-style bump keeps them strictly increasing inside a window when two
     * saves land in the same millisecond. Ids are NOT globally unique: two
     * windows saving in the same millisecond before observing each other mint
     * the same id, which is unavoidable at mint time. A receiver can therefore
     * tell strictly-older from strictly-newer, and resolves an exact tie on
     * explicit intent instead (see the asrWriteIsNewer tie rule in the storage
     * listener).
     */
    function _nextSharedWriteId() {
        let now = 0;
        try { now = Date.now(); } catch (_) { now = 0; }
        // Floor the mint by the highest id this window has ever APPLIED, not
        // just the highest it has minted. Without it, a window that already
        // applied another window's write could mint an id at or below it and
        // have its own write read as superseded — discarding a genuine
        // cross-window ASR toggle, not just an incidental copy.
        // This covers only the case where the other write was already OBSERVED
        // here. A genuinely CONCURRENT same-millisecond tie (neither window has
        // processed the other's storage event yet) cannot be broken at mint
        // time at all; the listener resolves it on explicit intent instead.
        const idFloor = Math.max(_lastSharedWriteId, _lastAppliedSharedWriteId);
        _lastSharedWriteId = now > idFloor ? now : idFloor + 1;
        return _lastSharedWriteId;
    }

    function _nextAsrDecisionWriteId(envelopeWriteId) {
        const decisionFloor = Math.max(
            _asrDecisionWriteIdFloor,
            _lastAsrDecision
            && Number.isSafeInteger(_lastAsrDecision.writeId)
            ? _lastAsrDecision.writeId
            : 0
        );
        if (decisionFloor >= Number.MAX_SAFE_INTEGER - 1) {
            return envelopeWriteId;
        }
        return Math.max(envelopeWriteId, decisionFloor + 1);
    }

    /**
     * Keys of `snapshot` this window may claim explicit user authority over:
     * still-pending keys plus keys that diverge from the current dirty-diff
     * baseline. Do not use the monotone _dirtySettingsKeys set here: after an
     * acknowledged edit, a later unrelated save must not mint a fresh per-key
     * token for an old value. The divergence term matters because the
     * independent-ASR toggle handler persists locally (saveSettings with
     * skipServerSync) BEFORE its userInitiated sync runs _markUserDirtySettings,
     * so at write time the diff is the only evidence the key was just toggled.
     */
    function _collectExplicitSharedKeys(snapshot) {
        const keys = [];
        _SHARED_SETTINGS_KEYS.forEach((key) => {
            if (!Object.prototype.hasOwnProperty.call(snapshot, key)) return;
            const divergedFromBaseline = !!_settingsBaseline
                && Object.prototype.hasOwnProperty.call(_settingsBaseline, key)
                && _settingsBaseline[key] !== snapshot[key];
            if (_pendingSettingsKeys.has(key) || divergedFromBaseline) {
                keys.push(key);
            }
        });
        return keys;
    }

    function _settingDivergedFromBaseline(snapshot, key) {
        return !!_settingsBaseline
            && Object.prototype.hasOwnProperty.call(_settingsBaseline, key)
            && _settingsBaseline[key] !== snapshot[key];
    }

    /**
     * Persist the shared settings snapshot with its write metadata.
     * `hydrated` records whether this window held ANY authoritative settings
     * event when it wrote — informational only, because it also flips on a
     * user edit that never touched the ASR key. `asrAuthoritative` is the
     * per-key fact a receiver actually needs: whether THIS window's
     * independentAsrEnabled came from a merged server GET, an explicit ASR
     * toggle, or a cross-window ASR flip (S.independentAsrAuthoritative, the
     * same latch the start_session handshake gates on). Without it a window
     * whose boot GET never merged flips `hydrated` true on any unrelated edit
     * and then stamps its pre-merge boot ASR default as trustworthy, and a
     * window that HAD merged the server value adopts it, mis-stamps its next
     * handshake, and POSTs the wrong value back on its next full sync.
     */
    function _writeSharedSettings(
        snapshot,
        explicitKeys,
        pendingRecovery,
        serverAuthoritativeKeys
    ) {
        const payload = Object.assign({}, snapshot);
        payload[_SHARED_WRITE_META_KEY] = {
            writeId: _nextSharedWriteId(),
            writerId: _SHARED_WRITER_ID,
            changedKeys: explicitKeys || [],
            hydrated: S.settingsHydrated === true,
            asrAuthoritative: S.independentAsrAuthoritative === true,
            // A recovery write reasserts this window's pending values after it
            // filtered an incidental copy from another full snapshot. Another
            // pending window must not answer it with another recovery write,
            // or conflicting snapshots can bounce through localStorage forever.
            pendingRecovery: pendingRecovery === true
        };
        const ownMeta = payload[_SHARED_WRITE_META_KEY];
        _rememberSharedKeyWrites(ownMeta.changedKeys, ownMeta);
        // Server authority and browser write tokens are separate clocks. A
        // server winner must carry its real revision instead of borrowing this
        // broadcast envelope's fresh writeId, which could make an old GET look
        // newer than an explicit edit the sender never observed.
        if (Array.isArray(serverAuthoritativeKeys)
            && serverAuthoritativeKeys.length > 0
            && Number.isInteger(_conversationSettingsRevision)) {
            ownMeta.serverRevision = _conversationSettingsRevision;
            ownMeta.serverAuthoritativeKeys =
                serverAuthoritativeKeys.slice();
            // Persist the server floor independently from source provenance. A
            // reloaded window may need the revision before its boot GET returns,
            // but the merge envelope did not produce any of these field values.
            for (const key of ownMeta.serverAuthoritativeKeys) {
                _knownServerKeyRevisions[key] = Math.max(
                    Number.isInteger(_knownServerKeyRevisions[key])
                        ? _knownServerKeyRevisions[key]
                        : 0,
                    ownMeta.serverRevision
                );
            }
        }
        ownMeta.knownKeyWrites = _knownSharedKeyWritesSnapshot();
        ownMeta.serverKeyRevisions = _knownServerKeyRevisionsSnapshot();
        if (ownMeta.changedKeys.indexOf('independentAsrEnabled') !== -1) {
            _noteAsrDecision(
                _nextAsrDecisionWriteId(ownMeta.writeId),
                ownMeta.writerId,
                snapshot.independentAsrEnabled,
                _settingDivergedFromBaseline(
                    snapshot,
                    'independentAsrEnabled'
                )
            );
        }
        if (ownMeta.changedKeys.indexOf('voiceInputResourceOptimizationEnabled') !== -1) {
            const isFreshOptimizationChoice = _settingDivergedFromBaseline(
                snapshot,
                'voiceInputResourceOptimizationEnabled'
            );
            _noteOptimizationDecision(
                ownMeta.writeId,
                ownMeta.writerId,
                snapshot.voiceInputResourceOptimizationEnabled,
                isFreshOptimizationChoice
            );
            if (isFreshOptimizationChoice) {
                _optimizationDecisionPendingSync = true;
            }
        }
        // Stamp the ASR key with the id of the decision that PRODUCED this
        // value. _noteAsrDecision already refuses to advance the LOCAL decision
        // for a mere re-assertion; the TRANSMITTED id must agree, or an
        // unrelated save re-stamps a stale choice with a fresh id and reverts a
        // newer toggle in another window. Omitted when the local decision does
        // not describe the value being written, so receivers fall back to the
        // write id exactly as they do for previous-build snapshots.
        if (_lastAsrDecision
            && _lastAsrDecision.value === snapshot.independentAsrEnabled) {
            ownMeta.asrDecision = {
                writeId: _lastAsrDecision.writeId,
                writerId: _lastAsrDecision.writerId,
                value: _lastAsrDecision.value
            };
        }
        if (_lastOptimizationDecision
            && _lastOptimizationDecision.value
                === snapshot.voiceInputResourceOptimizationEnabled) {
            ownMeta.optimizationDecision = {
                writeId: _lastOptimizationDecision.writeId,
                writerId: _lastOptimizationDecision.writerId,
                value: _lastOptimizationDecision.value
            };
            ownMeta.optimizationDecisionPendingSync =
                _optimizationDecisionPendingSync === true;
        }
        try {
            localStorage.setItem('project_neko_settings', JSON.stringify(payload));
        } catch (error) {
            // Local persistence/cross-window fan-out is best-effort. Do not
            // abort saveSettings before its CAS sync can persist and apply the
            // user's choice to the backend and active runtime.
            console.warn('[app-settings] 本地设置持久化失败，继续服务器同步:', error);
        }
    }

    /**
     * Read the write metadata of an incoming shared snapshot. Returns null for
     * payloads that carry none — a window still running the previous build —
     * so those keep falling back to the legacy value-difference detection.
     */
    function _readSharedWriteMeta(settings) {
        const meta = settings ? settings[_SHARED_WRITE_META_KEY] : null;
        if (!meta || typeof meta !== 'object') return null;
        if (!_isValidAsrWriteId(meta.writeId)) return null;
        const knownKeyWrites = {};
        const serverKeyRevisions = {};
        const rawKnownKeyWrites = meta.knownKeyWrites;
        const knownKeyWritesPresent = !!rawKnownKeyWrites
            && typeof rawKnownKeyWrites === 'object';
        if (knownKeyWritesPresent) {
            for (const key of Object.keys(rawKnownKeyWrites)) {
                const token = rawKnownKeyWrites[key];
                if (!token || typeof token !== 'object') continue;
                if (!_isValidAsrWriteId(token.writeId)) continue;
                knownKeyWrites[key] = {
                    writeId: token.writeId,
                    writerId: typeof token.writerId === 'string'
                        ? token.writerId
                        : ''
                };
                if (Number.isInteger(token.confirmedRevision)
                    && token.confirmedRevision >= 0) {
                    knownKeyWrites[key].confirmedRevision =
                        token.confirmedRevision;
                }
            }
        }
        const rawServerKeyRevisions = meta.serverKeyRevisions;
        if (rawServerKeyRevisions
            && typeof rawServerKeyRevisions === 'object') {
            for (const key of Object.keys(rawServerKeyRevisions)) {
                const revision = rawServerKeyRevisions[key];
                if (_SHARED_SETTINGS_KEYS.indexOf(key) !== -1
                    && Number.isInteger(revision)
                    && revision >= 0) {
                    serverKeyRevisions[key] = revision;
                }
            }
        }
        const serverRevision = Number.isInteger(meta.serverRevision)
            && meta.serverRevision >= 0
            ? meta.serverRevision
            : null;
        const serverAuthoritativeKeys =
            Array.isArray(meta.serverAuthoritativeKeys)
                ? meta.serverAuthoritativeKeys.filter(
                    (key) => _SHARED_SETTINGS_KEYS.indexOf(key) !== -1
                )
                : [];
        if (serverRevision !== null) {
            for (const key of serverAuthoritativeKeys) {
                serverKeyRevisions[key] = Math.max(
                    Number.isInteger(serverKeyRevisions[key])
                        ? serverKeyRevisions[key]
                        : 0,
                    serverRevision
                );
                // Upgrade snapshots written by the previous implementation:
                // it stamped the merge envelope itself as field provenance.
                const token = knownKeyWrites[key];
                if (token
                    && token.writeId === meta.writeId
                    && token.writerId === (
                        typeof meta.writerId === 'string' ? meta.writerId : ''
                    )
                    && token.confirmedRevision === serverRevision) {
                    delete knownKeyWrites[key];
                }
            }
        }
        return {
            writeId: meta.writeId,
            // Absent on snapshots written by the previous build: '' sorts below
            // every live writer id, so an untagged concurrent write never
            // outranks this window's own explicit choice.
            writerId: typeof meta.writerId === 'string' ? meta.writerId : '',
            changedKeys: Array.isArray(meta.changedKeys) ? meta.changedKeys : [],
            hydrated: meta.hydrated === true,
            // Absent on snapshots written by the previous build: default false
            // (fail closed). Such a writer's non-explicit ASR value is simply
            // not adopted by an already-hydrated receiver; a GENUINE toggle
            // from that build still carries independentAsrEnabled in
            // changedKeys and keeps flowing through asrChangedByOtherWindow.
            asrAuthoritative: meta.asrAuthoritative === true,
            pendingRecovery: meta.pendingRecovery === true,
            serverRevision,
            serverAuthoritativeKeys,
            knownKeyWrites,
            knownKeyWritesPresent,
            serverKeyRevisions,
            // Absent on previous-build snapshots and on writers with no
            // matching decision: null routes the comparison back to
            // (writeId, writerId), i.e. today's behaviour.
            asrDecision: (meta.asrDecision
                && _isValidAsrWriteId(
                    meta.asrDecision.writeId,
                    Number.isInteger(meta.serverRevision)
                ))
                ? {
                    writeId: meta.asrDecision.writeId,
                    writerId: typeof meta.asrDecision.writerId === 'string'
                        ? meta.asrDecision.writerId
                        : '',
                    value: meta.asrDecision.value
                }
                : null,
            optimizationDecision: (meta.optimizationDecision
                && _isValidAsrWriteId(
                    meta.optimizationDecision.writeId,
                    Number.isInteger(meta.serverRevision)
                ))
                ? {
                    writeId: meta.optimizationDecision.writeId,
                    writerId: typeof meta.optimizationDecision.writerId === 'string'
                        ? meta.optimizationDecision.writerId
                        : '',
                    value: meta.optimizationDecision.value
                }
                : null,
            // Snapshots from the previous PR head already carry the decision
            // tuple but not this bit. Treat those as pending so an interrupted
            // POST cannot be forgotten during rollout.
            optimizationDecisionPendingSync: !!meta.optimizationDecision
                && meta.optimizationDecisionPendingSync !== false
        };
    }

    /**
     * Build the POST body for a sync that runs before any server merge landed
     * (GET still in flight, or it failed and none ever will): only the keys the
     * user explicitly changed are authoritative, every other local value is a
     * pre-merge boot value that must not overwrite the server copy. Safe
     * because the backend MERGES
     * partial payloads instead of replacing the entry wholesale — see
     * utils/preferences.py save_global_conversation_settings, which copies the
     * existing global entry and applies `global_pref.update(filtered_settings)`
     * (fields absent from the request keep their persisted values), and
     * validates per field, so omitted keys are simply left alone.
     */
    function _pickDirtySettings(settings) {
        const partial = {};
        _pendingSettingsKeys.forEach((key) => {
            if (Object.prototype.hasOwnProperty.call(settings, key)) {
                partial[key] = settings[key];
            }
        });
        return partial;
    }

    function _clearAcknowledgedPendingSettings(payload) {
        const current = getConversationSettings();
        for (const key of Object.keys(payload)) {
            // A later local edit may have happened while this POST was in
            // flight. Clear only keys whose current value is still exactly the
            // value the server just acknowledged.
            if (Object.prototype.hasOwnProperty.call(current, key)
                && current[key] === payload[key]) {
                _pendingSettingsKeys.delete(key);
            }
        }
    }

    function _settingsChangedSince(snapshot, mutationVersion) {
        const changedKeys = new Set();
        const current = getConversationSettings();
        const keys = new Set(Object.keys(snapshot).concat(Object.keys(current)));
        keys.forEach((key) => {
            const before = Object.prototype.hasOwnProperty.call(snapshot, key)
                ? snapshot[key]
                : undefined;
            const now = Object.prototype.hasOwnProperty.call(current, key)
                ? current[key]
                : undefined;
            if (before !== now
                || (_crossWindowKeyMutationVersions[key] || 0) > mutationVersion) {
                changedKeys.add(key);
            }
        });
        return changedKeys;
    }

    function _etagKeyMutationVersionsSnapshot() {
        const snapshot = {};
        _SHARED_SETTINGS_KEYS.forEach((key) => {
            snapshot[key] =
                _conversationSettingsEtagKeyMutationVersions[key] || 0;
        });
        return snapshot;
    }

    function _crossWindowSettingsNewerThanEtag(etagKeyMutationVersions) {
        const changedKeys = new Set();
        _SHARED_SETTINGS_KEYS.forEach((key) => {
            if ((_crossWindowKeyMutationVersions[key] || 0)
                > (etagKeyMutationVersions[key] || 0)) {
                changedKeys.add(key);
            }
        });
        return changedKeys;
    }

    function _markEtagConfirmedSharedSettings(
        settingsAtSend,
        serverSettings,
        mutationVersionAtSend
    ) {
        if (!serverSettings || typeof serverSettings !== 'object') return;
        _SHARED_SETTINGS_KEYS.forEach((key) => {
            if (!Object.prototype.hasOwnProperty.call(serverSettings, key)) return;
            if (!Object.prototype.hasOwnProperty.call(settingsAtSend, key)
                || settingsAtSend[key] !== serverSettings[key]) return;
            _conversationSettingsEtagKeyMutationVersions[key] =
                mutationVersionAtSend;
        });
    }

    function _noteCrossWindowMutations(settings, explicitKeys) {
        _SHARED_SETTINGS_KEYS.forEach((key) => {
            if (!Object.prototype.hasOwnProperty.call(settings, key)) return;
            // Metadata declares user intent per key, including a same-value
            // choice made while a request is in flight. Server-merge broadcasts
            // carry an empty list; metadata-less previous-build writers retain
            // their value-change fallback.
            if (explicitKeys) {
                if (explicitKeys.indexOf(key) === -1) return;
            } else if (S[key] === settings[key]) {
                return;
            }
            _crossWindowMutationVersion += 1;
            _crossWindowKeyMutationVersions[key] = _crossWindowMutationVersion;
        });
    }

    function applySharedRuntimeSettings(settings) {
        if (!settings || typeof settings !== 'object') return false;
        let changed = false;
        _SHARED_SETTINGS_KEYS.forEach((key) => {
            if (!Object.prototype.hasOwnProperty.call(settings, key)) return;
            if (S[key] !== settings[key]) {
                S[key] = settings[key];
                changed = true;
            }
            // saveSettings() prefers several window.* mirrors over S. Keep every
            // mirror already present on this window aligned with the accepted
            // shared value so a later save cannot roll the merge back.
            if (Object.prototype.hasOwnProperty.call(window, key)) {
                window[key] = S[key];
            }
        });
        if (Object.prototype.hasOwnProperty.call(settings, 'noiseReductionEnabled')) {
            try {
                localStorage.setItem(
                    'neko_noise_reduction',
                    S.noiseReductionEnabled ? '1' : '0'
                );
            } catch (_) { }
        }
        if (
            Object.prototype.hasOwnProperty.call(settings, 'userLanguage') &&
            S.userLanguage !== settings.userLanguage
        ) {
            S.userLanguage = settings.userLanguage;
            changed = true;
        }
        if (changed && S.renderQuality) {
            window.cursorFollowPerformanceLevel = U.mapRenderQualityToFollowPerf(S.renderQuality);
        }
        return changed;
    }

    function isManualScreenShareActive() {
        try {
            const button = document.getElementById('screenButton');
            return !!(button && button.classList.contains('active'));
        } catch (_) {
            return false;
        }
    }

    function stopVisionAfterPrivacyEnabled() {
        if (S.proactiveVisionEnabled !== false) return;

        try {
            if (typeof window.stopProactiveVisionDuringSpeech === 'function') {
                window.stopProactiveVisionDuringSpeech();
            }
        } catch (error) {
            console.warn('[app-settings] 停止语音主动视觉失败:', error);
        }

        if (isManualScreenShareActive()) return;

        try {
            if (typeof window.stopScreening === 'function') {
                window.stopScreening();
            }
        } catch (error) {
            console.warn('[app-settings] 停止屏幕发送循环失败:', error);
        }

        try {
            if (S.screenCaptureStream && typeof S.screenCaptureStream.getTracks === 'function') {
                S.screenCaptureStream.getTracks().forEach((track) => {
                    try { track.stop(); } catch (_) { }
                });
            }
        } catch (error) {
            console.warn('[app-settings] 释放隐私模式屏幕流失败:', error);
        } finally {
            S.screenCaptureStream = null;
            S.screenCaptureStreamLastUsed = null;
            if (S.screenCaptureStreamIdleTimer) {
                clearTimeout(S.screenCaptureStreamIdleTimer);
                S.screenCaptureStreamIdleTimer = null;
            }
        }
    }

    /**
     * 从服务器加载对话设置（异步）
     * 成功时返回设置对象，失败时返回 null
     */
    async function loadSettingsFromServer() {
        try {
            const response = await fetch('/api/config/conversation-settings', {
                method: 'GET',
                headers: { 'Content-Type': 'application/json' }
            });
            if (!response.ok) return null;
            const data = await response.json();
            if (!data.success) return null;
            const hasSettings = data.settings && Object.keys(data.settings).length > 0;
            const telemetryBranch = (typeof data.telemetryBranch === 'string' && data.telemetryBranch) || null;
            const etag = _responseEtag(response);
            if (!hasSettings && !telemetryBranch && !etag) return null;
            return {
                settings: hasSettings ? data.settings : null,
                telemetryBranch,
                etag,
                revision: Number.isInteger(data.revision) ? data.revision : null,
                decisions: data.decisions || null,
                reset: data.reset === true
            };
        } catch (e) {
            console.warn('[app-settings] 从服务器加载设置失败:', e);
        }
        return null;
    }

    function _fetchConversationSettingsJsonWithTimeout(url, options) {
        const AbortControllerCtor =
            (typeof window.AbortController === 'function'
                && window.AbortController)
            || (typeof AbortController === 'function' && AbortController)
            || null;
        const abortController = AbortControllerCtor
            ? new AbortControllerCtor()
            : null;
        const requestOptions = Object.assign({}, options);
        if (abortController) {
            requestOptions.signal = abortController.signal;
        }
        let timeoutId = null;
        const timeoutPromise = new Promise((_, reject) => {
            timeoutId = setTimeout(() => {
                if (abortController) abortController.abort();
                reject(new Error('conversation settings request timed out'));
            }, _CONVERSATION_SETTINGS_REQUEST_TIMEOUT_MS);
            if (timeoutId && typeof timeoutId.unref === 'function') {
                timeoutId.unref();
            }
        });
        const requestPromise = (async () => {
            const response = await fetch(url, requestOptions);
            const data = await response.json();
            return { response, data };
        })();
        return Promise.race([requestPromise, timeoutPromise]).finally(() => {
            if (timeoutId !== null && typeof clearTimeout === 'function') {
                clearTimeout(timeoutId);
            }
        });
    }

    /**
     * 将对话设置同步到服务器（异步，不阻塞）
     * 用于定期备份和跨会话持久化
     *
     * @param {{ userInitiated?: boolean }} [options] 用户显式改设置的路径必须传
     *   userInitiated: true——只有这类调用才把设置标记为已水合（S.settingsHydrated）。
     *   周期同步 startPeriodicSync 不传，永远不标记。
     */
    async function syncSettingsToServer(options) {
        const userInitiated = !!(options && options.userInitiated);
        // 只有用户显式改设置的路径（saveSettings 完整路径、app-audio-capture.js 的独立
        // ASR 开关 handler，均传 userInitiated: true）才标记设置已水合
        // （S.settingsHydrated，见 app-state.js）：用户动作即使发生在 server GET 之前
        // 也是权威值，且本 POST 会把完整本地设置对象覆写到服务器；POST 失败也要立刻
        // 算权威，否则 start_session 握手退回省略字段、后端读到的还是用户刚改掉的旧
        // 持久化值，握手兜底（attachStartSessionHandshake）就失效了，所以在 await 前
        // 同步标记。周期同步 startPeriodicSync 不传 userInitiated、不标记——GET 持续
        // 失败（catch/finally 也会启动 periodic）时它跑的是未水合的启动默认值，标记
        // 会让握手把 boot 默认 false 盖掉后端持久化的 true。
        // 首启初始化那次 saveSettings 走 skipServerSync 不进这里，不会误标记。
        if (userInitiated) {
            S.settingsHydrated = true;
            // Synchronously (before any await) record WHICH keys this user
            // change touched: the in-flight boot GET's merge preserves exactly
            // these dirty keys while still hydrating every untouched field
            // from the server, instead of dropping the whole merge.
            _markUserDirtySettings();
            // Per-key authority for the one key the start_session handshake
            // carries. An unrelated user change (settings popup, subtitle
            // toggle, chat-window translate toggle) also sets settingsHydrated,
            // but that says nothing about independentAsrEnabled: while the boot
            // GET is pending or permanently failing it is still the boot
            // default, and stamping it would override the backend's persisted
            // choice. Mark ASR authority only when THIS user change actually
            // touched the ASR key, synchronously before any await so the very
            // next start_session already carries it.
            if (_dirtySettingsKeys.has('independentAsrEnabled')) S.independentAsrAuthoritative = true;
            if (_dirtySettingsKeys.has('voiceInputResourceOptimizationEnabled')) {
                S.voiceInputResourceOptimizationAuthoritative = true;
            }
        }
        // Serialize the POST behind any in-flight sync (Codex P2): the
        // settings snapshot is built inside runSync, at SEND time — after the
        // predecessor completed — so the last-issued request always carries
        // the final local state and at most one request is in flight, making
        // completion order equal issue order (a stale body can never win the
        // backend persistence race). The hydration/dirty-key marks above
        // stay synchronous at call time so the handshake stamp and the
        // field-level merge guard keep their pre-await semantics.
        const runSync = async () => {
            // Wait (bounded, never rejecting) for the boot settings GET to
            // settle before building the send-time snapshot: after the merge,
            // fields the user never touched carry server truth, so this POST
            // cannot overwrite persisted preferences with boot defaults. When
            // the GET outlives the bound (or fails outright) the POST still
            // goes out (liveness), but restricted to the user-dirty keys —
            // see _settingsMergedFromServer.
            await _settingsGetGate;
            try {
                const controller = window.NekoHomeTutorialFeatureController;
                if (controller && typeof controller.isActive === 'function' && controller.isActive()) {
                    console.log('[app-settings] home tutorial suppression active, skip conversation settings sync');
                    return;
                }
            } catch (_) {
                // keep settings sync best-effort if the tutorial controller is unavailable
            }
            for (let attempt = 0; attempt < _CONVERSATION_SETTINGS_MAX_ATTEMPTS; attempt += 1) {
                const settings = getConversationSettings();
                const mutationVersionAtSend = _crossWindowMutationVersion;
                const etagKeyMutationVersionsAtSend =
                    _etagKeyMutationVersionsSnapshot();
                const mergedAtSend = _settingsMergedFromServer;
                // Full snapshot only once server values were actually merged. If
                // the gate opened on its timeout instead — or the GET resolved to
                // null and merged nothing — a full body would clobber every
                // untouched server-persisted preference with this boot's values.
                // Rebuild on every CAS retry so a newer cross-window ASR decision
                // adopted from the conflict response replaces the stale body.
                const payload = _settingsMergedFromServer ? settings : _pickDirtySettings(settings);
                if (Object.keys(payload).length === 0) {
                    return;
                }
                const requestDecision = (
                    Object.prototype.hasOwnProperty.call(payload, 'independentAsrEnabled')
                    && _lastAsrDecision
                    && _lastAsrDecision.value === payload.independentAsrEnabled
                ) ? {
                    writeId: _lastAsrDecision.writeId,
                    writerId: _lastAsrDecision.writerId,
                    value: _lastAsrDecision.value
                } : null;
                const optimizationDecisionAtSend = (
                    Object.prototype.hasOwnProperty.call(
                        payload,
                        'voiceInputResourceOptimizationEnabled'
                    )
                    && _lastOptimizationDecision
                    && _lastOptimizationDecision.value
                        === payload.voiceInputResourceOptimizationEnabled
                )
                    ? Object.assign({}, _lastOptimizationDecision)
                    : null;
                const headers = { 'Content-Type': 'application/json' };
                if (_conversationSettingsEtag) {
                    headers['If-Match'] = _conversationSettingsEtag;
                }
                if (mergedAtSend) {
                    headers['X-Conversation-Settings-Full-Snapshot'] = '1';
                }
                if (requestDecision) {
                    headers['X-Conversation-Settings-ASR-Decision'] =
                        JSON.stringify(requestDecision);
                }
                try {
                    const { response, data } =
                        await _fetchConversationSettingsJsonWithTimeout(
                            '/api/config/conversation-settings',
                            {
                                method: 'POST',
                                headers,
                                body: JSON.stringify(payload)
                            }
                        );
                    const nextEtag = _responseEtag(response);
                    if (nextEtag) _conversationSettingsEtag = nextEtag;
                    if (Number.isInteger(data.revision)) {
                        _conversationSettingsRevision = data.revision;
                    }
                    if (response.status === 412) {
                        const preservedKeys = new Set(_pendingSettingsKeys);
                        _crossWindowSettingsNewerThanEtag(
                            etagKeyMutationVersionsAtSend
                        ).forEach((key) => {
                            preservedKeys.add(key);
                        });
                        _settingsChangedSince(settings, mutationVersionAtSend).forEach((key) => {
                            preservedKeys.add(key);
                        });
                        _mergeConversationSettingsSnapshot(data, preservedKeys);
                        if (attempt + 1 < _CONVERSATION_SETTINGS_MAX_ATTEMPTS) {
                            continue;
                        }
                    }
                    if (!response.ok) {
                        console.error('[app-settings] 同步设置到服务器失败: HTTP', response.status);
                        return;
                    }
                    if (!data.success) {
                        console.error('[app-settings] 同步设置到服务器失败:', data.error || '未知错误');
                        return;
                    }
                    if (
                        optimizationDecisionAtSend
                        && _lastOptimizationDecision
                        && optimizationDecisionAtSend.writeId
                            === _lastOptimizationDecision.writeId
                        && optimizationDecisionAtSend.writerId
                            === _lastOptimizationDecision.writerId
                        && optimizationDecisionAtSend.value
                            === _lastOptimizationDecision.value
                        && S.voiceInputResourceOptimizationEnabled
                            === optimizationDecisionAtSend.value
                    ) {
                        _optimizationDecisionPendingSync = false;
                        _writeSharedSettings(getConversationSettings(), []);
                    }
                    _confirmSharedKeyWrites(
                        payload,
                        data.settings,
                        data.revision
                    );
                    if (nextEtag) {
                        _markEtagConfirmedSharedSettings(
                            settings,
                            data.settings,
                            mutationVersionAtSend
                        );
                    }
                    const changedWhileInFlight = _settingsChangedSince(
                        settings,
                        mutationVersionAtSend
                    );
                    _clearAcknowledgedPendingSettings(payload);
                    const adoptedServerAsrDecision =
                        _adoptServerAsrDecision(data);
                    // A successful dirty-only write returns the complete
                    // authoritative snapshot. If the boot GET failed or lost
                    // the gate race, hydrate untouched fields from this response
                    // while preserving edits made after the request was sent.
                    if (!mergedAtSend) {
                        _crossWindowSettingsNewerThanEtag(
                            _conversationSettingsEtagKeyMutationVersions
                        ).forEach((key) => {
                            changedWhileInFlight.add(key);
                        });
                        _mergeConversationSettingsSnapshot(data, changedWhileInFlight);
                    } else if (adoptedServerAsrDecision) {
                        // Re-broadcast a server-generated/confirmed tuple with
                        // serverRevision authority. Its decision clock may be
                        // ahead of a sibling browser's local Date.now() bound.
                        saveSettings({
                            skipServerSync: true,
                            serverMerged: true,
                            serverAuthoritativeKeys: ['independentAsrEnabled']
                        });
                    }
                    return;
                } catch (err) {
                    console.error('[app-settings] 同步设置到服务器失败:', err);
                    return;
                }
            }
        };
        // runSync swallows its own failures, so chaining with it on both
        // fulfillment and rejection keeps the returned promise — the one the
        // toggle handler publishes as S.pendingSettingsSyncPromise for the
        // ensureWebSocketOpen gate — always resolving, never rejecting.
        const chained = _syncChainTail.then(runSync, runSync);
        _syncChainTail = chained;
        return chained;
    }

    /**
     * 启动定期同步到服务器
     *
     * 首启 settings/telemetry 决议未完成（_FIRST_LAUNCH_PENDING_KEY 还在）时跳过 periodic
     * POST：否则会把首启本地默认值抢先推到服务器，下次 GET 读到自家 echo 误判「云端已有
     * 偏好」、干扰设置合并与首启决议时序。用户主动改设置走的 saveSettings 不受影响（那条
     * 路径就是要持久化用户显式选择）。
     *
     * 设置未水合（S.settingsHydrated 为 false：GET 从没成功、用户也没改过设置）时同样
     * 跳过：GET 持续失败时 catch/finally 也会启动 periodic，若照常 POST 会把 boot 默认
     * 值（如 independentAsrEnabled=false）覆写掉服务器持久化的用户偏好。水合后（任一
     * 来源）恢复正常同步。
     */
    function startPeriodicSync() {
        if (_syncTimerId !== null) return; // 防止重复启动
        _syncTimerId = setInterval(() => {
            if (S.settingsHydrated !== true) {
                if (!_periodicSyncSkippedUnhydratedLogged) {
                    _periodicSyncSkippedUnhydratedLogged = true;
                    console.log('[app-settings] 设置尚未水合（server GET 未成功且用户未改过设置），跳过周期同步，避免用启动默认值覆盖服务器设置');
                }
                return;
            }
            try {
                if (localStorage.getItem(_FIRST_LAUNCH_PENDING_KEY) === '1') {
                    return;
                }
            } catch (_) { /* localStorage 不可用就当 pending 没 set，照常 sync */ }
            syncSettingsToServer();
        }, SYNC_INTERVAL_MS);
        console.log('[app-settings] 已启动定期同步到服务器，间隔', SYNC_INTERVAL_MS / 1000, '秒');
    }

    /**
     * 停止定期同步到服务器
     */
    function stopPeriodicSync() {
        if (_syncTimerId !== null) {
            clearInterval(_syncTimerId);
            _syncTimerId = null;
            console.log('[app-settings] 已停止定期同步到服务器');
        }
    }

    /**
     * 检测用户是否处于中国地区
     * 通过时区和浏览器语言判断
     */
    function _isUserRegionChina() {
        try {
            const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone || '').toLowerCase();
            if (/^asia\/(shanghai|chongqing|urumqi|harbin|kashgar)$/.test(tz)) return true;
            const lang = (navigator.language || '').toLowerCase();
            if (lang === 'zh' || lang.startsWith('zh-cn') || lang.startsWith('zh-hans')) return true;
        } catch (_) { }
        return false;
    }

    // ======================== saveSettings ========================

    /**
     * 将当前设置保存到 localStorage
     * 从 window 全局变量读取最新值（确保同步 live2d.js 中的更改）
     *
     * @param {{
     *   skipServerSync?: boolean,
     *   serverMerged?: boolean,
     *   pendingRecovery?: boolean,
     *   explicitSharedKeys?: string[],
     *   serverAuthoritativeKeys?: string[]
     * }} [options]
     *   传 skipServerSync 跳过 POST；
     *   serverMerged 表示共享快照来自服务端合并，不得标记成新的用户修改。
     *   首启用——避免在 loadSettingsFromServer 拿到 telemetryBranch 之前就把首启本地
     *   默认值写到服务器、回头被自己的 GET 当成「云端已有偏好」，干扰首启决议时序
     */
    function saveSettings(options) {
        const skipServerSync = !!(options && options.skipServerSync);
        const serverMerged = !!(options && options.serverMerged);
        const pendingRecovery = !!(options && options.pendingRecovery);
        const explicitSharedKeys = options && Array.isArray(options.explicitSharedKeys)
            ? options.explicitSharedKeys
            : null;
        const serverAuthoritativeKeys =
            options && Array.isArray(options.serverAuthoritativeKeys)
                ? options.serverAuthoritativeKeys
                : [];
        // 从全局变量读取最新值（确保同步 live2d.js 中的更改）
        const currentProactive = typeof window.proactiveChatEnabled !== 'undefined'
            ? window.proactiveChatEnabled
            : S.proactiveChatEnabled;
        const currentVision = typeof window.proactiveVisionEnabled !== 'undefined'
            ? window.proactiveVisionEnabled
            : S.proactiveVisionEnabled;
        const currentVisionChat = typeof window.proactiveVisionChatEnabled !== 'undefined'
            ? window.proactiveVisionChatEnabled
            : S.proactiveVisionChatEnabled;
        const currentNewsChat = typeof window.proactiveNewsChatEnabled !== 'undefined'
            ? window.proactiveNewsChatEnabled
            : S.proactiveNewsChatEnabled;
        const currentCommunityChat = typeof window.proactiveCommunityChatEnabled !== 'undefined'
            ? window.proactiveCommunityChatEnabled
            : S.proactiveCommunityChatEnabled;
        const currentVideoChat = typeof window.proactiveVideoChatEnabled !== 'undefined'
            ? window.proactiveVideoChatEnabled
            : S.proactiveVideoChatEnabled;
        const currentMerge = typeof window.mergeMessagesEnabled !== 'undefined'
            ? window.mergeMessagesEnabled
            : S.mergeMessagesEnabled;
        const currentFocus = typeof window.focusModeEnabled !== 'undefined'
            ? window.focusModeEnabled
            : S.focusModeEnabled;
        const currentFocusCognition = typeof window.focusCognitionEnabled !== 'undefined'
            ? window.focusCognitionEnabled
            : S.focusCognitionEnabled;
        const currentIndependentAsr = S.independentAsrEnabled === true;
        const currentVoiceResourceOptimization =
            S.voiceInputResourceOptimizationEnabled !== false;
        const currentProactiveChatInterval = typeof window.proactiveChatInterval !== 'undefined'
            ? window.proactiveChatInterval
            : S.proactiveChatInterval;
        const currentProactiveVisionInterval = typeof window.proactiveVisionInterval !== 'undefined'
            ? window.proactiveVisionInterval
            : S.proactiveVisionInterval;
        const currentPersonalChat = typeof window.proactivePersonalChatEnabled !== 'undefined'
            ? window.proactivePersonalChatEnabled
            : S.proactivePersonalChatEnabled;
        const currentMusicChat = typeof window.proactiveMusicEnabled !== 'undefined'
            ? window.proactiveMusicEnabled
            : S.proactiveMusicEnabled;
        const currentMemeChat = typeof window.proactiveMemeEnabled !== 'undefined'
            ? window.proactiveMemeEnabled
            : S.proactiveMemeEnabled;
        const currentMiniGameInviteChat = typeof window.proactiveMiniGameInviteEnabled !== 'undefined'
            ? window.proactiveMiniGameInviteEnabled
            : S.proactiveMiniGameInviteEnabled;
        const currentAvatarReactionBubble = typeof window.avatarReactionBubbleEnabled !== 'undefined'
            ? window.avatarReactionBubbleEnabled
            : S.avatarReactionBubbleEnabled;
        const currentSlopFilter = typeof window.slopFilterEnabled !== 'undefined'
            ? window.slopFilterEnabled
            : S.slopFilterEnabled;
        const currentTextGuardMaxLength = typeof window.textGuardMaxLength !== 'undefined'
            ? window.textGuardMaxLength
            : S.textGuardMaxLength;
        const currentRenderQuality = typeof window.renderQuality !== 'undefined'
            ? window.renderQuality
            : S.renderQuality;
        const currentTargetFrameRate = typeof window.targetFrameRate !== 'undefined'
            ? window.targetFrameRate
            : S.targetFrameRate;
        const currentMouseTracking = typeof window.mouseTrackingEnabled !== 'undefined'
            ? window.mouseTrackingEnabled
            : true;
        const currentLive2dFullscreenTracking = typeof window.live2dFullscreenTrackingEnabled !== 'undefined'
            ? window.live2dFullscreenTrackingEnabled
            : false;
        const currentHumanoidLocalTracking = typeof window.humanoidLocalTrackingEnabled !== 'undefined'
            ? window.humanoidLocalTrackingEnabled
            : false;
        const currentLockedHoverFade = typeof window.lockedHoverFadeEnabled !== 'undefined'
            ? window.lockedHoverFadeEnabled
            : true;
        const currentForgeDropEffects = typeof window.forgeDropEffectsEnabled !== 'undefined'
            ? window.forgeDropEffectsEnabled
            : true;

        // 读取字幕设置（统一走 subtitle-shared store，避免多处直接写 localStorage）
        const subtitleStore = window.nekoSubtitleShared;
        const subtitleState = subtitleStore && typeof subtitleStore.getSettings === 'function'
            ? subtitleStore.getSettings()
            : null;
        const currentSubtitleEnabled = typeof S.subtitleEnabled !== 'undefined'
            ? S.subtitleEnabled
            : (subtitleState ? !!subtitleState.subtitleEnabled : (localStorage.getItem('subtitleEnabled') === 'true'));
        const currentUserLanguage = S.hasOwnProperty('userLanguage')
            ? S.userLanguage
            : (subtitleState ? subtitleState.userLanguage : (localStorage.getItem('userLanguage') || null));

        const settings = {
            proactiveChatEnabled: currentProactive,
            proactiveVisionEnabled: currentVision,
            proactiveVisionChatEnabled: currentVisionChat,
            proactiveNewsChatEnabled: currentNewsChat,
            proactiveCommunityChatEnabled: currentCommunityChat,
            proactiveVideoChatEnabled: currentVideoChat,
            proactivePersonalChatEnabled: currentPersonalChat,
            proactiveMusicEnabled: currentMusicChat,
            proactiveMemeEnabled: currentMemeChat,
            proactiveMiniGameInviteEnabled: currentMiniGameInviteChat,
            mergeMessagesEnabled: currentMerge,
            focusModeEnabled: currentFocus,
            focusCognitionEnabled: currentFocusCognition,
            noiseReductionEnabled: S.noiseReductionEnabled,
            independentAsrEnabled: currentIndependentAsr,
            voiceInputResourceOptimizationEnabled: currentVoiceResourceOptimization,
            avatarReactionBubbleEnabled: currentAvatarReactionBubble,
            slopFilterEnabled: currentSlopFilter,
            proactiveChatInterval: currentProactiveChatInterval,
            proactiveVisionInterval: currentProactiveVisionInterval,
            textGuardMaxLength: currentTextGuardMaxLength,
            renderQuality: currentRenderQuality,
            targetFrameRate: currentTargetFrameRate,
            mouseTrackingEnabled: currentMouseTracking,
            live2dFullscreenTrackingEnabled: currentLive2dFullscreenTracking,
            humanoidLocalTrackingEnabled: currentHumanoidLocalTracking,
            lockedHoverFadeEnabled: currentLockedHoverFade,
            forgeDropEffectsEnabled: currentForgeDropEffects,
            subtitleEnabled: currentSubtitleEnabled,
            userLanguage: currentUserLanguage
        };
        // Stamp the keys the user explicitly changed (plus a monotonic write id)
        // into the shared snapshot: every save copies independentAsrEnabled
        // along, so the receiving window needs this metadata to tell a real
        // cross-window toggle from an unrelated save's incidental copy.
        _writeSharedSettings(
            settings,
            explicitSharedKeys !== null
                ? explicitSharedKeys
                : (serverMerged ? [] : _collectExplicitSharedKeys(settings)),
            pendingRecovery,
            serverAuthoritativeKeys
        );

        // 同步回共享状态，保持一致性
        S.proactiveChatEnabled = currentProactive;
        S.proactiveVisionEnabled = currentVision;
        S.proactiveVisionChatEnabled = currentVisionChat;
        S.proactiveNewsChatEnabled = currentNewsChat;
        S.proactiveCommunityChatEnabled = currentCommunityChat;
        S.proactiveVideoChatEnabled = currentVideoChat;
        S.proactivePersonalChatEnabled = currentPersonalChat;
        S.proactiveMusicEnabled = currentMusicChat;
        S.proactiveMemeEnabled = currentMemeChat;
        S.proactiveMiniGameInviteEnabled = currentMiniGameInviteChat;
        S.mergeMessagesEnabled = currentMerge;
        S.focusModeEnabled = currentFocus;
        S.focusCognitionEnabled = currentFocusCognition;
        S.independentAsrEnabled = currentIndependentAsr;
        S.voiceInputResourceOptimizationEnabled = currentVoiceResourceOptimization;
        S.avatarReactionBubbleEnabled = currentAvatarReactionBubble;
        S.slopFilterEnabled = currentSlopFilter;
        S.proactiveChatInterval = currentProactiveChatInterval;
        S.proactiveVisionInterval = currentProactiveVisionInterval;
        S.textGuardMaxLength = currentTextGuardMaxLength;
        S.renderQuality = currentRenderQuality;
        S.targetFrameRate = currentTargetFrameRate;
        stopVisionAfterPrivacyEnabled();
        // 同步字幕设置到共享状态
        S.subtitleEnabled = currentSubtitleEnabled;
        S.userLanguage = currentUserLanguage;
        if (subtitleStore && typeof subtitleStore.updateSettings === 'function') {
            subtitleStore.updateSettings({
                subtitleEnabled: S.subtitleEnabled,
                userLanguage: S.userLanguage
            }, {
                source: 'app-settings-save'
            });
        }

        // 同步到服务器（异步，不阻塞）；首启走 skipServerSync 等 branch 解析后再 POST。
        // userInitiated: true——不走 skipServerSync 的 saveSettings 调用要么来自用户显式
        // 改设置的 UI 路径（设置弹窗、字幕开关、聊天窗翻译开关等），要么来自 server merge
        // 成功后的回写（那时 settingsHydrated 已在 merge 回调里标记，重复标记无副作用），
        // 都是合法的水合来源。
        if (!skipServerSync) {
            syncSettingsToServer({ userInitiated: true });
        }
    }

    // ======================== loadSettings ========================

    /**
     * 从 localStorage 加载设置，包含迁移逻辑
     * 首次启动时检测用户地区，中国用户自动开启自主视觉
     * 加载后异步从服务器同步最新设置
     */
    function loadSettings() {
        // 内层 try：仅处理本地 JSON 解析与迁移
        try {
            const saved = localStorage.getItem('project_neko_settings');
            if (saved) {
                const settings = JSON.parse(saved);
                // A booting window has by definition already "applied" the
                // snapshot it is loading, so seed the applied-id floor from it.
                // Otherwise this window's first write can mint an id at or
                // below one another window already published, and the strict
                // freshness test drops it.
                const bootMeta = _readSharedWriteMeta(settings);
                if (bootMeta && bootMeta.writeId > _lastAppliedSharedWriteId) {
                    _lastAppliedSharedWriteId = bootMeta.writeId;
                }
                if (bootMeta) {
                    _rememberKnownSharedKeyWrites(bootMeta.knownKeyWrites, settings);
                    _rememberServerKeyRevisions(
                        bootMeta.serverKeyRevisions,
                        settings
                    );
                    _rememberSharedKeyWrites(bootMeta.changedKeys, bootMeta, settings);
                    if (Number.isInteger(bootMeta.serverRevision)) {
                        _conversationSettingsRevision =
                            bootMeta.serverRevision;
                        _conversationSettingsEtag =
                            `"conversation-settings-${bootMeta.serverRevision}"`;
                        for (const key of bootMeta.serverAuthoritativeKeys) {
                            if (!Object.prototype.hasOwnProperty.call(
                                settings,
                                key
                            )) continue;
                            _rememberServerKeyRevisions({
                                [key]: bootMeta.serverRevision
                            }, settings);
                        }
                    }
                }
                if (bootMeta && bootMeta.asrDecision
                    && settings.independentAsrEnabled === bootMeta.asrDecision.value) {
                    // A server-merge snapshot intentionally has no changedKeys:
                    // it carries authority, not a fresh user action. Restore its
                    // matching decision tuple anyway so reloads retain both the
                    // ordering token and the floor for the next local choice.
                    _adoptAsrDecisionTuple(
                        bootMeta.asrDecision,
                        Number.isInteger(bootMeta.serverRevision)
                    );
                } else if (bootMeta
                    && bootMeta.changedKeys.indexOf('independentAsrEnabled') !== -1) {
                    const bootDecision = bootMeta.asrDecision || bootMeta;
                    _noteAsrDecision(bootDecision.writeId, bootDecision.writerId, settings.independentAsrEnabled);
                }
                if (
                    bootMeta
                    && (
                        bootMeta.optimizationDecision
                        || bootMeta.changedKeys.indexOf(
                            'voiceInputResourceOptimizationEnabled'
                        ) !== -1
                    )
                ) {
                    const optimizationDecision =
                        bootMeta.optimizationDecision || bootMeta;
                    _noteOptimizationDecision(
                        optimizationDecision.writeId,
                        optimizationDecision.writerId,
                        settings.voiceInputResourceOptimizationEnabled
                    );
                    if (
                        bootMeta.optimizationDecisionPendingSync
                        && typeof settings.voiceInputResourceOptimizationEnabled
                            === 'boolean'
                    ) {
                        _optimizationDecisionPendingSync = true;
                        _dirtySettingsKeys.add(
                            'voiceInputResourceOptimizationEnabled'
                        );
                        _pendingSettingsKeys.add(
                            'voiceInputResourceOptimizationEnabled'
                        );
                        S.settingsHydrated = true;
                        S.voiceInputResourceOptimizationAuthoritative = true;
                    }
                }

                // 迁移逻辑：检测旧版设置并迁移到新字段
                // 如果旧版 proactiveChatEnabled=true 但新字段未定义，则迁移
                let needsSave = false;
                if (settings.proactiveChatEnabled === true) {
                    const hasNewFlags = settings.proactiveVisionChatEnabled !== undefined ||
                    settings.proactiveNewsChatEnabled !== undefined ||
                    settings.proactiveCommunityChatEnabled !== undefined ||
                    settings.proactiveVideoChatEnabled !== undefined ||
                    settings.proactivePersonalChatEnabled !== undefined ||
                    settings.proactiveMusicEnabled !== undefined ||
                    settings.proactiveMemeEnabled !== undefined ||
                    settings.proactiveMiniGameInviteEnabled !== undefined;
                    if (!hasNewFlags) {
                        // 根据旧的视觉偏好决定迁移策略
                        if (settings.proactiveVisionEnabled === false) {
                            // 用户之前禁用了视觉，保留偏好并默认启用新闻搭话
                            settings.proactiveVisionEnabled = false;
                            settings.proactiveVisionChatEnabled = false;
                            settings.proactiveNewsChatEnabled = true;
                            settings.proactivePersonalChatEnabled = false;
                            settings.proactiveMusicEnabled = false;
                            settings.proactiveMemeEnabled = false;
                            console.log('迁移旧版设置：保留禁用的视觉偏好，已启用新闻搭话');
                        } else {
                            // 视觉偏好为 true 或 undefined，默认启用视觉搭话
                            settings.proactiveVisionEnabled = true;
                            settings.proactiveVisionChatEnabled = true;
                            settings.proactivePersonalChatEnabled = false;
                            settings.proactiveMusicEnabled = false;
                            settings.proactiveMemeEnabled = false;
                            console.log('迁移旧版设置：已启用视觉搭话和自主视觉');
                        }
                        needsSave = true;
                    }
                }

                // 如果进行了迁移，持久化更新后的设置。走 _writeSharedSettings 带上写入
                // 元数据：迁移发生在水合之前、也不是用户显式改动，其他窗口据此不会把这份
                // 快照里的 independentAsrEnabled 当成一次跨窗口开关翻转
                if (needsSave) {
                    _writeSharedSettings(settings, []);
                }

                // 使用 ?? 运算符提供更好的默认值处理（避免将 false 误判为需要使用默认值）
                S.proactiveChatEnabled = settings.proactiveChatEnabled ?? true;
                S.proactiveVisionEnabled = settings.proactiveVisionEnabled ?? false;
                S.proactiveVisionChatEnabled = settings.proactiveVisionChatEnabled ?? true;
                S.proactiveNewsChatEnabled = settings.proactiveNewsChatEnabled ?? false;
                S.proactiveCommunityChatEnabled = settings.proactiveCommunityChatEnabled ?? false;
                S.proactiveVideoChatEnabled = settings.proactiveVideoChatEnabled ?? true;
                S.proactivePersonalChatEnabled = settings.proactivePersonalChatEnabled ?? false;
                S.proactiveMusicEnabled = settings.proactiveMusicEnabled ?? true;
                S.proactiveMemeEnabled = settings.proactiveMemeEnabled ?? true;
                S.proactiveMiniGameInviteEnabled = settings.proactiveMiniGameInviteEnabled ?? true;
                S.mergeMessagesEnabled = settings.mergeMessagesEnabled ?? false;
                S.focusModeEnabled = settings.focusModeEnabled ?? false;
                S.focusCognitionEnabled = settings.focusCognitionEnabled ?? true;
                S.independentAsrEnabled = settings.independentAsrEnabled ?? false;
                S.voiceInputResourceOptimizationEnabled =
                    settings.voiceInputResourceOptimizationEnabled ?? true;
                S.avatarReactionBubbleEnabled = settings.avatarReactionBubbleEnabled ?? true;
                S.slopFilterEnabled = settings.slopFilterEnabled ?? true;
                S.proactiveChatInterval = settings.proactiveChatInterval ?? C.DEFAULT_PROACTIVE_CHAT_INTERVAL;
                S.proactiveVisionInterval = settings.proactiveVisionInterval ?? C.DEFAULT_PROACTIVE_VISION_INTERVAL;
                // 回复 token 上限（默认 300 tiktoken tokens；0 = 无限制）
                S.textGuardMaxLength = settings.textGuardMaxLength ?? 300;
                window.textGuardMaxLength = S.textGuardMaxLength;
                // 画质设置
                S.renderQuality = settings.renderQuality ?? getDefaultRenderQuality();
                window.cursorFollowPerformanceLevel = U.mapRenderQualityToFollowPerf(S.renderQuality);
                // 帧率设置（0 = 不限帧 / VSync）
                S.targetFrameRate = settings.targetFrameRate ?? 60;
                // 鼠标跟踪设置（严格转换为布尔值）
                if (typeof settings.mouseTrackingEnabled === 'boolean') {
                    window.mouseTrackingEnabled = settings.mouseTrackingEnabled;
                } else if (typeof settings.mouseTrackingEnabled === 'string') {
                    window.mouseTrackingEnabled = settings.mouseTrackingEnabled === 'true';
                } else {
                    window.mouseTrackingEnabled = true;
                }
                syncMouseTrackingRuntimeManagers();

                // 跟踪模式设置
                if (typeof settings.live2dFullscreenTrackingEnabled === 'boolean') {
                    window.live2dFullscreenTrackingEnabled = settings.live2dFullscreenTrackingEnabled;
                } else if (typeof settings.live2dFullscreenTrackingEnabled === 'string') {
                    window.live2dFullscreenTrackingEnabled = settings.live2dFullscreenTrackingEnabled === 'true';
                }

                if (typeof settings.humanoidLocalTrackingEnabled === 'boolean') {
                    window.humanoidLocalTrackingEnabled = settings.humanoidLocalTrackingEnabled;
                } else if (typeof settings.humanoidLocalTrackingEnabled === 'string') {
                    window.humanoidLocalTrackingEnabled = settings.humanoidLocalTrackingEnabled === 'true';
                }

                // 锁定悬停淡化设置
                if (typeof settings.lockedHoverFadeEnabled === 'boolean') {
                    window.lockedHoverFadeEnabled = settings.lockedHoverFadeEnabled;
                } else if (typeof settings.lockedHoverFadeEnabled === 'string') {
                    window.lockedHoverFadeEnabled = settings.lockedHoverFadeEnabled === 'true';
                } else {
                    window.lockedHoverFadeEnabled = true;
                }

                // 锻造券掉落动画与音效设置
                if (typeof settings.forgeDropEffectsEnabled === 'boolean') {
                    window.forgeDropEffectsEnabled = settings.forgeDropEffectsEnabled;
                } else if (typeof settings.forgeDropEffectsEnabled === 'string') {
                    window.forgeDropEffectsEnabled = settings.forgeDropEffectsEnabled === 'true';
                } else {
                    window.forgeDropEffectsEnabled = true;
                }

                // 同步到运行中的实例
                if (typeof window.live2dManager !== 'undefined' && window.live2dManager && typeof window.live2dManager.setFullscreenTrackingEnabled === 'function') {
                    window.live2dManager.setFullscreenTrackingEnabled(window.live2dFullscreenTrackingEnabled === true);
                }
                if (typeof window.vrmManager !== 'undefined' && window.vrmManager && window.vrmManager._cursorFollow && typeof window.vrmManager._cursorFollow.setLocalTrackingEnabled === 'function') {
                    window.vrmManager._cursorFollow.setLocalTrackingEnabled(window.humanoidLocalTrackingEnabled === true);
                }
                if (typeof window.mmdManager !== 'undefined' && window.mmdManager && window.mmdManager.cursorFollow && typeof window.mmdManager.cursorFollow.setLocalTrackingEnabled === 'function') {
                    window.mmdManager.cursorFollow.setLocalTrackingEnabled(window.humanoidLocalTrackingEnabled === true);
                }

                console.log('已加载设置:', {
                    proactiveChatEnabled: S.proactiveChatEnabled,
                    proactiveVisionEnabled: S.proactiveVisionEnabled,
                    proactiveVisionChatEnabled: S.proactiveVisionChatEnabled,
                    proactiveNewsChatEnabled: S.proactiveNewsChatEnabled,
                    proactiveCommunityChatEnabled: S.proactiveCommunityChatEnabled,
                    proactiveVideoChatEnabled: S.proactiveVideoChatEnabled,
                    proactivePersonalChatEnabled: S.proactivePersonalChatEnabled,
                    mergeMessagesEnabled: S.mergeMessagesEnabled,
                    focusModeEnabled: S.focusModeEnabled,
                    proactiveChatInterval: S.proactiveChatInterval,
                    proactiveVisionInterval: S.proactiveVisionInterval,
                    focusModeDesc: S.focusModeEnabled ? 'AI说话时自动静音麦克风（不允许打断）' : '允许打断AI说话'
                });
            } else {
                // 首次启动：隐私模式按用户地区分流（仅中国地区默认关闭隐私 / vision 开）。
                // 历史上这里挂过隐私默认值实验（privacy_default_off_v2，已退役）和屏幕分享
                // 来源默认值实验（vision_chat_default_off，已合并进 main、默认回到「开」），
                // 现都不再做首启覆写，仅保留地区分流。
                if (_isUserRegionChina()) {
                    S.proactiveVisionEnabled = true;
                }

                // 首次启动默认开启音乐/meme搭话 + mini-game 邀请
                S.proactiveMusicEnabled = true;
                S.proactiveMemeEnabled = true;
                S.proactiveMiniGameInviteEnabled = true;
                // 首次启动默认 token 上限 300（tiktoken o200k_base）
                S.textGuardMaxLength = 300;
                window.textGuardMaxLength = 300;

                console.log('未找到保存的设置，使用默认值');
                window.cursorFollowPerformanceLevel = U.mapRenderQualityToFollowPerf(S.renderQuality);
                window.mouseTrackingEnabled = true;
                syncMouseTrackingRuntimeManagers();
                window.live2dFullscreenTrackingEnabled = false;
                window.humanoidLocalTrackingEnabled = false;
                window.lockedHoverFadeEnabled = true;
                window.forgeDropEffectsEnabled = true;

                // 首启专属 marker：告诉下方异步合并块「这次仍在等首次 settings/telemetry
                // 决议」。升级用户走的是 if (saved) 分支不会写这个，于是不会被误判成首启
                try { localStorage.setItem(_FIRST_LAUNCH_PENDING_KEY, '1'); } catch (_) {}
                // 持久化首次启动设置到 localStorage，避免每次重新检测。注意：故意跳过
                // 服务器 POST——loadSettingsFromServer GET 还没拿到 telemetryBranch，
                // 这时把首启本地默认值上行会被自家 GET 当作「云端已有偏好」回读、干扰设置
                // 合并与首启决议时序。等 branch 解析后再做一次完整 saveSettings 推送
                saveSettings({ skipServerSync: true });
            }

        } catch (error) {
            console.error('加载本地设置失败:', error);
            // 出错时也要确保全局变量被初始化
            S.textGuardMaxLength = 300;
            window.textGuardMaxLength = 300;
            window.cursorFollowPerformanceLevel = U.mapRenderQualityToFollowPerf(S.renderQuality);
            window.mouseTrackingEnabled = true;
            syncMouseTrackingRuntimeManagers();
            window.live2dFullscreenTrackingEnabled = false;
            window.humanoidLocalTrackingEnabled = false;
            window.lockedHoverFadeEnabled = true;
            window.forgeDropEffectsEnabled = true;
        }

        // 以下逻辑不依赖本地 JSON 解析结果，始终执行

        // 凝神总开关镜像到 window：设置弹窗（avatar-ui-popup.js）只读 window 全局，
        // 而 window.focusCognitionEnabled 否则仅在 server-merge 命中时才赋值——用户存了
        // 关、reload 时若没触发 merge，window 会是 undefined 让弹窗误显示为开。这里在
        // 每次 loadSettings 末尾从 S 权威镜像一次兜住该时序漏洞。
        window.focusCognitionEnabled = S.focusCognitionEnabled;
        // 同理：自然表达开关也镜像到 window，避免设置弹窗在 reload 后误显示为开。
        window.slopFilterEnabled = S.slopFilterEnabled;

        // 加载字幕设置（统一从 subtitle-shared store 读取）
        const subtitleStore = window.nekoSubtitleShared;
        const subtitleState = subtitleStore && typeof subtitleStore.getSettings === 'function'
            ? subtitleStore.getSettings()
            : null;
        S.subtitleEnabled = subtitleState ? !!subtitleState.subtitleEnabled : (localStorage.getItem('subtitleEnabled') === 'true');
        S.userLanguage = subtitleState ? subtitleState.userLanguage : (localStorage.getItem('userLanguage') || null);

        // 异步：从服务器加载对话设置并合并（不阻塞 UI）
        const _firstLaunchPending = (() => {
            try { return localStorage.getItem(_FIRST_LAUNCH_PENDING_KEY) === '1'; } catch (_) { return false; }
        })();
        // Snapshot the pre-GET settings as the dirty-diff baseline: keys the
        // user changes while the GET is in flight diverge from this snapshot,
        // get recorded in _dirtySettingsKeys, and are preserved by the
        // field-level merge below.
        _settingsBaseline = getConversationSettings();
        // No re-arming of _settingsMergedFromServer here: it already starts
        // false, and once a merge HAS happened the local snapshot holds server
        // truth for untouched keys — a later re-read does not make that
        // knowledge stale enough to justify dropping back to partial writes.
        // Until the first merge lands, POST bodies stay restricted to the
        // user-dirty keys so they cannot overwrite the server-persisted
        // preferences this client has not read yet.
        const mutationVersionAtGetStart = _crossWindowMutationVersion;
        try {
            const mergeSettled = loadSettingsFromServer().then(serverResult => {
                if (!serverResult) return;
                // server GET 成功返回：前端从此持有权威设置视图，标记水合
                // （S.settingsHydrated，见 app-state.js），start_session 握手才允许携带
                // independent_asr_enabled。GET 永久失败时 serverResult 为 null，这里
                // 不会执行——字段保持省略、由后端持久化值兜底，正是期望行为。
                S.settingsHydrated = true;
                // The GET merged server values into S, so independentAsrEnabled
                // now holds either server truth or a user change the field-level
                // merge preserved — authoritative for the handshake either way.
                S.independentAsrAuthoritative = true;
                S.voiceInputResourceOptimizationAuthoritative = true;
                // Distinct from the hydration mark above (which a user action
                // also sets, because a user choice is authoritative for the
                // handshake even before any GET): THIS flag means server values
                // were really merged into the local view, which is the only
                // evidence that licenses full-snapshot POSTs. Set before the
                // merge/writeback below so the writeback saveSettings() already
                // posts the converged full snapshot. A serverResult with a
                // telemetry branch but no settings counts too: the server holds
                // no persisted conversation settings at all, so there is
                // nothing a full write could clobber (and the first-launch
                // forced push below depends on it).
                _settingsMergedFromServer = true;
                const serverSnapshotOlderThanCurrent =
                    Number.isInteger(serverResult.revision)
                    && Number.isInteger(_conversationSettingsRevision)
                    && serverResult.revision < _conversationSettingsRevision;
                const serverSettings = serverSnapshotOlderThanCurrent
                    ? null
                    : _serverSettingsForMerge(serverResult);
                const telemetryBranch = serverResult.telemetryBranch;
                const rawServerAsrDecision = serverSnapshotOlderThanCurrent
                    ? null
                    : _serverAsrDecision(serverResult);
                const resetReplacesAsrDecision =
                    !serverSnapshotOlderThanCurrent
                    && serverResult.reset === true
                    && serverSettings
                    && typeof serverSettings.independentAsrEnabled === 'boolean'
                    && !_pendingSettingsKeys.has('independentAsrEnabled')
                    && (_crossWindowKeyMutationVersions.independentAsrEnabled || 0)
                        <= mutationVersionAtGetStart;
                if (resetReplacesAsrDecision) {
                    _rebaseAsrDecisionForReset(
                        rawServerAsrDecision,
                        serverSettings.independentAsrEnabled
                    );
                }
                const serverAsrDecision = serverResult.reset === true
                    ? null
                    : rawServerAsrDecision;
                // A cross-window toggle can reach localStorage before its POST
                // reaches the server. In that window the local decision tuple is
                // newer than the GET snapshot, so the generic field merge must
                // not copy the older server value into S before the decision
                // merge gets a chance to reject it.
                const preserveLocalAsrDecision =
                    !serverSnapshotOlderThanCurrent
                    && serverResult.reset === true
                    ? !resetReplacesAsrDecision
                    : !!(_lastAsrDecision
                    && (!serverAsrDecision
                        || _asrDecisionOutranks(_lastAsrDecision, serverAsrDecision)));
                const serverSnapshotNewerThanCurrent =
                    (!serverSnapshotOlderThanCurrent
                        && serverResult.reset === true)
                    || (Number.isInteger(serverResult.revision)
                        && Number.isInteger(_conversationSettingsRevision)
                        && serverResult.revision > _conversationSettingsRevision);
                const shouldAdoptServerVersion =
                    !Number.isInteger(_conversationSettingsRevision)
                    || (Number.isInteger(serverResult.revision)
                        && serverResult.revision
                            >= _conversationSettingsRevision);
                if (serverResult.etag && shouldAdoptServerVersion) {
                    _conversationSettingsEtag = serverResult.etag;
                    if (Number.isInteger(serverResult.revision)) {
                        _conversationSettingsRevision = serverResult.revision;
                    }
                    _SHARED_SETTINGS_KEYS.forEach((key) => {
                        _conversationSettingsEtagKeyMutationVersions[key] =
                            mutationVersionAtGetStart;
                    });
                }
                let hasUpdate = false;

                // 只要 server 给了 branch，本次首启决议就算完成，清掉 pending marker；下次
                // 启动不再尝试。GET 失败则 marker 留着，下次在线启动重新决议。原本这里还会
                // 对实验组 vision_chat_default_off 把屏幕分享来源（proactiveVisionChatEnabled）
                // 首启默认翻成「关」，现该实验已合并进 main、默认回到控制组「开」（见
                // app-state.js / loadSettings 的 ?? true），故不再做首启覆写；仅保留 marker
                // 决议时序——情境弹窗 app-context-prompt.js 靠下方 neko:telemetry-branch-resolved
                // 广播判断 settings 已就绪。
                const branchResolutionFinalized = !!(telemetryBranch && _firstLaunchPending);
                if (branchResolutionFinalized) {
                    try { localStorage.removeItem(_FIRST_LAUNCH_PENDING_KEY); } catch (_) {}
                    // 首启决议完后强制 POST 一次：没有 server merge 时 hasUpdate 仍是 false，
                    // 若用户在 60s periodic 之前关掉 app，首启的本地默认值就永远到不了服务器。
                    // 这里 hasUpdate=true 让下方 saveSettings 走完整路径推一次
                    hasUpdate = true;
                }

                const acceptedSharedSettings = {};
                if (serverSettings) {
                    // Field-level merge (Codex P2): apply server values to the
                    // keys the user never touched, preserve the dirty ones. A
                    // user change during the in-flight GET therefore keeps its
                    // own keys authoritative while every other field still
                    // hydrates from the server — the old whole-merge-drop let
                    // one unrelated toggle turn the entire boot-default
                    // snapshot into the POSTed truth.
                    for (const key of Object.keys(serverSettings)) {
                        if (serverSettings[key] === undefined) continue;
                        if (_pendingSettingsKeys.has(key)) continue;
                        if (_dirtySettingsKeys.has(key)
                            && !serverSnapshotNewerThanCurrent) continue;
                        if ((_crossWindowKeyMutationVersions[key] || 0)
                            > mutationVersionAtGetStart) continue;
                        if (key === 'independentAsrEnabled' && preserveLocalAsrDecision) continue;
                        if (_SHARED_SETTINGS_KEYS.indexOf(key) !== -1
                            || key === 'userLanguage') {
                            acceptedSharedSettings[key] = serverSettings[key];
                        } else if (S[key] !== serverSettings[key]) {
                            S[key] = serverSettings[key];
                            hasUpdate = true;
                        }
                    }
                    if (applySharedRuntimeSettings(acceptedSharedSettings)) {
                        hasUpdate = true;
                    }
                    // Subtitle bridge mirrors follow the same dirty gating so
                    // a user-changed subtitle preference survives the merge.
                    if (serverSettings.subtitleEnabled !== undefined
                        && !_pendingSettingsKeys.has('subtitleEnabled')
                        && (!_dirtySettingsKeys.has('subtitleEnabled')
                            || serverSnapshotNewerThanCurrent)
                        && window.subtitleBridge) {
                        window.subtitleBridge.setSubtitleEnabled(serverSettings.subtitleEnabled);
                    }
                    if (serverSettings.userLanguage !== undefined
                        && !_pendingSettingsKeys.has('userLanguage')
                        && (!_dirtySettingsKeys.has('userLanguage')
                            || serverSnapshotNewerThanCurrent)
                        && window.subtitleBridge) {
                        window.subtitleBridge.setUserLanguage(serverSettings.userLanguage);
                    }
                }
                if (!serverSnapshotOlderThanCurrent
                    && serverResult.reset !== true
                    && serverResult.decisions) {
                    const previousAsr = S.independentAsrEnabled;
                    const adoptedAsrDecision = _adoptServerAsrDecision({
                        settings: serverSettings,
                        decisions: serverResult.decisions
                    });
                    if (adoptedAsrDecision
                        || S.independentAsrEnabled !== previousAsr) {
                        hasUpdate = true;
                    }
                }

                // Roll the baseline to the merged state BEFORE the writeback
                // save below: server-applied values must not be misattributed
                // as user-dirty by the writeback's own userInitiated diff.
                _settingsBaseline = getConversationSettings();

                if (hasUpdate) {
                    console.log('[app-settings] 已从服务器合并对话设置');
                    // 同步 window 镜像变量，防止 saveSettings() 回滚
                    window.proactiveChatEnabled = S.proactiveChatEnabled;
                    window.proactiveVisionEnabled = S.proactiveVisionEnabled;
                    window.proactiveVisionChatEnabled = S.proactiveVisionChatEnabled;
                    window.proactiveNewsChatEnabled = S.proactiveNewsChatEnabled;
                    window.proactiveCommunityChatEnabled = S.proactiveCommunityChatEnabled;
                    window.proactiveVideoChatEnabled = S.proactiveVideoChatEnabled;
                    window.proactivePersonalChatEnabled = S.proactivePersonalChatEnabled;
                    window.proactiveMusicEnabled = S.proactiveMusicEnabled;
                    window.proactiveMemeEnabled = S.proactiveMemeEnabled;
                    window.proactiveMiniGameInviteEnabled = S.proactiveMiniGameInviteEnabled;
                    window.mergeMessagesEnabled = S.mergeMessagesEnabled;
                    window.focusModeEnabled = S.focusModeEnabled;
                    window.focusCognitionEnabled = S.focusCognitionEnabled;
                    window.avatarReactionBubbleEnabled = S.avatarReactionBubbleEnabled;
                    window.slopFilterEnabled = S.slopFilterEnabled;
                    window.proactiveChatInterval = S.proactiveChatInterval;
                    window.proactiveVisionInterval = S.proactiveVisionInterval;
                    window.textGuardMaxLength = S.textGuardMaxLength;
                    // 同步回 localStorage
                    saveSettings({
                        serverMerged: true,
                        serverAuthoritativeKeys: Object.keys(
                            acceptedSharedSettings
                        )
                    });
                    // 重新初始化主动搭话调度器（使用最新标志）
                    if (typeof window.appProactive !== 'undefined' && window.appProactive.scheduleProactiveChat) {
                        window.appProactive.scheduleProactiveChat();
                    } else if (typeof window.scheduleProactiveChat === 'function') {
                        window.scheduleProactiveChat();
                    }
                }

                // 把 branch 暴露给情境弹窗模块（app-context-prompt.js）并广播 settings-ready
                // 信号——必须放在所有设置合并（server merge + saveSettings）之后。否则被缓存的
                // context 在重放时，_isActionable 会读到合并前的旧 proactiveVisionChatEnabled，
                // 误判该不该弹（Codex P2）。GET 失败 telemetryBranch 为 null 时不挂、不广播，弹窗
                // 模块拿不到「就绪」信号默认不弹（fail-closed，宁可漏弹也不拿未合并设置误弹）。
                if (telemetryBranch) {
                    window.nekoTelemetryBranch = telemetryBranch;
                    window.dispatchEvent(new CustomEvent('neko:telemetry-branch-resolved', {
                        detail: { branch: telemetryBranch },
                    }));
                }
            }).finally(() => {
                // Runs on both the merged and the failed path — which is
                // exactly why it must NOT touch _settingsMergedFromServer: on
                // the failure path (serverResult === null) nothing was merged,
                // so re-enabling full snapshots here would let the next user
                // edit POST the whole boot snapshot and overwrite every
                // untouched persisted preference. The merged path already set
                // the flag inside the callback above, before mergeSettled
                // resolves, so a sync woken by the gate observes it.
                // 必须等 GET 解析后再起 periodic sync：否则 60s 间隔的 POST 可能比 GET 先到，
                // 把首启本地默认值写到服务器；GET 回来读到自家 echo 误判「云端已有偏好」、干扰
                // 设置合并，marker 也可能错误留存。GET 走 finally 后周期同步才安全
                startPeriodicSync();
            });
            // Gate settings POSTs (bounded) behind the GET+merge so their
            // send-time snapshots carry server truth for untouched fields;
            // the catch keeps the gate non-rejecting if the merge throws.
            _settingsGetGate = Promise.race([
                mergeSettled.catch(() => { }),
                new Promise((resolve) => { setTimeout(resolve, SETTINGS_GET_GATE_TIMEOUT_MS); })
            ]);
        } catch (error) {
            console.error('服务器设置同步启动失败:', error);
            // The GET never got off the ground, so nothing was merged either:
            // _settingsMergedFromServer stays false and POSTs stay dirty-only.
            // (No in-flight read can be clobbered here, but the local snapshot
            // is still pre-merge, so a full write would overwrite server-side
            // preferences with this boot's values just the same.)
            // GET 链路本身就挂了，至少把 periodic sync 起来兜底，
            // 避免用户的本地修改永远上不了服务器
            startPeriodicSync();
        }
    }

    // ======================== 初始化调用 ========================

    window.addEventListener('storage', function (event) {
        if (event.key !== 'project_neko_settings' || !event.newValue) return;
        try {
            const settings = JSON.parse(event.newValue);
            // Cross-window independent-ASR flips are authoritative (Codex P2):
            // detect the flip BEFORE applySharedRuntimeSettings mutates S.
            const meta = _readSharedWriteMeta(settings);
            const writeIdFloorBeforeIncoming = Math.max(
                _lastSharedWriteId,
                _lastAppliedSharedWriteId
            );
            const asrValueDiffers =
                Object.prototype.hasOwnProperty.call(settings, 'independentAsrEnabled') &&
                S.independentAsrEnabled !== settings.independentAsrEnabled;
            const asrMarkedExplicit = !!meta
                && meta.changedKeys.indexOf('independentAsrEnabled') !== -1;
            const asrWriteIsNewer = !meta
                || meta.writeId > _lastAppliedSharedWriteId
                || (meta.writeId === _lastAppliedSharedWriteId && asrMarkedExplicit);
            // With metadata, a value difference alone proves nothing — every
            // saveSettings() copies independentAsrEnabled into the snapshot.
            // Require the writer to have marked the key as an explicit user
            // change AND the write to be newer than the last one applied here
            // (out-of-order or replayed snapshots must not re-flip a route).
            // An EQUAL id is not supersession, though: two windows saving in
            // the same millisecond before either observed the other's storage
            // event both mint that millisecond (the applied-id floor in
            // _nextSharedWriteId only rises once the other write has been
            // APPLIED here), and a strict `>` would drop whichever arrived
            // second — silently discarding a genuine ASR toggle and leaving
            // this window to stamp the pre-toggle route on its next handshake.
            // Concurrent writes are unordered, so break the tie on INTENT
            // rather than the clock: an explicitly toggled key wins, which
            // makes both delivery orders converge on the user's choice. A
            // strictly OLDER write is still refused, and no new metadata field
            // is involved, so windows on the previous build are unaffected.
            // Metadata-less payloads come from a window still running the
            // previous build: keep the legacy value-difference behaviour so a
            // genuine toggle from such a window is not silently dropped.
            const asrOutranksLocalChoice = !meta || _asrWriteOutranksLocalChoice(meta);
            const asrChangedByOtherWindow = meta
                ? (asrValueDiffers && asrMarkedExplicit && asrWriteIsNewer
                    && asrOutranksLocalChoice)
                : asrValueDiffers;
            const optimizationKey = 'voiceInputResourceOptimizationEnabled';
            const optimizationValueDiffers =
                Object.prototype.hasOwnProperty.call(settings, optimizationKey)
                && S[optimizationKey] !== settings[optimizationKey];
            const optimizationMarkedExplicit = !!meta
                && meta.changedKeys.indexOf(optimizationKey) !== -1;
            const optimizationWriteIsNewer = !meta
                || meta.writeId > _lastAppliedSharedWriteId
                || (
                    meta.writeId === _lastAppliedSharedWriteId
                    && optimizationMarkedExplicit
                );
            const optimizationOutranksLocalChoice =
                !meta || _optimizationWriteOutranksLocalChoice(meta);
            // Like independentAsrEnabled, this key is copied into every shared
            // snapshot. With metadata, only an explicit user change may alter
            // another window; otherwise an unhydrated writer's boot default
            // could overwrite a server-merged preference incidentally.
            const optimizationChangedByOtherWindow = meta
                ? (
                    optimizationValueDiffers
                    && optimizationMarkedExplicit
                    && optimizationWriteIsNewer
                    && optimizationOutranksLocalChoice
                )
                : optimizationValueDiffers;
            const optimizationSyncAcknowledgesLocalDecision = !!meta
                && !!meta.optimizationDecision
                && meta.optimizationDecisionPendingSync === false
                && !!_lastOptimizationDecision
                && meta.optimizationDecision.writeId
                    === _lastOptimizationDecision.writeId
                && meta.optimizationDecision.writerId
                    === _lastOptimizationDecision.writerId
                && meta.optimizationDecision.value
                    === _lastOptimizationDecision.value;
            if (optimizationSyncAcknowledgesLocalDecision) {
                _optimizationDecisionPendingSync = false;
            }
            const activeRouteBeforeSharedVoiceChange = S.voiceChatActive === true
                ? (
                    S.independentAsrActive === true
                    || (
                        S.voiceInputLifecycleState === 'blocked'
                        && S.independentAsrEnabled === true
                    )
                )
                : null;
            // Drop the key from the apply set when the snapshot's ASR value
            // carries neither user intent nor trustworthy server truth: an
            // already-superseded write, or one made before its own window
            // hydrated (pre-merge boot default for every key the user did not
            // touch) while this window already merged the server value. A
            // hydrated writer's non-explicit value still propagates as before,
            // and every other shared key keeps syncing untouched.
            const asrValueIsStale = !!meta
                && !asrChangedByOtherWindow
                && (!asrWriteIsNewer || !asrOutranksLocalChoice
                    || (!meta.asrAuthoritative && S.settingsHydrated === true));
            const optimizationValueIsStale = !!meta
                && optimizationValueDiffers
                && (
                    !optimizationChangedByOtherWindow
                    || !optimizationOutranksLocalChoice
                );
            if (meta && meta.writeId > _lastAppliedSharedWriteId) {
                _lastAppliedSharedWriteId = meta.writeId;
            }
            let incoming = settings;
            if (asrValueIsStale || optimizationValueIsStale) {
                incoming = Object.assign({}, settings);
                if (asrValueIsStale) delete incoming.independentAsrEnabled;
                if (optimizationValueIsStale) delete incoming[optimizationKey];
            }
            if (meta) {
                for (const key of meta.changedKeys) {
                    if (key === 'independentAsrEnabled') continue;
                    if (!Object.prototype.hasOwnProperty.call(
                        incoming,
                        key
                    )) continue;
                    const localToken = _knownSharedKeyWrites[key];
                    const incomingToken = meta.knownKeyWrites[key];
                    const localServerRevision =
                        _knownServerKeyRevisions[key];
                    const incomingPredatesServerFloor =
                        Number.isInteger(localServerRevision)
                        && incomingToken
                        && Number.isInteger(
                            incomingToken.confirmedRevision
                        )
                        && incomingToken.confirmedRevision
                            < localServerRevision;
                    if (incomingPredatesServerFloor
                        || (localToken
                            && !_sharedWriteTokenOutranks(meta, localToken)
                            && !_sharedWriteTokensEqual(meta, localToken))) {
                        if (incoming === settings) {
                            incoming = Object.assign({}, settings);
                        }
                        delete incoming[key];
                    }
                }
            }
            const serverMergePredatesLocalWrite = !!meta
                && meta.changedKeys.length === 0
                && !meta.knownKeyWritesPresent
                && meta.writeId <= writeIdFloorBeforeIncoming;
            if (serverMergePredatesLocalWrite) {
                if (incoming === settings) incoming = Object.assign({}, settings);
                // Compatibility for snapshots written before knownKeyWrites:
                // once this window has written a newer snapshot, an older merge
                // arriving late must not roll confirmed local values back. New
                // peers use the per-key provenance check below instead. ASR
                // keeps its independent decision-tuple ordering above.
                for (const key of _SHARED_SETTINGS_KEYS) {
                    if (key === 'independentAsrEnabled') continue;
                    delete incoming[key];
                }
            }
            if (meta && meta.knownKeyWritesPresent) {
                for (const key of _SHARED_SETTINGS_KEYS) {
                    if (key === 'independentAsrEnabled') continue;
                    if (!Object.prototype.hasOwnProperty.call(incoming, key)) continue;
                    const localToken = _knownSharedKeyWrites[key];
                    const mergeToken = meta.knownKeyWrites[key];
                    const serverAuthoritative =
                        Number.isInteger(meta.serverRevision)
                        && meta.serverAuthoritativeKeys.indexOf(key) !== -1;
                    if (serverAuthoritative) {
                        let confirmedRevision = localToken
                            && Number.isInteger(localToken.confirmedRevision)
                            ? localToken.confirmedRevision
                            : null;
                        if (_sharedWriteTokensEqual(localToken, mergeToken)
                            && Number.isInteger(
                                mergeToken.confirmedRevision
                            )) {
                            confirmedRevision = Math.max(
                                Number.isInteger(confirmedRevision)
                                    ? confirmedRevision
                                    : 0,
                                mergeToken.confirmedRevision
                            );
                        }
                        const localServerRevision =
                            _knownServerKeyRevisions[key];
                        const serverSnapshotIsOlder =
                            (Number.isInteger(_conversationSettingsRevision)
                                && meta.serverRevision
                                    < _conversationSettingsRevision)
                            || (localToken
                                && !Number.isInteger(confirmedRevision))
                            || (Number.isInteger(confirmedRevision)
                                && meta.serverRevision
                                    < confirmedRevision)
                            || (Number.isInteger(localServerRevision)
                                && meta.serverRevision
                                    < localServerRevision);
                        if (serverSnapshotIsOlder) {
                            if (incoming === settings) {
                                incoming = Object.assign({}, settings);
                            }
                            delete incoming[key];
                        }
                        continue;
                    }
                    // The merge envelope may be newer while this particular
                    // field still predates an explicit write already accepted
                    // here. Apply only when the sender's per-key provenance is
                    // at least as new as ours.
                    if (localToken
                        && !_sharedWriteTokenOutranks(mergeToken, localToken)
                        && !(mergeToken
                            && mergeToken.writeId === localToken.writeId
                            && mergeToken.writerId === localToken.writerId)) {
                        if (incoming === settings) incoming = Object.assign({}, settings);
                        delete incoming[key];
                    }
                }
            }
            const pendingKeysToReassert = [];
            if (meta) {
                // Keep this as for...of: static listener-contract tests slice
                // at the first callback terminator.
                for (const key of _pendingSettingsKeys) {
                    // A full snapshot carries incidental copies of every other
                    // key. A sender-declared edit supersedes local pending
                    // intent only when its per-key token is at least as new.
                    const localToken = _knownSharedKeyWrites[key];
                    const incomingIsExplicit =
                        meta.changedKeys.indexOf(key) !== -1;
                    const incomingCanSupersede = incomingIsExplicit
                        && (!localToken
                            || _sharedWriteTokenOutranks(meta, localToken)
                            || (meta.writeId === localToken.writeId
                                && meta.writerId === localToken.writerId));
                    if (incomingCanSupersede) continue;
                    if (!Object.prototype.hasOwnProperty.call(settings, key)) continue;
                    if (Object.prototype.hasOwnProperty.call(incoming, key)) {
                        if (incoming === settings) incoming = Object.assign({}, settings);
                        delete incoming[key];
                    }
                    pendingKeysToReassert.push(key);
                }
            }
            _noteCrossWindowMutations(incoming, meta ? meta.changedKeys : null);
            const changed = applySharedRuntimeSettings(incoming);
            if (meta) {
                _rememberKnownSharedKeyWrites(meta.knownKeyWrites, incoming);
                _rememberServerKeyRevisions(
                    meta.serverKeyRevisions,
                    incoming
                );
                _rememberSharedKeyWrites(meta.changedKeys, meta, incoming);
                for (const key of meta.serverAuthoritativeKeys) {
                    if (Object.prototype.hasOwnProperty.call(incoming, key)) {
                        _rememberServerKeyRevisions({
                            [key]: meta.serverRevision
                        }, incoming);
                    }
                }
                if (Number.isInteger(meta.serverRevision)) {
                    if (!Number.isInteger(_conversationSettingsRevision)
                        || meta.serverRevision > _conversationSettingsRevision) {
                        _conversationSettingsRevision = meta.serverRevision;
                        _conversationSettingsEtag =
                            `"conversation-settings-${meta.serverRevision}"`;
                    }
                    if (meta.serverRevision === _conversationSettingsRevision) {
                        for (const key of meta.serverAuthoritativeKeys) {
                            if (!Object.prototype.hasOwnProperty.call(
                                incoming,
                                key
                            )) continue;
                            _conversationSettingsEtagKeyMutationVersions[key] =
                                _crossWindowKeyMutationVersions[key] || 0;
                        }
                    }
                }
            }
            if (meta && meta.asrDecision
                && Object.prototype.hasOwnProperty.call(
                    incoming,
                    'independentAsrEnabled'
                )
                && incoming.independentAsrEnabled === meta.asrDecision.value) {
                _adoptAsrDecisionTuple(
                    meta.asrDecision,
                    Number.isInteger(meta.serverRevision)
                );
            }
            // Roll the dirty-diff baseline for every key just adopted from
            // another window. _markUserDirtySettings diffs against this
            // baseline, so without the roll a value this window merely
            // RECEIVED looks like a local user edit on the next unrelated
            // save: the key gets marked dirty, that dirty mark grants
            // S.independentAsrAuthoritative, the key rides out in changedKeys
            // as an explicit toggle other windows then trust, and the still
            // pending settings GET skips it as user-owned. A received value
            // laundered into user intent that way can pin a whole window to a
            // stale ASR route and POST it back over the server's. Keys this
            // window really did touch stay dirty and keep their authority.
            // for...of rather than a forEach callback on purpose: the
            // callback's closing punctuation would truncate the listener slice
            // that test_cross_window_asr_flip_marks_hydration_and_asr_dirty
            // extracts by string split.
            if (_settingsBaseline) {
                for (const key of _SHARED_SETTINGS_KEYS) {
                    if (_dirtySettingsKeys.has(key)) continue;
                    if (!Object.prototype.hasOwnProperty.call(incoming, key)) continue;
                    _settingsBaseline[key] = S[key];
                }
            }
            if (asrChangedByOtherWindow) {
                // The writer marked independentAsrEnabled as an explicit user
                // change (or is a metadata-less legacy window, where a value
                // difference is the only signal available). Either way this is
                // a real cross-window toggle, not the incidental copy an
                // unrelated save carries. Treat it exactly like a local user
                // change: mark hydration so the next start_session handshake stamps the new
                // value (app-websocket.js attachStartSessionHandshake gates on
                // S.settingsHydrated; without it the backend would read the OLD
                // persisted value while the other window's POST is in flight),
                // and mark the key dirty so this window's still-pending settings
                // GET preserves the flip during its field-level merge instead of
                // overwriting it and POSTing the old value back via
                // saveSettings(). Deliberately no
                // POST from here: the originating window owns persistence, and a
                // receiving-window POST would duplicate writes and loop storage
                // events. Other shared keys stay non-authoritative — they never
                // reach the handshake, and marking hydration for them would let
                // a first-launch write in another window arm this window's
                // periodic sync with non-authoritative boot values.
                S.settingsHydrated = true;
                _dirtySettingsKeys.add('independentAsrEnabled');
                S.independentAsrAuthoritative = true;
                if (meta) {
                    const adopted = meta.asrDecision || meta;
                    _noteAsrDecision(adopted.writeId, adopted.writerId, settings.independentAsrEnabled);
                }
            }
            if (optimizationChangedByOtherWindow) {
                // Preserve a genuine cross-window toggle across this window's
                // still-pending server merge without granting unrelated fields
                // handshake authority or emitting a duplicate POST.
                S.settingsHydrated = true;
                S.voiceInputResourceOptimizationAuthoritative = true;
                _dirtySettingsKeys.add(optimizationKey);
                _optimizationDecisionPendingSync = !meta
                    || meta.optimizationDecisionPendingSync;
                if (meta) {
                    const adopted = meta.optimizationDecision || meta;
                    _noteOptimizationDecision(
                        adopted.writeId,
                        adopted.writerId,
                        settings[optimizationKey]
                    );
                }
            }
            if (asrChangedByOtherWindow || optimizationChangedByOtherWindow) {
                const targetEpoch = (Number(S.voiceSessionStartEpoch) || 0) + 1;
                if (
                    asrChangedByOtherWindow
                    && (
                        S.voiceSettingsPendingUntilEpoch !== targetEpoch
                        || S.pendingVoiceRouteIndependentAsr === null
                    )
                ) {
                    S.pendingVoiceRouteIndependentAsr =
                        activeRouteBeforeSharedVoiceChange;
                }
                S.voiceSettingsPendingUntilEpoch = targetEpoch;
                window.dispatchEvent(new CustomEvent(
                    'neko:voice-settings-pending-changed'
                ));
            }
            stopVisionAfterPrivacyEnabled();
            if (changed && typeof window.scheduleProactiveChat === 'function') {
                window.scheduleProactiveChat();
            }
            if (pendingKeysToReassert.length > 0 && !(meta && meta.pendingRecovery)) {
                // A server-merge broadcast can have been built before this
                // window's latest pending edit reached its sender. Restore the
                // local full snapshot without claiming a NEW user edit. Mark it
                // as a one-hop recovery: another pending window still filters
                // its own values, but does not answer with another recovery and
                // start a localStorage ping-pong. The already queued user sync
                // remains the sole server writer.
                saveSettings({
                    skipServerSync: true,
                    serverMerged: true,
                    pendingRecovery: true
                });
            }
        } catch (error) {
            console.warn('[app-settings] 跨窗口设置同步失败:', error);
        }
    });

    // 加载设置
    loadSettings();

    // ======================== 启动后调度 ========================

    /**
     * 初始化后启动主动搭话调度器
     * 需要在其他模块加载完成后由 app.js 主调度器调用
     * 或在 DOMContentLoaded / 入口处调用
     */
    function initProactiveChatScheduler() {
        // 防止重复初始化
        if (S._proactiveSchedulerInitialized) {
            console.log('[主动搭话] 调度器已初始化，跳过重复调用');
            return;
        }
        
        // 加载麦克风设备选择
        if (typeof window.appAudioCapture !== 'undefined' && window.appAudioCapture.loadSelectedMicrophone) {
            window.appAudioCapture.loadSelectedMicrophone();
        } else if (typeof window.loadSelectedMicrophone === 'function') {
            window.loadSelectedMicrophone();
        }

        // 加载麦克风增益设置
        if (typeof window.appAudioCapture !== 'undefined' && window.appAudioCapture.loadMicGainSetting) {
            window.appAudioCapture.loadMicGainSetting();
        } else if (typeof window.loadMicGainSetting === 'function') {
            window.loadMicGainSetting();
        }

        // 加载降噪设置
        if (typeof window.appAudioCapture !== 'undefined' && window.appAudioCapture.loadNoiseReductionSetting) {
            window.appAudioCapture.loadNoiseReductionSetting();
        }

        // 加载扬声器音量设置
        if (typeof window.appAudioPlayback !== 'undefined' && window.appAudioPlayback.loadSpeakerVolumeSetting) {
            window.appAudioPlayback.loadSpeakerVolumeSetting();
        } else if (typeof window.loadSpeakerVolumeSetting === 'function') {
            window.loadSpeakerVolumeSetting();
        }

        // 加载播放设备选择；默认显式使用 Chromium 的 default 多媒体输出，
        // 不使用 communications 默认语音通话设备。
        if (typeof window.appAudioPlayback !== 'undefined' && window.appAudioPlayback.loadSelectedSpeaker) {
            window.appAudioPlayback.loadSelectedSpeaker();
        } else if (typeof window.loadSelectedSpeaker === 'function') {
            window.loadSelectedSpeaker();
        }

        // 如果已开启主动搭话且选择了搭话方式，立即启动定时器
        if (S.proactiveChatEnabled && (S.proactiveVisionChatEnabled || S.proactiveNewsChatEnabled || S.proactiveCommunityChatEnabled || S.proactiveVideoChatEnabled || S.proactivePersonalChatEnabled || S.proactiveMusicEnabled || S.proactiveMemeEnabled || S.proactiveMiniGameInviteEnabled)) {
            // 主动搭话启动自检
            console.log('========== 主动搭话启动自检 ==========');
            console.log('[自检] proactiveChatEnabled: ' + S.proactiveChatEnabled);
            console.log('[自检] proactiveVisionChatEnabled: ' + S.proactiveVisionChatEnabled);
            console.log('[自检] proactiveNewsChatEnabled: ' + S.proactiveNewsChatEnabled);
            console.log('[自检] proactiveCommunityChatEnabled: ' + S.proactiveCommunityChatEnabled);
            console.log('[自检] proactiveVideoChatEnabled: ' + S.proactiveVideoChatEnabled);
            console.log('[自检] proactivePersonalChatEnabled: ' + S.proactivePersonalChatEnabled);
            console.log('[自检] proactiveMusicEnabled: ' + S.proactiveMusicEnabled);
            console.log('[自检] proactiveMemeEnabled: ' + S.proactiveMemeEnabled);
            console.log('[自检] proactiveMiniGameInviteEnabled: ' + S.proactiveMiniGameInviteEnabled);
            console.log('[自检] localStorage设置: ' + (localStorage.getItem('project_neko_settings') ? '已存在' : '不存在'));

            // 检查WebSocket连接状态
            var wsStatus = S.socket ? S.socket.readyState : undefined;
            console.log('[自检] WebSocket状态: ' + wsStatus + ' (1=OPEN, 0=CONNECTING, 2=CLOSING, 3=CLOSED)');

            if (typeof window.appProactive !== 'undefined' && window.appProactive.scheduleProactiveChat) {
                window.appProactive.scheduleProactiveChat();
            } else if (typeof window.scheduleProactiveChat === 'function') {
                window.scheduleProactiveChat();
            }
            console.log('========== 主动搭话启动自检完成 ==========');
        } else {
            console.log('[App] 主动搭话未满足启动条件，跳过调度器启动:');
            console.log('  - proactiveChatEnabled: ' + S.proactiveChatEnabled);
            console.log('  - 任意搭话模式启用: ' + (S.proactiveVisionChatEnabled || S.proactiveNewsChatEnabled || S.proactiveCommunityChatEnabled || S.proactiveVideoChatEnabled || S.proactivePersonalChatEnabled || S.proactiveMusicEnabled || S.proactiveMemeEnabled || S.proactiveMiniGameInviteEnabled));
        }

        // 所有步骤完成后，最后才设置初始化成功的标志
        S._proactiveSchedulerInitialized = true;
    }

    // ======================== 导出 ========================

    mod.saveSettings = saveSettings;
    mod.loadSettings = loadSettings;
    mod.syncSettingsToServer = syncSettingsToServer;
    mod.getConversationSettings = getConversationSettings;
    mod.initProactiveChatScheduler = initProactiveChatScheduler;
    mod._isUserRegionChina = _isUserRegionChina;
    mod.stopPeriodicSync = stopPeriodicSync;

    window.appSettings = mod;

    // 暴露到全局作用域，供 live2d.js 等其他模块调用（向后兼容）
    window.saveNEKOSettings = saveSettings;
})();
