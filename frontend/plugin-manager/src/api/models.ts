import request from '@/utils/request'
import type { ErrorDisplayRequestConfig } from '@/utils/request'
import type { ModelBindings, ModelSlot, ModelSlotInput, ModelSlotTestResult, ModelUsageResult } from '@/types/model-api'

// These screens render errors inline, including authentication and missing-slot errors.
const config: ErrorDisplayRequestConfig = { suppressErrorMessage: true }
const slotsUrl = '/api/model-config/slots'
const slotUrl = (id: string) => `${slotsUrl}/${encodeURIComponent(id)}`
const bindingsUrl = (id: string) => `/api/model-config/plugins/${encodeURIComponent(id)}/bindings`

export function listModelSlots(): Promise<{ schema_version: 1; slots: ModelSlot[] }> {
  return request.get(slotsUrl, config)
}
export function createModelSlot(payload: ModelSlotInput): Promise<ModelSlot> {
  return request.post(slotsUrl, payload, config)
}
export function updateModelSlot(id: string, payload: Partial<ModelSlotInput>): Promise<ModelSlot> {
  return request.patch(slotUrl(id), payload, config)
}
export function deleteModelSlot(id: string): Promise<{ success: boolean }> {
  return request.delete(slotUrl(id), config)
}
export function testModelSlot(id: string): Promise<ModelSlotTestResult> {
  return request.post(`${slotUrl(id)}/test`, undefined, { ...config, timeout: 35000 })
}
export function getModelBindings(pluginId: string): Promise<ModelBindings> {
  return request.get(bindingsUrl(pluginId), config)
}
export function setModelBinding(pluginId: string, usageId: string, slotId: string): Promise<{ plugin_id: string; usage_id: string; slot_id: string }> {
  return request.put(`${bindingsUrl(pluginId)}/${encodeURIComponent(usageId)}`, { slot_id: slotId }, config)
}
export function deleteModelBinding(pluginId: string, usageId: string): Promise<{ success: boolean }> {
  return request.delete(`${bindingsUrl(pluginId)}/${encodeURIComponent(usageId)}`, config)
}
export function getModelUsage(filters: { plugin_id?: string; slot_id?: string; limit?: number } = {}): Promise<ModelUsageResult> {
  return request.get('/api/model-config/usage', { ...config, params: filters })
}
