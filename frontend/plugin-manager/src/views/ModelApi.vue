<template>
  <div class="model-api">
    <header class="page-heading">
      <div><h1>{{ t('modelApi.title') }}</h1><p>{{ t('modelApi.subtitle') }}</p></div>
      <el-button type="primary" data-testid="add-slot" @click="editSlot()">{{ t('modelApi.addSlot') }}</el-button>
    </header>

    <el-tabs v-model="activeTab">
      <el-tab-pane :label="t('modelApi.slots')" name="slots">
        <div class="section-heading"><p>{{ t('modelApi.bindingHint') }}</p><el-button :loading="loadingSlots" @click="loadSlots">{{ t('common.refresh') }}</el-button></div>
        <el-alert v-if="slotsError" :title="slotsError" type="error" :closable="false" show-icon />
        <div v-loading="loadingSlots" class="slot-grid">
          <el-empty v-if="!loadingSlots && !slotsError && !slots.length" :description="t('modelApi.noSlots')" />
          <el-card v-for="slot in slots" :key="slot.id" shadow="never" class="slot-card" :data-slot-id="slot.id">
            <template #header>
              <div class="slot-heading"><h2>{{ slot.name }}</h2><el-tag size="small" effect="plain">{{ protocolLabel(slot.protocol) }}</el-tag></div>
            </template>
            <div class="slot-model">{{ slot.model }}</div>
            <p class="endpoint">{{ slot.base_url }}</p>
            <div class="capabilities"><el-tag v-for="capability in slot.capabilities" :key="capability" size="small" type="info">{{ t(`modelApi.capability.${capability}`) }}</el-tag></div>
            <dl class="slot-details">
              <div><dt>{{ t('modelApi.key') }}</dt><dd>{{ t(slot.api_key ? 'modelApi.keySet' : 'modelApi.keyEmpty') }}</dd></div>
              <div><dt>{{ t('modelApi.timeout') }}</dt><dd>{{ t('modelApi.seconds', { count: slot.timeout_seconds }) }}</dd></div>
              <div><dt>{{ t('modelApi.fallback') }}</dt><dd>{{ slot.fallback_slot_id ? slotName(slot.fallback_slot_id) : t('modelApi.noFallback') }}</dd></div>
            </dl>
            <div class="consumers"><span>{{ t('modelApi.usedBy') }}</span>
              <span v-if="!slot.bound_by.length" class="muted">{{ t('modelApi.notBound') }}</span>
              <div v-for="binding in slot.bound_by" :key="`${binding.plugin_id}:${binding.usage_id}`" class="consumer-binding">
                <router-link :to="{ path: `/plugins/${encodeURIComponent(binding.plugin_id)}`, query: { tab: 'config' } }">{{ binding.plugin_id }} / {{ binding.usage_id }}</router-link>
                <el-button link type="danger" size="small" :loading="unbindingId === `${binding.plugin_id}:${binding.usage_id}`" :disabled="!!unbindingId" @click="unbindSlot(binding)">{{ t('modelApi.unbind') }}</el-button>
              </div>
            </div>
            <div class="slot-actions">
              <el-button size="small" :disabled="!!testingId" @click="editSlot(slot)">{{ t('common.edit') }}</el-button>
              <el-button size="small" :loading="testingId === slot.id" :disabled="!!testingId && testingId !== slot.id" @click="testSlot(slot)">{{ t('modelApi.test') }}</el-button>
              <el-tooltip :content="t('modelApi.deleteBlocked')" :disabled="!slotInUse(slot)"><span><el-button size="small" type="danger" plain :disabled="slotInUse(slot) || !!testingId" @click="removeSlot(slot)">{{ t('common.delete') }}</el-button></span></el-tooltip>
            </div>
            <el-alert v-if="testResults[slot.id]" class="test-result" :title="testResults[slot.id]?.message" :type="testResults[slot.id]?.ok ? 'success' : 'error'" :closable="false" show-icon />
          </el-card>
        </div>
        <p class="section-note">{{ t('modelApi.testHint') }}</p>
      </el-tab-pane>

      <el-tab-pane :label="t('modelApi.usage')" name="usage">
        <div class="usage-filters">
          <el-input v-model="pluginFilter" clearable :placeholder="t('modelApi.pluginFilter')" :aria-label="t('modelApi.pluginFilter')" @keyup.enter="loadUsage" />
          <el-select v-model="slotFilter" clearable :placeholder="t('modelApi.allSlots')" :aria-label="t('modelApi.allSlots')"><el-option v-for="slot in slots" :key="slot.id" :label="slot.name" :value="slot.id" /></el-select>
          <el-button :loading="loadingUsage" @click="loadUsage">{{ t('common.refresh') }}</el-button>
        </div>
        <el-alert v-if="usageError" :title="usageError" type="error" :closable="false" show-icon />
        <div v-if="usage" class="usage-summary">
          <div><strong>{{ usage.summary.logical_request_count }}</strong><span>{{ t('modelApi.requests') }}</span></div>
          <div><strong>{{ usage.summary.upstream_attempt_count }}</strong><span>{{ t('modelApi.attempts') }}</span></div>
          <div><strong>{{ knownTokens }}</strong><span>{{ t('modelApi.knownTokens') }}</span></div>
          <div><strong>{{ usage.summary.usage_counts.partial }} / {{ usage.summary.usage_counts.unknown }}</strong><span>{{ t('modelApi.partialUnknown') }}</span></div>
        </div>
        <p class="section-note">{{ t('modelApi.usageHint') }}</p>
        <el-table v-loading="loadingUsage" :data="usage?.requests ?? []" row-key="request_id" :empty-text="t('modelApi.noUsage')">
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="attempt-details">
                <div class="request-id">{{ row.request_id }}</div>
                <div v-if="!row.attempts.length" class="muted">{{ t('modelApi.noAttempt') }}</div>
                <div v-for="attempt in row.attempts" :key="attempt.attempt_id" class="attempt-row">
                  <strong>{{ slotName(attempt.slot_id) }}</strong><span>{{ attempt.model }}</span>
                  <span>{{ t(`modelApi.status.${attempt.status}`) }} · {{ duration(attempt.duration_ms) }}</span>
                  <span>{{ t(`modelApi.usageStatus.${attempt.usage_status}`) }} · {{ t('modelApi.tokenCounts', { input: tokenValue(attempt, 'prompt_tokens'), output: tokenValue(attempt, 'completion_tokens'), total: tokenValue(attempt, 'total_tokens') }) }}</span>
                  <code v-if="attempt.error_code">{{ attempt.error_code }}</code>
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column :label="t('modelApi.time')" min-width="170"><template #default="{ row }">{{ new Date(row.started_at * 1000).toLocaleString() }}</template></el-table-column>
          <el-table-column :label="t('modelApi.consumer')" min-width="180"><template #default="{ row }"><span v-if="row.plugin_id === '@host:model_probe'">{{ t('modelApi.connectionTest') }}</span><span v-else>{{ row.plugin_id }} / {{ row.usage_id }}</span></template></el-table-column>
          <el-table-column :label="t('modelApi.slot')" min-width="160"><template #default="{ row }">{{ slotName(row.slot_id) }}</template></el-table-column>
          <el-table-column :label="t('modelApi.result')" min-width="140"><template #default="{ row }"><el-tag :type="row.status === 'success' ? 'success' : row.status === 'cancelled' ? 'info' : 'danger'" size="small">{{ t(`modelApi.status.${row.status}`) }}</el-tag><div v-if="row.error_code" class="error-code">{{ row.error_code }}</div></template></el-table-column>
          <el-table-column :label="t('modelApi.duration')" width="105"><template #default="{ row }">{{ duration(row.duration_ms) }}</template></el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="editorOpen" :title="t(editingSlot ? 'modelApi.editSlot' : 'modelApi.addSlot')" width="640px" top="5vh" class="model-slot-dialog" :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving" @closed="clearEditor">
      <el-form label-position="top" @submit.prevent="saveSlot">
        <el-alert v-if="formError" :title="formError" type="error" :closable="false" show-icon />
        <el-form-item :label="t('modelApi.name')" required><el-input v-model="form.name" data-testid="slot-name" maxlength="128" :disabled="saving" /></el-form-item>
        <div class="form-pair">
          <el-form-item :label="t('modelApi.protocol')" required><el-select v-model="form.protocol" :disabled="saving"><el-option label="OpenAI Chat Completions" value="openai_chat" /><el-option label="Anthropic Messages" value="anthropic_messages" /></el-select></el-form-item>
          <el-form-item :label="t('modelApi.model')" required><el-input v-model="form.model" data-testid="slot-model" maxlength="256" :disabled="saving" /></el-form-item>
        </div>
        <el-form-item :label="t('modelApi.endpoint')" required><el-input v-model="form.base_url" data-testid="slot-endpoint" :placeholder="form.protocol === 'openai_chat' ? 'https://api.openai.com/v1' : 'https://api.anthropic.com'" :disabled="saving" /><p class="field-hint">{{ t('modelApi.endpointHint') }}</p></el-form-item>
        <el-form-item :label="t('modelApi.key')">
          <el-input v-model="form.api_key" data-testid="slot-key" :type="form.api_key === form.initial_api_key ? 'text' : 'password'" autocomplete="new-password" :placeholder="t('modelApi.keyPlaceholder')" :disabled="saving" @copy="copyMaskedKey" @cut="copyMaskedKey" @focus="selectMaskedKey" />
          <p class="field-hint">{{ t('modelApi.keyEditHint') }}</p>
          <p v-if="keyUpdateRequired" class="field-error">{{ t('modelApi.keyUpdateRequired') }}</p>
        </el-form-item>
        <el-form-item :label="t('modelApi.capabilities')"><el-checkbox-group v-model="form.capabilities" :disabled="saving"><el-checkbox v-for="capability in capabilities" :key="capability" :value="capability" :disabled="capability === 'text'">{{ t(`modelApi.capability.${capability}`) }}</el-checkbox></el-checkbox-group><p class="field-hint">{{ t('modelApi.capabilitiesHint') }}</p></el-form-item>
        <div class="form-pair">
          <el-form-item :label="t('modelApi.temperature')"><el-input-number v-model="form.temperature" :min="0" :max="form.protocol === 'anthropic_messages' ? 1 : 2" :step="0.1" :precision="2" :disabled="saving" :placeholder="t('modelApi.providerDefault')" /></el-form-item>
          <el-form-item :label="t('modelApi.maxTokens')"><el-input-number v-model="form.max_output_tokens" :min="1" :max="1000000" :precision="0" :disabled="saving" placeholder="1024" /></el-form-item>
          <el-form-item :label="t('modelApi.timeout')"><el-input-number v-model="form.timeout_seconds" :min="1" :max="300" :disabled="saving" /><p class="field-hint">{{ t('modelApi.timeoutHint') }}</p></el-form-item>
          <el-form-item :label="t('modelApi.fallback')"><el-select v-model="form.fallback_slot_id" clearable :placeholder="t('modelApi.noFallback')" :disabled="saving"><el-option v-for="slot in fallbackSlots" :key="slot.id" :value="slot.id" :label="slot.name" /></el-select><p class="field-hint">{{ t('modelApi.fallbackHint') }}</p></el-form-item>
        </div>
      </el-form>
      <template #footer><el-button :disabled="saving" @click="editorOpen = false">{{ t('common.cancel') }}</el-button><el-button type="primary" :loading="saving" :disabled="keyUpdateRequired" data-testid="save-slot" @click="saveSlot">{{ t('common.save') }}</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessageBox } from 'element-plus'
import { createModelSlot, deleteModelBinding, deleteModelSlot, getModelUsage, listModelSlots, testModelSlot, updateModelSlot } from '@/api/models'
import { formatHttpError } from '@/utils/request'
import type { ModelCapability, ModelSlot, ModelUsageAttempt, ModelUsageResult } from '@/types/model-api'
import { maskedKeyCopy, modelSlotForm, modelSlotPayload, needsModelKeyUpdate } from './model-api-form'

const { t } = useI18n()
const route = useRoute()
const activeTab = ref(route.query.tab === 'usage' ? 'usage' : 'slots')
const slots = ref<ModelSlot[]>([])
const loadingSlots = ref(false)
const slotsError = ref('')
const usage = ref<ModelUsageResult | null>(null)
const loadingUsage = ref(false)
const usageError = ref('')
const pluginFilter = ref(typeof route.query.plugin_id === 'string' ? route.query.plugin_id : '')
const slotFilter = ref(typeof route.query.slot_id === 'string' ? route.query.slot_id : '')
const editorOpen = ref(false)
const editingSlot = ref<ModelSlot | null>(null)
const form = reactive(modelSlotForm())
const saving = ref(false)
const formError = ref('')
const testingId = ref('')
const unbindingId = ref('')
const testResults = reactive<Record<string, { ok: boolean; message: string }>>({})
const capabilities: ModelCapability[] = ['text', 'image_input', 'tool_calling', 'streaming']
const keyUpdateRequired = computed(() => needsModelKeyUpdate(form, editingSlot.value))
const fallbackSlots = computed(() => slots.value.filter(slot => slot.id !== editingSlot.value?.id && form.capabilities.every(capability => slot.capabilities.includes(capability))))
const knownTokens = computed(() => {
  const summary = usage.value?.summary
  // A partial snapshot may contain only input tokens; an absent total is not zero.
  return summary && (summary.usage_counts.reported > 0 || summary.tokens.total_tokens > 0) ? summary.tokens.total_tokens.toLocaleString() : '—'
})
const errorMessage = (error: unknown) => formatHttpError(error) || t('messages.requestFailed')
const protocolLabel = (protocol: string) => protocol === 'openai_chat' ? 'OpenAI Chat' : 'Anthropic'
const slotName = (id: string) => slots.value.find(slot => slot.id === id)?.name ?? id
const duration = (ms: number) => `${(ms / 1000).toFixed(1)} s`
const tokenValue = (attempt: ModelUsageAttempt, field: 'prompt_tokens' | 'completion_tokens' | 'total_tokens') => attempt.usage_status === 'unknown' ? '—' : (attempt.usage?.[field]?.toLocaleString() ?? '—')
const slotInUse = (slot: ModelSlot) => slot.bound_by.length > 0 || slots.value.some(other => other.fallback_slot_id === slot.id)

async function loadSlots() {
  if (loadingSlots.value) return
  loadingSlots.value = true
  slotsError.value = ''
  try { slots.value = (await listModelSlots()).slots }
  catch (error) { slotsError.value = errorMessage(error) }
  finally { loadingSlots.value = false }
}
async function loadUsage() {
  if (loadingUsage.value) return
  loadingUsage.value = true
  usageError.value = ''
  try { usage.value = await getModelUsage({ plugin_id: pluginFilter.value.trim() || undefined, slot_id: slotFilter.value || undefined, limit: 100 }) }
  catch (error) { usage.value = null; usageError.value = errorMessage(error) }
  finally { loadingUsage.value = false }
}
function editSlot(slot?: ModelSlot) {
  editingSlot.value = slot ?? null
  Object.assign(form, modelSlotForm(slot))
  formError.value = ''
  editorOpen.value = true
}
function clearEditor() {
  // Drop the replacement key from component state as soon as the editor closes.
  Object.assign(form, modelSlotForm())
  editingSlot.value = null
  formError.value = ''
}
function selectMaskedKey(event: FocusEvent) {
  if (form.api_key && form.api_key === form.initial_api_key) (event.target as HTMLInputElement).select()
}
function copyMaskedKey(event: ClipboardEvent) {
  event.preventDefault()
  event.clipboardData?.setData('text/plain', maskedKeyCopy(form.api_key))
}
async function saveSlot() {
  if (saving.value || keyUpdateRequired.value) return
  if (!form.name.trim() || !form.model.trim() || !form.base_url.trim()) { formError.value = t('modelApi.requiredFields'); return }
  if (form.api_key.trim() !== form.initial_api_key && form.api_key.includes('......')) { formError.value = t('modelApi.keyEditHint'); return }
  saving.value = true
  formError.value = ''
  try {
    const payload = modelSlotPayload(form)
    const saved = editingSlot.value ? await updateModelSlot(editingSlot.value.id, payload) : await createModelSlot(payload)
    delete testResults[saved.id]
    editorOpen.value = false
    form.api_key = ''
    await loadSlots()
  } catch (error) { formError.value = errorMessage(error) }
  finally { saving.value = false }
}
async function removeSlot(slot: ModelSlot) {
  try { await ElMessageBox.confirm(t('modelApi.deleteConfirm', { name: slot.name }), t('common.delete'), { type: 'warning', confirmButtonText: t('common.delete'), cancelButtonText: t('common.cancel') }) }
  catch { return }
  try { await deleteModelSlot(slot.id); delete testResults[slot.id]; await loadSlots() }
  catch (error) { slotsError.value = errorMessage(error) }
}
async function unbindSlot(binding: ModelSlot['bound_by'][number]) {
  if (unbindingId.value) return
  unbindingId.value = `${binding.plugin_id}:${binding.usage_id}`
  try {
    try { await ElMessageBox.confirm(t('modelApi.unbindConfirm', { plugin: binding.plugin_id, usage: binding.usage_id }), t('modelApi.unbind'), { type: 'warning', confirmButtonText: t('modelApi.unbind'), cancelButtonText: t('common.cancel') }) }
    catch { return }
    await deleteModelBinding(binding.plugin_id, binding.usage_id)
    await loadSlots()
  } catch (error) { slotsError.value = errorMessage(error) }
  finally { unbindingId.value = '' }
}
async function testSlot(slot: ModelSlot) {
  if (testingId.value) return
  testingId.value = slot.id
  delete testResults[slot.id]
  try {
    const result = await testModelSlot(slot.id)
    testResults[slot.id] = { ok: true, message: t('modelApi.testSuccess', { duration: duration(result.duration_ms) }) }
  } catch (error) { testResults[slot.id] = { ok: false, message: errorMessage(error) } }
  finally { testingId.value = ''; void loadUsage() }
}
onMounted(() => { void loadSlots(); void loadUsage() })
</script>

<style scoped>
.model-api { max-width: 1400px; margin: 0 auto; padding: 24px; }
.page-heading, .section-heading, .slot-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.page-heading { margin-bottom: 22px; }
h1 { margin: 0 0 8px; font-size: 26px; }
h2 { margin: 0; font-size: 17px; overflow-wrap: anywhere; }
p { line-height: 1.6; }
.page-heading p, .section-heading p { margin: 0; color: var(--el-text-color-secondary); }
.section-heading { margin: 6px 0 20px; }
.slot-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 360px), 1fr)); gap: 18px; min-height: 140px; }
.slot-grid > .el-empty { grid-column: 1 / -1; }
.slot-card { border-radius: 12px; }
.slot-heading { align-items: flex-start; }
.slot-model { font-weight: 600; overflow-wrap: anywhere; }
.endpoint { font-size: 12px; color: var(--el-text-color-secondary); overflow-wrap: anywhere; margin: 8px 0 14px; }
.capabilities, .consumers, .slot-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.slot-details { margin: 20px 0; font-size: 13px; }
.slot-details > div { display: flex; justify-content: space-between; gap: 16px; margin: 10px 0; }
dt, .muted, .section-note, .field-hint { color: var(--el-text-color-secondary); }
dd { margin: 0; text-align: right; overflow-wrap: anywhere; }
.consumers { align-items: flex-start; font-size: 12px; border-top: 1px solid var(--el-border-color-lighter); padding-top: 14px; }
.consumers a { color: var(--el-color-primary); overflow-wrap: anywhere; }
.consumer-binding { display: flex; align-items: center; gap: 8px; max-width: 100%; }
.consumer-binding .el-button { flex-shrink: 0; }
.slot-actions { margin-top: 18px; }
.slot-actions .el-button + .el-button { margin-left: 0; }
.test-result { margin-top: 12px; }
.section-note { font-size: 12px; margin: 18px 0; }
.usage-filters { display: flex; gap: 12px; margin: 6px 0 20px; }
.usage-filters .el-input, .usage-filters .el-select { max-width: 260px; }
.usage-summary { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-top: 20px; }
.usage-summary > div { display: flex; flex-direction: column; padding: 20px; border: 1px solid var(--el-border-color-lighter); border-radius: 12px; background: var(--el-fill-color-blank); }
.usage-summary strong { font-size: 25px; margin-bottom: 7px; }
.usage-summary span { font-size: 12px; color: var(--el-text-color-secondary); }
.attempt-details { padding: 12px 24px; }
.request-id { margin-bottom: 12px; font-family: monospace; color: var(--el-text-color-secondary); font-size: 12px; }
.attempt-row { display: flex; flex-wrap: wrap; gap: 10px 18px; padding: 10px 0; font-size: 13px; }
.error-code { overflow-wrap: anywhere; font-size: 11px; margin-top: 4px; }
.form-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 0 20px; }
.form-pair .el-input-number, .form-pair .el-select { width: 100%; }
.field-hint, .field-error { font-size: 12px; margin: 6px 0 0; line-height: 1.5; width: 100%; }
.field-error { color: var(--el-color-danger); }
@media (max-width: 700px) {
  .model-api { padding: 16px; }
  .page-heading, .section-heading { align-items: flex-start; flex-wrap: wrap; }
  .usage-summary { grid-template-columns: 1fr 1fr; }
  .usage-filters { flex-wrap: wrap; }
  .usage-filters .el-input, .usage-filters .el-select { max-width: none; flex: 1 1 180px; }
  .form-pair { grid-template-columns: 1fr; }
}
</style>

<style>
.model-slot-dialog { max-width: calc(100vw - 32px); }
.model-slot-dialog .el-dialog__body { max-height: 70vh; overflow-y: auto; padding-right: 8px; }
</style>
