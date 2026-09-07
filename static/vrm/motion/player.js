(function () {
    'use strict';

    const MANIFEST_URL = '/static/vrm/motion/manifest.json';
    const FETCH_TIMEOUT_MS = 10000;
    const ASSET_ROOT = '/static/vrm/';
    const LOW_POSES = new Set(['sit', 'lie', 'sleep']);
    const POSE_DEPTH = Object.freeze({ stand: 0, sit: 1, lie: 2, sleep: 2 });
    const LOW_POSE_PRESERVING_KINDS = new Set(['gesture', 'emotion-body']);
    const LOW_POSE_PRESERVING_INTENTS = new Set(['yawn']);
    const COUNTABLE_INTENTS = new Set(['nod', 'shake', 'clap', 'wave']);
    const DISABLED_INTENTS = new Set(['cheer']);
    const MAX_TRANSIENT_MS = 14000;
    const DEFAULT_FADE_SECONDS = 0.32;
    const IDLE_SWITCH_MIN_MS = 22000;
    const IDLE_SWITCH_MAX_MS = 34000;
    const IDLE_SWITCH_FADE_SECONDS = 0.55;
    const COMPRESSED_BUNDLED_REST_NAMES = new Set([
        'liked', 'wait01', 'wait02', 'wait03', 'wait04', 'wait05',
        '全身展示', '射击姿态', '屈伸运动', '旋转', '模特姿势', '比 V 手势', '致意问候'
    ]);
    const SHA256_K = Object.freeze([
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
    ]);

    function stableHash(value) {
        let result = 2166136261;
        const source = String(value || '');
        for (let index = 0; index < source.length; index += 1) {
            result ^= source.charCodeAt(index);
            result = Math.imul(result, 16777619);
        }
        return result >>> 0;
    }

    function emit(name, detail) {
        window.dispatchEvent(new CustomEvent(name, { detail: detail || {} }));
    }

    function assetUrl(asset) {
        const suffix = asset.compression === 'gzip' ? '.gz' : '';
        return ASSET_ROOT + asset.f + suffix;
    }

    async function fetchWithTimeout(url, options, consumeResponse) {
        const controller = typeof AbortController === 'function' ? new AbortController() : null;
        const requestOptions = Object.assign({}, options || {});
        let timeoutId = null;
        if (controller && !requestOptions.signal) {
            requestOptions.signal = controller.signal;
            timeoutId = setTimeout(function () { controller.abort(); }, FETCH_TIMEOUT_MS);
        }
        try {
            const response = await fetch(url, requestOptions);
            return consumeResponse ? await consumeResponse(response) : response;
        } finally {
            if (timeoutId !== null) clearTimeout(timeoutId);
        }
    }

    function sha256Fallback(buffer) {
        const source = new Uint8Array(buffer);
        const paddedLength = Math.ceil((source.length + 9) / 64) * 64;
        const bytes = new Uint8Array(paddedLength);
        const view = new DataView(bytes.buffer);
        const bitLength = source.length * 8;
        bytes.set(source);
        bytes[source.length] = 0x80;
        view.setUint32(paddedLength - 8, Math.floor(bitLength / 0x100000000), false);
        view.setUint32(paddedLength - 4, bitLength >>> 0, false);
        const hash = new Uint32Array([
            0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
            0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
        ]);
        const words = new Uint32Array(64);
        const rotateRight = function (value, bits) {
            return (value >>> bits) | (value << (32 - bits));
        };
        for (let offset = 0; offset < paddedLength; offset += 64) {
            for (let index = 0; index < 16; index += 1) {
                words[index] = view.getUint32(offset + index * 4, false);
            }
            for (let index = 16; index < 64; index += 1) {
                const left = words[index - 15];
                const right = words[index - 2];
                const sigma0 = rotateRight(left, 7) ^ rotateRight(left, 18) ^ (left >>> 3);
                const sigma1 = rotateRight(right, 17) ^ rotateRight(right, 19) ^ (right >>> 10);
                words[index] = (words[index - 16] + sigma0 + words[index - 7] + sigma1) >>> 0;
            }
            let [a, b, c, d, e, f, g, h] = hash;
            for (let index = 0; index < 64; index += 1) {
                const upper = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
                const choice = (e & f) ^ (~e & g);
                const temp1 = (h + upper + choice + SHA256_K[index] + words[index]) >>> 0;
                const lower = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
                const majority = (a & b) ^ (a & c) ^ (b & c);
                const temp2 = (lower + majority) >>> 0;
                h = g; g = f; f = e; e = (d + temp1) >>> 0;
                d = c; c = b; b = a; a = (temp1 + temp2) >>> 0;
            }
            [a, b, c, d, e, f, g, h].forEach(function (value, index) {
                hash[index] = (hash[index] + value) >>> 0;
            });
        }
        return Array.from(hash).map(function (value) {
            return value.toString(16).padStart(8, '0');
        }).join('');
    }

    async function sha256(buffer) {
        if (typeof crypto === 'undefined' || !crypto.subtle
            || typeof crypto.subtle.digest !== 'function') return sha256Fallback(buffer);
        const digest = await crypto.subtle.digest('SHA-256', buffer);
        return Array.from(new Uint8Array(digest)).map(function (byte) {
            return byte.toString(16).padStart(2, '0');
        }).join('');
    }

    async function gunzip(buffer) {
        if (typeof DecompressionStream !== 'function') throw new Error('gzip decompression is unavailable');
        const stream = new Blob([buffer]).stream().pipeThrough(new DecompressionStream('gzip'));
        return new Response(stream).arrayBuffer();
    }

    class MotionPlayer {
        constructor() {
            this.manifest = null;
            this.assets = [];
            this.state = {
                posture: 'stand',
                phase: 'boot',
                restAsset: null,
                poseAsset: null,
                poseStyle: null,
                currentAsset: null
            };
            this.queueGeneration = 0;
            this.queue = Promise.resolve();
            this.waiters = new Set();
            this.idleSwitchTimer = null;
            this.idleActivityVersion = 0;
            this.idleScheduleSequence = 0;
            this.lastIdleDelayMs = 0;
            // 外部功能（目前是点歌台）直接占用同一个 VRMA mixer 时，语义动作运行时
            // 必须暂停待机轮换和对话动作，否则仍保持 rest 状态的旧定时器会在
            // 18-38 秒后把正在播放的整曲舞蹈覆盖掉。Map 中的 token 用来防止旧的
            // 异步播放请求在新请求接管后误释放占用。
            this.externalPlaybackOwners = new Map();
            this.busy = false;
            this.sequence = 0;
            this.lastByIntent = new Map();
            this.recentRestIds = [];
            this.savedRestAssets = [];
            this.savedRestAssetIds = new Set();
            this.profile = { energy: 0.5, restraint: 0.5, warmth: 0.5, key: 'default' };
            this.metrics = {
                loaded: 0,
                played: 0,
                skipped: 0,
                failures: 0,
                integrityFailures: 0,
                transitions: 0,
                restEntries: 0,
                idleSchedules: 0,
                idleSwitches: 0,
                externalPlaybackHolds: 0,
                externalPlaybackReleases: 0,
                externalPlaybackBlocks: 0,
                heldAfterAction: 0,
                cancelled: 0
            };
        }

        async load() {
            const manifest = await fetchWithTimeout(MANIFEST_URL, { cache: 'no-store' }, async function (response) {
                if (!response.ok) throw new Error('Motion manifest HTTP ' + response.status);
                return response.json();
            });
            const policy = manifest && manifest.policy || {};
            const localHost = location.hostname === '127.0.0.1' || location.hostname === 'localhost';
            const isPublicRelease = policy.distribution === 'public-release'
                && policy.localTestEnabled === false
                && policy.previewUnrated === false;
            const isLocalTest = policy.distribution === 'local-test-only'
                && policy.localTestEnabled === true
                && policy.previewUnrated === false
                && localHost;
            if (!isPublicRelease && !isLocalTest) {
                throw new Error('Motion pack violates its distribution policy');
            }
            if (!Array.isArray(manifest.assets)) throw new Error('Motion manifest has no assets');
            this.manifest = manifest;
            this.assets = manifest.assets.filter(function (asset) {
                return asset && asset.id && asset.m && asset.f
                    && asset.nameZh && asset.card && asset.card.stableId === asset.id
                    && asset.card.nameZh === asset.nameZh
                    && asset.packedSha && asset.decodedSha
                    && ['none', 'gzip'].includes(asset.compression);
            });
            if (isPublicRelease && manifest.assets.some(function (asset) {
                return asset.ok !== true || asset.license === '?' || !asset.license;
            })) {
                throw new Error('Public motion pack contains an unapproved or unlicensed asset');
            }
            if (this.assets.length !== manifest.counts.files) {
                throw new Error('Motion manifest asset count mismatch');
            }
            return this;
        }

        static normalizeLocale(locale) {
            const raw = String(locale || 'en').trim().replace('_', '-');
            const lower = raw.toLowerCase();
            if (lower === 'zh-cn' || lower === 'zh-hans' || lower.startsWith('zh-hans-')) return 'zh-CN';
            if (lower === 'zh-tw' || lower === 'zh-hk' || lower === 'zh-hant'
                || lower.startsWith('zh-hant-')) return 'zh-TW';
            const base = lower.split('-')[0];
            return ['en', 'ja', 'ko', 'ru', 'es', 'pt'].includes(base) ? base : 'en';
        }

        static sourceName(asset) {
            const sources = asset && Array.isArray(asset.src) ? asset.src : [];
            const source = String(sources[0] || asset && asset.f || asset && asset.id || '');
            const leaf = source.split('/').pop() || source;
            return leaf.replace(/\.vrma(?:\.gz)?$/i, '').trim();
        }

        static localizedName(asset, locale) {
            if (!asset) return '';
            const normalized = MotionPlayer.normalizeLocale(locale);
            const names = asset.names && typeof asset.names === 'object' ? asset.names : {};
            if (typeof names[normalized] === 'string' && names[normalized].trim()) {
                return names[normalized].trim();
            }
            if (normalized === 'zh-CN' && typeof asset.nameZh === 'string' && asset.nameZh.trim()) {
                return asset.nameZh.trim();
            }
            // 动作卡可以先提供中文规范名，再逐步补齐各语言。非中文界面缺少
            // 对应翻译时回退到素材标题，不能显示无关的硬编码中文名称。
            if (typeof names.en === 'string' && names.en.trim()) return names.en.trim();
            const sourceName = MotionPlayer.sourceName(asset);
            if (sourceName) return sourceName;
            return String(asset.nameZh || asset.id || '');
        }

        catalog(locale) {
            return this.assets.filter(function (asset) {
                return asset.disabled !== true
                    && !DISABLED_INTENTS.has(asset.m)
                    && (!asset.card || asset.card.kind !== 'control');
            }).map(function (asset) {
                return {
                    id: asset.id,
                    name: MotionPlayer.localizedName(asset, locale),
                    nameZh: asset.nameZh,
                    filename: MotionPlayer.sourceName(asset),
                    path: assetUrl(asset),
                    url: assetUrl(asset),
                    type: 'vrma',
                    compression: asset.compression,
                    playback: asset.mode,
                    systemMotion: true
                };
            });
        }

        async playAsset(assetId, options) {
            if (!this.manifest) await this.load();
            const settings = options || {};
            const asset = this.assets.find(function (candidate) {
                return candidate.id === assetId;
            });
            if (!asset) throw new Error('Unknown motion asset: ' + assetId);
            const card = asset.card || {};
            const decision = {
                intent: asset.m,
                kind: card.kind || 'manual',
                intensity: asset.i || 2,
                count: 1,
                style: Array.isArray(asset.s) && asset.s.length ? asset.s[0] : '',
                preferredStyles: Array.isArray(asset.s) ? asset.s.slice() : [],
                evidence: {
                    assetId: asset.id,
                    assetExplicit: true,
                    canonicalZh: asset.nameZh || ''
                },
                manualLoop: asset.mode === 'loop',
                scheduleNextRest: settings.scheduleNext !== false
            };
            return this.playPlan([decision], Object.assign({
                seed: 'catalog-preview:' + asset.id + ':' + Date.now()
            }, settings));
        }

        _manager() {
            const manager = window.vrmManager;
            return manager && manager.currentModel && manager.currentModel.vrm
                && typeof manager.playVRMAAnimation === 'function' ? manager : null;
        }

        _requestedIntensity(decision) {
            return Math.max(1, Math.min(3, Math.round(Number(decision && decision.intensity) || 2)));
        }

        _assetIntensity(asset) {
            return Math.max(1, Math.min(3, Math.round(Number(asset && asset.i) || 2)));
        }

        _timeScale(intensity) {
            if (intensity === 1) return 0.9;
            if (intensity === 3) return 1.1;
            return 1;
        }

        _semanticScore(asset, decision) {
            const card = asset.card || {};
            const evidence = decision.evidence || {};
            const text = String(evidence.canonicalZh
                || decision.clause && decision.clause.raw || '');
            const styles = Array.isArray(card.styles) ? card.styles : [];
            const positive = Array.isArray(card.positiveZh) ? card.positiveZh : [];
            const negative = Array.isArray(card.negativeZh) ? card.negativeZh : [];
            const body = Array.isArray(card.body) ? card.body : [];
            const evidenceBody = Array.isArray(evidence.bodyParts) ? evidence.bodyParts : [];
            let score = card.default === true ? 0.5 : 0;

            if (card.nameZh && text === card.nameZh) score += 50;
            else if (card.nameZh && text.includes(card.nameZh)) score += 20;
            positive.forEach(function (term) {
                if (term && text.includes(term)) score += 5 + Math.min(2, term.length * 0.2);
            });
            negative.forEach(function (term) {
                if (term && text.includes(term)) score -= 100;
            });
            if (decision.style) {
                score += styles.includes(decision.style) ? 12 : styles.length ? -3 : 0;
            }
            const preferredStyles = Array.isArray(decision.preferredStyles)
                ? decision.preferredStyles : [];
            preferredStyles.forEach(function (style) {
                if (styles.includes(style)) score += 2;
            });
            evidenceBody.forEach(function (part) {
                if (body.includes(part)) score += 0.75;
            });
            if (decision.emotion && Array.isArray(card.emotions)
                && card.emotions.includes(decision.emotion)) score += 2;
            score -= Math.abs(this._assetIntensity(asset) - this._requestedIntensity(decision)) * 1.2;
            return Number(score.toFixed(3));
        }

        _restStyles() {
            if (this.profile.restraint >= 0.68) return ['breathing', 'neutral', 'composed', 'relaxed'];
            if (this.profile.energy >= 0.68) return ['positive', 'lively', 'neutral'];
            return ['neutral', 'breathing', 'relaxed', 'official'];
        }

        setProfile(profile) {
            this.profile = Object.assign({}, this.profile, profile || {});
        }

        setSavedRestAnimations(urls) {
            const seen = new Set();
            const compressedAssetUrls = new Set(this.assets.map(function (asset) {
                return assetUrl(asset).split(/[?#]/u)[0];
            }).filter(function (url) { return /\.vrma\.gz$/iu.test(url); }));
            const normalizedUrls = (Array.isArray(urls) ? urls : [])
                .map(function (value) { return typeof value === 'string' ? value.trim() : ''; })
                .map(function (value) {
                    const match = value.match(/^\/static\/vrm\/animation\/([^/?#]+)\.vrma(?:[?#]|$)/iu);
                    let bundledName = match && match[1];
                    try {
                        if (bundledName) bundledName = decodeURIComponent(bundledName);
                    } catch (_) {
                        // Keep the original segment when it is not valid URI encoding.
                    }
                    const compressed = value.replace(
                        /^(\/static\/vrm\/[^?#]+)\.vrma(?=([?#]|$))/iu,
                        '$1.vrma.gz'
                    );
                    const compressedPath = compressed.split(/[?#]/u)[0];
                    return COMPRESSED_BUNDLED_REST_NAMES.has(bundledName)
                        || compressedAssetUrls.has(compressedPath) ? compressed : value;
                })
                .filter(function (value) {
                    if (!value || seen.has(value)) return false;
                    seen.add(value);
                    return true;
                });
            this.savedRestAssetIds = new Set();
            const externalUrls = normalizedUrls.filter((url) => {
                const catalogAsset = this.assets.find(function (asset) {
                    return assetUrl(asset) === url;
                });
                if (!catalogAsset) return true;
                if (catalogAsset.m !== 'idle') return true;
                this.savedRestAssetIds.add(catalogAsset.id);
                return false;
            });
            this.savedRestAssets = externalUrls.map(function (url, index) {
                return {
                    id: 'saved_rest_' + stableHash(url) + '_' + index,
                    m: 'idle',
                    in: 'stand',
                    out: 'stand',
                    i: 2,
                    s: ['saved', 'user'],
                    mode: 'loop',
                    url: url,
                    origin: 'user-config',
                    card: { systemRestEligible: true }
                };
            });
            if (this.state.restAsset) {
                const selectedRestStillAvailable = this.savedRestAssetIds.has(this.state.restAsset.id)
                    || this.savedRestAssets.some((asset) => {
                        return asset.id === this.state.restAsset.id
                            || asset.url === this.state.restAsset.url;
                    });
                if (normalizedUrls.length === 0 || !selectedRestStillAvailable) {
                    this.state.restAsset = null;
                }
            }
            return normalizedUrls.length;
        }

        select(decision, seed, posture) {
            if (!decision || !decision.intent || DISABLED_INTENTS.has(decision.intent)) return null;
            const reuseProneSleepBody = decision.intent === 'lie' && decision.style === 'prone';
            let candidates = this.assets.filter(function (asset) {
                const intentMatches = asset.m === decision.intent
                    || (reuseProneSleepBody && asset.m === 'sleep');
                return intentMatches && asset.disabled !== true;
            });
            if (decision.intent === 'idle' && this.savedRestAssets.length) {
                candidates = candidates.concat(this.savedRestAssets);
            }
            if (decision.intent === 'idle' && decision.systemRest === true) {
                const hasSavedRestSelection = this.savedRestAssets.length > 0
                    || this.savedRestAssetIds.size > 0;
                const companionRest = candidates.filter((asset) => {
                    if (hasSavedRestSelection) {
                        return this.savedRestAssetIds.has(asset.id)
                            || asset.origin === 'user-config';
                    }
                    return asset.card && asset.card.systemRestEligible === true;
                });
                if (!companionRest.length) return null;
                candidates = companionRest;
            }
            if (!candidates.length) return null;

            const requestedAssetId = decision.evidence && decision.evidence.assetId;
            if (requestedAssetId) {
                const exact = candidates.filter(function (asset) {
                    return asset.id === requestedAssetId;
                });
                if (exact.length) candidates = exact;
            }

            if (posture && decision.intent === 'recover') {
                const compatibleRecovery = candidates.filter(function (asset) {
                    const inputPose = String(asset.in || 'stand');
                    return inputPose === posture || inputPose === 'low' || inputPose === 'any';
                });
                if (compatibleRecovery.length) candidates = compatibleRecovery;
            } else if (posture && !LOW_POSES.has(decision.intent)) {
                const compatible = candidates.filter(function (asset) {
                    return String(asset.in || 'stand') === posture;
                });
                if (compatible.length) candidates = compatible;
                else return null;
            }

            const explicitSemantic = !!(decision.evidence && decision.evidence.canonicalZh);
            const requestedStyles = decision.intent === 'idle' && decision.systemRest !== true && !explicitSemantic
                ? this._restStyles()
                : Array.isArray(decision.preferredStyles) ? decision.preferredStyles : [];
            if (requestedStyles.length) {
                const styled = candidates.filter(function (asset) {
                    return Array.isArray(asset.s) && asset.s.some(function (style) {
                        return requestedStyles.includes(style);
                    });
                });
                if (styled.length) candidates = styled;
            }

            if ((decision.intent !== 'idle' || explicitSemantic) && candidates.length > 1) {
                const ranked = candidates.map((asset) => {
                    return { asset: asset, score: this._semanticScore(asset, decision) };
                }).sort(function (a, b) {
                    return b.score - a.score || String(a.asset.id).localeCompare(String(b.asset.id));
                });
                const bestScore = ranked[0].score;
                candidates = ranked.filter(function (item) {
                    return item.score === bestScore;
                }).map(function (item) { return item.asset; });
            }

            if (decision.intent === 'idle' && decision.avoidAssetId) {
                const alternate = candidates.filter(function (asset) {
                    return asset.id !== decision.avoidAssetId;
                });
                if (alternate.length) candidates = alternate;
            }

            if (decision.intent === 'idle' && candidates.length > 2 && this.recentRestIds.length) {
                const freshRest = candidates.filter((asset) => !this.recentRestIds.includes(asset.id));
                if (freshRest.length) candidates = freshRest;
            }

            if (decision.intent !== 'idle' && candidates.length > 1) {
                const lastId = this.lastByIntent.get(decision.intent);
                const fresh = candidates.filter(function (asset) { return asset.id !== lastId; });
                if (fresh.length) candidates = fresh;
            }
            const key = String(seed || '') + ':' + decision.intent + ':' + this.profile.key;
            return candidates[stableHash(key) % candidates.length] || null;
        }

        async _assetUrl(asset) {
            // 官方内置动作直接复用 static/vrm/animation，避免在源码和安装包
            // 同时保存 VRMA 与 gzip 副本。外部已授权动作包仍可声明 gzip transport。
            // 无论 transport 为何，解码后的 Blob URL 都只在当前播放期间存在。
            const packed = await fetchWithTimeout(assetUrl(asset), { cache: 'no-cache' }, async function (response) {
                if (!response.ok) throw new Error(asset.id + ' HTTP ' + response.status);
                return response.arrayBuffer();
            });
            const packedBytes = new Uint8Array(packed);
            const isGzipPayload = packedBytes.length >= 2
                && packedBytes[0] === 0x1f
                && packedBytes[1] === 0x8b;
            let decoded = packed;
            if (asset.compression === 'gzip' && isGzipPayload) {
                if (await sha256(packed) !== asset.packedSha) {
                    this.metrics.integrityFailures += 1;
                    throw new Error(asset.id + ' packed SHA-256 mismatch');
                }
                decoded = await gunzip(packed);
            } else if (asset.compression !== 'gzip') {
                if (await sha256(packed) !== asset.packedSha) {
                    this.metrics.integrityFailures += 1;
                    throw new Error(asset.id + ' packed SHA-256 mismatch');
                }
            }
            if (await sha256(decoded) !== asset.decodedSha) {
                this.metrics.integrityFailures += 1;
                throw new Error(asset.id + ' decoded SHA-256 mismatch');
            }
            const url = URL.createObjectURL(new Blob([decoded], { type: 'model/gltf-binary' }));
            this.metrics.loaded += 1;
            return url;
        }

        async _playAsset(asset, generation, options) {
            const requestIsCurrent = () => generation === this.queueGeneration
                && (options.idleActivityVersion === undefined
                    || options.idleActivityVersion === this.idleActivityVersion);
            if (!requestIsCurrent()) return false;
            const manager = this._manager();
            if (!manager) {
                this.metrics.skipped += 1;
                return false;
            }
            let url = null;
            let temporaryUrl = false;
            try {
                if (typeof asset.url === 'string' && asset.url) {
                    url = asset.url;
                } else {
                    url = await this._assetUrl(asset);
                    temporaryUrl = true;
                }
                if (!requestIsCurrent()) return false;
                const played = await manager.playVRMAAnimation(url, {
                    loop: options.loop === true,
                    immediate: options.immediate === true,
                    isIdle: options.idle === true,
                    timeScale: options.timeScale === undefined ? 1 : options.timeScale,
                    fadeDuration: options.fadeDuration === undefined ? DEFAULT_FADE_SECONDS : options.fadeDuration,
                    shouldApply: requestIsCurrent
                });
                if (played !== true || !requestIsCurrent()) return false;
                this.state.currentAsset = asset;
                this.metrics.played += 1;
                if (asset.m !== 'idle') this.lastByIntent.set(asset.m, asset.id);
                emit('neko-motion-playback', {
                    status: 'playing',
                    assetId: asset.id,
                    intent: asset.m,
                    posture: this.state.posture,
                    phase: this.state.phase,
                    requestedIntensity: options.intensity || 2,
                    assetIntensity: this._assetIntensity(asset),
                    repeatIndex: options.repeatIndex || 1,
                    repeatTotal: options.repeatTotal || 1,
                    origin: asset.origin || 'local-test-pack'
                });
                return true;
            } finally {
                // GLTFLoader.loadAsync 已在 playVRMAAnimation 返回前完成读取和解析，
                // 此时撤销临时 URL 不影响当前 AnimationClip 播放。
                if (url && temporaryUrl) URL.revokeObjectURL(url);
            }
        }

        _wait(milliseconds, generation) {
            return new Promise((resolve) => {
                const waiter = { timer: null, resolve: resolve };
                waiter.timer = setTimeout(() => {
                    this.waiters.delete(waiter);
                    resolve(generation === this.queueGeneration);
                }, Math.max(0, milliseconds));
                this.waiters.add(waiter);
            });
        }

        _releaseWaiters() {
            this.waiters.forEach(function (waiter) {
                clearTimeout(waiter.timer);
                waiter.resolve(false);
            });
            this.waiters.clear();
        }

        _tailMilliseconds(asset, timeScale) {
            const raw = Math.max(650, Number(asset.sec || 1.2) * 1000) / Math.max(0.1, timeScale);
            const bounded = Math.min(MAX_TRANSIENT_MS, raw);
            return Math.max(180, bounded - (DEFAULT_FADE_SECONDS * 1000));
        }

        _loopForPose(asset) {
            return asset.mode === 'loop';
        }

        _clearIdleSwitch() {
            if (!this.idleSwitchTimer) return;
            clearTimeout(this.idleSwitchTimer);
            this.idleSwitchTimer = null;
        }

        _scheduleIdleSwitch(generation, seed, mode, allowBusy) {
            this._clearIdleSwitch();
            if (this.externalPlaybackOwners.size > 0
                || generation !== this.queueGeneration || (!allowBusy && this.busy)
                || this.state.posture !== 'stand' || this.state.phase !== 'rest'
                || !this.state.restAsset) return false;

            this.idleScheduleSequence += 1;
            const scheduleSequence = this.idleScheduleSequence;
            const activityVersion = this.idleActivityVersion;
            let minimum = IDLE_SWITCH_MIN_MS;
            let maximum = IDLE_SWITCH_MAX_MS;
            if (this.profile.restraint >= 0.68) {
                minimum = 28000;
                maximum = 38000;
            } else if (this.profile.energy >= 0.68) {
                minimum = 18000;
                maximum = 28000;
            }
            const spread = Math.max(1, maximum - minimum + 1);
            const delay = minimum + (stableHash([
                seed || 'rest', mode || 'idle', scheduleSequence,
                this.profile.key, this.state.restAsset.id
            ].join(':')) % spread);
            this.lastIdleDelayMs = delay;
            this.metrics.idleSchedules += 1;
            this.idleSwitchTimer = setTimeout(() => {
                this.idleSwitchTimer = null;
                if (generation !== this.queueGeneration
                    || activityVersion !== this.idleActivityVersion
                    || this.externalPlaybackOwners.size > 0
                    || this.busy || this.state.posture !== 'stand'
                    || this.state.phase !== 'rest') return;
                this.metrics.idleSwitches += 1;
                void this.enterRest({
                    seed: String(seed || 'rest') + ':idle-switch:' + scheduleSequence,
                    force: true,
                    reselect: true,
                    allowOfficial: false,
                    fadeDuration: IDLE_SWITCH_FADE_SECONDS,
                    scheduleNext: true,
                    idleActivityVersion: activityVersion
                }).catch((error) => {
                    this.metrics.failures += 1;
                    console.warn('[NekoMotion] idle switch failed:', error);
                });
            }, delay);
            return true;
        }

        noteActivity() {
            this.idleActivityVersion += 1;
            this._clearIdleSwitch();
            return true;
        }

        resumeIdleCountdown(seed) {
            return this._scheduleIdleSwitch(
                this.queueGeneration,
                seed || 'activity-finished',
                'activity-finished'
            );
        }

        holdExternalPlayback(owner, options) {
            const settings = options || {};
            const ownerKey = String(owner || 'external');
            const token = Object.prototype.hasOwnProperty.call(settings, 'token')
                ? settings.token : null;

            this.externalPlaybackOwners.set(ownerKey, token);
            this.idleActivityVersion += 1;
            this._clearIdleSwitch();
            this.queueGeneration += 1;
            this._releaseWaiters();
            this.queue = Promise.resolve();
            this.busy = false;
            this.state.phase = 'external';
            this.state.currentAsset = null;
            this.metrics.externalPlaybackHolds += 1;
            emit('neko-motion-playback', {
                status: 'external-held',
                owner: ownerKey
            });
            return true;
        }

        async releaseExternalPlayback(owner, options) {
            const settings = options || {};
            const ownerKey = String(owner || 'external');
            if (!this.externalPlaybackOwners.has(ownerKey)) return false;
            const hasToken = Object.prototype.hasOwnProperty.call(settings, 'token');
            const heldToken = this.externalPlaybackOwners.get(ownerKey);
            if ((hasToken && heldToken !== settings.token)
                || (!hasToken && heldToken !== null)) {
                return false;
            }

            this.externalPlaybackOwners.delete(ownerKey);
            this.metrics.externalPlaybackReleases += 1;
            if (this.externalPlaybackOwners.size > 0) return true;

            this.queueGeneration += 1;
            this._releaseWaiters();
            this.queue = Promise.resolve();
            this.busy = false;
            const generation = this.queueGeneration;
            if (settings.resume === false) {
                this.state.phase = 'boot';
                this.state.currentAsset = null;
                return true;
            }

            return this._resumeBase(
                generation,
                'external-release:' + ownerKey,
                settings.scheduleNext !== false
            );
        }

        async enterRest(options) {
            const settings = options || {};
            this._clearIdleSwitch();
            if (this.externalPlaybackOwners.size > 0) {
                this.metrics.externalPlaybackBlocks += 1;
                return false;
            }
            if (settings.idleActivityVersion === undefined) {
                this.idleActivityVersion += 1;
            } else if (settings.idleActivityVersion !== this.idleActivityVersion) {
                return false;
            }
            if (settings.profile) this.setProfile(settings.profile);
            if (this.state.posture !== 'stand' || (this.busy && settings.force !== true)) return false;
            const requestedRest = settings.assetId && this.assets.concat(this.savedRestAssets).find(function (asset) {
                return asset.id === settings.assetId && asset.m === 'idle';
            });
            if (requestedRest) {
                this.state.restAsset = requestedRest;
            } else if (!this.state.restAsset || settings.reselect === true) {
                const previousRestId = this.state.restAsset && this.state.restAsset.id;
                this.state.restAsset = this.select({
                    intent: 'idle',
                    intensity: this.profile.energy >= 0.68 ? 3 : this.profile.restraint >= 0.68 ? 1 : 2,
                    systemRest: settings.allowOfficial !== true,
                    preferredStyles: this._restStyles(),
                    avoidAssetId: settings.reselect === true ? previousRestId : null
                }, settings.seed || this.profile.key || 'rest', 'stand');
            }
            if (!this.state.restAsset) return false;
            this.state.phase = 'rest';
            const played = await this._playAsset(this.state.restAsset, this.queueGeneration, {
                loop: true,
                idle: true,
                fadeDuration: settings.immediate ? 0
                    : settings.fadeDuration === undefined ? DEFAULT_FADE_SECONDS : settings.fadeDuration,
                immediate: settings.immediate === true,
                idleActivityVersion: settings.idleActivityVersion
            });
            if (played) {
                this.state.poseAsset = null;
                this.state.poseStyle = null;
                this.recentRestIds.push(this.state.restAsset.id);
                if (this.recentRestIds.length > 2) this.recentRestIds.shift();
                this.metrics.restEntries += 1;
                emit('neko-motion-playback', {
                    status: 'settled',
                    posture: 'stand',
                    phase: 'rest',
                    assetId: this.state.restAsset.id
                });
                if (settings.scheduleNext !== false) {
                    this._scheduleIdleSwitch(
                        this.queueGeneration,
                        settings.seed || this.profile.key || 'rest',
                        'rest',
                        true
                    );
                }
            }
            return played;
        }

        async _resumeBase(generation, seed, scheduleNext) {
            if (generation !== this.queueGeneration) return false;
            if (this.externalPlaybackOwners.size > 0) {
                this.metrics.externalPlaybackBlocks += 1;
                return false;
            }
            if (this.state.posture !== 'stand' && this.state.poseAsset) {
                this.state.phase = 'pose';
                const played = await this._playAsset(this.state.poseAsset, generation, {
                    loop: this._loopForPose(this.state.poseAsset),
                    idle: true
                });
                if (played) {
                    emit('neko-motion-playback', {
                        status: 'settled',
                        posture: this.state.posture,
                        phase: 'pose',
                        assetId: this.state.poseAsset.id
                    });
                }
                return played;
            }
            this.state.posture = 'stand';
            // Resume the exact base that preceded the gesture. Selecting another
            // rest clip here makes every short gesture look like two unrelated
            // actions with a pose reset in between.
            return this.enterRest({
                seed: seed,
                force: true,
                reselect: false,
                scheduleNext: scheduleNext !== false
            });
        }

        async _playTransient(asset, decision, generation, options) {
            const settings = options || {};
            const intensity = this._requestedIntensity(decision);
            const timeScale = this._timeScale(intensity);
            this.state.phase = 'transient';
            const played = await this._playAsset(asset, generation, {
                loop: decision.manualLoop === true,
                idle: false,
                intensity: intensity,
                repeatIndex: settings.repeatIndex,
                repeatTotal: settings.repeatTotal,
                timeScale: timeScale
            });
            if (!played) return false;
            if (decision.manualLoop === true) return generation === this.queueGeneration;
            const reachedTail = await this._wait(this._tailMilliseconds(asset, timeScale), generation);
            return reachedTail && generation === this.queueGeneration;
        }

        async _recoverToStand(generation, seed, resume) {
            this._clearIdleSwitch();
            if (this.state.posture === 'stand') {
                if (resume) await this._resumeBase(generation, seed);
                return true;
            }
            const recoveryStyle = this.state.poseStyle || this.state.posture;
            const recoveryDecision = {
                intent: 'recover',
                intensity: 2,
                style: recoveryStyle,
                preferredStyles: [recoveryStyle, this.state.posture],
                evidence: { canonicalZh: '从' + this.state.posture + '姿态起身' }
            };
            const asset = this.select(recoveryDecision, seed + ':recover', this.state.posture);
            if (!asset) return false;
            this.metrics.transitions += 1;
            emit('neko-motion-transition', {
                from: this.state.posture,
                to: 'stand',
                assetId: asset.id
            });
            const finished = await this._playTransient(asset, recoveryDecision, generation, {});
            if (!finished) return false;
            this.state.posture = 'stand';
            this.state.poseAsset = null;
            this.state.poseStyle = null;
            if (resume) await this._resumeBase(generation, seed + ':rest');
            return true;
        }

        async _enterLowPose(decision, generation, seed) {
            this._clearIdleSwitch();
            const target = decision.intent;
            const currentDepth = POSE_DEPTH[this.state.posture] || 0;
            const targetDepth = POSE_DEPTH[target] || 1;
            const requestedStyle = decision.style || null;
            if (this.state.posture === target && this.state.poseAsset
                && (!requestedStyle || requestedStyle === this.state.poseStyle)) {
                // Repeated language evidence confirms the current posture; it
                // must not restart the same clip on every assistant turn.
                this.state.phase = 'pose';
                return true;
            }

            // Moving upward from lying/sleeping to sitting uses the authored
            // recovery clip first. Moving downward (sit -> lie/sleep) never
            // inserts a stand pose in between.
            if (target === 'sit' && currentDepth > targetDepth) {
                const recovered = await this._recoverToStand(generation, seed, false);
                if (!recovered) return false;
            }

            const asset = this.select(decision, seed);
            if (!asset) return false;
            const from = this.state.posture;
            this.state.phase = 'transition';
            this.metrics.transitions += 1;
            emit('neko-motion-transition', { from: from, to: target, assetId: asset.id });
            const played = await this._playAsset(asset, generation, {
                loop: this._loopForPose(asset),
                idle: true,
                intensity: this._requestedIntensity(decision)
            });
            if (!played) return false;
            this.state.posture = target;
            this.state.poseAsset = asset;
            this.state.poseStyle = requestedStyle;
            this.state.phase = 'pose';
            emit('neko-motion-playback', {
                status: 'settled',
                posture: target,
                phase: 'pose',
                assetId: asset.id
            });
            return true;
        }

        async _executeDecision(decision, generation, seed, resumeAfter) {
            if (LOW_POSES.has(decision.intent)) {
                return this._enterLowPose(decision, generation, seed);
            }
            if (decision.intent === 'recover' || decision.intent === 'idle') {
                if (this.state.posture !== 'stand') {
                    const recovered = await this._recoverToStand(generation, seed, false);
                    if (!recovered || generation !== this.queueGeneration) return false;
                }
                return this.enterRest({
                    seed: seed,
                    force: true,
                    reselect: decision.intent === 'idle',
                    allowOfficial: decision.intent === 'idle',
                    assetId: decision.evidence && decision.evidence.assetId,
                    scheduleNext: decision.scheduleNextRest !== false
                });
            }

            const requestedAssetId = decision.evidence && decision.evidence.assetId;
            const requestedAsset = requestedAssetId && this.assets.find(function (asset) {
                return asset.id === requestedAssetId;
            });
            if (requestedAsset && requestedAsset.in === 'sit' && this.state.posture !== 'sit') {
                const entered = await this._enterLowPose({
                    intent: 'sit',
                    kind: 'pose',
                    style: 'upright',
                    intensity: 1,
                    evidence: { canonicalZh: '双脚平放端正地坐' }
                }, generation, seed + ':required-sit');
                if (!entered || generation !== this.queueGeneration) return false;
            } else if (requestedAsset && requestedAsset.in === 'stand'
                && decision.evidence.assetExplicit === true
                && this.state.posture !== 'stand') {
                const recovered = await this._recoverToStand(generation, seed + ':required-stand', false);
                if (!recovered || generation !== this.queueGeneration) return false;
            }

            let asset = this.select(decision, seed, this.state.posture);
            if (!asset && this.state.posture !== 'stand') {
                // A brief expression or conversational gesture must not silently
                // stand a seated/lying companion up merely because the library
                // has no clip authored for the current posture. Explicit pose,
                // recover and large activity decisions still own transitions.
                if (LOW_POSE_PRESERVING_KINDS.has(decision.kind)
                    || LOW_POSE_PRESERVING_INTENTS.has(decision.intent)) {
                    this.metrics.skipped += 1;
                    emit('neko-motion-playback', {
                        status: 'skipped',
                        intent: decision.intent,
                        reason: 'preserve_low_pose',
                        posture: this.state.posture
                    });
                    return false;
                }
                const recovered = await this._recoverToStand(generation, seed, false);
                if (!recovered || generation !== this.queueGeneration) return false;
                asset = this.select(decision, seed, 'stand');
            }
            if (!asset) {
                this.metrics.skipped += 1;
                emit('neko-motion-playback', {
                    status: 'skipped',
                    intent: decision.intent,
                    reason: 'no_compatible_asset',
                    posture: this.state.posture
                });
                return false;
            }

            const repeatTotal = COUNTABLE_INTENTS.has(decision.intent)
                ? Math.max(1, Math.min(3, Math.round(Number(decision.count) || 1)))
                : 1;
            for (let repeatIndex = 1; repeatIndex <= repeatTotal; repeatIndex += 1) {
                if (generation !== this.queueGeneration) return false;
                if (repeatIndex > 1) {
                    asset = this.select(decision, seed + ':repeat:' + repeatIndex, this.state.posture) || asset;
                }
                const finished = await this._playTransient(asset, decision, generation, {
                    repeatIndex: repeatIndex,
                    repeatTotal: repeatTotal
                });
                if (!finished) return false;
            }
            if (resumeAfter && decision.manualLoop !== true && generation === this.queueGeneration) {
                // Crossfade immediately from the authored action tail into the
                // current posture base. Do not freeze the final frame between
                // actions; _resumeBase never inserts a T-pose.
                return this._resumeBase(
                    generation,
                    seed + ':post-action',
                    decision.scheduleNextRest !== false
                );
            }
            return generation === this.queueGeneration;
        }

        beginPlan() {
            this._clearIdleSwitch();
            this.queueGeneration += 1;
            this._releaseWaiters();
            this.queue = Promise.resolve();
            this.busy = false;
            return this.queueGeneration;
        }

        enqueuePlan(plan, context) {
            if (this.externalPlaybackOwners.size > 0) {
                this.metrics.externalPlaybackBlocks += 1;
                return Promise.resolve(false);
            }
            const items = Array.isArray(plan) ? plan.slice(0, 3) : [];
            if (!items.length) return Promise.resolve(false);
            const generation = this.queueGeneration || this.beginPlan();
            const seed = context && context.seed || Date.now();
            this.sequence += 1;
            const task = async () => {
                // hold/cancel 会替换 this.queue，但旧 Promise 已排进微任务队列的 task
                // 仍可能晚到；必须在写 busy 前先验世代，避免把已清零的状态重新置真。
                if (generation !== this.queueGeneration) return false;
                this.busy = true;
                let succeeded = true;
                for (let index = 0; index < items.length; index += 1) {
                    if (generation !== this.queueGeneration) break;
                    try {
                        const completed = await this._executeDecision(
                            items[index],
                            generation,
                            seed + ':' + index,
                            index === items.length - 1
                        );
                        if (completed !== true) succeeded = false;
                    } catch (error) {
                        succeeded = false;
                        this.metrics.failures += 1;
                        console.warn('[NekoMotion] playback failed:', error);
                        emit('neko-motion-playback', {
                            status: 'failed',
                            intent: items[index].intent,
                            error: String(error.message || error)
                        });
                    }
                }
                if (generation === this.queueGeneration) this.busy = false;
                return succeeded && generation === this.queueGeneration;
            };
            this.queue = this.queue.then(task, task);
            return this.queue;
        }

        playPlan(plan, context) {
            if (this.externalPlaybackOwners.size > 0) {
                this.metrics.externalPlaybackBlocks += 1;
                return Promise.resolve(false);
            }
            this.beginPlan();
            return this.enqueuePlan(plan, context);
        }

        cancel(reason, options) {
            const settings = options || {};
            this._clearIdleSwitch();
            this.queueGeneration += 1;
            this._releaseWaiters();
            this.queue = Promise.resolve();
            this.busy = false;
            this.metrics.cancelled += 1;
            const generation = this.queueGeneration;
            // Crossfade back to the already selected base immediately. Stopping
            // the mixer first exposes the model's bind pose, while selecting a
            // new rest clip creates the visible "extra pose" users reported.
            if (settings.resume !== false) {
                void this._resumeBase(generation, 'cancel:' + this.sequence).catch((error) => {
                    this.metrics.failures += 1;
                    console.warn('[NekoMotion] cancel recovery failed:', error);
                });
            } else {
                this.state.posture = 'stand';
                this.state.phase = 'boot';
                this.state.currentAsset = null;
                this.state.restAsset = null;
                this.state.poseAsset = null;
                this.state.poseStyle = null;
            }
            emit('neko-motion-playback', {
                status: 'cancelled',
                reason: String(reason || 'cancel')
            });
            return true;
        }

        stats() {
            return Object.assign({
                posture: this.state.posture,
                phase: this.state.phase,
                currentAsset: this.state.currentAsset && this.state.currentAsset.id || null,
                restAsset: this.state.restAsset && this.state.restAsset.id || null,
                poseAsset: this.state.poseAsset && this.state.poseAsset.id || null,
                poseStyle: this.state.poseStyle,
                busy: this.busy,
                externalPlaybackOwners: Array.from(this.externalPlaybackOwners.keys()),
                assets: this.assets.length,
                cachedAssets: 0,
                idleTimerPending: !!this.idleSwitchTimer,
                nextIdleSwitchInMs: this.idleSwitchTimer ? this.lastIdleDelayMs : 0
            }, this.metrics);
        }
    }

    window.NekoMotionPlayer = MotionPlayer;
})();
