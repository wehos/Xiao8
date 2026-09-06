export type ModelCapability = 'text' | 'image_input' | 'tool_calling' | 'streaming'
export type ModelProtocol = 'openai_chat' | 'anthropic_messages'

export interface ModelSlotInput {
  name: string
  protocol: ModelProtocol
  base_url: string
  model: string
  api_key?: string
  capabilities: ModelCapability[]
  defaults: { temperature: number | null; max_output_tokens: number | null }
  timeout_seconds: number
  fallback_slot_id: string | null
}

export interface ModelSlot extends ModelSlotInput {
  id: string
  api_key: string
  api_key_preview?: string
  bound_by: { plugin_id: string; usage_id: string }[]
}

export interface ModelRequirement {
  label: string
  description: string
  required: boolean
  capabilities: ModelCapability[]
  slot_id: string | null
  status: 'bound' | 'unbound' | 'incompatible'
}

export interface ModelBindings {
  plugin_id: string
  requirements: Record<string, ModelRequirement>
  bindings: Record<string, string>
  ready: boolean
}

export type ModelUsageStatus = 'reported' | 'partial' | 'unknown'
export type ModelRequestStatus = 'success' | 'error' | 'timeout' | 'cancelled'
export interface ModelTokenUsage {
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
  prompt_tokens_details?: { cached_tokens?: number }
}
export interface ModelUsageAttempt {
  attempt_id: string
  slot_id: string
  protocol: ModelProtocol
  model: string
  duration_ms: number
  status: ModelRequestStatus
  error_code: string | null
  upstream_started: boolean
  usage_status: ModelUsageStatus
  usage: ModelTokenUsage | null
}
export interface ModelUsageRequest {
  request_id: string
  plugin_id: string
  usage_id: string
  slot_id: string
  started_at: number
  duration_ms: number
  status: ModelRequestStatus
  error_code: string | null
  attempts: ModelUsageAttempt[]
}
export interface ModelUsageResult {
  requests: ModelUsageRequest[]
  summary: {
    window: 'recent_retained'
    retained_request_count: number
    logical_request_count: number
    upstream_attempt_count: number
    usage_counts: Record<ModelUsageStatus, number>
    status_counts: Record<ModelRequestStatus, number>
    tokens: { prompt_tokens: number; completion_tokens: number; total_tokens: number; cached_tokens: number }
  }
  filters: { plugin_id: string | null; slot_id: string | null }
}
export interface ModelSlotTestResult {
  slot_id: string
  status: 'success'
  duration_ms: number
  usage_status: ModelUsageStatus
  usage: ModelTokenUsage | null
}
