/// <reference types="vite/client" />

// Vite 构建时注入的全局变量
declare const __VITE_PROXY_TARGET_HOSTNAME__: string

declare module 'element-plus/dist/locale/zh-cn.mjs'
declare module 'element-plus/dist/locale/zh-tw.mjs'
declare module 'element-plus/dist/locale/en.mjs'
declare module 'element-plus/dist/locale/ja.mjs'
declare module 'element-plus/dist/locale/ko.mjs'
declare module 'element-plus/dist/locale/ru.mjs'
declare module 'element-plus/dist/locale/es.mjs'
declare module 'element-plus/dist/locale/pt.mjs'

interface NekoWindowControlResult {
  ok?: boolean
  isMaximized?: boolean
  available?: boolean
  pinned?: boolean
}

interface NekoWindowControlApi {
  minimize?: () => Promise<unknown> | unknown
  restore?: () => Promise<unknown> | unknown
  maximize?: () => Promise<NekoWindowControlResult> | NekoWindowControlResult
  isMaximized?: () => Promise<boolean> | boolean
  getPinState?: () => Promise<NekoWindowControlResult> | NekoWindowControlResult
  togglePin?: () => Promise<NekoWindowControlResult> | NekoWindowControlResult
}

interface Window {
  nekoWindowControl?: NekoWindowControlApi
}
