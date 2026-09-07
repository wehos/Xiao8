// 允许的来源列表
const ALLOWED_ORIGINS = [window.location.origin];
const MINIMAX_PREFIX_MAX_LENGTH = 10;
let workshopReferenceFile = null;
let workshopReferenceAudioUrl = '';
let providerTouchedByUser = false;
let suppressProviderTouchedTracking = false;
let activeVoicePreviewNotice = null;
// 防止并发应用音色的可重入守卫
let isApplyingVoice = false;
const VOICE_CLONE_PROVIDER_REGISTRY_KEYS = Object.freeze({
    cosyvoice: 'qwen',
    cosyvoice_intl: 'qwen_intl',
    minimax: 'minimax',
    minimax_intl: 'minimax_intl',
    elevenlabs: 'elevenlabs',
    mimo: 'mimo',
    vllm_omni: 'vllm_omni',
    doubao_tts: 'doubao_tts',
});
const VOICE_CLONE_RESTRICTED_REGISTRY_KEYS = new Set([
    'qwen_intl',
    'minimax_intl',
    'elevenlabs',
]);
const VOICE_CLONE_PROVIDER_KEY_FIELDS = Object.freeze([
    ['cosyvoice', 'assistApiKeyQwen'],
    ['cosyvoice_intl', 'assistApiKeyQwenIntl'],
    ['minimax', 'assistApiKeyMinimax'],
    ['minimax_intl', 'assistApiKeyMinimaxIntl'],
    ['elevenlabs', 'assistApiKeyElevenlabs'],
    ['mimo', 'assistApiKeyMimo'],
    ['doubao_tts', 'assistApiKeyDoubaoTts'],
]);
const voiceCloneProviderRestrictionState = {
    loaded: false,
    loadingPromise: null,
    isMainlandChinaUser: false,
    apiKeyRegistry: {},
    ttsProviders: {},
};
const voiceCloneApiConfigState = {
    loaded: false,
    loadingPromise: null,
    cfg: null,
    isLocalTts: false,
};
const VOICE_CLONE_LOADER_FETCH_TIMEOUT_MS = 5000;
const VOICE_CLONE_LOADER_FETCH_ATTEMPTS = 3;
const VOICE_CLONE_LOADER_FETCH_BACKOFF_MS = 250;
function sleepVoiceCloneLoaderRetry(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function fetchVoiceCloneLoaderResponse(url, options = {}) {
    let lastError = null;
    for (let attempt = 1; attempt <= VOICE_CLONE_LOADER_FETCH_ATTEMPTS; attempt += 1) {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), VOICE_CLONE_LOADER_FETCH_TIMEOUT_MS);
        try {
            const response = await fetch(url, {
                ...options,
                signal: controller.signal,
            });
            if (response.ok || response.status < 500 || attempt >= VOICE_CLONE_LOADER_FETCH_ATTEMPTS) {
                return response;
            }
            lastError = new Error(`API returned ${response.status}`);
        } catch (error) {
            lastError = error;
            if (attempt >= VOICE_CLONE_LOADER_FETCH_ATTEMPTS) break;
        } finally {
            clearTimeout(timeoutId);
        }
        await sleepVoiceCloneLoaderRetry(VOICE_CLONE_LOADER_FETCH_BACKOFF_MS * attempt);
    }
    throw lastError || new Error('请求失败');
}

async function fetchVoiceCloneLoaderJson(url, options = {}) {
    const response = await fetchVoiceCloneLoaderResponse(url, options);
    let data = null;
    try {
        data = await response.json();
    } catch (error) {
        if (response.ok) {
            throw error;
        }
    }
    if (!response.ok) {
        throw new Error(`API returned ${response.status}`);
    }
    return data;
}

function notifyApiSettingsKeyBookFocus(win) {
    if (!win) return;
    [250, 800, 1500].forEach(delay => {
        setTimeout(() => {
            try {
                win.postMessage({ type: 'focus_api_key_book' }, window.location.origin);
            } catch (_) { }
        }, delay);
    });
}

// 打开API设置页（带弹窗拦截回退）
function openApiSettings(options = {}) {
    const focusKeyBook = !!(options && options.focusKeyBook);
    const url = focusKeyBook ? '/api_key?focus=key_book' : '/api_key';
    const windowName = 'neko_api_key';
    const features = typeof window.buildApiKeySettingsWindowFeatures === 'function'
        ? window.buildApiKeySettingsWindowFeatures()
        : undefined;
    const win = typeof window.openOrFocusWindow === 'function'
        ? window.openOrFocusWindow(url, windowName, features)
        : window.open(url, windowName, features);
    if (win) {
        const modal = document.getElementById('noApiModal');
        if (modal) modal.style.display = 'none';
        if (typeof win.focus === 'function') {
            try {
                win.focus();
            } catch (_) {}
        }
        if (focusKeyBook) {
            notifyApiSettingsKeyBookFocus(win);
        }
    } else {
        location.href = url;
    }
}

function openApiSettingsKeyBook() {
    openApiSettings({ focusKeyBook: true });
}

// 安全地解析 fetch 响应：当后端/反向代理返回 HTML（404/502/504/网关错误等）时
// 不应抛出 "Unexpected token '<', '<html>...' is not valid JSON"，而应返回带状态码的可读错误。
async function safeReadResponse(res) {
    const contentType = (res.headers.get('content-type') || '').toLowerCase();
    // 识别 application/json 以及 RFC 6839 的结构化后缀（如 application/problem+json,
    // application/vnd.api+json 等），它们都是合法 JSON。
    const isJsonContentType = contentType.includes('application/json') || /\+json(\s*;|\s*$)/.test(contentType);
    if (isJsonContentType) {
        try {
            return { data: await res.json(), nonJson: false, text: '' };
        } catch (_) {
            // Content-Type 声明 JSON 但解析失败，落到文本分支
        }
    }
    let text = '';
    try { text = await res.text(); } catch (_) { text = ''; }
    return { data: null, nonJson: true, text };
}

function buildNonJsonError(res, text) {
    // 去除 HTML 标签并截断，避免把整段 HTML 报告给用户
    const snippet = text
        ? text.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 120)
        : '';
    if (window.t) {
        if (res.status === 404) {
            return window.t('voice.serverRouteNotFound', { status: res.status });
        }
        return window.t('voice.serverNonJsonError', {
            status: res.status,
            snippet: snippet || res.statusText || ''
        });
    }
    if (res.status === 404) {
        return `接口未找到 (HTTP 404)，请确认服务端已正确部署并重启`;
    }
    return `服务端返回了非JSON响应 (HTTP ${res.status})${snippet ? ': ' + snippet : ''}`;
}

// 把后端错误响应体转成可读消息：
// 只有在 errors.<code> 翻译确实存在时才使用 i18n，否则回退到响应自带文案，
// 避免 i18next 的「缺失 key 回退成 key 本身」行为把 "errors.XXX_UNKNOWN" 直接丢给用户。
function resolveBackendErrorMsg(data, status) {
    if (data && data.code && window.t) {
        const i18nKey = 'errors.' + data.code;
        const translated = window.t(i18nKey, data.details || {});
        if (translated && translated !== i18nKey) {
            return translated;
        }
    }
    return (data && (data.detail || data.message || data.error)) || `API returned ${status}`;
}

function normalizeVoicePreviewLanguage(rawLanguage) {
    const current = String(rawLanguage || '').trim().toLowerCase();
    if (!current || current === 'auto') return 'zh-CN';
    if (current === 'tchinese' || current.startsWith('zh-tw') || current.startsWith('zh-hk') || current.includes('hant')) return 'zh-TW';
    if (current === 'schinese' || current.startsWith('zh')) return 'zh-CN';
    if (current === 'english' || current.startsWith('en')) return 'en';
    if (current === 'japanese' || current.startsWith('ja')) return 'ja';
    if (current === 'koreana' || current === 'korean' || current.startsWith('ko')) return 'ko';
    if (current === 'russian' || current.startsWith('ru')) return 'ru';
    if (current === 'spanish' || current === 'latam' || current.startsWith('es')) return 'es';
    if (current === 'portuguese' || current === 'brazilian' || current.startsWith('pt')) return 'pt';
    return 'en';
}

function voiceCloneI18n(key, fallback) {
    return VoiceDisplayUtils.t(key, fallback);
}

function getNativeProviderShortName(provider) {
    return VoiceDisplayUtils.providerShortName(provider, {
        freeKey: 'voice.providerFreeApi',
        freeFallback: 'Free API',
    });
}

function getNativeVoiceProviderLabel(nativeEntries) {
    if (!Array.isArray(nativeEntries)) return '';
    for (const [, voiceData] of nativeEntries) {
        const provider = voiceData && String(voiceData.provider || '').trim();
        const configuredLabel = voiceData && String(voiceData.provider_label || '').trim();
        // Configured presets share the Custom API source label without losing
        // the provider key that owns preview and synthesis behavior.
        // 配置音色共用“自定义 API”来源名，provider key 仍负责试听与合成路由。
        if (VoiceDisplayUtils.isKnownProvider(configuredLabel)) {
            return getNativeProviderShortName(configuredLabel);
        }
        if (VoiceDisplayUtils.isKnownProvider(provider)) {
            return getNativeProviderShortName(provider);
        }
        const label = configuredLabel || provider;
        if (label) return String(label);
    }
    return '';
}

function formatNativeVoiceLabel(nativeEntries) {
    const providerLabel = getNativeVoiceProviderLabel(nativeEntries);
    if (providerLabel) {
        return window.t
            ? window.t('voice.nativePresetLabel', { provider: providerLabel })
            : providerLabel + ' 原生音色';
    }
    return window.t ? window.t('voice.nativePresetLabelGeneric') : '原生预设音色';
}

function getNativeVoiceDisplayName(voiceId, voiceData) {
    return VoiceDisplayUtils.nativeVoiceDisplayName(voiceId, voiceData);
}

function getVoicePreviewLanguage() {
    const candidates = [
        window.i18n && window.i18n.language,
        window.localStorage && window.localStorage.getItem('i18nextLng'),
        document.documentElement && document.documentElement.lang,
        navigator.language,
        window.localStorage && window.localStorage.getItem('locale')
    ];

    for (let index = 0; index < candidates.length; index += 1) {
        const candidate = String(candidates[index] || '').trim();
        if (candidate && candidate.toLowerCase() !== 'auto') {
            return normalizeVoicePreviewLanguage(candidate);
        }
    }
    return 'zh-CN';
}

function appendVoiceApplyStatus(resultDiv, message, className = '') {
    if (!resultDiv) return;
    if (resultDiv.textContent || resultDiv.childNodes.length) {
        resultDiv.appendChild(document.createElement('br'));
    }
    const statusSpan = document.createElement('span');
    if (className) statusSpan.className = className;
    statusSpan.textContent = message;
    resultDiv.appendChild(statusSpan);
}

function notifyVoiceIdUpdated(voiceId, lanlanName, sessionRestarted) {
    const payload = { type: 'voice_id_updated', voice_id: voiceId, lanlan_name: lanlanName, session_restarted: sessionRestarted };
    if (window.parent !== window) {
        try { window.parent.postMessage(payload, window.location.origin); } catch (e) { }
    }
    if (window.opener && !window.opener.closed) {
        try { window.opener.postMessage(payload, window.location.origin); } catch (e) { }
    }
}

function createVoiceConfigSwitchOpId(lanlanName) {
    return 'voice-config-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8) + '-' + (lanlanName || 'current');
}

function notifyVoiceConfigSwitching(lanlanName, active, opId) {
    const payload = {
        action: 'voice_config_switching',
        type: 'voice_config_switching',
        active: !!active,
        op_id: opId || '',
        lanlan_name: lanlanName || '',
        timestamp: Date.now()
    };

    if (typeof BroadcastChannel !== 'undefined') {
        try {
            const channel = new BroadcastChannel('neko_page_channel');
            channel.postMessage(payload);
            setTimeout(() => channel.close(), 1000);
        } catch (_) { /* 跨窗口同步失败时继续走 postMessage 兜底 */ }
    }

    if (window.nekoElectronVoiceConfigSwitching && typeof window.nekoElectronVoiceConfigSwitching.send === 'function') {
        try { window.nekoElectronVoiceConfigSwitching.send(payload); } catch (_) { }
    }

    if (window.parent !== window) {
        try { window.parent.postMessage(payload, window.location.origin); } catch (_) { }
    }
    if (window.opener && !window.opener.closed) {
        try { window.opener.postMessage(payload, window.location.origin); } catch (_) { }
    }
}

async function saveVoiceIdToCurrentCharacter(voiceId) {
    const lanlanInput = document.getElementById('lanlan_name');
    const lanlanName = (lanlanInput && lanlanInput.value ? lanlanInput.value : '').trim();
    if (!lanlanName) {
        throw new Error(window.t ? window.t('voice.noCurrentCharacterForApply') : '未找到当前角色，无法应用音色');
    }

    const switchOpId = createVoiceConfigSwitchOpId(lanlanName);
    notifyVoiceConfigSwitching(lanlanName, true, switchOpId);
    let resp;
    try {
        resp = await fetch(`/api/characters/catgirl/voice_id/${encodeURIComponent(lanlanName)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ voice_id: voiceId })
        });
    } finally {
        notifyVoiceConfigSwitching(lanlanName, false, switchOpId);
    }
    const { data, nonJson, text } = await safeReadResponse(resp);
    if (!resp.ok) {
        if (data && (data.error || data.detail)) {
            throw new Error(data.error || data.detail);
        }
        throw new Error(buildNonJsonError(resp, text));
    }
    if (nonJson) {
        throw new Error(buildNonJsonError(resp, text));
    }
    if (!data || data.success === false) {
        throw new Error((data && data.error) || (window.t ? window.t('common.unknownError') : '未知错误'));
    }
    notifyVoiceIdUpdated(voiceId, lanlanName, data.session_restarted);
    return { lanlanName, result: data };
}

async function getCurrentCharacterVoiceId() {
    const lanlanInput = document.getElementById('lanlan_name');
    const lanlanName = (lanlanInput && lanlanInput.value ? lanlanInput.value : '').trim();
    if (!lanlanName) return '';

    const resp = await fetchVoiceCloneLoaderResponse('/api/characters?language=zh-CN', { cache: 'no-store' });
    const { data, nonJson, text } = await safeReadResponse(resp);
    if (!resp.ok) {
        if (data && (data.error || data.detail)) {
            throw new Error(data.error || data.detail);
        }
        throw new Error(buildNonJsonError(resp, text));
    }
    if (nonJson) {
        throw new Error(buildNonJsonError(resp, text));
    }

    const catgirls = data && data['猫娘'];
    const currentCatgirl = catgirls && catgirls[lanlanName];
    if (!currentCatgirl || typeof currentCatgirl !== 'object') return '';
    return String(currentCatgirl.voice_id || '').trim();
}

function markSelectedVoiceItem(item, selected) {
    if (!item) return;
    item.classList.toggle('selected', !!selected);
    item.setAttribute('aria-pressed', selected ? 'true' : 'false');
}

async function applyVoiceToCurrentCharacter(voiceId, displayName, item) {
    if (!voiceId) return;
    if (isApplyingVoice) return;
    isApplyingVoice = true;

    const resultDiv = document.getElementById('result');
    const refreshBtn = document.getElementById('refresh-voices-btn');
    const voiceItems = document.querySelectorAll('.voice-list-item');
    const previousSelectedVoiceIds = new Set(
        Array.from(voiceItems)
            .filter(node => node.classList.contains('selected'))
            .map(node => node.dataset.voiceId || '')
            .filter(Boolean)
    );

    voiceItems.forEach(node => {
        node.classList.remove('applying', 'selected');
        node.setAttribute('aria-pressed', 'false');
    });
    if (item) item.classList.add('applying');
    if (refreshBtn) refreshBtn.disabled = true;

    if (resultDiv) {
        resultDiv.className = 'result';
        resultDiv.textContent = window.t ? window.t('voice.applyingVoice', { name: displayName || voiceId }) : `正在应用音色「${displayName || voiceId}」...`;
    }

    try {
        const { result } = await saveVoiceIdToCurrentCharacter(voiceId);
        if (item) {
            item.classList.remove('applying');
            markSelectedVoiceItem(item, true);
        }
        if (resultDiv) {
            resultDiv.className = 'result';
            resultDiv.textContent = window.t ? window.t('voice.applyVoiceSuccess', { name: displayName || voiceId }) : `已将音色「${displayName || voiceId}」应用到当前角色`;
            const statusText = result.session_restarted
                ? (window.t ? window.t('voice.pageWillRefresh') : '当前页面即将自动刷新以应用新语音')
                : (window.t ? window.t('voice.voiceWillTakeEffect') : '新语音将在下次对话时生效');
            appendVoiceApplyStatus(resultDiv, statusText);
        }
    } catch (e) {
        if (item) item.classList.remove('applying');
        voiceItems.forEach(node => markSelectedVoiceItem(node, previousSelectedVoiceIds.has(node.dataset.voiceId || '')));
        const errorMsg = e?.message || e?.toString() || (window.t ? window.t('common.unknownError') : '未知错误');
        if (resultDiv) {
            resultDiv.className = 'result error';
            resultDiv.textContent = window.t ? window.t('voice.applyVoiceFailed', { error: errorMsg }) : `应用音色失败：${errorMsg}`;
        }
    } finally {
        isApplyingVoice = false;
        if (refreshBtn) refreshBtn.disabled = false;
    }
}

function parseVoiceRegisterError(errorObj) {
    const errorCode = errorObj?.code;
    const errorMsg = errorObj?.message || errorObj?.error || errorObj || '';
    let displayError = errorMsg;
    let shouldFlash = false;

    if (errorCode === 'PREFIX_INVALID') {
        displayError = window.t ? window.t('voice.prefixShouldBeEnglishLetterAndNumber') : '前缀应为英文字母和数字';
        shouldFlash = true;
    } else if (errorCode === 'VOICE_DESIGN_PREFIX_INVALID') {
        const prefixMax = Number(errorObj?.details?.max);
        if (prefixMax > 0) {
            displayError = window.t
                ? window.t('voice.designPrefixInvalid', { max: prefixMax })
                : `前缀必须是 1-${prefixMax} 个字符，只能包含英文字母和数字，不能包含下划线或空格`;
        } else {
            displayError = window.t
                ? window.t('voice.prefixShouldBeEnglishLetterAndNumber')
                : '前缀应为英文字母和数字';
        }
        shouldFlash = true;
    } else if (errorCode === 'INVALID_API_KEY') {
        displayError = window.t ? window.t('voice.invalidApiKeyProvided') : '提供的API密钥无效';
        shouldFlash = true;
    } else {
        const lowerMsg = errorMsg.toLowerCase();
        if (lowerMsg.includes('prefix should be') && lowerMsg.includes('english letter and number')) {
            displayError = window.t ? window.t('voice.prefixShouldBeEnglishLetterAndNumber') : '前缀应为英文字母和数字';
            shouldFlash = true;
        } else if (lowerMsg.includes('invalid api-key provided')) {
            displayError = window.t ? window.t('voice.invalidApiKeyProvided') : '提供的API密钥无效';
            shouldFlash = true;
        }
    }

    return { displayError, shouldFlash };
}

function isMiniMaxProvider(provider) {
    return provider === 'minimax' || provider === 'minimax_intl';
}

function isDoubaoTtsProvider(provider) {
    return provider === 'doubao_tts';
}

function isDoubaoSpeakerId(value) {
    return /^S_[A-Za-z0-9]+$/.test(String(value || '').trim());
}

function getVoiceCloneProviderKeyField(provider) {
    const entry = VOICE_CLONE_PROVIDER_KEY_FIELDS.find(([providerKey]) => providerKey === provider);
    return entry ? entry[1] : '';
}

// MiMo 的可用凭据在普通 key 或 Token Plan key 两个字段之一——但**不是 OR**：后端
// get_tts_api_key('mimo') 严格按「assistApi=='mimo' 且 useMimoTokenPlan」二选一取其中一个
// （token plan 激活取 assistApiKeyMimoTokenPlan，否则取 assistApiKeyMimo）。前端可用性判定必须
// 镜像同一条规则，否则会出现「OR 判定有 key 但后端取的是另一个空字段 → 上传后 400」的假阳性
// （Codex review #1851）。
const VOICE_CLONE_MIMO_TOKEN_PLAN_KEY_FIELD = 'assistApiKeyMimoTokenPlan';

function getActiveMimoKeyField(cfg) {
    const tokenPlanActive = String((cfg && cfg.assistApi) || '').trim().toLowerCase() === 'mimo'
        && !!(cfg && cfg.useMimoTokenPlan);
    return tokenPlanActive ? VOICE_CLONE_MIMO_TOKEN_PLAN_KEY_FIELD : 'assistApiKeyMimo';
}

function cfgHasDoubaoTtsKey(cfg) {
    return !!(cfg && cfg.assistApiKeyDoubaoTts);
}

function cfgHasCloneProviderKey(cfg, provider) {
    if (!cfg || typeof cfg !== 'object') return false;
    if (provider === 'mimo') {
        return !!cfg[getActiveMimoKeyField(cfg)];
    }
    if (provider === 'doubao_tts') {
        return cfgHasDoubaoTtsKey(cfg);
    }
    const fieldName = getVoiceCloneProviderKeyField(provider);
    return !!(fieldName && cfg[fieldName]);
}

function getVoiceCloneProviderRegistryKey(provider) {
    return VOICE_CLONE_PROVIDER_REGISTRY_KEYS[provider] || provider;
}

function isLocalVoiceCloneServerConfigured(cfg) {
    if (!cfg || typeof cfg !== 'object') return false;
    const ttsUrl = String(cfg.ttsModelUrl || '');
    return !!(cfg.enableCustomApi && (ttsUrl.startsWith('ws://') || ttsUrl.startsWith('wss://')));
}

async function loadVoiceCloneApiConfigState(options = {}) {
    const force = !!(options && options.force);
    if (voiceCloneApiConfigState.loaded && !force) {
        return voiceCloneApiConfigState;
    }
    if (voiceCloneApiConfigState.loadingPromise && !force) {
        return voiceCloneApiConfigState.loadingPromise;
    }

    voiceCloneApiConfigState.loadingPromise = (async () => {
        const cfg = await fetchVoiceCloneLoaderJson('/api/config/core_api');
        if (!cfg || cfg.success === false) {
            throw new Error((cfg && cfg.error) || 'core_api config unavailable');
        }
        voiceCloneApiConfigState.cfg = cfg;
        voiceCloneApiConfigState.isLocalTts = isLocalVoiceCloneServerConfigured(cfg);
        voiceCloneApiConfigState.loaded = true;
        return voiceCloneApiConfigState;
    })().finally(() => {
        voiceCloneApiConfigState.loadingPromise = null;
    });

    return voiceCloneApiConfigState.loadingPromise;
}

async function ensureVoiceCloneApiConfigState(options = {}) {
    try {
        await loadVoiceCloneApiConfigState(options);
    } catch (error) {
        console.warn('检查克隆API Key失败:', error);
        voiceCloneApiConfigState.cfg = null;
        voiceCloneApiConfigState.isLocalTts = false;
        voiceCloneApiConfigState.loaded = false;
    }
    return voiceCloneApiConfigState;
}

function getConfiguredTtsProviderLabel(provider) {
    const normalizedProvider = String(provider || '').trim().toLowerCase();
    const translationKeyByProvider = {
        follow_core: 'api.customModelProviderFollowCore',
        follow_assist: 'api.customModelProviderFollowAssist',
        follow_conversation: 'api.customModelProviderFollowConversation',
        follow_summary: 'api.customModelProviderFollowSummary',
        custom: 'api.customModelProviderCustom',
        vllm_omni: 'api.providerNames.vllm_omni',
    };
    const translationKey = translationKeyByProvider[normalizedProvider]
        || `api.assistProviderNames.${normalizedProvider}`;
    if (window.t) {
        const translated = window.t(translationKey);
        if (translated && translated !== translationKey) return translated;
    }
    return normalizedProvider || voiceCloneI18n('voice.providerUnknown', 'Other');
}

function isVoiceCloneConfigFlagEnabled(value) {
    if (typeof value === 'string') {
        return ['true', '1', 'yes', 'on'].includes(value.trim().toLowerCase());
    }
    return typeof value === 'number' ? value !== 0 : value === true;
}

function getVllmOmniCloneConfigurationWarning(cfg) {
    if (!cfg || typeof cfg !== 'object') {
        return window.t
            ? window.t('voice.vllmCloneConfigurationUnavailableWarning')
            : 'The active TTS setting could not be read. The sample will still be saved locally, but configure vLLM-Omni in API Settings before previewing or using this voice.';
    }
    const configuredProvider = String((cfg && cfg.ttsModelProvider) || '').trim().toLowerCase();
    const providerLabel = getConfiguredTtsProviderLabel(configuredProvider);
    const customApiEnabled = isVoiceCloneConfigFlagEnabled(cfg.enableCustomApi);

    if (isVoiceCloneConfigFlagEnabled(cfg.disableTts)) {
        return window.t
            ? window.t('voice.vllmCloneTtsDisabledWarning', { provider: providerLabel })
            : `The current TTS setting is "${providerLabel}", but TTS is globally disabled. The sample will still be saved locally; enable TTS and configure vLLM-Omni before previewing or using this voice.`;
    }
    if (!customApiEnabled) {
        return window.t
            ? window.t('voice.vllmCloneCustomApiDisabledWarning', { provider: providerLabel })
            : `The current TTS setting is "${providerLabel}" and Custom API is disabled. The sample will still be saved locally, but enable Custom API and switch TTS to vLLM-Omni before previewing or using this voice.`;
    }
    if (configuredProvider !== 'vllm_omni') {
        return window.t
            ? window.t('voice.vllmCloneInactiveProviderWarning', { provider: providerLabel })
            : `The current TTS setting is "${providerLabel}", not vLLM-Omni. The sample will still be saved locally, but switch TTS to vLLM-Omni before previewing or using this voice.`;
    }
    return '';
}

function appendVllmOmniCloneConfigurationWarning(resultDiv, warning) {
    if (!resultDiv || !warning) return;
    const warningEl = document.createElement('span');
    warningEl.className = 'vllm-omni-clone-configuration-warning';
    warningEl.style.color = 'var(--warning-color, #a66d00)';
    warningEl.style.fontSize = '0.9em';
    warningEl.textContent = warning;
    resultDiv.appendChild(document.createElement('br'));
    resultDiv.appendChild(warningEl);
}

function hasVoiceCloneProviderApi(provider) {
    if (provider === 'vllm_omni') return true;
    if (voiceCloneApiConfigState.isLocalTts) return true;
    return cfgHasCloneProviderKey(voiceCloneApiConfigState.cfg, provider);
}

async function checkVoiceCloneMainlandChinaUser() {
    let data = null;
    try {
        data = await fetchVoiceCloneLoaderJson('/api/config/steam_language', {
            method: 'GET',
            headers: { 'Content-Type': 'application/json' }
        });
    } catch (error) {
        console.warn('声音克隆地区检测失败，使用受限服务商策略:', error);
        return true;
    }

    if (data && data.is_mainland_china === true) {
        return true;
    }

    const ipCountry = String((data && data.ip_country) || '').trim().toUpperCase();
    if (data && data.success === true && ipCountry && ipCountry !== 'CN') {
        return false;
    }

    return true;
}

async function loadVoiceCloneProviderRestrictionState() {
    if (voiceCloneProviderRestrictionState.loaded) {
        return voiceCloneProviderRestrictionState;
    }
    if (voiceCloneProviderRestrictionState.loadingPromise) {
        return voiceCloneProviderRestrictionState.loadingPromise;
    }

    voiceCloneProviderRestrictionState.loadingPromise = (async () => {
        const [isMainlandChinaUser, providersData] = await Promise.all([
            checkVoiceCloneMainlandChinaUser(),
            fetchVoiceCloneLoaderJson('/api/config/api_providers').catch(() => null)
        ]);
        let apiKeyRegistry = {};
        const ttsProviders = {};
        if (providersData && providersData.success) {
            apiKeyRegistry = providersData.api_key_registry || {};
            if (Array.isArray(providersData.tts_providers)) {
                providersData.tts_providers.forEach(meta => {
                    if (!meta || !meta.key) return;
                    ttsProviders[meta.key] = meta;
                    (meta.aliases || []).forEach(alias => {
                        ttsProviders[alias] = meta;
                    });
                });
            }
        }
        voiceCloneProviderRestrictionState.isMainlandChinaUser = !!isMainlandChinaUser;
        voiceCloneProviderRestrictionState.apiKeyRegistry = apiKeyRegistry;
        voiceCloneProviderRestrictionState.ttsProviders = ttsProviders;
        voiceCloneProviderRestrictionState.loaded = true;
        return voiceCloneProviderRestrictionState;
    })().finally(() => {
        voiceCloneProviderRestrictionState.loadingPromise = null;
    });

    return voiceCloneProviderRestrictionState.loadingPromise;
}

async function ensureVoiceCloneProviderRestrictionsLoaded() {
    try {
        await loadVoiceCloneProviderRestrictionState();
    } catch (error) {
        console.warn('声音克隆服务商地区配置加载失败，使用默认显示策略:', error);
    }
    return voiceCloneProviderRestrictionState;
}

function isVoiceCloneProviderRestricted(provider) {
    if (!voiceCloneProviderRestrictionState.isMainlandChinaUser) return false;
    const registryKey = getVoiceCloneProviderRegistryKey(provider);
    const entry = voiceCloneProviderRestrictionState.apiKeyRegistry[registryKey];
    if (entry && Object.prototype.hasOwnProperty.call(entry, 'restricted')) {
        return entry.restricted === true;
    }
    return VOICE_CLONE_RESTRICTED_REGISTRY_KEYS.has(registryKey);
}

function getFirstAvailableVoiceCloneProviderValue(providerSelect) {
    if (!providerSelect) return '';
    const options = Array.from(providerSelect.options || []);
    const availableOption = options.find(option => !option.disabled && !option.hidden && option.style.display !== 'none');
    return availableOption ? availableOption.value : '';
}

function applyVoiceCloneProviderRestrictions(providerSelect) {
    if (!providerSelect) return false;
    const previousValue = providerSelect.value;
    Array.from(providerSelect.options || []).forEach(option => {
        const restricted = isVoiceCloneProviderRestricted(option.value);
        option.disabled = restricted;
        option.hidden = restricted;
        option.style.display = restricted ? 'none' : '';
    });

    const selectedOption = providerSelect.options[providerSelect.selectedIndex];
    if (selectedOption && !selectedOption.disabled && !selectedOption.hidden && selectedOption.style.display !== 'none') {
        syncVoiceCloneSelectDropdowns(providerSelect, { rebuild: true });
        return false;
    }

    const fallbackValue = getFirstAvailableVoiceCloneProviderValue(providerSelect);
    if (fallbackValue) {
        providerSelect.value = fallbackValue;
    }
    syncVoiceCloneSelectDropdowns(providerSelect, { rebuild: true });
    return providerSelect.value !== previousValue;
}

let voiceCloneDropdownHandlersBound = false;

function getVoiceCloneDropdownPlaceholder(select) {
    const fallbackText = window.t ? window.t('api.providerSelectPlaceholder') : '请选择服务商';
    if (!select) return fallbackText;

    const label = select.id ? document.querySelector(`label[for="${select.id}"]`) : null;
    const labelText = label ? label.textContent?.trim() : '';
    return labelText || fallbackText;
}

function closeVoiceCloneSelectDropdown(wrapper) {
    if (!wrapper) return;

    wrapper.classList.remove('open');

    const trigger = wrapper.querySelector('.api-provider-dropdown-trigger');
    if (trigger) {
        trigger.setAttribute('aria-expanded', 'false');
    }
}

function closeAllVoiceCloneSelectDropdowns(exceptWrapper = null) {
    document.querySelectorAll('.api-provider-dropdown.open').forEach(wrapper => {
        if (wrapper !== exceptWrapper) {
            closeVoiceCloneSelectDropdown(wrapper);
        }
    });
}

function openVoiceCloneSelectDropdown(wrapper) {
    if (!wrapper || wrapper.classList.contains('disabled')) return;

    closeAllVoiceCloneSelectDropdowns(wrapper);
    wrapper.classList.add('open');

    const trigger = wrapper.querySelector('.api-provider-dropdown-trigger');
    if (trigger) {
        trigger.setAttribute('aria-expanded', 'true');
    }
}

function isVoiceCloneDropdownOptionVisible(option) {
    return !!option && !option.hidden && option.style.display !== 'none';
}

function buildVoiceCloneSelectDropdownMenu(select) {
    if (!select) return;

    const wrapper = select.closest('.api-provider-dropdown');
    const menu = wrapper ? wrapper.querySelector('.api-provider-dropdown-menu') : null;
    const menuScroll = menu ? menu.querySelector('.api-provider-dropdown-menu-scroll') : null;
    if (!wrapper || !menu || !menuScroll) return;

    menuScroll.innerHTML = '';

    const options = Array.from(select.options).filter(isVoiceCloneDropdownOptionVisible);
    if (options.length === 0) {
        const emptyState = document.createElement('div');
        emptyState.className = 'api-provider-dropdown-empty';
        emptyState.textContent = window.t ? window.t('api.noOptionsAvailable') : '暂无可选项';
        menuScroll.appendChild(emptyState);
        return;
    }

    options.forEach(option => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'api-provider-dropdown-option';
        item.setAttribute('role', 'option');
        item.dataset.value = option.value;
        item.textContent = option.textContent;

        if (option.disabled) {
            item.disabled = true;
            item.setAttribute('aria-disabled', 'true');
        }

        item.addEventListener('click', event => {
            event.preventDefault();

            if (option.disabled || select.disabled || !isVoiceCloneDropdownOptionVisible(option)) {
                return;
            }

            select.value = option.value;
            syncVoiceCloneSelectDropdowns(select);
            select.dispatchEvent(new Event('change', { bubbles: true }));
            closeVoiceCloneSelectDropdown(wrapper);
        });

        menuScroll.appendChild(item);
    });
}

function syncVoiceCloneSelectDropdowns(targetSelect = null, { rebuild = false } = {}) {
    const selects = targetSelect
        ? [targetSelect]
        : Array.from(document.querySelectorAll('.api-provider-select[data-dropdown-enhanced="true"]'));

    selects.forEach(select => {
        if (!select) return;

        const wrapper = select.closest('.api-provider-dropdown');
        const trigger = wrapper ? wrapper.querySelector('.api-provider-dropdown-trigger') : null;
        const current = wrapper ? wrapper.querySelector('.api-provider-dropdown-current') : null;
        const menu = wrapper ? wrapper.querySelector('.api-provider-dropdown-menu') : null;

        if (!wrapper || !trigger || !current || !menu) return;

        if (rebuild) {
            buildVoiceCloneSelectDropdownMenu(select);
        }

        const selectedOption = select.options[select.selectedIndex] || null;
        const placeholder = getVoiceCloneDropdownPlaceholder(select);

        current.textContent = selectedOption && isVoiceCloneDropdownOptionVisible(selectedOption)
            ? selectedOption.textContent
            : placeholder;
        current.classList.toggle('placeholder', !selectedOption || !isVoiceCloneDropdownOptionVisible(selectedOption));

        trigger.disabled = !!select.disabled;
        wrapper.classList.toggle('disabled', !!select.disabled);

        menu.querySelectorAll('.api-provider-dropdown-option').forEach(item => {
            const isSelected = item.dataset.value === select.value;
            item.classList.toggle('selected', isSelected);
            item.setAttribute('aria-selected', isSelected ? 'true' : 'false');
        });

        if (select.disabled) {
            closeVoiceCloneSelectDropdown(wrapper);
        }
    });
}

function bindVoiceCloneDropdownGlobalHandlers() {
    if (voiceCloneDropdownHandlersBound) return;

    document.addEventListener('click', event => {
        if (!event.target.closest('.api-provider-dropdown')) {
            closeAllVoiceCloneSelectDropdowns();
        }
    });

    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
            closeAllVoiceCloneSelectDropdowns();
        }
    });

    window.addEventListener('resize', () => closeAllVoiceCloneSelectDropdowns());

    voiceCloneDropdownHandlersBound = true;
}

function initVoiceCloneSelectDropdown(select) {
    if (!select || select.dataset.dropdownEnhanced === 'true' || !select.parentNode) return;

    bindVoiceCloneDropdownGlobalHandlers();

    const wrapper = document.createElement('div');
    wrapper.className = 'api-provider-dropdown';

    select.parentNode.insertBefore(wrapper, select);
    wrapper.appendChild(select);

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'api-provider-dropdown-trigger';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-label', getVoiceCloneDropdownPlaceholder(select));
    trigger.innerHTML = '<span class="api-provider-dropdown-current"></span><span class="api-provider-dropdown-arrow" aria-hidden="true"></span>';

    const menu = document.createElement('div');
    menu.className = 'api-provider-dropdown-menu';
    menu.setAttribute('role', 'listbox');

    const menuScroll = document.createElement('div');
    menuScroll.className = 'api-provider-dropdown-menu-scroll';

    if (select.id) {
        menu.id = `${select.id}-menu`;
        trigger.id = `${select.id}-dropdown-trigger`;
        trigger.setAttribute('aria-controls', menu.id);
    }

    menu.appendChild(menuScroll);
    wrapper.appendChild(trigger);
    wrapper.appendChild(menu);

    select.classList.add('is-enhanced');
    select.dataset.dropdownEnhanced = 'true';

    trigger.addEventListener('click', event => {
        event.preventDefault();

        if (wrapper.classList.contains('open')) {
            closeVoiceCloneSelectDropdown(wrapper);
        } else {
            openVoiceCloneSelectDropdown(wrapper);
        }
    });

    select.addEventListener('change', () => syncVoiceCloneSelectDropdowns(select));

    const observer = new MutationObserver(() => {
        syncVoiceCloneSelectDropdowns(select, { rebuild: true });
    });

    observer.observe(select, {
        childList: true,
        subtree: true,
        characterData: true,
        attributes: true,
        attributeFilter: ['disabled', 'hidden', 'style', 'label', 'value', 'selected']
    });

    syncVoiceCloneSelectDropdowns(select, { rebuild: true });
}

function initVoiceCloneSelectDropdowns() {
    document.querySelectorAll('.api-provider-select').forEach(initVoiceCloneSelectDropdown);
    syncVoiceCloneSelectDropdowns(null, { rebuild: true });
}

async function initVoiceCloneProviderRestrictions() {
    await ensureVoiceCloneProviderRestrictionsLoaded();
    const providerSelect = document.getElementById('voiceProvider');
    const changed = applyVoiceCloneProviderRestrictions(providerSelect);
    if (changed && providerSelect) {
        suppressProviderTouchedTracking = true;
        providerSelect.dispatchEvent(new Event('change'));
        suppressProviderTouchedTracking = false;
    }
    if (providerSelect && typeof updateVoiceSourceForProvider === 'function') {
        updateVoiceSourceForProvider(providerSelect.value);
    }
    return voiceCloneProviderRestrictionState;
}

function getPreferredCloneProviderFromConfig(cfg) {
    if (!cfg || typeof cfg !== 'object') return '';
    for (const [provider] of VOICE_CLONE_PROVIDER_KEY_FIELDS) {
        if (cfgHasCloneProviderKey(cfg, provider) && !isVoiceCloneProviderRestricted(provider)) {
            return provider;
        }
    }
    return '';
}

function hasUsableCloneApiFromConfig(cfg, isLocalTts) {
    if (isLocalTts) return true;
    if (!cfg || typeof cfg !== 'object') return false;
    // vLLM-Omni 是本地服务，无需 API key；只要配置了 ttsModelUrl 即可克隆。
    const vllmUrl = (cfg.ttsModelUrl || cfg.TTS_MODEL_URL || '').trim();
    if (vllmUrl) return true;
    return VOICE_CLONE_PROVIDER_KEY_FIELDS.some(([provider]) => (
        cfgHasCloneProviderKey(cfg, provider) && !isVoiceCloneProviderRestricted(provider)
    ));
}

function updateVoiceCloneProviderNoticeText(noticeDiv, provider) {
    const span = noticeDiv ? noticeDiv.querySelector('span') : null;
    if (!span) return;

    const keyMap = {
        'cosyvoice_intl': 'voice.alibabaIntlApiRequired',
        'minimax': 'voice.minimaxApiRequired',
        'minimax_intl': 'voice.minimaxIntlApiRequired',
        'elevenlabs': 'voice.elevenlabsApiRequired',
        'mimo': 'voice.mimoApiRequired',
        'vllm_omni': 'voice.vllmOmniNotice',
        'doubao_tts': 'voice.doubaoTtsApiRequired',
    };
    const fallbackMap = {
        'cosyvoice_intl': '请先在 API 设置中填写阿里国际版 API Key',
        'elevenlabs': '请先在 API 设置中填写 ElevenLabs API Key',
        'mimo': '请先在 API 设置中填写 MiMo API Key',
        'vllm_omni': '本地 vLLM-Omni 服务，无需 API Key',
        'doubao_tts': 'Please save a Doubao Speech (Volcengine) API Key in the API Key Book.',
    };
    const i18nKey = keyMap[provider] || 'voice.alibabaApiRequired';
    span.setAttribute('data-i18n', i18nKey);
    if (window.t) {
        const translated = window.t(i18nKey);
        span.textContent = (translated && translated !== i18nKey) ? translated : (fallbackMap[provider] || translated);
    } else if (fallbackMap[provider]) {
        span.textContent = fallbackMap[provider];
    }
    // 若 window.t 不可用，保留 HTML 中的原始文本，不覆盖
}

async function refreshVoiceCloneProviderNotice(providerSelect, noticeDiv) {
    if (!providerSelect || !noticeDiv) return;
    updateVoiceCloneProviderNoticeText(noticeDiv, providerSelect.value);
    noticeDiv.style.display = 'none';
    await ensureVoiceCloneApiConfigState();
    const provider = providerSelect.value || 'cosyvoice';
    updateVoiceCloneProviderNoticeText(noticeDiv, provider);
    noticeDiv.style.display = hasVoiceCloneProviderApi(provider) ? 'none' : '';
}

function sanitizeMiniMaxPrefix(prefix) {
    return String(prefix || '')
        .replace(/[^0-9a-z]/gi, '')
        .slice(0, MINIMAX_PREFIX_MAX_LENGTH);
}

function normalizePrefixInputForProvider() {
    const prefixInput = document.getElementById('prefix');
    const provider = (document.getElementById('voiceProvider') || {}).value || 'cosyvoice';
    if (!prefixInput) {
        return '';
    }

    updatePrefixFieldForProvider(provider);
    if (isDoubaoTtsProvider(provider)) {
        return prefixInput.value.trim();
    }

    if (!isMiniMaxProvider(provider)) {
        prefixInput.removeAttribute('maxlength');
        return prefixInput.value.trim();
    }

    prefixInput.maxLength = MINIMAX_PREFIX_MAX_LENGTH;
    const trimmedValue = prefixInput.value.trim();
    const sanitized = sanitizeMiniMaxPrefix(trimmedValue);
    if (trimmedValue !== sanitized || prefixInput.value !== sanitized) {
        prefixInput.value = sanitized;
    }
    return sanitized;
}

function setI18nText(element, key, fallback) {
    if (!element) return;
    element.setAttribute('data-i18n', key);
    element.textContent = voiceCloneI18n(key, fallback);
}

function setI18nPlaceholder(element, key, fallback) {
    if (!element) return;
    element.setAttribute('data-i18n-placeholder', key);
    element.placeholder = voiceCloneI18n(key, fallback);
}

function updatePrefixFieldForProvider(provider) {
    const prefixInput = document.getElementById('prefix');
    if (!prefixInput) return;
    const row = prefixInput.closest('.field-row');
    const registerText = document.querySelector('.register-voice-btn .register-text');
    const prefixLabel = document.getElementById('prefixLabel')
        || (row ? row.querySelector('label[data-i18n="voice.customPrefix"], label[data-i18n="voice.doubaoSpeakerIdLabel"]') : null);
    let prefixHint = document.getElementById('prefixHint');
    if (!prefixHint && row) {
        prefixHint = document.createElement('label');
        prefixHint.id = 'prefixHint';
        prefixHint.className = 'hint';
        row.insertBefore(prefixHint, prefixInput);
    }

    if (isDoubaoTtsProvider(provider)) {
        prefixInput.removeAttribute('maxlength');
        setI18nText(prefixLabel, 'voice.doubaoSpeakerIdLabel', 'Doubao Speaker ID (overwrite existing S_ voice, required)');
        setI18nText(prefixHint, 'voice.doubaoSpeakerIdNote', 'Clone the voice in the Doubao Speech console first and copy its S_ Speaker ID here; NEKO will overwrite that existing voice and will not create a new Doubao voice ID, e.g. S_xeC2CDp72');
        setI18nPlaceholder(prefixInput, 'voice.doubaoSpeakerIdPlaceholder', 'e.g. S_xeC2CDp72');
        return;
    }

    setI18nText(prefixLabel, 'voice.customPrefix', '自定义前缀（必填，用于区分音色）');
    setI18nText(prefixHint, 'voice.customPrefixNote', '不超过10个字符，只支持数字和英文字母');
    setI18nPlaceholder(prefixInput, 'voice.voiceIdPlaceholder', '不超过10个字符，只支持数字和英文字母');
    setI18nText(registerText, 'voice.register', 'Register Voice');
}

function guessAudioMimeType(filename) {
    return /\.mp3$/i.test(filename || '') ? 'audio/mpeg' : 'audio/wav';
}

function getEffectiveAudioFile() {
    const fileInput = document.getElementById('audioFile');
    if (fileInput && fileInput.files && fileInput.files.length) {
        return fileInput.files[0];
    }
    return workshopReferenceFile;
}

function setWorkshopVoiceSourceStatus(message, isError = false) {
    const statusEl = document.getElementById('workshopVoiceSourceStatus');
    if (!statusEl) return;
    statusEl.textContent = message || '';
    statusEl.style.display = message ? 'block' : 'none';
    statusEl.classList.toggle('error', !!message && isError);
}

function revokeWorkshopReferenceAudioUrl() {
    if (workshopReferenceAudioUrl) {
        URL.revokeObjectURL(workshopReferenceAudioUrl);
        workshopReferenceAudioUrl = '';
    }
}

function applyWorkshopProviderHint(providerHint) {
    const providerSelect = document.getElementById('voiceProvider');
    if (!providerSelect || !providerHint) return;
    if (providerTouchedByUser) return;
    if (isVoiceCloneProviderRestricted(providerHint)) return;
    if (providerSelect.value !== 'cosyvoice') return;

    suppressProviderTouchedTracking = true;
    providerSelect.value = providerHint;
    providerSelect.dispatchEvent(new Event('change'));
    suppressProviderTouchedTracking = false;
}

window.addEventListener('beforeunload', revokeWorkshopReferenceAudioUrl);

// 关闭页面函数
function closeVoiceClonePage() {
    if (window.opener) {
        // 如果是通过 window.open() 打开的，直接关闭
        window.close();
    } else if (window.parent && window.parent !== window) {
        // 如果在 iframe 中，通知父窗口关闭
        window.parent.postMessage({ type: 'close_voice_clone' }, window.location.origin);
    } else {
        // 否则尝试关闭窗口
        // 注意：如果是用户直接访问的页面，浏览器可能不允许关闭
        // 在这种情况下，可以尝试返回上一页或显示提示
        if (window.history.length > 1) {
            window.history.back();
        } else {
            window.close();
            // 如果 window.close() 失败（页面仍然存在），可以显示提示
            setTimeout(() => {
                if (!window.closed) {
                    // 窗口未能关闭，返回主页
                    window.location.href = '/';
                }
            }, 100);
        }
    }
}

// 更新文件选择显示
function updateFileDisplay() {
    const fileInput = document.getElementById('audioFile');
    const fileNameDisplay = document.getElementById('fileNameDisplay');
    if (!fileInput || !fileNameDisplay) {
        return; // 如果元素不存在，直接返回
    }
    if (fileInput.files.length > 0) {
        fileNameDisplay.textContent = fileInput.files[0].name;
    } else if (workshopReferenceFile) {
        const workshopPreloadedSuffix = (window.t && typeof window.t === 'function')
            ? window.t('voice.workshopPreloaded')
            : '（创意工坊预载入）';
        fileNameDisplay.textContent = `${workshopReferenceFile.name}${workshopPreloadedSuffix}`;
    } else {
        fileNameDisplay.textContent = window.t ? window.t('voice.noFileSelected') : '未选择文件';
    }
}

// 监听文件选择变化
document.addEventListener('DOMContentLoaded', () => {
    initVoiceCloneSelectDropdowns();
    initVoiceCloneProviderRestrictions().catch(error => {
        console.warn('初始化声音克隆服务商地区过滤失败:', error);
    });
    const audioFile = document.getElementById('audioFile');
    if (audioFile) {
        audioFile.addEventListener('change', updateFileDisplay);
    } else {
        console.error('未找到 audioFile 元素');
    }
});

// 更新文件选择按钮的 data-text 属性（用于文字描边效果）
function updateFileButtonText() {
    const fileText = document.querySelector('.file-text');
    if (fileText) {
        const text = fileText.textContent || fileText.innerText;
        fileText.setAttribute('data-text', text);
    }
}

// 更新注册音色按钮的 data-text 属性（用于文字描边效果）
function updateRegisterButtonText() {
    const registerText = document.querySelector('.register-text');
    if (registerText) {
        const text = registerText.textContent || registerText.innerText;
        registerText.setAttribute('data-text', text);
    }
}

// 监听 i18n 更新事件，同步更新 data-text
if (window.i18n) {
    window.i18n.on('languageChanged', function () {
        updateFileButtonText();
        updateRegisterButtonText();
    });
    // 监听所有翻译更新
    const originalChangeLanguage = window.i18n.changeLanguage;
    if (originalChangeLanguage) {
        window.i18n.changeLanguage = function (...args) {
            const result = originalChangeLanguage.apply(this, args);
            if (result && typeof result.then === 'function') {
                result.then(() => {
                    setTimeout(() => {
                        updateFileButtonText();
                        updateRegisterButtonText();
                    }, 100);
                });
            } else {
                setTimeout(() => {
                    updateFileButtonText();
                    updateRegisterButtonText();
                }, 100);
            }
            return result;
        };
    }
}

// 使用 MutationObserver 监听文字内容变化
const fileTextObserver = new MutationObserver(function (mutations) {
    mutations.forEach(function (mutation) {
        if (mutation.type === 'childList' || mutation.type === 'characterData') {
            updateFileButtonText();
        }
    });
});

const registerTextObserver = new MutationObserver(function (mutations) {
    mutations.forEach(function (mutation) {
        if (mutation.type === 'childList' || mutation.type === 'characterData') {
            updateRegisterButtonText();
        }
    });
});

// 页面加载时更新文件选择显示
// 如果 i18next 已经初始化完成，立即更新
if (window.i18n && window.i18n.isInitialized) {
    updateFileDisplay();
    updateFileButtonText();
    updateRegisterButtonText();
    const fileText = document.querySelector('.file-text');
    if (fileText) {
        fileTextObserver.observe(fileText, {
            childList: true,
            characterData: true,
            subtree: true
        });
    }
    const registerText = document.querySelector('.register-text');
    if (registerText) {
        registerTextObserver.observe(registerText, {
            childList: true,
            characterData: true,
            subtree: true
        });
    }
} else {
    // 延迟更新，等待 i18next 初始化
    setTimeout(() => {
        updateFileDisplay();
        updateFileButtonText();
        updateRegisterButtonText();
        const fileText = document.querySelector('.file-text');
        if (fileText) {
            fileTextObserver.observe(fileText, {
                childList: true,
                characterData: true,
                subtree: true
            });
        }
        const registerText = document.querySelector('.register-text');
        if (registerText) {
            registerTextObserver.observe(registerText, {
                childList: true,
                characterData: true,
                subtree: true
            });
        }
    }, 500);
}

// 页面加载时获取 lanlan_name
(async function initLanlanName() {
    // Electron白屏修复
    if (document.body) {
        void document.body.offsetHeight;
        const currentOpacity = document.body.style.opacity || '1';
        document.body.style.opacity = '0.99';
        requestAnimationFrame(() => {
            document.body.style.opacity = currentOpacity;
        });
    }

    const lanlanInput = document.getElementById('lanlan_name');

    try {
        // 优先从 URL 获取 lanlan_name
        const urlParams = new URLSearchParams(window.location.search);
        let lanlanName = urlParams.get('lanlan_name') || "";

        // 如果 URL 中没有，从 API 获取
        if (!lanlanName) {
            const data = await fetchVoiceCloneLoaderJson('/api/config/page_config');
            if (data.success) {
                lanlanName = data.lanlan_name || "";
            }
        }

        // 设置到隐藏字段
        if (lanlanInput) {
            lanlanInput.value = lanlanName;
        }
        // lanlan_name 就绪后再刷新音色列表，确保当前音色选中态正确
        if (typeof loadVoices === 'function') {
            try { await loadVoices(); } catch (_) { /* 静默忽略二次加载错误 */ }
        }
    } catch (error) {
        console.error('获取 lanlan_name 失败:', error);
        if (lanlanInput) {
            lanlanInput.value = "";
        }
    }

    await initVoiceCloneProviderRestrictions();

    const apiConfigState = await ensureVoiceCloneApiConfigState({ force: true });
    const cfg = apiConfigState.cfg;
    if (cfg) {
        const hasCloneApi = hasUsableCloneApiFromConfig(cfg, apiConfigState.isLocalTts);
        const preferredProvider = getPreferredCloneProviderFromConfig(cfg);
        const providerSelect = document.getElementById('voiceProvider');
        if (!providerTouchedByUser && preferredProvider && providerSelect && providerSelect.value === 'cosyvoice') {
            suppressProviderTouchedTracking = true;
            providerSelect.value = preferredProvider;
            providerSelect.dispatchEvent(new Event('change'));
            suppressProviderTouchedTracking = false;
        }
        if (!hasCloneApi) {
            const modal = document.getElementById('noApiModal');
            if (modal) modal.style.display = 'flex';
        }
        await refreshVoiceCloneProviderNotice(
            document.getElementById('voiceProvider'),
            document.getElementById('provider-notice')
        );
    }

    await initWorkshopVoiceReference();
})();

// 服务商切换时更新提示横幅
document.addEventListener('DOMContentLoaded', function initProviderSwitch() {
    const providerSelect = document.getElementById('voiceProvider');
    const noticeDiv = document.getElementById('provider-notice');
    const prefixInput = document.getElementById('prefix');
    if (!providerSelect || !noticeDiv) return;

    noticeDiv.setAttribute('role', 'button');
    noticeDiv.setAttribute('tabindex', '0');
    noticeDiv.style.cursor = 'pointer';
    noticeDiv.addEventListener('click', openApiSettingsKeyBook);
    noticeDiv.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            openApiSettingsKeyBook();
        }
    });

    providerSelect.addEventListener('change', () => {
        if (!suppressProviderTouchedTracking) {
            providerTouchedByUser = true;
        }
        refreshVoiceCloneProviderNotice(providerSelect, noticeDiv);
        normalizePrefixInputForProvider();
        updateCloneMethodForProvider(providerSelect.value);
        updateRefTextRowForProvider(providerSelect.value);
        updateVoiceSourceForProvider(providerSelect.value);
    });
    if (prefixInput) {
        prefixInput.addEventListener('input', () => {
            normalizePrefixInputForProvider();
        });
    }
    refreshVoiceCloneProviderNotice(providerSelect, noticeDiv);
    normalizePrefixInputForProvider();
    updateCloneMethodForProvider(providerSelect.value);
    updateRefTextRowForProvider(providerSelect.value);
    updateVoiceSourceForProvider(providerSelect.value);
});

// 当前克隆方式
let currentCloneMethod = 'file';
let currentVoiceSource = 'clone';

function isVoiceDesignSupportedProvider(provider) {
    const meta = voiceCloneProviderRestrictionState.ttsProviders[provider];
    return !!(meta && Array.isArray(meta.capabilities) && meta.capabilities.includes('design'));
}

function getVoiceDesignMetadata(provider) {
    const meta = voiceCloneProviderRestrictionState.ttsProviders[provider];
    return meta && meta.voice_design ? meta.voice_design : {};
}

function isVoiceDesignLanguageSupportedProvider(provider) {
    const languageHints = getVoiceDesignMetadata(provider).language_hints;
    return Array.isArray(languageHints) && languageHints.length > 0;
}

function isElevenLabsProvider(provider) {
    return provider === 'elevenlabs';
}

function setVoiceCloneI18nText(element, key, fallback, options = null) {
    if (!element) return;
    element.setAttribute('data-i18n', key);
    if (options) {
        element.setAttribute('data-i18n-options', JSON.stringify(options));
    } else {
        element.removeAttribute('data-i18n-options');
    }
    const text = window.t ? window.t(key, options || undefined) : fallback;
    element.textContent = text && text !== key ? text : fallback;
}

function updateRefLanguageForVoiceSource(provider) {
    const row = document.getElementById('refLanguageRow');
    const label = document.getElementById('refLanguageLabel');
    const hint = document.getElementById('refLanguageHint');
    const select = document.getElementById('refLanguage');
    if (!row || !select) return;

    const designLanguageMode = currentVoiceSource === 'design' && isVoiceDesignLanguageSupportedProvider(provider);
    if (currentVoiceSource === 'design' && !designLanguageMode) {
        row.style.display = 'none';
        return;
    }

    row.style.display = '';
    if (designLanguageMode) {
        const languageHints = getVoiceDesignMetadata(provider).language_hints;
        setVoiceCloneI18nText(label, 'voice.designLanguage', 'Voice language hint');
        setVoiceCloneI18nText(hint, 'voice.designLanguageNote', 'Only Chinese and English hints are supported for CosyVoice Voice Design.');
        Array.from(select.options).forEach(option => {
            const allowed = languageHints.includes(option.value);
            option.hidden = !allowed;
            option.disabled = !allowed;
        });
        if (!languageHints.includes(select.value)) {
            select.value = languageHints[0];
            select.dispatchEvent(new Event('change', { bubbles: true }));
        }
        if (typeof syncVoiceCloneSelectDropdowns === 'function') {
            syncVoiceCloneSelectDropdowns(select, { rebuild: true });
        }
    } else {
        setVoiceCloneI18nText(label, 'voice.refLanguage', 'Reference audio language');
        setVoiceCloneI18nText(hint, 'voice.refLanguageNote', 'Select the language spoken in the uploaded reference audio.');
        Array.from(select.options).forEach(option => {
            option.hidden = false;
            option.disabled = false;
        });
        if (typeof syncVoiceCloneSelectDropdowns === 'function') {
            syncVoiceCloneSelectDropdowns(select, { rebuild: true });
        }
    }
}

function updateVoiceDesignHint(provider) {
    const hint = document.getElementById('voiceDesignHint');
    if (!hint) return;

    if (isElevenLabsProvider(provider)) {
        const constraints = getVoiceDesignMetadata(provider);
        const promptMin = Number(constraints.prompt_min);
        const promptMax = Number(constraints.prompt_max);
        if (Number.isFinite(promptMin) && promptMin > 0 && Number.isFinite(promptMax) && promptMax >= promptMin) {
            setVoiceCloneI18nText(
                hint,
                'voice.designHintElevenlabs',
                `Describe only the voice, not the character personality. ElevenLabs requires ${promptMin}-${promptMax} characters.`,
                { min: promptMin, max: promptMax }
            );
            return;
        }
    }

    setVoiceCloneI18nText(
        hint,
        'voice.designHint',
        'Describe only the voice, not the character personality. Previews use the same template as Voice Cloning.'
    );
}

// MiMo 只支持本地文件克隆：它把参考样本存在本地、不走 /voice_clone_direct（后端
// valid_providers 不含 mimo，直链会直接 TTS_PROVIDER_INVALID）。选中 MiMo 时禁用直链方式。
function isDirectLinkUnsupportedProvider(provider) {
    return provider === 'mimo' || provider === 'vllm_omni' || provider === 'doubao_tts';
}

function updateCloneMethodForProvider(provider) {
    const btnDirectLinkClone = document.getElementById('btnDirectLinkClone');
    const disabled = isDirectLinkUnsupportedProvider(provider);
    if (btnDirectLinkClone) {
        btnDirectLinkClone.disabled = disabled;
        btnDirectLinkClone.classList.toggle('disabled', disabled);
        btnDirectLinkClone.setAttribute('aria-disabled', disabled ? 'true' : 'false');
        btnDirectLinkClone.title = disabled
            ? (window.t ? window.t('voice.directLinkUnsupported') : '当前服务商暂不支持直链克隆，请上传本地文件')
            : '';
    }
    // 当前在直链方式但切到了不支持直链的 provider → 强制回退到本地文件
    if (disabled && currentCloneMethod === 'directlink') {
        switchCloneMethod('file');
    }
}

// vLLM-Omni 克隆需要参考音频原文（ref_text），其它 provider 不需要。
// 选中 vllm_omni 时显示 ref_text 输入区，其它 provider 时隐藏。
function updateRegisterButtonForVoiceSource() {
    const registerText = document.querySelector('.register-text');
    if (!registerText) return;
    const i18nKey = currentVoiceSource === 'design' ? 'voice.generateVoice' : 'voice.register';
    const fallback = currentVoiceSource === 'design' ? 'Generate Voice' : 'Register Voice';
    const text = window.t ? window.t(i18nKey) : fallback;
    registerText.setAttribute('data-i18n', i18nKey);
    registerText.textContent = text && text !== i18nKey ? text : fallback;
    registerText.setAttribute('data-text', registerText.textContent);
}

function switchVoiceSource(source) {
    const provider = (document.getElementById('voiceProvider') || {}).value || 'cosyvoice';
    if (source === 'design' && !isVoiceDesignSupportedProvider(provider)) {
        source = 'clone';
    }
    currentVoiceSource = source === 'design' ? 'design' : 'clone';

    const btnClone = document.getElementById('btnVoiceSourceClone');
    const btnDesign = document.getElementById('btnVoiceSourceDesign');
    const cloneMethodRow = document.getElementById('cloneMethodRow');
    const fileCloneSection = document.getElementById('fileCloneSection');
    const directLinkCloneSection = document.getElementById('directLinkCloneSection');
    const voiceDesignSection = document.getElementById('voiceDesignSection');

    if (btnClone) {
        btnClone.classList.toggle('active', currentVoiceSource === 'clone');
        btnClone.setAttribute('aria-selected', currentVoiceSource === 'clone' ? 'true' : 'false');
        btnClone.setAttribute('tabindex', currentVoiceSource === 'clone' ? '0' : '-1');
    }
    if (btnDesign) {
        btnDesign.classList.toggle('active', currentVoiceSource === 'design');
        btnDesign.setAttribute('aria-selected', currentVoiceSource === 'design' ? 'true' : 'false');
        btnDesign.setAttribute('tabindex', currentVoiceSource === 'design' ? '0' : '-1');
    }

    if (currentVoiceSource === 'design') {
        if (cloneMethodRow) cloneMethodRow.style.display = 'none';
        if (fileCloneSection) fileCloneSection.style.display = 'none';
        if (directLinkCloneSection) directLinkCloneSection.style.display = 'none';
        if (voiceDesignSection) voiceDesignSection.style.display = 'block';
    } else {
        if (cloneMethodRow) cloneMethodRow.style.display = '';
        if (voiceDesignSection) voiceDesignSection.style.display = 'none';
        switchCloneMethod(currentCloneMethod);
    }
    updateRegisterButtonForVoiceSource();
    updateRefLanguageForVoiceSource(provider);
    updateVoiceDesignHint(provider);
}

function updateVoiceSourceForProvider(provider) {
    const sourceRow = document.getElementById('voiceSourceRow');
    const btnDesign = document.getElementById('btnVoiceSourceDesign');
    const supported = isVoiceDesignSupportedProvider(provider);
    if (sourceRow) sourceRow.style.display = supported ? '' : 'none';
    if (btnDesign) {
        btnDesign.disabled = !supported;
        btnDesign.hidden = !supported;
        btnDesign.style.display = supported ? '' : 'none';
        btnDesign.setAttribute('aria-disabled', supported ? 'false' : 'true');
    }
    if (!supported && currentVoiceSource === 'design') {
        switchVoiceSource('clone');
    } else {
        switchVoiceSource(currentVoiceSource);
    }
}

function updateRefTextRowForProvider(provider) {
    const refTextRow = document.getElementById('vllmRefTextRow');
    if (refTextRow) {
        refTextRow.style.display = (provider === 'vllm_omni') ? '' : 'none';
    }
}

// 切换克隆方式
function switchCloneMethod(method) {
    // 防御：不支持直链的 provider（MiMo）被选中时，无视直链切换请求
    if (method === 'directlink') {
        const providerSelect = document.getElementById('voiceProvider');
        if (providerSelect && isDirectLinkUnsupportedProvider(providerSelect.value)) {
            method = 'file';
        }
    }
    currentCloneMethod = method;
    const btnFileClone = document.getElementById('btnFileClone');
    const btnDirectLinkClone = document.getElementById('btnDirectLinkClone');
    const fileCloneSection = document.getElementById('fileCloneSection');
    const directLinkCloneSection = document.getElementById('directLinkCloneSection');

    if (!btnFileClone || !btnDirectLinkClone || !fileCloneSection || !directLinkCloneSection) {
        console.warn('克隆方式切换：部分DOM元素未找到');
        return;
    }

    if (currentVoiceSource === 'design') {
        fileCloneSection.style.display = 'none';
        directLinkCloneSection.style.display = 'none';
        return;
    }

    if (method === 'file') {
        btnFileClone.classList.add('active');
        btnFileClone.setAttribute('aria-selected', 'true');
        btnFileClone.setAttribute('tabindex', '0');
        btnDirectLinkClone.classList.remove('active');
        btnDirectLinkClone.setAttribute('aria-selected', 'false');
        btnDirectLinkClone.setAttribute('tabindex', '-1');
        fileCloneSection.style.display = 'block';
        directLinkCloneSection.style.display = 'none';
    } else {
        btnFileClone.classList.remove('active');
        btnFileClone.setAttribute('aria-selected', 'false');
        btnFileClone.setAttribute('tabindex', '-1');
        btnDirectLinkClone.classList.add('active');
        btnDirectLinkClone.setAttribute('aria-selected', 'true');
        btnDirectLinkClone.setAttribute('tabindex', '0');
        fileCloneSection.style.display = 'none';
        directLinkCloneSection.style.display = 'block';
    }
}

async function initWorkshopVoiceReference() {
    const urlParams = new URLSearchParams(window.location.search);
    const workshopItemId = urlParams.get('workshop_item_id');
    const source = urlParams.get('source');
    if (!workshopItemId || source !== 'workshop') {
        return;
    }

    const sourceCard = document.getElementById('workshopVoiceSource');
    const sourceTitle = document.getElementById('workshopVoiceSourceTitle');
    const sourceMeta = document.getElementById('workshopVoiceSourceMeta');
    const previewAudio = document.getElementById('workshopVoicePreview');
    const t = (key, fallback, options) => window.t ? window.t(key, options) : fallback;
    const workshopSourceTitleText = t('voice.workshopSourceTitle', 'Workshop Reference Voice');
    if (!sourceCard || !sourceTitle || !sourceMeta || !previewAudio) {
        return;
    }

    sourceCard.style.display = 'block';
    sourceTitle.textContent = workshopSourceTitleText;
    sourceMeta.textContent = '';
    setWorkshopVoiceSourceStatus(t('voice.workshopSourceLoading', 'Loading workshop reference voice...'));

    try {
        const manifestResponse = await fetchVoiceCloneLoaderResponse(`/api/steam/workshop/voice-reference/${encodeURIComponent(workshopItemId)}`);
        const manifestData = await manifestResponse.json();
        if (!manifestResponse.ok) {
            throw new Error(manifestData.error || `HTTP ${manifestResponse.status}`);
        }
        if (!manifestData.available || !manifestData.manifest) {
            throw new Error(t('voice.workshopSourceUnavailable', 'This workshop item has no available reference voice.'));
        }

        const audioResponse = await fetchVoiceCloneLoaderResponse(`/api/steam/workshop/voice-reference/${encodeURIComponent(workshopItemId)}/audio`);
        if (!audioResponse.ok) {
            const errorData = await audioResponse.json().catch(() => ({}));
            throw new Error(errorData.error || `HTTP ${audioResponse.status}`);
        }

        const manifest = manifestData.manifest;
        const audioBlob = await audioResponse.blob();
        workshopReferenceFile = new File(
            [audioBlob],
            manifest.reference_audio,
            { type: audioBlob.type || guessAudioMimeType(manifest.reference_audio) }
        );
        revokeWorkshopReferenceAudioUrl();
        workshopReferenceAudioUrl = URL.createObjectURL(audioBlob);

        sourceTitle.textContent = manifestData.title || manifest.display_name || workshopSourceTitleText;
        sourceMeta.textContent = t('voice.workshopSourceMeta', 'Sample: {{sample}} | Prefix: {{prefix}} | Language: {{language}}', {
            sample: manifest.display_name || manifest.reference_audio,
            prefix: manifest.prefix,
            language: manifest.ref_language
        });
        previewAudio.src = workshopReferenceAudioUrl;
        previewAudio.style.display = 'block';
        setWorkshopVoiceSourceStatus(t('voice.workshopSourceReady', 'Reference voice preloaded. Submission will use the file upload clone flow.'));

        switchCloneMethod('file');
        const prefixInput = document.getElementById('prefix');
        const refLanguageSelect = document.getElementById('refLanguage');
        if (prefixInput) prefixInput.value = manifest.prefix || '';
        if (refLanguageSelect) {
            refLanguageSelect.value = manifest.ref_language || 'ch';
            refLanguageSelect.dispatchEvent(new Event('change'));
        }
        applyWorkshopProviderHint(manifest.provider_hint);
        updateFileDisplay();
    } catch (error) {
        workshopReferenceFile = null;
        revokeWorkshopReferenceAudioUrl();
        sourceTitle.textContent = workshopSourceTitleText;
        sourceMeta.textContent = '';
        previewAudio.removeAttribute('src');
        previewAudio.style.display = 'none';
        setWorkshopVoiceSourceStatus(error?.message || t('voice.workshopSourceLoadFailed', 'Failed to load workshop reference voice'), true);
        updateFileDisplay();
    }
}

function setFormDisabled(disabled) {
    const audioFile = document.getElementById('audioFile');
    const directLinkUrl = document.getElementById('directLinkUrl');
    const voiceDesignPrompt = document.getElementById('voiceDesignPrompt');
    const refLanguage = document.getElementById('refLanguage');
    const prefix = document.getElementById('prefix');
    const voiceProvider = document.getElementById('voiceProvider');
    const vllmRefText = document.getElementById('vllmRefText');
    if (audioFile) audioFile.disabled = disabled;
    if (directLinkUrl) directLinkUrl.disabled = disabled;
    if (voiceDesignPrompt) voiceDesignPrompt.disabled = disabled;
    if (refLanguage) refLanguage.disabled = disabled;
    if (prefix) prefix.disabled = disabled;
    if (voiceProvider) voiceProvider.disabled = disabled;
    if (vllmRefText) vllmRefText.disabled = disabled;
    // 禁用所有按钮
    const buttons = document.querySelectorAll('button');
    if (buttons && buttons.length > 0) {
        buttons.forEach(btn => {
            if (btn) btn.disabled = disabled;
        });
    }
    // 重新启用时恢复「按 provider 的方法可用性」策略：上面的全局启用会把 MiMo 的直链禁用
    // 状态冲掉，若不重新触发 provider change，MiMo 下直链按钮会变回可点（与策略不一致）。
    if (!disabled && typeof updateCloneMethodForProvider === 'function') {
        updateCloneMethodForProvider(voiceProvider ? voiceProvider.value : '');
    }
    if (!disabled && typeof updateVoiceSourceForProvider === 'function') {
        updateVoiceSourceForProvider(voiceProvider ? voiceProvider.value : '');
    }
}

async function registerVoice() {
    const fileInput = document.getElementById('audioFile');
    const directLinkUrl = document.getElementById('directLinkUrl');
    const refLanguage = document.getElementById('refLanguage').value;
    const resultDiv = document.getElementById('result');

    // 清空现有内容并重置类名
    resultDiv.textContent = '';
    resultDiv.className = 'result';

    const effectiveAudioFile = getEffectiveAudioFile();
    const providerSelect = document.getElementById('voiceProvider');
    await ensureVoiceCloneProviderRestrictionsLoaded();
    applyVoiceCloneProviderRestrictions(providerSelect);
    const provider = (providerSelect || {}).value || 'cosyvoice';
    const prefix = normalizePrefixInputForProvider();
    const designPromptEl = document.getElementById('voiceDesignPrompt');
    const designPrompt = designPromptEl ? designPromptEl.value.trim() : '';
    const validateDoubaoSpeakerId = () => {
        if (provider !== 'doubao_tts') {
            return false;
        }
        if (isDoubaoSpeakerId(prefix)) return false;
        resultDiv.textContent = window.t
            ? window.t('voice.doubaoSpeakerIdRequired')
            : '豆包声音复刻需要填写 S_ 开头的 Speaker ID';
        resultDiv.className = 'result error';
        return true;
    };

    // 根据克隆方式验证输入
    if (currentVoiceSource === 'design') {
        if (!isVoiceDesignSupportedProvider(provider)) {
            resultDiv.textContent = window.t ? window.t('voice.designProviderUnsupported') : 'Voice Design is not available for the selected provider.';
            resultDiv.className = 'result error';
            return;
        }
        if (!prefix) {
            resultDiv.textContent = window.t ? window.t('voice.pleaseEnterPrefix') : '请填写自定义前缀';
            resultDiv.className = 'result error';
            return;
        }
        const designConstraints = getVoiceDesignMetadata(provider);
        const prefixMax = Number(designConstraints.prefix_max);
        const prefixPattern = String(designConstraints.prefix_pattern || '');
        let prefixMatches = true;
        if (prefixPattern) {
            try {
                prefixMatches = new RegExp(prefixPattern).test(prefix);
            } catch (error) {
                console.warn('Invalid Voice Design prefix pattern in provider metadata:', error);
            }
        }
        if ((prefixMax > 0 && prefix.length > prefixMax) || !prefixMatches) {
            resultDiv.textContent = window.t
                ? window.t('voice.designPrefixInvalid', { max: prefixMax })
                : `The prefix must be 1-${prefixMax} characters using only English letters and numbers, with no underscores or spaces.`;
            resultDiv.className = 'result error';
            return;
        }
        if (!designPrompt) {
            resultDiv.textContent = window.t ? window.t('voice.pleaseEnterDesignPrompt') : 'Please describe the voice you want to design.';
            resultDiv.className = 'result error';
            return;
        }
        const promptMin = Number(designConstraints.prompt_min);
        const promptMax = Number(designConstraints.prompt_max);
        if (promptMin > 0 && designPrompt.length < promptMin) {
            const key = isElevenLabsProvider(provider)
                ? 'voice.designPromptTooShortElevenlabs'
                : 'voice.designPromptTooShort';
            resultDiv.textContent = window.t
                ? window.t(key, { min: promptMin })
                : `Voice description must be at least ${promptMin} characters.`;
            resultDiv.className = 'result error';
            return;
        }
        if (promptMax > 0 && designPrompt.length > promptMax) {
            const key = isElevenLabsProvider(provider)
                ? 'voice.designPromptTooLongElevenlabs'
                : 'voice.designPromptTooLong';
            resultDiv.textContent = window.t
                ? window.t(key, { max: promptMax })
                : `Voice description must be at most ${promptMax} characters.`;
            resultDiv.className = 'result error';
            return;
        }
    } else if (currentCloneMethod === 'file') {
        // 先检查文件
        if (!effectiveAudioFile) {
            resultDiv.textContent = window.t ? window.t('voice.pleaseUploadFile') : '请选择音频文件';
            resultDiv.className = 'result error';
            return;
        }
        // 再检查前缀
        if (!prefix) {
            resultDiv.textContent = window.t ? window.t('voice.pleaseEnterPrefix') : '请填写自定义前缀';
            resultDiv.className = 'result error';
            return;
        }
        if (validateDoubaoSpeakerId()) {
            return;
        }
        // vLLM-Omni 必须填写参考音频原文
        if (provider === 'vllm_omni') {
            const refTextEl = document.getElementById('vllmRefText');
            const refTextVal = refTextEl ? refTextEl.value.trim() : '';
            if (!refTextVal) {
                resultDiv.textContent = window.t ? window.t('voice.vllmRefTextRequired') : '请填写参考音频原文（vLLM-Omni 克隆必填）';
                resultDiv.className = 'result error';
                return;
            }
            if (refTextVal.length > 100) {
                resultDiv.textContent = window.t ? window.t('voice.vllmRefTextTooLong') : 'vLLM-Omni 参考音频原文过长，请控制在 100 字以内';
                resultDiv.className = 'result error';
                return;
            }
        }
    } else {
        // 直链克隆
        const url = directLinkUrl.value.trim();
        // 先检查URL
        if (!url) {
            resultDiv.textContent = window.t ? window.t('voice.pleaseEnterDirectLink') : '请输入音频直链URL';
            resultDiv.className = 'result error';
            return;
        }
        // 再检查前缀
        if (!prefix) {
            resultDiv.textContent = window.t ? window.t('voice.pleaseEnterPrefix') : '请填写自定义前缀';
            resultDiv.className = 'result error';
            return;
        }
        if (validateDoubaoSpeakerId()) {
            return;
        }
        // 验证URL格式
        if (!url.startsWith('http://') && !url.startsWith('https://')) {
            resultDiv.textContent = window.t ? window.t('voice.invalidDirectLink') : '请输入有效的HTTP/HTTPS链接';
            resultDiv.className = 'result error';
            return;
        }
    }

    // Lock the form before refreshing configuration. The refresh can retry for
    // several seconds, and keeping inputs active would allow concurrent uploads
    // or a request assembled from values edited during that wait.
    setFormDisabled(true);

    let vllmOmniConfigurationWarning = '';
    if (currentVoiceSource === 'clone' && provider === 'vllm_omni') {
        // This is informational only: an inline vLLM-Omni clone can be saved
        // locally before its TTS endpoint is configured. Refresh the setting so
        // the notice reflects the active TTS route instead of a stale page cache.
        await ensureVoiceCloneApiConfigState({ force: true });
        vllmOmniConfigurationWarning = getVllmOmniCloneConfigurationWarning(
            voiceCloneApiConfigState.cfg
        );
    }

    resultDiv.textContent = currentVoiceSource === 'design'
        ? (window.t ? window.t('voice.generatingVoice') : 'Generating voice, please wait...')
        : (window.t ? window.t('voice.registering') : '正在注册声音，请稍后！');
    resultDiv.className = 'result';
    appendVllmOmniCloneConfigurationWarning(resultDiv, vllmOmniConfigurationWarning);

    // 根据克隆方式选择API端点和参数
    let requestOptions;
    let apiUrl = '';
    if (currentVoiceSource === 'design') {
        requestOptions = {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                provider: provider,
                prefix: prefix,
                voice_prompt: designPrompt,
                ref_language: refLanguage,
                i18n_language: getVoicePreviewLanguage()
            })
        };
        apiUrl = '/api/characters/voice_design';
    } else if (currentCloneMethod === 'file') {
        // 本地文件克隆
        const formData = new FormData();
        formData.append('file', effectiveAudioFile, effectiveAudioFile.name);
        formData.append('ref_language', refLanguage);
        formData.append('prefix', prefix);
        formData.append('provider', provider);
        if (provider === 'vllm_omni') {
            const refTextEl = document.getElementById('vllmRefText');
            formData.append('ref_text', refTextEl ? refTextEl.value.trim() : '');
        }
        requestOptions = {
            method: 'POST',
            body: formData
        };
        apiUrl = '/api/characters/voice_clone';
    } else {
        // 直链克隆
        requestOptions = {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                direct_link: directLinkUrl.value.trim(),
                ref_language: refLanguage,
                prefix: prefix,
                provider: provider
            })
        };
        apiUrl = '/api/characters/voice_clone_direct';
    }

    fetch(apiUrl, requestOptions)
        .then(async res => {
            const { data, nonJson, text } = await safeReadResponse(res);
            if (!res.ok) {
                if (data) {
                    // 从响应体中提取详细错误信息（优先已翻译的 errors.<code>，缺失则回退到 message/detail/error）
                    const error = new Error(resolveBackendErrorMsg(data, res.status));
                    // Voice Design needs code/details to render provider-specific constraints.
                    // Keep the user-facing message too, so Voice Clone's existing errors stay unchanged.
                    error.voiceRegisterError = data;
                    throw error;
                }
                // 后端/网关返回了 HTML（如 404/502/504），构造可读错误而不是 "Unexpected token '<'"
                throw new Error(buildNonJsonError(res, text));
            }
            if (nonJson) {
                // 状态码 2xx 但响应体不是 JSON——不应发生，但仍优雅处理
                throw new Error(buildNonJsonError(res, text));
            }
            return data;
        })
        .then(data => {
            if (data.voice_id) {
                if (data.reused) {
                    resultDiv.textContent = window.t ? window.t('voice.reusedExisting', { voiceId: data.voice_id }) : '已复用现有音色，跳过上传。voice_id: ' + data.voice_id;
                } else if (data.local_save_failed) {
                    // 部分成功：音色注册成功但本地保存失败
                    resultDiv.innerHTML = '';
                    const partialMsg = document.createElement('span');
                    partialMsg.style.color = 'orange';
                    partialMsg.textContent = window.t ? window.t('voice.registerSuccessButSaveFailed') : '音色注册成功，但本地保存失败';
                    resultDiv.appendChild(partialMsg);
                    resultDiv.appendChild(document.createElement('br'));
                    
                    const voiceIdLabel = document.createElement('span');
                    voiceIdLabel.textContent = 'voice_id: ';
                    resultDiv.appendChild(voiceIdLabel);
                    
                    const voiceIdCode = document.createElement('code');
                    voiceIdCode.style.background = '#f0f0f0';
                    voiceIdCode.style.padding = '2px 6px';
                    voiceIdCode.style.borderRadius = '4px';
                    voiceIdCode.style.userSelect = 'all';
                    voiceIdCode.textContent = data.voice_id;
                    resultDiv.appendChild(voiceIdCode);
                    
                    resultDiv.appendChild(document.createElement('br'));
                    const copyHint = document.createElement('span');
                    copyHint.style.fontSize = '12px';
                    copyHint.style.color = '#666';
                    copyHint.textContent = window.t ? window.t('voice.pleaseCopyVoiceId') : '请复制上面的voice_id手动保存';
                    resultDiv.appendChild(copyHint);
                    appendVllmOmniCloneConfigurationWarning(resultDiv, vllmOmniConfigurationWarning);
                    
                    setFormDisabled(false);
                    return;
                } else {
                    resultDiv.textContent = window.t ? window.t('voice.registerSuccess', { voiceId: data.voice_id }) : '注册成功！voice_id: ' + data.voice_id;
                }
                appendVllmOmniCloneConfigurationWarning(resultDiv, vllmOmniConfigurationWarning);
                // 刷新音色列表
                setTimeout(() => {
                    if (typeof loadVoices === 'function') {
                        loadVoices();
                    }
                }, 1000);
                // 自动更新voice_id到后端
                const lanlanName = document.getElementById('lanlan_name').value;
                if (lanlanName) {
                    saveVoiceIdToCurrentCharacter(data.voice_id).then(({ result: res }) => {
                        const successMsg = document.createElement('span');
                        successMsg.textContent = (window.t ? window.t('voice.voiceIdSaved') : 'voice_id已自动保存到角色');
                        resultDiv.appendChild(document.createElement('br'));
                        resultDiv.appendChild(successMsg);

                        // 如果session被结束，页面会自动刷新
                        const statusSpan = document.createElement('span');
                        statusSpan.style.color = 'blue';
                        if (res.session_restarted) {
                            statusSpan.textContent = (window.t ? window.t('voice.pageWillRefresh') : '当前页面即将自动刷新以应用新语音');
                        } else {
                            statusSpan.textContent = (window.t ? window.t('voice.voiceWillTakeEffect') : '新语音将在下次对话时生效');
                        }
                        resultDiv.appendChild(document.createElement('br'));
                        resultDiv.appendChild(statusSpan);
                    }).catch(e => {
                        // e 可能携带 safeReadResponse/buildNonJsonError 构造的可读错误
                        // （含 HTTP 状态和正文摘要），必须拼进最终提示，否则诊断信息被吞。
                        const saveErrorMsg = e?.message || e?.toString() || (window.t ? window.t('common.unknownError') : '未知错误');
                        const base = window.t ? window.t('voice.voiceIdSaveRequestError') : 'voice_id自动保存请求出错';
                        const errorSpan = document.createElement('span');
                        errorSpan.className = 'error';
                        errorSpan.textContent = saveErrorMsg ? `${base}: ${saveErrorMsg}` : base;
                        resultDiv.appendChild(document.createElement('br'));
                        resultDiv.appendChild(errorSpan);
                    });
                }
            } else {
                // Keep structured API fields such as code/details for Voice Design validation errors.
                const errorObj = data && typeof data === 'object'
                    ? data
                    : (window.t ? window.t('common.unknownError') : '未知错误');
                const { displayError, shouldFlash } = parseVoiceRegisterError(errorObj);
                resultDiv.textContent = window.t ? window.t('voice.registerFailed', { error: displayError }) : '注册失败：' + displayError;
                resultDiv.className = 'result error';
                if (shouldFlash) {
                    resultDiv.classList.add('error-flash');
                }
            }
            setFormDisabled(false);
        })
        .catch(err => {
            const errorObj = err?.voiceRegisterError
                ? { ...err.voiceRegisterError, message: err.message }
                : (err?.message || err?.toString() || (window.t ? window.t('common.unknownError') : '未知错误'));
            const { displayError, shouldFlash } = parseVoiceRegisterError(errorObj);
            resultDiv.textContent = window.t ? window.t('voice.requestError', { error: displayError }) : '请求出错：' + displayError;
            resultDiv.className = 'result error';
            if (shouldFlash) {
                resultDiv.classList.add('error-flash');
            }
            setFormDisabled(false);
        });
}

// 监听API Key变更事件
window.addEventListener('message', function (event) {
    if (!ALLOWED_ORIGINS.includes(event.origin)) return;
    if (event.data.type === 'api_key_changed') {
        // API Key已更改，可以在这里添加其他需要的处理逻辑
        console.log('API Key已更改，音色注册页面已收到通知');
        ensureVoiceCloneApiConfigState({ force: true }).then(() => {
            refreshVoiceCloneProviderNotice(
                document.getElementById('voiceProvider'),
                document.getElementById('provider-notice')
            );
        });
        // 刷新音色列表
        loadVoices();
    }
});

function showVoicePreviewErrorNotice(message) {
    if (activeVoicePreviewNotice) {
        activeVoicePreviewNotice.dispose();
    }

    const backdrop = document.createElement('div');
    backdrop.className = 'voice-preview-notice-backdrop';

    const card = document.createElement('div');
    card.className = 'voice-preview-notice-card';
    card.setAttribute('role', 'alertdialog');
    card.setAttribute('aria-modal', 'true');
    card.setAttribute('aria-labelledby', 'voice-preview-notice-title');
    card.setAttribute('aria-describedby', 'voice-preview-notice-message');

    const header = document.createElement('div');
    header.className = 'voice-preview-notice-header';

    const mark = document.createElement('span');
    mark.className = 'voice-preview-notice-mark';
    mark.setAttribute('aria-hidden', 'true');

    const title = document.createElement('h3');
    title.className = 'voice-preview-notice-title';
    title.id = 'voice-preview-notice-title';
    title.textContent = window.t ? window.t('voice.preview') : 'Preview';

    const body = document.createElement('div');
    body.className = 'voice-preview-notice-body';

    const messageEl = document.createElement('p');
    messageEl.className = 'voice-preview-notice-message';
    messageEl.id = 'voice-preview-notice-message';
    messageEl.textContent = String(message || '');

    const actions = document.createElement('div');
    actions.className = 'voice-preview-notice-actions';

    const okButton = document.createElement('button');
    okButton.type = 'button';
    okButton.className = 'voice-preview-notice-ok';
    okButton.textContent = window.t ? window.t('common.ok') : 'OK';

    header.appendChild(mark);
    header.appendChild(title);
    body.appendChild(messageEl);
    actions.appendChild(okButton);
    card.appendChild(header);
    card.appendChild(body);
    card.appendChild(actions);
    backdrop.appendChild(card);

    let closed = false;
    let cleaned = false;

    function cleanup() {
        if (cleaned) return;
        cleaned = true;
        document.removeEventListener('keydown', onKeydown, true);
        if (backdrop.parentNode) {
            backdrop.parentNode.removeChild(backdrop);
        }
        if (activeVoicePreviewNotice && activeVoicePreviewNotice.backdrop === backdrop) {
            activeVoicePreviewNotice = null;
        }
    }

    function close() {
        if (closed) return;
        closed = true;
        backdrop.classList.add('is-closing');
        window.setTimeout(cleanup, 160);
    }

    function dispose() {
        closed = true;
        cleanup();
    }

    function onKeydown(event) {
        if (event.key === 'Tab') {
            event.preventDefault();
            event.stopPropagation();
            okButton.focus();
            return;
        }
        if (event.key === 'Escape' || event.key === 'Enter') {
            event.preventDefault();
            event.stopPropagation();
            close();
        }
    }

    okButton.addEventListener('click', close);
    backdrop.addEventListener('click', event => {
        if (event.target === backdrop) close();
    });
    document.addEventListener('keydown', onKeydown, true);
    document.body.appendChild(backdrop);
    activeVoicePreviewNotice = { backdrop, dispose };
    window.setTimeout(() => okButton.focus(), 0);
}

const activeVoicePreviewSessions = new Map();
const voicePreviewOriginalButtonContents = new WeakMap();

function setVoicePreviewButtonState(btn, state) {
    if (!btn) return;

    if (state === 'idle') {
        const originalContent = voicePreviewOriginalButtonContents.get(btn);
        if (originalContent !== undefined) {
            btn.innerHTML = originalContent;
            voicePreviewOriginalButtonContents.delete(btn);
        }
        btn.disabled = false;
        delete btn.dataset.previewState;
        return;
    }

    if (!voicePreviewOriginalButtonContents.has(btn)) {
        voicePreviewOriginalButtonContents.set(btn, btn.innerHTML);
    }
    btn.textContent = state === 'playing'
        ? (window.t ? window.t('voice.previewing') : '正在预览')
        : (window.t ? window.t('voice.loading') : '...');
    btn.disabled = true;
    btn.dataset.previewState = state;
}

function attachVoicePreviewButton(voiceId, btn) {
    const session = activeVoicePreviewSessions.get(String(voiceId));
    if (!session) return;
    session.buttons.add(btn);
    setVoicePreviewButtonState(btn, session.state);
}

function updateVoicePreviewSessionState(session, state) {
    if (activeVoicePreviewSessions.get(session.voiceId) !== session) return;
    session.state = state;
    session.buttons.forEach(btn => setVoicePreviewButtonState(btn, state));
}

function finishVoicePreviewSession(session) {
    if (activeVoicePreviewSessions.get(session.voiceId) !== session) return;
    activeVoicePreviewSessions.delete(session.voiceId);
    session.buttons.forEach(btn => setVoicePreviewButtonState(btn, 'idle'));
    session.buttons.clear();
}

async function playPreview(voiceId, btn, options = {}) {
    if (btn.disabled) return;

    const voiceIdKey = String(voiceId);
    const existingSession = activeVoicePreviewSessions.get(voiceIdKey);
    if (existingSession) {
        attachVoicePreviewButton(voiceIdKey, btn);
        return;
    }
    const session = {
        voiceId: voiceIdKey,
        state: 'loading',
        buttons: new Set(),
    };
    activeVoicePreviewSessions.set(voiceIdKey, session);
    attachVoicePreviewButton(voiceIdKey, btn);

    try {
        const storageKey = `voice_preview_${voiceId}`;
        const previewLanguage = getVoicePreviewLanguage();
        const cachedPreview = localStorage.getItem(storageKey);
        let audioSrc = '';
        if (cachedPreview) {
            try {
                const cachedData = JSON.parse(cachedPreview);
                if (
                    cachedData
                    && cachedData.version === 2
                    && cachedData.language === previewLanguage
                    && typeof cachedData.audioSrc === 'string'
                    && cachedData.audioSrc
                ) {
                    audioSrc = cachedData.audioSrc;
                }
            } catch (_) {
                // 旧版缓存没有语言信息，忽略并重新生成，避免切换语言后继续播放旧试听。
            }
        }

        if (!audioSrc) {
            // 如果本地没有缓存，则从服务器获取
            // 保留 Voice Clone 原有的 voice-id 判定；Voice Design 仅通过
            // source/design id 追加到同一实时合成超时档位。
            const voiceSource = String(options.source || '').trim().toLowerCase();
            const isCloneVoice = typeof voiceId === 'string' && voiceId.includes('-clone-');
            const isDesignVoice = voiceSource === 'design'
                || (typeof voiceId === 'string' && voiceId.includes('-design-'));
            const isRealtimeRegisteredVoice = isCloneVoice || isDesignVoice;
            const ttsTimeoutMs = isRealtimeRegisteredVoice ? 30_000 : 5_000;
            const ttsMaxAttempts = isRealtimeRegisteredVoice ? 2 : 3;
            let lastTtsError = null;
            let response = null;
            for (let attempt = 1; attempt <= ttsMaxAttempts; attempt += 1) {
                response = null;
                const ctrl = new AbortController();
                const tid = setTimeout(() => ctrl.abort(), ttsTimeoutMs);
                try {
                    response = await fetch(
                        `/api/characters/voice_preview?voice_id=${encodeURIComponent(voiceId)}&language=${encodeURIComponent(previewLanguage)}`,
                        { signal: ctrl.signal }
                    );
                    if (response.ok || response.status < 500 || attempt >= ttsMaxAttempts) break;
                    lastTtsError = new Error(`API returned ${response.status}`);
                } catch (error) {
                    lastTtsError = (voiceSource === 'design' && error && error.name === 'AbortError')
                        ? new Error(window.t ? window.t('voice.previewTimeout') : '试听生成超时，请稍后重试')
                        : error;
                    if (attempt >= ttsMaxAttempts) break;
                } finally {
                    clearTimeout(tid);
                }
                await sleepVoiceCloneLoaderRetry(VOICE_CLONE_LOADER_FETCH_BACKOFF_MS * attempt);
            }
            if (!response) throw lastTtsError || new Error('请求失败');
            const { data, nonJson, text } = await safeReadResponse(response);
            if (!response.ok) {
                if (data && (data.error || data.detail)) {
                    throw new Error(data.error || data.detail);
                }
                throw new Error(buildNonJsonError(response, text));
            }
            if (nonJson) {
                throw new Error(buildNonJsonError(response, text));
            }

            if (data.success && data.audio) {
                audioSrc = `data:${data.mime_type || 'audio/mpeg'};base64,${data.audio}`;
                // 保存到 localStorage
                try {
                    localStorage.setItem(storageKey, JSON.stringify({
                        version: 2,
                        language: previewLanguage,
                        audioSrc
                    }));
                } catch (e) {
                    console.warn('Failed to save preview to localStorage:', e);
                    // localStorage 可能满了，但我们仍然可以播放这一次生成的音频
                }
            } else {
                const _errMsg = resolveBackendErrorMsg(data, response.status) || 'Failed to get preview';
                throw new Error(_errMsg);
            }
        }

        if (audioSrc) {
            const audio = new Audio(audioSrc);
            let playbackFinished = false;
            const restorePreviewButton = () => {
                if (playbackFinished) return;
                playbackFinished = true;
                finishVoicePreviewSession(session);
            };

            audio.addEventListener('ended', restorePreviewButton, { once: true });
            audio.addEventListener('error', restorePreviewButton, { once: true });

            try {
                await audio.play();
            } catch (e) {
                restorePreviewButton();
                console.error('Audio play error:', e);
                const errorMsg = e?.message || e?.toString();
                showVoicePreviewErrorNotice(
                    window.t ? window.t('voice.playFailed', { error: errorMsg }) : '播放失败: ' + errorMsg
                );
                return;
            }

            if (!playbackFinished) {
                updateVoicePreviewSessionState(session, 'playing');
            }
        }
    } catch (error) {
        console.error('Preview error:', error);
        const errorMsg = error?.message || error?.toString();
        showVoicePreviewErrorNotice(
            window.t ? window.t('voice.previewFailed', { error: errorMsg }) : '预览失败: ' + errorMsg
        );
        finishVoicePreviewSession(session);
    }
}

// 加载音色列表
async function loadVoices() {
    const container = document.getElementById('voice-list-container');
    const refreshBtn = document.getElementById('refresh-voices-btn');

    if (!container) return;

    // 显示加载状态
    const loadingText = window.t ? window.t('voice.loading') : '加载中...';
    container.textContent = '';
    const loadingDiv = document.createElement('div');
    loadingDiv.style.textAlign = 'center';
    loadingDiv.style.color = '#999';
    loadingDiv.style.padding = '20px';
    loadingDiv.id = 'voice-list-loading';
    const loadingSpan = document.createElement('span');
    loadingSpan.textContent = loadingText;
    loadingDiv.appendChild(loadingSpan);
    container.appendChild(loadingDiv);

    if (refreshBtn) refreshBtn.disabled = true;

    try {
        const currentVoiceId = await getCurrentCharacterVoiceId().catch(error => {
            console.warn('获取当前角色音色失败:', error);
            return '';
        });
        const response = await fetchVoiceCloneLoaderResponse('/api/characters/voices');
        const { data, nonJson, text } = await safeReadResponse(response);
        if (!response.ok) {
            if (data && (data.error || data.detail)) {
                throw new Error(data.error || data.detail);
            }
            throw new Error(buildNonJsonError(response, text));
        }
        if (nonJson) {
            throw new Error(buildNonJsonError(response, text));
        }

        if ((!data.voices || Object.keys(data.voices).length === 0) &&
            (!data.free_voices || Object.keys(data.free_voices).length === 0) &&
            (!data.pinned_voices || data.pinned_voices.length === 0) &&
            (!data.native_voices || Object.keys(data.native_voices).length === 0)) {
            const noVoicesText = window.t ? window.t('voice.noVoices') : '暂无已注册音色';
            container.textContent = '';
            const emptyDiv = document.createElement('div');
            emptyDiv.className = 'voice-list-empty';
            const emptySpan = document.createElement('span');
            emptySpan.textContent = noVoicesText;
            emptyDiv.appendChild(emptySpan);
            container.appendChild(emptyDiv);
            return;
        }

        // 清空容器
        container.textContent = '';

        // 置顶音色（海外免费 free_intl：yui + default）。永远排在列表最上面，
        // 展示名按 i18n_key 本地化；支持预览与点击应用，不可删除。
        if (Array.isArray(data.pinned_voices) && data.pinned_voices.length > 0) {
            data.pinned_voices.forEach((pin) => {
                const voiceId = pin && pin.voice_id;
                if (!voiceId) return;
                const item = document.createElement('div');
                item.className = 'voice-list-item';
                item.dataset.voiceId = voiceId;
                item.style.opacity = '0.85';
                item.tabIndex = 0;
                item.setAttribute('role', 'button');
                markSelectedVoiceItem(item, voiceId === currentVoiceId);

                const displayName = (window.t && pin.i18n_key)
                    ? window.t(pin.i18n_key)
                    : (pin.prefix || voiceId);

                const infoDiv = document.createElement('div');
                infoDiv.className = 'voice-info';
                const nameDiv = document.createElement('div');
                nameDiv.className = 'voice-name';
                nameDiv.textContent = displayName;
                infoDiv.appendChild(nameDiv);
                const idDiv = document.createElement('div');
                idDiv.className = 'voice-id';
                idDiv.textContent = `ID: ${voiceId}`;
                infoDiv.appendChild(idDiv);

                const voiceActions = document.createElement('div');
                voiceActions.className = 'voice-actions';
                const previewBtn = document.createElement('button');
                previewBtn.className = 'voice-preview-btn';
                const previewText = window.t ? window.t('voice.preview') : '预览';
                const previewImg = document.createElement('img');
                previewImg.src = '/static/icons/sound.png';
                previewImg.alt = '';
                previewBtn.appendChild(previewImg);
                previewBtn.appendChild(document.createTextNode(previewText));
                attachVoicePreviewButton(voiceId, previewBtn);
                previewBtn.onclick = (event) => {
                    event.stopPropagation();
                    playPreview(voiceId, previewBtn);
                };
                voiceActions.appendChild(previewBtn);

                item.appendChild(infoDiv);
                item.appendChild(voiceActions);
                item.setAttribute('aria-label', window.t ? window.t('voice.applyVoiceAria', { name: displayName }) : `应用音色 ${displayName}`);
                item.addEventListener('click', () => applyVoiceToCurrentCharacter(voiceId, displayName, item));
                item.addEventListener('keydown', (event) => {
                    if (event.target !== item) return;
                    if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        applyVoiceToCurrentCharacter(voiceId, displayName, item);
                    }
                });
                container.appendChild(item);
            });
        }

        // 按创建时间排序（如果有）
        const voicesArray = Object.entries(data.voices).map(([voiceId, voiceData]) => ({
            voiceId,
            ...voiceData
        }));

        // 如果有创建时间，按时间倒序排列
        voicesArray.sort((a, b) => {
            if (a.created_at && b.created_at) {
                return new Date(b.created_at) - new Date(a.created_at);
            }
            return 0;
        });

        // 创建音色列表项
        voicesArray.forEach(({ voiceId, prefix, created_at, source, provider }) => {
            const item = document.createElement('div');
            item.className = 'voice-list-item';
            item.dataset.voiceId = voiceId;
            item.tabIndex = 0;
            item.setAttribute('role', 'button');
            item.setAttribute('aria-label', window.t ? window.t('voice.applyVoiceAria', { name: prefix || voiceId }) : `应用音色 ${prefix || voiceId}`);
            markSelectedVoiceItem(item, voiceId === currentVoiceId);

            const voiceName = prefix || voiceId;
            const displayName = voiceName.length > 30 ? voiceName.substring(0, 30) + '...' : voiceName;

            let dateStr = '';
            if (created_at) {
                try {
                    const date = new Date(created_at);
                    // 使用 i18n locale，回退到 navigator.language，最后回退到 'en-US'
                    const locale = (window.i18n && window.i18n.language) || navigator.language || 'en-US';
                    dateStr = date.toLocaleString(locale, {
                        year: 'numeric',
                        month: '2-digit',
                        day: '2-digit',
                        hour: '2-digit',
                        minute: '2-digit'
                    });
                } catch (e) {
                    // 忽略日期解析错误
                }
            }

            const voiceActions = document.createElement('div');
            voiceActions.className = 'voice-actions';

            const previewBtn = document.createElement('button');
            previewBtn.className = 'voice-preview-btn';
            const previewText = window.t ? window.t('voice.preview') : '预览';
            const previewImg = document.createElement('img');
            previewImg.src = '/static/icons/sound.png';
            previewImg.alt = '';
            previewBtn.appendChild(previewImg);
            previewBtn.appendChild(document.createTextNode(previewText));
            attachVoicePreviewButton(voiceId, previewBtn);
            previewBtn.onclick = (event) => {
                event.stopPropagation();
                playPreview(voiceId, previewBtn, { source, provider });
            };

            const deleteBtn = document.createElement('button');
            deleteBtn.className = 'voice-delete-btn';
            const deleteText = window.t ? window.t('voice.delete') : '删除';
            const deleteImg = document.createElement('img');
            deleteImg.src = '/static/icons/delete.png';
            deleteImg.alt = '';
            deleteBtn.appendChild(deleteImg);
            deleteBtn.appendChild(document.createTextNode(deleteText));
            deleteBtn.onclick = (event) => {
                event.stopPropagation();
                deleteVoice(voiceId, displayName);
            };

            voiceActions.appendChild(previewBtn);
            voiceActions.appendChild(deleteBtn);

            const infoDiv = document.createElement('div');
            infoDiv.className = 'voice-info';

            const nameDiv = document.createElement('div');
            nameDiv.className = 'voice-name';
            nameDiv.textContent = displayName;
            infoDiv.appendChild(nameDiv);

            const idDiv = document.createElement('div');
            idDiv.className = 'voice-id';
            idDiv.textContent = `ID: ${voiceId}`;
            infoDiv.appendChild(idDiv);

            if (dateStr) {
                const dateDiv = document.createElement('div');
                dateDiv.className = 'voice-date';
                dateDiv.textContent = dateStr;
                infoDiv.appendChild(dateDiv);
            }

            item.appendChild(infoDiv);
            item.appendChild(voiceActions);
            item.addEventListener('click', () => applyVoiceToCurrentCharacter(voiceId, displayName, item));
            item.addEventListener('keydown', (event) => {
                if (event.target !== item) return;
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    applyVoiceToCurrentCharacter(voiceId, displayName, item);
                }
            });

            container.appendChild(item);
        });

        // 渲染免费预设音色（不可删除，放在最后）
        if (data.free_voices && Object.keys(data.free_voices).length > 0) {
            // 用户注册音色与预设音色之间的分隔线
            if (voicesArray.length > 0) {
                const divider = document.createElement('div');
                divider.style.cssText = 'border-top: 1px dashed #b0d4f1; margin: 12px 0; padding-top: 8px; color: #90b8d8; font-size: 12px; text-align: center;';
                const freeLabel = window.t ? window.t('voice.freePresetLabel') : '免费预设音色';
                divider.textContent = freeLabel;
                container.appendChild(divider);
            }

            Object.entries(data.free_voices).forEach(([voiceKey, voiceId]) => {
                const item = document.createElement('div');
                item.className = 'voice-list-item';
                item.dataset.voiceId = voiceId;
                item.style.opacity = '0.85';
                item.tabIndex = 0;
                item.setAttribute('role', 'button');
                markSelectedVoiceItem(item, voiceId === currentVoiceId);

                const infoDiv = document.createElement('div');
                infoDiv.className = 'voice-info';

                const nameDiv = document.createElement('div');
                nameDiv.className = 'voice-name';
                // 使用 i18n 翻译键获取显示名称
                const displayName = window.t ? window.t(`voice.freeVoice.${voiceKey}`) : voiceKey;
                nameDiv.textContent = displayName;
                // 添加预设标签
                const badge = document.createElement('span');
                badge.style.cssText = 'margin-left: 8px; font-size: 10px; padding: 1px 6px; border-radius: 8px; background: rgba(100,180,255,0.25); color: #7ac4ff;';
                badge.textContent = window.t ? window.t('voice.freePresetBadge') : '预设';
                nameDiv.appendChild(badge);
                infoDiv.appendChild(nameDiv);

                const idDiv = document.createElement('div');
                idDiv.className = 'voice-id';
                idDiv.textContent = `ID: ${voiceId}`;
                infoDiv.appendChild(idDiv);

                const voiceActions = document.createElement('div');
                voiceActions.className = 'voice-actions';

                const previewBtn = document.createElement('button');
                previewBtn.className = 'voice-preview-btn';
                const previewText = window.t ? window.t('voice.preview') : '预览';
                const previewImg = document.createElement('img');
                previewImg.src = '/static/icons/sound.png';
                previewImg.alt = '';
                previewBtn.appendChild(previewImg);
                previewBtn.appendChild(document.createTextNode(previewText));
                attachVoicePreviewButton(voiceId, previewBtn);
                previewBtn.onclick = (event) => {
                    event.stopPropagation();
                    playPreview(voiceId, previewBtn);
                };
                voiceActions.appendChild(previewBtn);

                item.appendChild(infoDiv);
                item.appendChild(voiceActions);
                item.setAttribute('aria-label', window.t ? window.t('voice.applyVoiceAria', { name: displayName }) : `应用音色 ${displayName}`);
                item.addEventListener('click', () => applyVoiceToCurrentCharacter(voiceId, displayName, item));
                item.addEventListener('keydown', (event) => {
                    if (event.target !== item) return;
                    if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        applyVoiceToCurrentCharacter(voiceId, displayName, item);
                    }
                });

                container.appendChild(item);
            });
        }

        // 渲染当前 Realtime Provider 的原生音色（由后端按 core_api_type 注入）
        // 去重范围：自定义注册音色 + 免费预设音色 ID，避免冲突时列表里重复条目和多重选中态。
        // 自定义/免费音色优先保留，与 _has_custom_tts 的路由优先级一致。
        if (data.native_voices && Object.keys(data.native_voices).length > 0) {
            const renderedVoiceIds = new Set(voicesArray.map((v) => String(v.voiceId).toLowerCase()));
            if (data.free_voices) {
                Object.values(data.free_voices).forEach((id) => {
                    if (id) renderedVoiceIds.add(String(id).toLowerCase());
                });
            }
            const nativeEntries = Object.entries(data.native_voices)
                .filter(([voiceId]) => !renderedVoiceIds.has(String(voiceId).toLowerCase()));
            if (nativeEntries.length > 0) {
                const hasPriorContent = voicesArray.length > 0
                    || (data.free_voices && Object.keys(data.free_voices).length > 0);
                if (hasPriorContent) {
                    const divider = document.createElement('div');
                    divider.style.cssText = 'border-top: 1px dashed #b0d4f1; margin: 12px 0; padding-top: 8px; color: #90b8d8; font-size: 12px; text-align: center;';
                    divider.textContent = formatNativeVoiceLabel(nativeEntries);
                    container.appendChild(divider);
                }

                nativeEntries.forEach(([voiceId, voiceData]) => {
                    const item = document.createElement('div');
                    item.className = 'voice-list-item';
                    item.dataset.voiceId = voiceId;
                    item.style.opacity = '0.85';
                    item.tabIndex = 0;
                    item.setAttribute('role', 'button');
                    markSelectedVoiceItem(item, voiceId === currentVoiceId);

                    const infoDiv = document.createElement('div');
                    infoDiv.className = 'voice-info';

                    const nameDiv = document.createElement('div');
                    nameDiv.className = 'voice-name';
                    const displayName = getNativeVoiceDisplayName(voiceId, voiceData);
                    nameDiv.textContent = displayName;
                    const badge = document.createElement('span');
                    badge.style.cssText = 'margin-left: 8px; font-size: 10px; padding: 1px 6px; border-radius: 8px; background: rgba(140,120,220,0.25); color: #b8a4ff;';
                    badge.textContent = window.t ? window.t('voice.nativePresetBadge') : '原生';
                    nameDiv.appendChild(badge);
                    infoDiv.appendChild(nameDiv);

                    const idDiv = document.createElement('div');
                    idDiv.className = 'voice-id';
                    idDiv.textContent = `ID: ${voiceId}`;
                    infoDiv.appendChild(idDiv);

                    const voiceActions = document.createElement('div');
                    voiceActions.className = 'voice-actions';

                    const previewBtn = document.createElement('button');
                    previewBtn.className = 'voice-preview-btn';
                    const previewText = window.t ? window.t('voice.preview') : '预览';
                    const previewImg = document.createElement('img');
                    previewImg.src = '/static/icons/sound.png';
                    previewImg.alt = '';
                    previewBtn.appendChild(previewImg);
                    previewBtn.appendChild(document.createTextNode(previewText));
                    attachVoicePreviewButton(voiceId, previewBtn);
                    previewBtn.onclick = (event) => {
                        event.stopPropagation();
                        playPreview(voiceId, previewBtn);
                    };
                    voiceActions.appendChild(previewBtn);

                    item.appendChild(infoDiv);
                    item.appendChild(voiceActions);
                    item.setAttribute('aria-label', window.t ? window.t('voice.applyVoiceAria', { name: displayName }) : `应用音色 ${displayName}`);
                    item.addEventListener('click', () => applyVoiceToCurrentCharacter(voiceId, displayName, item));
                    item.addEventListener('keydown', (event) => {
                        if (event.target !== item) return;
                        if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault();
                            applyVoiceToCurrentCharacter(voiceId, displayName, item);
                        }
                    });

                    // Provider 原生音色：支持预览和点击应用，不支持删除

                    container.appendChild(item);
                });
            }
        }

    } catch (error) {
        console.error('加载音色列表失败:', error);
        const loadErrorText = window.t ? window.t('voice.loadError') : '加载失败，请稍后重试';
        container.textContent = '';
        const errorDiv = document.createElement('div');
        errorDiv.className = 'voice-list-empty';
        errorDiv.style.color = '#f44336';
        const errorSpan = document.createElement('span');
        errorSpan.textContent = loadErrorText;
        errorDiv.appendChild(errorSpan);
        container.appendChild(errorDiv);
    } finally {
        if (refreshBtn) refreshBtn.disabled = false;
    }
}

// 删除音色
async function deleteVoice(voiceId, voiceName) {
    const confirmMsg = window.t
        ? window.t('voice.confirmDelete', { name: voiceName })
        : `确定要删除音色"${voiceName}"吗？此操作不可恢复。`;

    if (!confirm(confirmMsg)) {
        return;
    }

    const container = document.getElementById('voice-list-container');
    const refreshBtn = document.getElementById('refresh-voices-btn');

    if (!container) return;

    // 禁用刷新按钮
    if (refreshBtn) refreshBtn.disabled = true;

    // 显示删除中状态
    container.textContent = '';
    const deletingDiv = document.createElement('div');
    deletingDiv.style.textAlign = 'center';
    deletingDiv.style.color = '#999';
    deletingDiv.style.padding = '20px';
    const deletingSpan = document.createElement('span');
    deletingSpan.textContent = window.t ? window.t('voice.deleting') : '删除中...';
    deletingDiv.appendChild(deletingSpan);
    container.appendChild(deletingDiv);

    try {
        const response = await fetch(`/api/characters/voices/${encodeURIComponent(voiceId)}`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' }
        });

        const { data: parsed, nonJson, text } = await safeReadResponse(response);
        if (!response.ok && !parsed) {
            // 后端/网关返回了 HTML（如 404/502），抛出可读错误
            throw new Error(buildNonJsonError(response, text));
        }
        if (nonJson) {
            throw new Error(buildNonJsonError(response, text));
        }
        const data = parsed || {};

        if (response.ok && data.success) {
            // 删除本地缓存的预览音频
            localStorage.removeItem(`voice_preview_${voiceId}`);
            
            // 删除成功，刷新列表
            await loadVoices();
            // 显示成功消息
            const resultDiv = document.getElementById('result');
            if (resultDiv) {
                resultDiv.textContent = window.t
                    ? window.t('voice.deleteSuccess', { name: voiceName })
                    : `音色"${voiceName}"已成功删除`;
                resultDiv.className = 'result';
                // 3秒后清除消息
                setTimeout(() => {
                    resultDiv.textContent = '';
                }, 3000);
            }
        } else {
            // 删除失败，重新加载列表以恢复事件处理器
            const errorMsg = data.error || (window.t ? window.t('voice.deleteFailed') : '删除失败');
            alert(errorMsg);
            await loadVoices();
        }
    } catch (error) {
        console.error('删除音色失败:', error);
        const errorMsg = window.t
            ? window.t('voice.deleteError', { error: error.message })
            : `删除失败: ${error.message}`;
        alert(errorMsg);
        // 重新加载列表以恢复事件处理器
        await loadVoices();
    } finally {
        if (refreshBtn) refreshBtn.disabled = false;
    }
}

// 页面加载时自动加载音色列表
(async function initVoiceList() {
    // 等待 i18n 初始化完成
    const waitForI18n = () => {
        if (window.i18n && window.i18n.isInitialized && typeof window.t === 'function') {
            // 确保页面文本已更新
            if (typeof window.updatePageTexts === 'function') {
                window.updatePageTexts();
            }
            // 等待页面完全加载后再加载音色列表
            setTimeout(loadVoices, 500);
        } else {
            // 继续等待
            setTimeout(waitForI18n, 100);
        }
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', waitForI18n);
    } else {
        waitForI18n();
    }
})();
