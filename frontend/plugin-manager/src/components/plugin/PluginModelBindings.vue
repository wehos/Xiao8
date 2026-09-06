<template>
  <section v-if="hasRequirements || error" class="model-bindings" aria-labelledby="model-bindings-title">
    <div class="model-bindings__header">
      <h3 id="model-bindings-title">{{ t('modelBindings.title') }}</h3>
      <div class="model-bindings__actions">
        <router-link :to="{ path: '/model-api', query: { plugin_id: pluginId } }">
          {{ t('modelBindings.manage') }}
        </router-link>
        <el-button size="small" :loading="loading" :disabled="saving" @click="load">
          {{ t('common.refresh') }}
        </el-button>
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="model-bindings__notice" />

    <template v-if="hasRequirements">
      <p class="model-bindings__hint">{{ t('modelBindings.description') }}</p>
      <el-alert
        :title="t(bindings?.ready ? 'modelBindings.ready' : 'modelBindings.notReady')"
        :description="t('modelBindings.readinessHint')"
        :type="bindings?.ready ? 'success' : 'warning'"
        :closable="false"
        show-icon
        class="model-bindings__notice"
      />
      <p v-if="!loading && slots.length === 0" class="model-bindings__hint">{{ t('modelBindings.noSlots') }}</p>

      <div v-for="(requirement, usageId) in bindings?.requirements" :key="usageId" class="model-bindings__row" :data-usage-id="usageId">
        <div class="model-bindings__usage">
          <label :id="`model-usage-${usageId}`" class="model-bindings__label">
            {{ requirement.label }}
          </label>
          <el-tag size="small" :type="requirement.required ? 'warning' : 'info'">
            {{ t(requirement.required ? 'modelBindings.required' : 'modelBindings.optional') }}
          </el-tag>
          <code>{{ usageId }}</code>
          <p v-if="requirement.description" class="model-bindings__description">{{ requirement.description }}</p>
          <div class="model-bindings__capabilities">
            <el-tag v-for="capability in requirement.capabilities" :key="capability" size="small" type="info">
              {{ t(`modelBindings.capabilities.${capability}`) }}
            </el-tag>
          </div>
        </div>

        <div class="model-bindings__selection">
          <el-select
            v-model="selections[String(usageId)]"
            :aria-label="requirement.label"
            :disabled="loading || saving"
            :placeholder="t('modelBindings.unbound')"
            @change="(value: string) => changeBinding(String(usageId), value)"
          >
            <el-option :value="''" :label="t('modelBindings.unbound')" />
            <el-option
              v-for="slot in slots"
              :key="slot.id"
              :value="slot.id"
              :disabled="!compatible(slot, requirement)"
              :label="`${slot.name} · ${slot.model}${compatible(slot, requirement) ? '' : ` (${t('modelBindings.incompatible')})`}`"
            />
          </el-select>
          <span class="model-bindings__status" :class="{ 'model-bindings__status--warning': requirement.status === 'incompatible' }">
            {{ t(`modelBindings.status.${requirement.status}`) }}
          </span>
        </div>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { deleteModelBinding, getModelBindings, listModelSlots, setModelBinding } from '@/api/models'
import type { ModelBindings, ModelRequirement, ModelSlot } from '@/types/model-api'
import { formatHttpError } from '@/utils/request'

const props = defineProps<{ pluginId: string }>()
const { t } = useI18n()
const bindings = ref<ModelBindings | null>(null)
const slots = ref<ModelSlot[]>([])
const selections = ref<Record<string, string>>({})
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const hasRequirements = computed(() => Object.keys(bindings.value?.requirements ?? {}).length > 0)
let generation = 0

function compatible(slot: ModelSlot, requirement: ModelRequirement): boolean {
  return requirement.capabilities.every((capability) => slot.capabilities.includes(capability))
}

async function load(): Promise<void> {
  const revision = ++generation
  const pluginId = props.pluginId
  loading.value = true
  saving.value = false
  error.value = ''
  bindings.value = null
  slots.value = []
  selections.value = {}
  try {
    const result = await getModelBindings(pluginId)
    if (revision !== generation) return
    bindings.value = result
    selections.value = Object.fromEntries(Object.entries(result.requirements).map(([usageId, requirement]) => [usageId, requirement.slot_id ?? '']))
    if (Object.keys(result.requirements).length > 0) {
      const available = await listModelSlots()
      if (revision !== generation) return
      slots.value = available.slots
    }
  } catch (cause) {
    if (revision === generation) error.value = formatHttpError(cause) || t('modelBindings.loadFailed')
  } finally {
    if (revision === generation) loading.value = false
  }
}

async function changeBinding(usageId: string, slotId: string): Promise<void> {
  const current = bindings.value
  const requirement = current?.requirements[usageId]
  if (!current || !requirement || loading.value || saving.value || (requirement.slot_id ?? '') === slotId) return
  const target = slots.value.find((slot) => slot.id === slotId)
  if (slotId && (!target || !compatible(target, requirement))) return
  const revision = generation
  const pluginId = props.pluginId
  saving.value = true
  error.value = ''
  try {
    if (slotId) await setModelBinding(pluginId, usageId, slotId)
    else await deleteModelBinding(pluginId, usageId)
    if (revision !== generation) return
    requirement.slot_id = slotId || null
    requirement.status = slotId ? 'bound' : 'unbound'
    if (slotId) current.bindings[usageId] = slotId
    else delete current.bindings[usageId]
    current.ready = Object.values(current.requirements).every((item) => !item.required || item.status === 'bound')
  } catch (cause) {
    if (revision === generation) {
      selections.value[usageId] = requirement.slot_id ?? ''
      error.value = formatHttpError(cause) || t('modelBindings.saveFailed')
    }
  } finally {
    if (revision === generation) saving.value = false
  }
}

watch(() => props.pluginId, load, { immediate: true })
onBeforeUnmount(() => { generation += 1 })
</script>

<style scoped>
.model-bindings {
  margin-bottom: 28px;
  padding-bottom: 24px;
  border-bottom: 1px solid var(--el-border-color-light);
}
.model-bindings__header, .model-bindings__actions, .model-bindings__usage, .model-bindings__capabilities {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.model-bindings__header {
  justify-content: space-between;
  gap: 12px;
}
.model-bindings__header h3 { margin: 0; }
.model-bindings__actions a { color: var(--el-color-primary); }
.model-bindings__hint, .model-bindings__description { color: var(--el-text-color-secondary); line-height: 1.6; }
.model-bindings__notice { margin-top: 12px; }
.model-bindings__row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(240px, 40%);
  gap: 16px;
  padding: 16px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.model-bindings__label { font-weight: 600; }
.model-bindings__usage code { color: var(--el-text-color-secondary); overflow-wrap: anywhere; }
.model-bindings__description { width: 100%; margin: 0; }
.model-bindings__capabilities { width: 100%; }
.model-bindings__selection { min-width: 0; align-self: center; }
.model-bindings__selection .el-select { width: 100%; }
.model-bindings__status { display: block; margin-top: 6px; color: var(--el-text-color-secondary); font-size: 12px; }
.model-bindings__status--warning { color: var(--el-color-warning); }
@media (max-width: 700px) {
  .model-bindings__row { grid-template-columns: minmax(0, 1fr); }
}
</style>
