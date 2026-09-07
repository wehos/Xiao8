// Open a URL in the user's system browser when running inside Electron,
// or fall back to plain window.open (new tab) in real browser contexts.
//
// Why this helper exists: target="_blank" / window.open inside Electron
// opens an embedded Chromium webview that has no close affordance —
// users get trapped. The host preload script exposes window.electronShell
// as the bridge to shell.openExternal in the main process; the same
// convention is used by static/app/app-proactive.js for url-card / meme
// links, and by frontend/react-neko-chat/src/openExternal.ts on the
// chat side. The plugin-manager surface lives in the same Electron host,
// so it needs the same routing.
//
// In browser contexts (e.g. running the manager standalone via `vite dev`)
// the electronShell global is absent and the fallback gives normal
// new-tab behavior; in Electron the IPC bridge dispatches to the system
// browser.
//
// Two safety steps before handing the URL to either path:
//
// 1. Absolutize relative inputs against window.location.href. Callers
//    sometimes pass manager-internal routes (router.resolve().href →
//    "/plugins/foo?tab=ui") expecting "open this in a new tab" — fine
//    for window.open, but shell.openExternal in Electron passes the
//    string straight to ShellExecute / xdg-open which can't resolve
//    relative paths. URL() constructor handles both absolute and
//    relative forms uniformly.
// 2. Whitelist http / https / mailto. shell.openExternal is a known
//    sharp edge (file:// could open arbitrary local content, javascript:
//    is a non-starter, data: is unsupported by most OS handlers) — only
//    schemes a sane new-tab/browser would accept get through.
export function openExternalUrl(url: string): void {
  if (!url) return
  let normalized: URL
  try {
    normalized = new URL(url, window.location.href)
  } catch {
    return
  }
  if (!['http:', 'https:', 'mailto:'].includes(normalized.protocol)) return
  const href = normalized.toString()
  const shell = (window as unknown as {
    electronShell?: { openExternal?: (u: string) => void | Promise<unknown> }
  }).electronShell
  if (shell && typeof shell.openExternal === 'function') {
    // The preload bridge may be backed by ipcRenderer.invoke (Promise<void>)
    // or ipcRenderer.send (void) — we don't control which side it's on.
    // Promise.resolve normalizes both; .catch swallows the unhandled
    // rejection that would otherwise fire if invoke rejects. We deliberately
    // do NOT fall back to window.open here: in Electron context window.open
    // is exactly the trapped-inner-webview behavior this helper exists to
    // avoid, so silently failing is the lesser evil than re-triggering the
    // bug.
    Promise.resolve(shell.openExternal(href)).catch((err) => {
      console.warn('[openExternalUrl] electronShell.openExternal failed:', err)
    })
    return
  }
  window.open(href, '_blank', 'noopener,noreferrer')
}

export function openLocalPath(path: string): Promise<void> {
  const raw = String(path || '').trim()
  if (!isLocalPath(raw)) return Promise.reject(new Error('Not a valid local path'))
  const target = normalizeLocalPath(raw)
  const host = (window as unknown as {
    nekoHost?: { openPath?: (payload: { path: string }) => void | Promise<unknown> }
    electronShell?: {
      openPath?: (path: string) => void | Promise<unknown>
      showItemInFolder?: (path: string) => void | Promise<unknown>
      openExternal?: (url: string) => void | Promise<unknown>
    }
  })
  if (host.nekoHost && typeof host.nekoHost.openPath === 'function') {
    return Promise.resolve(host.nekoHost.openPath({ path: target })).then((result) => {
      // nekoHost.openPath 可能返回 {ok: false, error: ...} 结构化失败
      if (result && typeof result === 'object' && 'ok' in result && result.ok === false) {
        const error = 'error' in result && result.error ? String(result.error) : 'Failed to open path'
        throw new Error(error)
      }
      return undefined
    })
  }
  if (host.electronShell && typeof host.electronShell.openPath === 'function') {
    return Promise.resolve(host.electronShell.openPath(target)).then((result) => {
      // Electron shell.openPath 返回 Promise<string>：空字符串表示成功，非空字符串是错误消息
      if (typeof result === 'string' && result !== '') {
        throw new Error(result)
      }
      return undefined
    })
  }
  if (host.electronShell && typeof host.electronShell.showItemInFolder === 'function') {
    return Promise.resolve(host.electronShell.showItemInFolder(target)).then(() => undefined)
  }
  if (host.electronShell && typeof host.electronShell.openExternal === 'function') {
    return Promise.resolve(host.electronShell.openExternal(localPathToFileUrl(target))).then(() => undefined)
  }
  // 纯浏览器环境：没有桌面桥接
  return Promise.reject(new Error('No desktop bridge available'))
}

function isLocalPath(value: string): boolean {
  if (!value) return false
  if (/^[a-zA-Z]:[\\/]/.test(value)) return true
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(value)) return value.toLowerCase().startsWith('file://')
  return value.startsWith('/') || value.startsWith('~/') || value.startsWith('\\\\')
}

function localPathToFileUrl(value: string): string {
  if (value.toLowerCase().startsWith('file://')) return value
  let normalized = value
  if (normalized.startsWith('\\\\')) {
    const parts = normalized.replace(/^\\\\+/, '').split(/[\\/]+/).filter(Boolean)
    const host = parts.shift()
    if (!host) return value
    return `file://${host}/${encodeFilePathParts(parts)}`
  }
  normalized = normalized.replace(/\\/g, '/')
  if (/^[a-zA-Z]:\//.test(normalized)) normalized = `/${normalized}`
  return `file://${encodeFilePathParts(normalized.split('/'))}`
}

function normalizeLocalPath(value: string): string {
  if (!value.toLowerCase().startsWith('file://')) return value
  try {
    const parsed = new URL(value)
    const pathname = decodeURIComponent(parsed.pathname)
    if (parsed.host && parsed.host !== 'localhost') {
      return `\\\\${parsed.host}${pathname.replace(/\//g, '\\')}`
    }
    if (/^\/[a-zA-Z]:($|\/)/.test(pathname)) {
      return pathname.slice(1).replace(/\//g, '\\')
    }
    return pathname
  } catch {
    return value
  }
}

function encodeFilePathParts(parts: string[]): string {
  return parts.map((part) => (/^[a-zA-Z]:$/.test(part) ? part : encodeURIComponent(part))).join('/')
}
