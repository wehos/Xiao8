<template>
  <div class="hosted-surface-frame" :style="frameStyle">
    <el-alert
      v-if="runtimeError"
      class="hosted-surface-frame__runtime-alert"
      :type="runtimeErrorFatal ? 'error' : 'warning'"
      show-icon
      :closable="true"
      :title="runtimeErrorTitle"
      :description="runtimeError"
      @close="runtimeError = ''"
    />

    <!-- Preserve standard alert/confirm/prompt behavior authored by static plugins. -->
    <iframe
      v-if="surface.mode === 'static' && surfaceUrl"
      ref="iframeRef"
      :key="iframeKey"
      :src="surfaceUrl"
      :title="surfaceTitle"
      class="hosted-surface-frame__iframe"
      sandbox="allow-scripts allow-forms allow-popups allow-same-origin allow-modals"
      @load="handleLoad"
      @error="handleError"
    />

    <iframe
      v-else-if="(surface.mode === 'hosted-tsx' || surface.mode === 'markdown') && hostedDocument"
      ref="iframeRef"
      :key="iframeKey"
      :srcdoc="hostedDocument"
      :title="surfaceTitle"
      class="hosted-surface-frame__iframe"
      sandbox="allow-scripts"
      @load="handleLoad"
      @error="handleError"
    />

    <div v-else class="hosted-surface-frame__placeholder" :class="{ 'is-unavailable': surface.available === false }">
      <el-icon :size="42" class="hosted-surface-frame__icon">
        <Loading v-if="loading" class="is-loading" />
        <WarningFilled v-else-if="surface.available === false || error" />
        <Document v-else />
      </el-icon>
      <h3>{{ placeholderTitle }}</h3>
      <p>{{ placeholderText }}</p>
      <div class="hosted-surface-frame__meta">
        <el-tag size="small" effect="plain">{{ surface.kind }}</el-tag>
        <el-tag size="small" type="info" effect="plain">{{ surface.mode }}</el-tag>
        <el-tag v-if="surface.entry" size="small" type="success" effect="plain">
          {{ surface.entry }}
        </el-tag>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Document, Loading, WarningFilled } from '@element-plus/icons-vue'
import { callPluginHostedSurfaceAction, getPluginHostedSurfaceContext, getPluginHostedSurfaceSource, parseHostedDocument } from '@/api/plugins'
import { buildHostedTsxDocument } from '@/components/plugin/hosted/tsxRuntime'
import { openExternalUrl, openLocalPath } from '@/utils/openExternal'
import type { PluginUiSurface } from '@/types/api'

const props = withDefaults(defineProps<{
  pluginId: string
  surface: PluginUiSurface
  height?: string
  active?: boolean
  activationRevision?: number
}>(), {
  height: 'clamp(520px, calc(100vh - 220px), 1200px)',
  active: false,
  activationRevision: 0,
})

const emit = defineEmits<{
  load: []
  error: [error: string]
  openLogs: []
  message: [data: unknown]
}>()

const { locale, t } = useI18n()
const iframeRef = ref<HTMLIFrameElement | null>(null)
const iframeKey = ref(0)
const staticSurfaceReady = ref(false)
const pendingStaticSurfaceMessages: unknown[] = []
const maxPendingStaticSurfaceMessages = 100
const hostedDocument = ref('')
const loading = ref(false)
const error = ref('')
const runtimeError = ref('')
const runtimeErrorFatal = ref(false)
let currentLoadId = 0
let hostedRequestGeneration = 0
let componentMounted = false
const hostedDocumentControllers = new Map<string, AbortController>()
const hostedActionControllers = new Map<string, AbortController>()

function abortAllHostedRequests(reason: string) {
  for (const controller of hostedDocumentControllers.values()) controller.abort(reason)
  hostedDocumentControllers.clear()
  for (const controller of hostedActionControllers.values()) controller.abort(reason)
  hostedActionControllers.clear()
}

const HOST_STARTUP_RETRY_DELAYS_MS = [100, 200, 400, 800, 1600] as const
const MAX_HOSTED_DOCUMENT_BYTES = 16 * 1024 * 1024
const HOSTED_DOCUMENT_MIME_BY_EXTENSION = {
  pdf: 'application/pdf',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
} as const

type HostedBridgeError = {
  message: string
  code?: string
  details?: unknown
  status?: number
}

const frameStyle = computed(() => ({
  height: props.height,
  minHeight: props.height,
}))

const surfaceTitle = computed(() => {
  return props.surface.title || props.surface.id || props.pluginId
})

const surfaceUrl = computed(() => {
  const explicitUrl = props.surface.url || props.surface.ui_path
  if (explicitUrl) return explicitUrl
  if (props.surface.mode === 'static') {
    // LEGACY_STATIC_UI_COMPAT:
    // Static surfaces currently use the old /plugin/{id}/ui/ route.
    // Later this URL should come from the unified surface metadata.
    return `/plugin/${encodeURIComponent(props.pluginId)}/ui/`
  }
  return ''
})

// PR #1480 review-fix 1.30: trust boundary for postMessage between this
// component and the embedded iframe. Two iframe modes coexist:
//
//   - ``surface.mode === 'static'``: iframe loads ``surfaceUrl`` (an http(s)
//     URL or a same-origin path). The trusted origin is parsed from that URL
//     and resolved against ``window.location.origin`` for relative paths.
//
//   - ``hosted-tsx`` / ``markdown``: iframe is loaded via ``srcdoc=...``. The
//     spec mandates these iframes report ``event.origin === 'null'`` (an opaque
//     origin), so we accept the literal string ``'null'`` as the trusted
//     origin sentinel for srcdoc iframes.
//
// ``handleMessage`` rejects any message whose ``event.origin`` does not match
// this value, and ``handleHostedRequest`` posts responses with this origin
// rather than ``'*'``. The fallback to ``'*'`` is intentional and only used
// for the srcdoc case where the standard requires ``'*'`` because the child
// is in an opaque origin and cannot be addressed by name; in that branch the
// inbound origin check (combined with ``event.source ===
// iframeRef.value.contentWindow``) is what enforces the trust boundary.
const trustedIframeOrigin = computed(() => {
  if (props.surface.mode === 'static') {
    const url = surfaceUrl.value
    if (!url) return window.location.origin
    try {
      return new URL(url, window.location.origin).origin
    } catch {
      return window.location.origin
    }
  }
  // srcdoc iframes (hosted-tsx / markdown). Per HTML spec the resulting origin
  // is opaque and is reported as the literal string 'null'.
  return 'null'
})

const placeholderTitle = computed(() => {
  if (loading.value) return t('plugins.ui.loading')
  if (error.value) return t('plugins.ui.loadError')
  if (props.surface.available === false) return t('plugins.ui.surfaceUnavailable')
  if (props.surface.mode === 'hosted-tsx') return t('plugins.ui.hostedTsxPending')
  if (props.surface.mode === 'markdown') return t('plugins.ui.markdownPending')
  if (props.surface.mode === 'auto') return t('plugins.ui.autoPending')
  return t('plugins.ui.surfaceUnavailable')
})

const placeholderText = computed(() => {
  if (error.value) return error.value
  if (props.surface.available === false) return t('plugins.ui.surfaceEntryMissing')
  if (props.surface.mode === 'static') return t('plugins.ui.noUI')
  return t('plugins.ui.hostedRuntimePending')
})

const runtimeErrorTitle = computed(() => {
  return runtimeErrorFatal.value ? t('plugins.ui.loadError') : t('plugins.ui.controlError')
})

function escapeHtml(value: string) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function escapeAttribute(value: string) {
  return escapeHtml(value).replace(/'/g, '&#39;')
}

function renderInlineMarkdown(value: string) {
  const escaped = escapeHtml(value)
  return escaped
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_match, label, url) => {
      const safeUrl = escapeAttribute(String(url))
      return `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer">${label}</a>`
    })
}

function renderMarkdownToHtml(markdown: string) {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n')
  const html: string[] = []
  let inCode = false
  let codeLines: string[] = []
  let inList = false
  const closeList = () => {
    if (inList) {
      html.push('</ul>')
      inList = false
    }
  }

  for (const line of lines) {
    const fence = line.match(/^```/)
    if (fence) {
      if (inCode) {
        html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`)
        codeLines = []
        inCode = false
      } else {
        closeList()
        inCode = true
      }
      continue
    }
    if (inCode) {
      codeLines.push(line)
      continue
    }
    if (!line.trim()) {
      closeList()
      continue
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/)
    if (heading) {
      closeList()
      const level = heading[1]?.length || 1
      html.push(`<h${level}>${renderInlineMarkdown(heading[2] || '')}</h${level}>`)
      continue
    }
    const listItem = line.match(/^\s*[-*]\s+(.+)$/)
    if (listItem) {
      if (!inList) {
        html.push('<ul>')
        inList = true
      }
      html.push(`<li>${renderInlineMarkdown(listItem[1] || '')}</li>`)
      continue
    }
    const quote = line.match(/^>\s?(.+)$/)
    if (quote) {
      closeList()
      html.push(`<blockquote>${renderInlineMarkdown(quote[1] || '')}</blockquote>`)
      continue
    }
    closeList()
    html.push(`<p>${renderInlineMarkdown(line)}</p>`)
  }
  closeList()
  if (inCode) {
    html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`)
  }
  return html.join('\n')
}

function readResponseHeader(headers: Record<string, any> | undefined, name: string) {
  if (!headers || typeof headers !== 'object') return ''
  if (typeof headers.get === 'function') {
    const got = headers.get(name)
    if (got !== undefined && got !== null) return String(got)
  }
  const value = headers[name] ?? headers[name.toLowerCase()]
  if (Array.isArray(value)) return value.length > 0 ? String(value[0] || '') : ''
  return typeof value === 'string' ? value : ''
}

function normalizeHostedBridgeError(caught: any): HostedBridgeError {
  const data = caught?.response?.data
  const detail = data?.detail
  const status = typeof caught?.response?.status === 'number' ? caught.response.status : undefined
  let code = readResponseHeader(caught?.response?.headers, 'X-Error-Code')
  let details: unknown
  let message = ''

  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const record = detail as Record<string, any>
    if (!code && typeof record.code === 'string') code = record.code
    if (record.details !== undefined) details = record.details
    if (typeof record.message === 'string') message = record.message
    else if (typeof record.detail === 'string') message = record.detail
  } else if (typeof detail === 'string') {
    message = detail
  }

  if (!code && typeof data?.code === 'string') code = data.code
  if (details === undefined && data?.details !== undefined) details = data.details
  if (!message && typeof data?.message === 'string') message = data.message
  if (!message) message = caught?.message || String(caught)

  return { message, code: code || undefined, details, status }
}

// Inline click-interceptor: the markdown document is loaded into a sandboxed
// iframe (sandbox="allow-scripts", no allow-popups), so `<a target="_blank">`
// generated by renderInlineMarkdown cannot navigate on its own. Route the
// click through the parent via postMessage so HostedSurfaceFrame can hand
// the URL to openExternalUrl — same trapped-webview fix we apply elsewhere
// (frontend/react-neko-chat/src/openExternal.ts, src/utils/openExternal.ts).
const MARKDOWN_LINK_INTERCEPTOR_SCRIPT = `
<script>
(function () {
  document.addEventListener('click', function (event) {
    var el = event.target;
    while (el && el !== document.body) {
      if (el.tagName === 'A' && el.getAttribute('href')) {
        var href = el.getAttribute('href');
        if (/^https?:\\/\\//i.test(href)) {
          event.preventDefault();
          window.parent.postMessage({
            type: 'neko-hosted-surface-open-external',
            payload: { url: href },
          }, '*');
        }
        return;
      }
      el = el.parentNode;
    }
  }, true);
})();
<\/script>`

function buildMarkdownDocument(source: string, title: string) {
  return `<!doctype html>
<html lang="${escapeAttribute(String(locale.value))}">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root { color-scheme: light dark; }
    body { margin: 0; padding: 24px; font: 14px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #1f2937; background: #fff; }
    main { max-width: 880px; margin: 0 auto; }
    h1, h2, h3 { line-height: 1.25; color: #111827; }
    h1 { font-size: 28px; margin: 0 0 20px; }
    h2 { font-size: 22px; margin: 28px 0 12px; }
    h3 { font-size: 17px; margin: 22px 0 10px; }
    p, ul, blockquote, pre { margin: 12px 0; }
    ul { padding-left: 22px; }
    code { padding: 2px 5px; border-radius: 5px; background: #f3f4f6; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    pre { overflow: auto; padding: 14px; border-radius: 10px; background: #111827; color: #f9fafb; }
    pre code { padding: 0; background: transparent; color: inherit; }
    blockquote { padding: 8px 14px; border-left: 4px solid #93c5fd; background: #eff6ff; color: #374151; }
    a { color: #2563eb; }
    @media (prefers-color-scheme: dark) {
      body { color: #e5e7eb; background: #111827; }
      h1, h2, h3 { color: #f9fafb; }
      code { background: #1f2937; }
      blockquote { background: #172554; color: #dbeafe; }
      a { color: #93c5fd; }
    }
  </style>
</head>
<body>
  <main>
    <h1>${escapeHtml(title)}</h1>
    ${renderMarkdownToHtml(source)}
  </main>
  ${MARKDOWN_LINK_INTERCEPTOR_SCRIPT}
</body>
</html>`
}

function handleLoad() {
  if (props.surface.mode === 'static') {
    staticSurfaceReady.value = true
    flushStaticSurfaceMessages()
  }
  postActivation()
  emit('load')
}

function postActivation() {
  if (!props.active || !Number.isSafeInteger(props.activationRevision) || props.activationRevision < 0) return
  const targetOrigin = trustedIframeOrigin.value === 'null' ? '*' : trustedIframeOrigin.value
  iframeRef.value?.contentWindow?.postMessage({
    type: 'neko-hosted-surface-activated',
    payload: {
      surfaceId: props.surface.id,
      revision: props.activationRevision,
    },
  }, targetOrigin)
}

function handleError() {
  loading.value = false
  staticSurfaceReady.value = false
  error.value = t('plugins.ui.loadError')
  emit('error', t('plugins.ui.loadError'))
}

function flushStaticSurfaceMessages() {
  const target = iframeRef.value?.contentWindow
  if (!target || !staticSurfaceReady.value) return
  for (const message of pendingStaticSurfaceMessages.splice(0)) {
    target.postMessage(message, trustedIframeOrigin.value)
  }
}

function sendSurfaceMessage(message: unknown) {
  if (props.surface.mode !== 'static') return
  const target = iframeRef.value?.contentWindow
  if (target && staticSurfaceReady.value) {
    target.postMessage(message, trustedIframeOrigin.value)
    return
  }
  if (pendingStaticSurfaceMessages.length >= maxPendingStaticSurfaceMessages) {
    pendingStaticSurfaceMessages.shift()
  }
  pendingStaticSurfaceMessages.push(message)
}

async function loadHostedTsx() {
  if (!['hosted-tsx', 'markdown'].includes(props.surface.mode) || props.surface.available === false) {
    hostedDocument.value = ''
    error.value = ''
    runtimeError.value = ''
    runtimeErrorFatal.value = false
    loading.value = false
    return
  }

  const loadId = ++currentLoadId
  loading.value = true
  error.value = ''
  runtimeError.value = ''
  runtimeErrorFatal.value = false
  hostedDocument.value = ''
  try {
    const response = await getPluginHostedSurfaceSource(props.pluginId, {
      kind: props.surface.kind,
      id: props.surface.id,
      locale: String(locale.value),
    })
    if (loadId !== currentLoadId) return
    if (props.surface.mode === 'markdown') {
      hostedDocument.value = buildMarkdownDocument(response.source, surfaceTitle.value)
    } else {
      const context = await getPluginHostedSurfaceContext(props.pluginId, {
        kind: props.surface.kind,
        id: props.surface.id,
        locale: String(locale.value),
      })
      if (loadId !== currentLoadId) return
      hostedDocument.value = buildHostedTsxDocument({
        source: response.source,
        dependencies: response.dependencies,
        pluginId: props.pluginId,
        surface: props.surface,
        context,
        locale: String(locale.value),
      })
    }
    iframeKey.value += 1
  } catch (caught: any) {
    if (loadId !== currentLoadId) return
    error.value = normalizeHostedBridgeError(caught).message
    emit('error', error.value)
  } finally {
    if (loadId === currentLoadId) {
      loading.value = false
    }
  }
}

function handleMessage(event: MessageEvent) {
  // PR #1480 review-fix 1.30: enforce the trust boundary on inbound messages.
  // Both checks are required:
  //   - ``event.source`` ensures the message comes from THIS iframe (not from
  //     some other iframe that happens to share an origin).
  //   - ``event.origin`` ensures the iframe has not been redirected to a
  //     third-party origin since it was loaded; without this, a malicious
  //     navigation inside the iframe could let attacker code act as the
  //     plugin.
  if (event.source !== iframeRef.value?.contentWindow) return
  if (event.origin !== trustedIframeOrigin.value) return
  const data = event.data
  if (data && typeof data === 'object' && data.type === 'neko-hosted-surface-error') {
    const message = typeof data.payload?.message === 'string' ? data.payload.message : t('plugins.ui.loadError')
    const fatal = data.payload?.fatal !== false
    runtimeError.value = message
    runtimeErrorFatal.value = fatal
    console.error('[HostedSurfaceFrame] plugin UI error', {
      pluginId: props.pluginId,
      surface: `${props.surface.kind}:${props.surface.id}`,
      fatal,
      scope: data.payload?.scope,
      details: data.payload?.details,
      message,
    })
    if (fatal) error.value = message
    emit('error', message)
    return
  }
  if (data && typeof data === 'object' && data.type === 'neko-hosted-surface-console') {
    const level = typeof data.payload?.level === 'string' ? data.payload.level : 'log'
    const args = Array.isArray(data.payload?.args) ? data.payload.args : []
    const consoleMethod: 'debug' | 'info' | 'warn' | 'error' | 'log' = level === 'debug' || level === 'info' || level === 'warn' || level === 'error' ? level : 'log'
    console[consoleMethod]('[HostedSurfaceFrame] plugin UI console', {
      pluginId: props.pluginId,
      surface: `${props.surface.kind}:${props.surface.id}`,
      args,
      timestamp: data.payload?.timestamp,
    })
    emit('message', data)
    return
  }
  if (data && typeof data === 'object' && data.type === 'neko-hosted-surface-open-logs') {
    emit('openLogs')
    return
  }
  if (data && typeof data === 'object' && data.type === 'neko-hosted-surface-open-external') {
    const url = typeof data.payload?.url === 'string' ? data.payload.url : ''
    if (url) openExternalUrl(url)
    return
  }
  if (data && typeof data === 'object' && data.type === 'neko-hosted-surface-open-path') {
    const path = typeof data.payload?.path === 'string' ? data.payload.path : ''
    if (path) openLocalPath(path).catch(() => {}) // 宿主插件发起的打开请求，失败时静默处理
    return
  }
  if (data && typeof data === 'object' && data.type === 'neko-hosted-surface-cancel') {
    const requestId = typeof data.requestId === 'string' ? data.requestId : ''
    hostedDocumentControllers.get(requestId)?.abort('client-cancelled')
    hostedActionControllers.get(requestId)?.abort('client-cancelled')
    return
  }
  if (data && typeof data === 'object' && data.type === 'neko-hosted-surface-request') {
    handleHostedRequest(data)
    return
  }
  if (data && typeof data === 'object' && typeof data.type === 'string') {
    emit('message', data)
  }
}

async function handleHostedRequest(data: any) {
  const requestId = typeof data.requestId === 'string' ? data.requestId : ''
  const method = typeof data.method === 'string' ? data.method : ''
  const actionId = method === 'call' ? String(data.payload?.actionId || '') : ''
  const userInitiated = (method === 'call' || method === 'parseDocument') && data.userInitiated === true
  const requestGeneration = hostedRequestGeneration
  let responded = false
  const isCurrentRequest = () => componentMounted && requestGeneration === hostedRequestGeneration
  const respond = (payload: Record<string, any>) => {
    if (responded || !isCurrentRequest()) return
    responded = true
    // PR #1480 review-fix 1.30: target the trusted origin instead of '*'.
    // For srcdoc iframes (opaque origin, reported as 'null'), the postMessage
    // spec rejects 'null' as a target; the standard idiom is to use '*' and
    // rely on the source/origin checks in handleMessage to enforce trust.
    const targetOrigin = trustedIframeOrigin.value === 'null' ? '*' : trustedIframeOrigin.value
    iframeRef.value?.contentWindow?.postMessage({
      type: 'neko-hosted-surface-response',
      requestId,
      ...payload,
    }, targetOrigin)
  }
  if (!requestId) return
  try {
    if (method === 'call') {
      const args = data.payload?.args && typeof data.payload.args === 'object' ? data.payload.args : {}
      const timeoutMs = Number(data.timeoutMs)
      const hasRequestDeadline = Number.isFinite(timeoutMs) && timeoutMs > 0
      const requestDeadline = hasRequestDeadline ? Date.now() + timeoutMs : undefined
      const pluginId = props.pluginId
      const surfaceKind = props.surface.kind
      const surfaceId = props.surface.id
      const requestLocale = String(locale.value)
      const controller = new AbortController()
      hostedActionControllers.set(requestId, controller)
      try {
        for (let attempt = 0; ; attempt += 1) {
          try {
            const remainingTimeoutMs = requestDeadline === undefined
              ? undefined
              : Math.max(1, Math.ceil(requestDeadline - Date.now()))
            const result = await callPluginHostedSurfaceAction(pluginId, actionId, args, {
              kind: surfaceKind,
              id: surfaceId,
              locale: requestLocale,
              timeoutMs: remainingTimeoutMs,
              signal: controller.signal,
              userInitiated,
            })
            if (controller.signal.aborted) return
            respond({ ok: true, result })
            return
          } catch (caught: any) {
            if (controller.signal.aborted) return
            const bridgeError = normalizeHostedBridgeError(caught)
            const retryDelayMs = HOST_STARTUP_RETRY_DELAYS_MS[attempt]
            if (userInitiated || bridgeError.code !== 'PLUGIN_NOT_RUNNING' || retryDelayMs === undefined) {
              throw caught
            }
            if (!isCurrentRequest()) return
            if (requestDeadline !== undefined && retryDelayMs >= requestDeadline - Date.now()) {
              throw caught
            }
            await new Promise<void>((resolve) => window.setTimeout(resolve, retryDelayMs))
            if (controller.signal.aborted || !isCurrentRequest()) return
          }
        }
      } finally {
        if (hostedActionControllers.get(requestId) === controller) {
          hostedActionControllers.delete(requestId)
        }
      }
    }
    if (method === 'refresh') {
      const context = await getPluginHostedSurfaceContext(props.pluginId, {
        kind: props.surface.kind,
        id: props.surface.id,
        locale: String(locale.value),
      })
      respond({ ok: true, result: context })
      return
    }
    if (method === 'parseDocument') {
      if (!props.surface.permissions?.includes('document:parse')) {
        respond({
          ok: false,
          error: 'This hosted surface is not allowed to parse documents.',
          code: 'document_parse_permission_denied',
        })
        return
      }
      if (!userInitiated) {
        respond({
          ok: false,
          error: 'Document parsing must be started by a user action.',
          code: 'document_parse_permission_denied',
        })
        return
      }
      const file = data.payload?.file
      if (typeof File === 'undefined' || !(file instanceof File)) {
        respond({ ok: false, error: 'parseDocument requires one File.', code: 'unsupported_document' })
        return
      }
      if (file.size > MAX_HOSTED_DOCUMENT_BYTES) {
        respond({ ok: false, error: 'Document exceeds the 16 MiB upload limit.', code: 'document_too_large' })
        return
      }
      const extension = file.name.split('.').pop()?.toLowerCase() || ''
      const expectedMime = HOSTED_DOCUMENT_MIME_BY_EXTENSION[extension as keyof typeof HOSTED_DOCUMENT_MIME_BY_EXTENSION]
      if (!expectedMime || (file.type && ![expectedMime, 'application/octet-stream'].includes(file.type))) {
        respond({ ok: false, error: 'Only PDF and DOCX documents are supported.', code: 'unsupported_document' })
        return
      }
      const requestedTimeoutMs = Number(data.timeoutMs)
      const timeoutMs = Number.isFinite(requestedTimeoutMs) && requestedTimeoutMs > 0 ? requestedTimeoutMs : 30000
      const controller = new AbortController()
      hostedDocumentControllers.set(requestId, controller)
      const timeoutId = window.setTimeout(() => controller.abort('timeout'), timeoutMs)
      try {
        const parsed = await parseHostedDocument(file, { timeoutMs, signal: controller.signal })
        if (!parsed?.document) throw new Error('Document parser returned an invalid response.')
        respond({ ok: true, result: parsed.document })
      } catch (caught: any) {
        if (controller.signal.aborted && controller.signal.reason === 'timeout') {
          respond({ ok: false, error: 'Document parsing timed out.', code: 'document_parse_timeout' })
          return
        }
        if (controller.signal.aborted) return
        throw caught
      } finally {
        window.clearTimeout(timeoutId)
        if (hostedDocumentControllers.get(requestId) === controller) {
          hostedDocumentControllers.delete(requestId)
        }
      }
      return
    }
    respond({ ok: false, error: `Unsupported hosted surface method: ${method}` })
  } catch (caught: any) {
    const bridgeError = normalizeHostedBridgeError(caught)
    respond({
      ok: false,
      error: bridgeError.message,
      code: bridgeError.code,
      details: {
        surface: `${props.surface.kind}:${props.surface.id}`,
        method,
        actionId: actionId || undefined,
        cause: bridgeError.details,
      },
      status: bridgeError.status,
    })
  }
}

async function refreshContext() {
  if (props.surface.mode !== 'hosted-tsx' || !componentMounted) return
  const requestGeneration = hostedRequestGeneration
  const context = await getPluginHostedSurfaceContext(props.pluginId, {
    kind: props.surface.kind,
    id: props.surface.id,
    locale: String(locale.value),
  })
  if (!componentMounted || requestGeneration !== hostedRequestGeneration) return
  const targetOrigin = trustedIframeOrigin.value === 'null' ? '*' : trustedIframeOrigin.value
  iframeRef.value?.contentWindow?.postMessage({
    type: 'neko-hosted-surface-context',
    context,
  }, targetOrigin)
}

onMounted(() => {
  componentMounted = true
  window.addEventListener('message', handleMessage)
  loadHostedTsx()
})

onUnmounted(() => {
  componentMounted = false
  hostedRequestGeneration += 1
  abortAllHostedRequests('surface-disposed')
  window.removeEventListener('message', handleMessage)
})

watch(
  // Static iframe messages must be queued again only when its document is
  // actually replaced. Locale changes leave a static iframe in place.
  () => [props.pluginId, props.surface.mode, surfaceUrl.value],
  () => {
    staticSurfaceReady.value = false
    pendingStaticSurfaceMessages.length = 0
  },
)

watch(
  () => [
    props.pluginId,
    props.surface.kind,
    props.surface.id,
    props.surface.mode,
    props.surface.entry,
    props.surface.available,
    surfaceUrl.value,
    locale.value,
    props.surface.mode === 'markdown' ? surfaceTitle.value : undefined,
  ],
  () => {
    hostedRequestGeneration += 1
    abortAllHostedRequests('surface-changed')
    if (props.surface.mode === 'static') return
    loadHostedTsx()
  },
)

watch(
  () => [props.active, props.activationRevision] as const,
  () => postActivation(),
)

defineExpose({
  sendSurfaceMessage,
  refreshContext,
})
</script>

<style scoped>
.hosted-surface-frame {
  position: relative;
  width: 100%;
  border: 1px solid color-mix(in srgb, var(--el-border-color) 72%, transparent);
  border-radius: 16px;
  background: color-mix(in srgb, var(--el-bg-color) 92%, transparent);
  overflow: hidden;
}

.hosted-surface-frame__runtime-alert {
  margin: 12px;
}

.hosted-surface-frame__iframe {
  width: 100%;
  height: 100%;
  min-height: inherit;
  border: none;
  display: block;
}

.hosted-surface-frame__placeholder {
  height: 100%;
  min-height: inherit;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 32px;
  text-align: center;
  color: var(--el-text-color-secondary);
}

.hosted-surface-frame__placeholder h3 {
  margin: 0;
  color: var(--el-text-color-primary);
  font-size: 17px;
}

.hosted-surface-frame__placeholder p {
  max-width: 520px;
  margin: 0;
  line-height: 1.7;
}

.hosted-surface-frame__icon {
  color: var(--el-color-primary);
}

.hosted-surface-frame__placeholder.is-unavailable .hosted-surface-frame__icon {
  color: var(--el-color-warning);
}

.hosted-surface-frame__meta {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
}
</style>
