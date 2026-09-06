<template>
  <div class="app-root">
    <div class="window-titlebar">
      <div class="titlebar-left">
        <img src="@/assets/paw.png" alt="" class="titlebar-paw" draggable="false" />
        <span class="titlebar-text">{{ t('app.titleSuffix') }}</span>
      </div>
      <div class="titlebar-controls">
        <button
          v-if="pinAvailable"
          class="titlebar-control titlebar-control--pin"
          :class="{ 'is-pinned': isPinned }"
          type="button"
          :title="pinLabel"
          :aria-label="pinLabel"
          :aria-pressed="isPinned"
          :disabled="pinPending"
          @click="togglePin"
        >
          <span class="titlebar-pin-icon" aria-hidden="true"></span>
        </button>
        <button
          class="titlebar-control"
          type="button"
          :title="t('common.minimize')"
          :aria-label="t('common.minimize')"
          @click="minimizeWindow"
        >
          <span class="titlebar-minimize-icon" aria-hidden="true"></span>
        </button>
        <button
          class="titlebar-control"
          type="button"
          :title="maximizeLabel"
          :aria-label="maximizeLabel"
          @click="toggleMaximize"
        >
          <span class="titlebar-maximize-icon" :class="{ 'is-restored': isMaximized }" aria-hidden="true"></span>
        </button>
        <button
          class="titlebar-control titlebar-control--close"
          type="button"
          :title="t('common.close')"
          :aria-label="t('common.close')"
          @click="closeWindow"
        >
          <svg class="titlebar-close-icon" viewBox="0 0 10 10" fill="none" aria-hidden="true" focusable="false">
            <path d="M1 1L9 9M9 1L1 9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
        </button>
      </div>
    </div>

    <div class="app-shell">
      <aside class="app-sidebar">
        <Sidebar />
      </aside>

      <div class="app-body">
        <div v-if="connectionStore.disconnected" class="connection-banner">
          <div class="connection-banner__inner">
            {{ t('common.disconnected') }}
          </div>
        </div>

        <header class="app-header">
          <Header />
        </header>

        <main class="app-main" data-yui-guide-id="plugin-main">
          <router-view v-slot="{ Component, route: currentRoute }">
            <Transition name="page" mode="out-in">
              <component :is="Component" :key="currentRoute.path" />
            </Transition>
          </router-view>
        </main>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import Sidebar from './Sidebar.vue'
import Header from './Header.vue'
import { useI18n } from 'vue-i18n'
import { useConnectionStore } from '@/stores/connection'

const { t } = useI18n()
const connectionStore = useConnectionStore()
const PLUGIN_MANAGER_BOOT_SHELL_ID = 'plugin-manager-boot-shell'
const PIN_STATE_RETRY_DELAYS_MS = [50, 150, 350, 750]
const isMaximized = ref(false)
const pinAvailable = ref(false)
const isPinned = ref(false)
const pinPending = ref(false)
let pinRequestGeneration = 0
let pinRetryTimer: number | null = null
let pinDisposed = false
const pinLabel = computed(() => isPinned.value ? t('common.unpinWindow') : t('common.pinWindow'))
const maximizeLabel = computed(() => isMaximized.value ? t('common.restore') : t('common.maximize'))

function getWindowControlApi() {
  return window.nekoWindowControl
}

async function refreshMaximizeState() {
  const api = getWindowControlApi()
  if (!api || typeof api.isMaximized !== 'function') return
  try {
    isMaximized.value = !!(await api.isMaximized())
  } catch {
    // 非桌面窗口环境下忽略状态查询失败
  }
}

function applyPinState(state: NekoWindowControlResult | null | undefined) {
  if (!state) {
    pinAvailable.value = false
    isPinned.value = false
    return
  }
  pinAvailable.value = !!state.available
  isPinned.value = !!state.pinned
}

function clearPinStateRetry() {
  if (pinRetryTimer === null) return
  window.clearTimeout(pinRetryTimer)
  pinRetryTimer = null
}

function schedulePinStateRetry(generation: number, retryIndex: number) {
  if (
    pinDisposed
    || generation !== pinRequestGeneration
    || retryIndex >= PIN_STATE_RETRY_DELAYS_MS.length
  ) return
  clearPinStateRetry()
  pinRetryTimer = window.setTimeout(() => {
    pinRetryTimer = null
    if (pinDisposed || generation !== pinRequestGeneration) return
    void refreshPinState({ generation, retryIndex: retryIndex + 1 })
  }, PIN_STATE_RETRY_DELAYS_MS[retryIndex])
}

async function refreshPinState(retryContext?: { generation: number; retryIndex: number }) {
  if (pinPending.value) return
  const isRetry = !!(
    retryContext
    && retryContext.generation === pinRequestGeneration
    && Number.isInteger(retryContext.retryIndex)
  )
  if (!isRetry) clearPinStateRetry()
  const generation = isRetry ? retryContext.generation : ++pinRequestGeneration
  const retryIndex = isRetry ? retryContext.retryIndex : 0
  const api = getWindowControlApi()
  if (!api || typeof api.getPinState !== 'function') {
    if (generation === pinRequestGeneration) applyPinState(null)
    schedulePinStateRetry(generation, retryIndex)
    return
  }
  try {
    const state = await api.getPinState()
    if (pinDisposed || generation !== pinRequestGeneration) return
    applyPinState(state)
    if (!state || !state.available) schedulePinStateRetry(generation, retryIndex)
  } catch {
    if (pinDisposed || generation !== pinRequestGeneration) return
    applyPinState(null)
    schedulePinStateRetry(generation, retryIndex)
  }
}

async function togglePin() {
  const api = getWindowControlApi()
  if (!api || typeof api.togglePin !== 'function') return
  if (pinPending.value) return
  clearPinStateRetry()
  pinPending.value = true
  const generation = ++pinRequestGeneration
  let refreshAfterFailure = false
  try {
    const state = await api.togglePin()
    if (generation === pinRequestGeneration) applyPinState(state)
  } catch {
    refreshAfterFailure = true
  } finally {
    if (generation === pinRequestGeneration) pinPending.value = false
  }
  if (refreshAfterFailure && generation === pinRequestGeneration) await refreshPinState()
}

async function minimizeWindow() {
  const api = getWindowControlApi()
  if (!api || typeof api.minimize !== 'function') return
  try {
    await api.minimize()
  } catch {
    // 非桌面窗口环境下忽略最小化失败
  }
}

async function toggleMaximize() {
  const api = getWindowControlApi()
  if (!api || typeof api.maximize !== 'function') return
  try {
    const result = await api.maximize()
    if (result && result.ok && typeof result.isMaximized === 'boolean') {
      isMaximized.value = result.isMaximized
      return
    }
    await refreshMaximizeState()
  } catch {
    // 非桌面窗口环境下忽略最大化失败
  }
}

function handleWindowResize() {
  void refreshMaximizeState()
}

function handleWindowFocus() {
  void refreshPinState()
}

function closeWindow() {
  window.close()
}

onMounted(() => {
  document.getElementById(PLUGIN_MANAGER_BOOT_SHELL_ID)?.remove()
  void refreshPinState()
  void refreshMaximizeState()
  window.addEventListener('resize', handleWindowResize)
  window.addEventListener('focus', handleWindowFocus)
})

onBeforeUnmount(() => {
  pinDisposed = true
  pinRequestGeneration += 1
  clearPinStateRetry()
  pinPending.value = false
  window.removeEventListener('resize', handleWindowResize)
  window.removeEventListener('focus', handleWindowFocus)
})
</script>

<style scoped>
.app-root {
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 标题栏玻璃效果 */
.window-titlebar {
  padding: 0 6px 0 12px;
  height: 38px;
  min-height: 38px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  -webkit-app-region: drag;
  user-select: none;
  z-index: 9999;
  background:
    linear-gradient(135deg,
      rgba(75, 212, 253, 0.82) 0%,
      rgba(23, 167, 255, 0.78) 50%,
      rgba(91, 141, 239, 0.80) 100%
    );
  backdrop-filter: blur(48px) saturate(180%);
  -webkit-backdrop-filter: blur(48px) saturate(180%);
  border-bottom: 1px solid rgba(255, 255, 255, 0.25);
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.3),
    inset 0 -0.5px 0 rgba(255, 255, 255, 0.12),
    0 1px 6px rgba(23, 120, 200, 0.12);
}

.titlebar-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.titlebar-paw {
  width: 20px;
  height: 16px;
  object-fit: contain;
  filter: brightness(0) invert(1);
  opacity: 0.9;
}

.titlebar-text {
  font-size: 12.5px;
  font-weight: 650;
  color: #fff;
  letter-spacing: 0.5px;
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
}

.titlebar-controls {
  display: flex;
  align-items: center;
  gap: 4px;
  -webkit-app-region: no-drag;
}

.titlebar-control {
  -webkit-app-region: no-drag;
  background: transparent;
  border: none;
  color: rgba(255, 255, 255, 0.75);
  cursor: pointer;
  width: 30px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  transition:
    background 0.18s ease,
    color 0.18s ease,
    box-shadow 0.18s ease;
}

.titlebar-control:hover {
  background: rgba(255, 255, 255, 0.18);
  color: #fff;
}

.titlebar-control:active {
  background: rgba(0, 0, 0, 0.08);
}

.titlebar-control--close:hover {
  background: rgba(255, 255, 255, 0.22);
}

.titlebar-control--pin {
  color: rgba(255, 255, 255, 0.88);
}

.titlebar-control--pin.is-pinned {
  background: linear-gradient(145deg, rgba(255, 255, 255, 0.34), rgba(255, 255, 255, 0.18));
  color: #fff;
  box-shadow:
    inset 0 0 0 1px rgba(255, 255, 255, 0.32),
    0 3px 10px rgba(15, 79, 120, 0.16);
}

.titlebar-control--pin.is-pinned:hover {
  background: linear-gradient(145deg, rgba(255, 255, 255, 0.42), rgba(255, 255, 255, 0.24));
  box-shadow:
    inset 0 0 0 1px rgba(255, 255, 255, 0.4),
    0 4px 12px rgba(15, 79, 120, 0.2);
}

.titlebar-control--pin:focus-visible {
  outline: 2px solid rgba(255, 255, 255, 0.92);
  outline-offset: 2px;
}

.titlebar-pin-icon {
  position: relative;
  display: block;
  width: 16px;
  height: 16px;
  background-color: currentColor;
  -webkit-mask: url("data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2024%2024'%20fill='none'%20stroke='%23000'%20stroke-width='1.8'%20stroke-linecap='round'%20stroke-linejoin='round'%3E%3Cpath%20d='M8%203h8l-1%206%203%204H6l3-4-1-6Z'%2F%3E%3Cpath%20d='M12%2013v8'%2F%3E%3C%2Fsvg%3E") center / contain no-repeat;
  mask: url("data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2024%2024'%20fill='none'%20stroke='%23000'%20stroke-width='1.8'%20stroke-linecap='round'%20stroke-linejoin='round'%3E%3Cpath%20d='M8%203h8l-1%206%203%204H6l3-4-1-6Z'%2F%3E%3Cpath%20d='M12%2013v8'%2F%3E%3C%2Fsvg%3E") center / contain no-repeat;
  transform: rotate(-40deg);
  transform-origin: 50% 68%;
  filter: drop-shadow(0 1px 1px rgba(15, 58, 82, 0.2));
  transition:
    transform 0.24s cubic-bezier(0.22, 1, 0.36, 1),
    filter 0.2s ease;
  will-change: transform;
}

@keyframes neko-plugin-pin-lock {
  0% {
    transform: rotate(-40deg) translateY(0) scale(0.96);
  }
  68% {
    transform: rotate(5deg) translateY(-1px) scale(1.06);
  }
  100% {
    transform: rotate(0deg) translateY(-1px) scale(1);
  }
}

.titlebar-control--pin.is-pinned .titlebar-pin-icon {
  transform: rotate(0deg) translateY(-1px);
  filter: drop-shadow(0 2px 2px rgba(15, 58, 82, 0.26));
  animation: neko-plugin-pin-lock 0.28s cubic-bezier(0.22, 1, 0.36, 1);
}

@media (prefers-reduced-motion: reduce) {
  .titlebar-pin-icon {
    transition: none;
  }

  .titlebar-control--pin.is-pinned .titlebar-pin-icon {
    animation: none;
  }
}

.titlebar-minimize-icon {
  width: 12px;
  height: 1.5px;
  border-radius: 999px;
  background: currentColor;
  transform: translateY(4px);
}

.titlebar-maximize-icon {
  position: relative;
  width: 11px;
  height: 11px;
  border: 1.5px solid currentColor;
  border-radius: 2px;
}

.titlebar-maximize-icon.is-restored {
  transform: translate(1.5px, 1.5px);
}

.titlebar-maximize-icon.is-restored::before {
  content: '';
  position: absolute;
  left: -4px;
  top: -4px;
  width: 11px;
  height: 11px;
  border: 1.5px solid currentColor;
  border-radius: 2px;
}

.titlebar-close-icon {
  width: 10px;
  height: 10px;
}

/* 主体布局 */
.app-shell {
  flex: 1;
  display: flex;
  overflow: hidden;
}

.app-sidebar {
  width: 220px;
  flex-shrink: 0;
  border-right: 1px solid rgba(255, 255, 255, 0.15);
  background: rgba(255, 255, 255, 0.72);
  backdrop-filter: blur(32px) saturate(160%);
  -webkit-backdrop-filter: blur(32px) saturate(160%);
  overflow-y: auto;
}

.app-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
}

.app-header {
  height: 54px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  padding: 0 20px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.18);
  background: rgba(255, 255, 255, 0.65);
  backdrop-filter: blur(32px) saturate(160%);
  -webkit-backdrop-filter: blur(32px) saturate(160%);
  box-shadow:
    inset 0 -0.5px 0 rgba(255, 255, 255, 0.15),
    0 1px 4px rgba(100, 120, 160, 0.04);
}

.app-main {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  background: var(--el-bg-color-page);
}

@media (max-width: 700px) {
  .app-shell { flex-direction: column; }
  .app-sidebar { width: 100%; height: auto; }
  .app-main { padding: 12px; }
}

/* 连接状态提示 */
.connection-banner {
  padding: 8px 20px 0;
}

.connection-banner__inner {
  padding: 8px 14px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--el-color-danger) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--el-color-danger) 20%, var(--el-border-color));
  color: var(--el-color-danger);
  font-size: 13px;
  font-weight: 500;
}

/* 页面切换动画 */
.page-enter-active {
  transition:
    opacity 0.3s cubic-bezier(0.22, 1, 0.36, 1),
    transform 0.34s cubic-bezier(0.22, 1, 0.36, 1),
    filter 0.3s ease;
}

.page-leave-active {
  transition:
    opacity 0.18s ease,
    transform 0.18s ease,
    filter 0.18s ease;
}

.page-enter-from {
  opacity: 0;
  transform: scale(0.98) translateY(8px);
  filter: blur(4px);
}

.page-leave-to {
  opacity: 0;
  transform: scale(0.99) translateY(-4px);
  filter: blur(2px);
}

/* 深色模式覆盖 */
html.dark .window-titlebar {
  background:
    linear-gradient(135deg,
      rgba(50, 50, 72, 0.75) 0%,
      rgba(38, 38, 58, 0.70) 50%,
      rgba(45, 42, 68, 0.72) 100%
    );
  border-bottom-color: rgba(255, 255, 255, 0.08);
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.08),
    inset 0 -0.5px 0 rgba(255, 255, 255, 0.04),
    0 1px 4px rgba(0, 0, 0, 0.2);
}

html.dark .app-sidebar {
  background: rgba(28, 28, 46, 0.78);
  border-right-color: rgba(255, 255, 255, 0.06);
}

html.dark .app-header {
  background: rgba(28, 28, 46, 0.72);
  border-bottom-color: rgba(255, 255, 255, 0.06);
  box-shadow:
    inset 0 -0.5px 0 rgba(255, 255, 255, 0.04),
    0 1px 4px rgba(0, 0, 0, 0.12);
}

@media (prefers-reduced-motion: reduce) {
  .page-enter-active,
  .page-leave-active {
    transition: opacity 0.15s ease;
  }

  .page-enter-from,
  .page-leave-to {
    transform: none;
    filter: none;
  }
}
</style>
