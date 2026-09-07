<template>
  <div class="log-viewer" data-yui-guide-id="log-viewer">
    <div class="toolbar" data-yui-guide-id="log-viewer-toolbar">
      <el-select v-model="levelFilter" class="toolbar-item level-select" data-yui-guide-id="log-filter-level" :placeholder="$t('logs.allLevels')" clearable>
        <el-option :label="$t('logs.allLevels')" value="" />
        <el-option v-for="level in levels" :key="level" :label="$t(`logLevel.${level}`)" :value="level" />
      </el-select>

      <el-input
        v-model="search"
        class="toolbar-item search-input"
        data-yui-guide-id="log-search"
        clearable
        :placeholder="$t('logs.search')"
        @keyup.enter="refreshLogs"
      />

      <el-input-number v-model="lines" class="toolbar-item lines-input" data-yui-guide-id="log-lines" :min="50" :max="5000" :step="50" />

      <el-button :loading="loading" data-yui-guide-id="log-refresh" @click="refreshLogs">{{ $t('common.refresh') }}</el-button>

      <el-button data-yui-guide-id="log-export" @click="handleExportLog">
        <el-icon><Download /></el-icon>
        {{ $t('logs.exportLog') }}
      </el-button>

      <el-button v-if="canOpenDirectory" data-yui-guide-id="log-open-directory" @click="handleOpenDirectory">
        <el-icon><Folder /></el-icon>
        {{ $t('logs.openLogDirectory') }}
      </el-button>

      <el-switch v-model="autoScroll" data-yui-guide-id="log-auto-scroll" :active-text="$t('logs.autoScroll')" />
    </div>

    <div class="meta-row" data-yui-guide-id="log-meta">
      <el-space wrap>
        <el-tag size="small" data-yui-guide-id="log-connection-status" :type="isConnected ? 'success' : 'warning'">
          {{ isConnected ? $t('logs.connected') : $t('logs.disconnected') }}
        </el-tag>
        <span class="meta-text">{{ $t('logs.totalLogs', { count: filteredLogs.length }) }}</span>
        <span v-if="logFileInfo?.log_file" class="meta-text">{{ $t('logs.logFile') }}: {{ logFileInfo.log_file }}</span>
        <span v-if="typeof logFileInfo?.total_lines === 'number'" class="meta-text">{{ $t('logs.totalLines') }}: {{ logFileInfo.total_lines }}</span>
        <span v-if="typeof logFileInfo?.returned_lines === 'number'" class="meta-text">{{ $t('logs.returnedLines') }}: {{ logFileInfo.returned_lines }}</span>
      </el-space>
    </div>

    <el-alert
      v-if="effectiveError"
      class="error-alert"
      data-yui-guide-id="log-load-error"
      type="warning"
      show-icon
      :closable="false"
      :title="$t('logs.loadError', { error: effectiveError })"
    />

    <div ref="logContainerRef" class="log-list" data-yui-guide-id="log-list">
      <template v-if="filteredLogs.length > 0">
        <div v-for="(log, index) in filteredLogs" :key="`${log.timestamp}-${index}`" class="log-item">
          <span class="log-time">{{ formatTimestamp(log.timestamp) }}</span>
          <el-tag size="small" :type="levelTagType(log.level)" class="log-level">{{ log.level || 'UNKNOWN' }}</el-tag>
          <span class="log-source">{{ log.file }}:{{ log.line }}</span>
          <span class="log-message">{{ log.message }}</span>
        </div>
      </template>
      <el-empty v-else :description="emptyText" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, toRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { Download, Folder } from '@element-plus/icons-vue'
import { useLogsStore } from '@/stores/logs'
import { useLogStream } from '@/composables/useLogStream'
import { getPluginLogDirectory, getPluginLogExportUrl } from '@/api/logs'
import { openLocalPath } from '@/utils/openExternal'
import { API_BASE_URL } from '@/utils/constants'

const props = defineProps<{
  pluginId: string
}>()

const { t } = useI18n()
const logsStore = useLogsStore()
const pluginIdRef = toRef(props, 'pluginId')
const { isConnected } = useLogStream(pluginIdRef)

// 检测是否有桌面桥接（Electron 环境）
// 只有在桌面环境中才能打开本地路径；即使后端是本地的，
// 如果运行在浏览器中也无法调用系统文件管理器。
const hasDesktopBridge = computed(() => {
  const w = window as unknown as {
    nekoHost?: { openPath?: unknown }
    electronShell?: { openPath?: unknown; showItemInFolder?: unknown; openExternal?: unknown }
  }
  return !!(
    (w.nekoHost && typeof w.nekoHost.openPath === 'function') ||
    (w.electronShell && (
      typeof w.electronShell.openPath === 'function' ||
      typeof w.electronShell.showItemInFolder === 'function' ||
      typeof w.electronShell.openExternal === 'function'
    ))
  )
})

// 检测后端是否为本地
// 即使有桌面桥接，如果后端在远程机器上，返回的路径也是远程服务器的绝对路径，
// 客户端无法打开或可能错误打开本地同名路径。
const isLocalBackend = computed(() => {
  const baseUrl = API_BASE_URL.replace(/\/$/, '')

  let hostname: string
  if (!baseUrl) {
    // 空字符串表示通过 Vite 代理。从构建时注入的变量获取代理目标的 hostname。
    // 生产环境中该变量未定义，默认为 'localhost'（但生产环境 baseUrl 不会为空）。
    hostname = (typeof __VITE_PROXY_TARGET_HOSTNAME__ !== 'undefined'
                ? __VITE_PROXY_TARGET_HOSTNAME__
                : 'localhost').toLowerCase()
  } else {
    // 有明确的 API_BASE_URL，解析它来提取 hostname
    try {
      const url = new URL(baseUrl, window.location.origin)
      hostname = url.hostname.toLowerCase()
    } catch {
      // URL 解析失败，视为非本地
      return false
    }
  }

  // 只允许回环地址：localhost, 127.0.0.1, 0.0.0.0, ::1
  // 注意：URL 解析 IPv6 地址时会保留方括号，所以需要检查 [::1]
  return hostname === 'localhost' ||
         hostname === '127.0.0.1' ||
         hostname === '0.0.0.0' ||
         hostname === '::1' ||
         hostname === '[::1]'
})

// 只有同时满足：有桌面桥接 AND 后端是本地时，才能安全打开目录
const canOpenDirectory = computed(() => hasDesktopBridge.value && isLocalBackend.value)

const levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
const levelFilter = ref('')
const search = ref('')
const lines = ref(500)
const autoScroll = ref(true)
const logContainerRef = ref<HTMLElement | null>(null)

const loading = computed(() => logsStore.loading)
const rawLogs = computed(() => logsStore.getLogs(props.pluginId))
const logFileInfo = computed(() => logsStore.getLogFileInfo(props.pluginId))
const effectiveError = computed(() => logFileInfo.value?.error || logsStore.error || '')

const filteredLogs = computed(() => {
  const keyword = search.value.trim().toLowerCase()
  return rawLogs.value.filter((log) => {
    if (levelFilter.value && String(log.level || '').toUpperCase() !== levelFilter.value) {
      return false
    }
    if (!keyword) return true
    const source = `${log.file}:${log.line}`.toLowerCase()
    return (
      String(log.message || '').toLowerCase().includes(keyword) ||
      String(log.level || '').toLowerCase().includes(keyword) ||
      String(log.timestamp || '').toLowerCase().includes(keyword) ||
      source.includes(keyword)
    )
  })
})

const emptyText = computed(() => {
  if (rawLogs.value.length === 0) return t('logs.noLogs')
  return t('logs.noMatches')
})

function formatTimestamp(value: string) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function levelTagType(level: string) {
  const normalized = String(level || '').toUpperCase()
  if (normalized === 'ERROR' || normalized === 'CRITICAL') return 'danger'
  if (normalized === 'WARNING') return 'warning'
  if (normalized === 'DEBUG') return 'info'
  return 'success'
}

async function refreshLogs() {
  if (!props.pluginId) return
  await logsStore.fetchLogs(props.pluginId, {
    lines: lines.value,
    level: levelFilter.value || undefined,
    search: search.value.trim() || undefined
  })
}

async function scrollToBottom() {
  if (!autoScroll.value) return
  await nextTick()
  if (logContainerRef.value) {
    logContainerRef.value.scrollTop = logContainerRef.value.scrollHeight
  }
}

async function handleExportLog() {
  // 在开始导出前捕获 pluginId，防止用户在下载期间切换插件导致文件名错误
  const pluginId = props.pluginId
  let objectUrl = ''
  try {
    // 用 fetch 而不是直接 <a href> 触发下载：后者拿不到响应状态，
    // 服务端返回 404（该插件没有日志）时也会弹成功提示。
    const response = await fetch(getPluginLogExportUrl(pluginId))
    if (!response.ok) {
      ElMessage.error(response.status === 404 ? t('logs.noLogFileToExport') : t('logs.exportFailed'))
      return
    }
    const blob = await response.blob()
    objectUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = objectUrl
    link.download = `${pluginId}_logs.zip`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    ElMessage.success(t('logs.exportSuccess'))
  } catch (error) {
    console.error('Failed to export log:', error)
    ElMessage.error(t('logs.exportFailed'))
  } finally {
    // 延迟撤销 Blob URL，避免浏览器尚未解析 href 时就撤销导致下载失败
    if (objectUrl) {
      const urlToRevoke = objectUrl
      setTimeout(() => URL.revokeObjectURL(urlToRevoke), 0)
    }
  }
}

function handleOpenDirectory() {
  getPluginLogDirectory(props.pluginId)
    .then((response) => {
      if (!response.directory) {
        ElMessage.warning(t('logs.noLogFileToExport'))
        return
      }
      return openLocalPath(response.directory)
    })
    .then(() => {
      // 成功打开目录，不显示任何消息
    })
    .catch((error) => {
      console.error('Failed to open log directory:', error)
      ElMessage.error(t('logs.openDirectoryFailed'))
    })
}

watch(
  () => props.pluginId,
  async (newId) => {
    if (!newId) return
    await refreshLogs()
  },
  { immediate: true }
)

watch(
  () => filteredLogs.value.length,
  async () => {
    await scrollToBottom()
  }
)

onMounted(async () => {
  await scrollToBottom()
})
</script>

<style scoped>
.log-viewer {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.toolbar-item {
  min-width: 140px;
}

.level-select {
  width: 160px;
}

.search-input {
  width: 280px;
}

.lines-input {
  width: 140px;
}

.meta-row {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.meta-text {
  line-height: 20px;
}

.error-alert {
  margin-bottom: 4px;
}

.log-list {
  height: 420px;
  overflow: auto;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
  padding: 8px;
}

.log-item {
  display: grid;
  grid-template-columns: 170px 90px 220px 1fr;
  gap: 8px;
  align-items: center;
  padding: 4px 8px;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.5;
}

.log-item:hover {
  background: var(--el-fill-color-light);
}

.log-time,
.log-source {
  color: var(--el-text-color-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.log-level {
  justify-self: start;
}

.log-message {
  white-space: pre-wrap;
  word-break: break-word;
}

@media (max-width: 900px) {
  .log-item {
    grid-template-columns: 1fr;
    gap: 4px;
  }
}
</style>
