/**
 * 日志相关 API
 */
import { get } from './index'
import { API_BASE_URL } from '@/utils/constants'
import type { LogEntry, LogFile } from '@/types/api'

/**
 * 获取插件日志
 */
export function getPluginLogs(
  pluginId: string,
  params?: {
    lines?: number
    level?: string
    start_time?: string
    end_time?: string
    search?: string
  }
): Promise<{
  plugin_id: string
  logs: LogEntry[]
  total_lines: number
  returned_lines: number
  log_file?: string
  error?: string
}> {
  return get(`/plugin/${encodeURIComponent(pluginId)}/logs`, { params })
}

/**
 * 获取插件日志文件列表
 */
export function getPluginLogFiles(pluginId: string): Promise<{
  plugin_id: string
  log_files: LogFile[]
  count: number
  time: string
}> {
  return get(`/plugin/${encodeURIComponent(pluginId)}/logs/files`)
}

/**
 * 获取插件日志目录路径
 */
export function getPluginLogDirectory(pluginId: string): Promise<{
  plugin_id: string
  directory: string
  time: string
}> {
  return get(`/plugin/${encodeURIComponent(pluginId)}/logs/directory`)
}

/**
 * 导出插件日志文件（返回下载 URL）
 */
export function getPluginLogExportUrl(pluginId: string): string {
  // 移除尾部斜杠避免双斜杠路径
  const baseUrl = API_BASE_URL.replace(/\/$/, '')
  return `${baseUrl}/plugin/${encodeURIComponent(pluginId)}/logs/export`
}

