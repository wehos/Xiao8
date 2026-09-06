// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import PluginModelBindings from './PluginModelBindings.vue'
import type { ModelBindings, ModelSlot } from '@/types/model-api'

const api = vi.hoisted(() => ({
  getModelBindings: vi.fn(),
  listModelSlots: vi.fn(),
  setModelBinding: vi.fn(),
  deleteModelBinding: vi.fn(),
}))

vi.mock('@/api/models', () => api)
vi.mock('@/utils/request', () => ({ formatHttpError: (error: Error) => error.message }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))

const mounted: Array<() => void> = []
const capable: ModelSlot = {
  id: 'slot_saved_id', name: 'Vision', model: 'vision-model', protocol: 'openai_chat',
  base_url: 'https://example.test/v1', api_key: '__NEKO_SECRET_MASKED__',
  capabilities: ['text', 'image_input'], defaults: { temperature: null, max_output_tokens: null },
  timeout_seconds: 60, fallback_slot_id: null, bound_by: [],
}
const textOnly: ModelSlot = { ...capable, id: 'slot_text', name: 'Text', capabilities: ['text'] }

function bindings(pluginId = 'vision_plugin', slotId: string | null = null): ModelBindings {
  return {
    plugin_id: pluginId,
    requirements: {
      vision: {
        label: `${pluginId} vision`, description: 'Describe an image', required: true,
        capabilities: ['text', 'image_input'], slot_id: slotId, status: slotId ? 'bound' : 'unbound',
      },
      optional: {
        label: 'Optional summary', description: '', required: false,
        capabilities: ['text'], slot_id: null, status: 'unbound',
      },
    },
    bindings: slotId ? { vision: slotId } : {},
    ready: Boolean(slotId),
  }
}

async function flush() {
  for (let index = 0; index < 10; index += 1) {
    await Promise.resolve()
    await nextTick()
  }
}

function mountBindings(initialPluginId = 'vision_plugin') {
  const pluginId = ref(initialPluginId)
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp(defineComponent(() => () => h(PluginModelBindings, { pluginId: pluginId.value })))
  app.component('el-button', defineComponent({
    props: { disabled: Boolean, loading: Boolean },
    setup: (props, { slots }) => () => h('button', { disabled: props.disabled || props.loading }, slots.default?.()),
  }))
  app.component('el-tag', defineComponent((_, { slots }) => () => h('span', slots.default?.())))
  app.component('el-alert', defineComponent({
    props: { title: String, description: String },
    setup: (props) => () => h('div', { role: 'status' }, `${props.title} ${props.description ?? ''}`),
  }))
  app.component('router-link', defineComponent({
    props: { to: Object },
    setup: (props, { slots }) => () => h('a', { href: JSON.stringify(props.to) }, slots.default?.()),
  }))
  app.component('el-select', defineComponent({
    props: { modelValue: String, disabled: Boolean },
    emits: ['update:modelValue', 'change'],
    setup: (props, { slots, emit }) => () => h('select', {
      value: props.modelValue, disabled: props.disabled,
      onChange: (event: Event) => {
        const value = (event.target as HTMLSelectElement).value
        emit('update:modelValue', value)
        emit('change', value)
      },
    }, slots.default?.()),
  }))
  app.component('el-option', defineComponent({
    props: { value: String, label: String, disabled: Boolean },
    setup: (props) => () => h('option', { value: props.value, disabled: props.disabled }, props.label),
  }))
  app.mount(host)
  mounted.push(() => { app.unmount(); host.remove() })
  return { host, pluginId }
}

function selectFor(host: HTMLElement, usageId = 'vision'): HTMLSelectElement {
  const select = host.querySelector<HTMLSelectElement>(`[data-usage-id="${usageId}"] select`)
  if (!select) throw new Error(`No selector for ${usageId}`)
  return select
}

function change(select: HTMLSelectElement, value: string) {
  select.value = value
  select.dispatchEvent(new Event('change'))
}

beforeEach(() => {
  vi.resetAllMocks()
  api.getModelBindings.mockResolvedValue(bindings())
  api.listModelSlots.mockResolvedValue({ schema_version: 1, slots: [capable, textOnly] })
  api.setModelBinding.mockResolvedValue({ plugin_id: 'vision_plugin', usage_id: 'vision', slot_id: capable.id })
  api.deleteModelBinding.mockResolvedValue({ success: true })
})

afterEach(() => { mounted.splice(0).forEach((cleanup) => cleanup()) })

describe('PluginModelBindings', () => {
  it('renders nothing and does not fetch slots for plugins with no declared model purposes', async () => {
    api.getModelBindings.mockResolvedValue({ plugin_id: 'plain', requirements: {}, bindings: {}, ready: true })
    const { host } = mountBindings('plain')
    await flush()
    expect(host.querySelector('section')).toBeNull()
    expect(api.listModelSlots).not.toHaveBeenCalled()
  })

  it('disables slots missing required capabilities and saves stable slot IDs, not model names', async () => {
    const { host } = mountBindings()
    await flush()
    const select = selectFor(host)
    const unsupported = select.querySelector<HTMLOptionElement>(`option[value="${textOnly.id}"]`)
    expect(unsupported?.disabled).toBe(true)
    expect(unsupported?.textContent).toContain('modelBindings.incompatible')
    expect(host.textContent).toContain('modelBindings.notReady')
    expect(host.textContent).toContain('modelBindings.required')
    expect(host.textContent).toContain('modelBindings.optional')
    change(select, capable.id)
    await flush()
    expect(api.setModelBinding).toHaveBeenCalledExactlyOnceWith('vision_plugin', 'vision', capable.id)
    expect(selectFor(host).value).toBe(capable.id)
    expect(host.textContent).toContain('modelBindings.ready')
    expect(host.textContent).toContain('modelBindings.readinessHint')
    expect(host.querySelector('a')?.getAttribute('href')).toBe(JSON.stringify({ path: '/model-api', query: { plugin_id: 'vision_plugin' } }))
  })

  it('unbinds independently and marks only required purposes as needed', async () => {
    api.getModelBindings.mockResolvedValue(bindings('vision_plugin', capable.id))
    const { host } = mountBindings()
    await flush()
    change(selectFor(host), '')
    await flush()
    expect(api.deleteModelBinding).toHaveBeenCalledExactlyOnceWith('vision_plugin', 'vision')
    expect(api.setModelBinding).not.toHaveBeenCalled()
    expect(selectFor(host).value).toBe('')
    expect(host.textContent).toContain('modelBindings.notReady')
  })

  it('restores the saved selection and shows an inline error after a rejected change', async () => {
    api.getModelBindings.mockResolvedValue(bindings('vision_plugin', capable.id))
    api.deleteModelBinding.mockRejectedValue(new Error('MODEL_BINDING_REJECTED'))
    const { host } = mountBindings()
    await flush()
    change(selectFor(host), '')
    await flush()
    expect(selectFor(host).value).toBe(capable.id)
    expect(host.textContent).toContain('MODEL_BINDING_REJECTED')
    expect(host.textContent).toContain('modelBindings.ready')
  })

  it('ignores a stale load after navigating to another plugin', async () => {
    let resolveOld!: (value: ModelBindings) => void
    api.getModelBindings.mockImplementation((pluginId: string) => pluginId === 'old'
      ? new Promise<ModelBindings>((resolve) => { resolveOld = resolve })
      : Promise.resolve(bindings(pluginId)))
    const { host, pluginId } = mountBindings('old')
    pluginId.value = 'new'
    await flush()
    resolveOld(bindings('old', capable.id))
    await flush()
    expect(host.textContent).toContain('new vision')
    expect(host.textContent).not.toContain('old vision')
    expect(selectFor(host).value).toBe('')
    expect(api.listModelSlots).toHaveBeenCalledTimes(1)
  })

  it('does not apply an old plugin save result to the new plugin', async () => {
    let resolveSave!: () => void
    api.getModelBindings.mockImplementation((pluginId: string) => Promise.resolve(bindings(pluginId)))
    api.setModelBinding.mockImplementation(() => new Promise<void>((resolve) => { resolveSave = resolve }))
    const { host, pluginId } = mountBindings('old')
    await flush()
    change(selectFor(host), capable.id)
    await flush()
    expect(selectFor(host).disabled).toBe(true)
    pluginId.value = 'new'
    await flush()
    resolveSave()
    await flush()
    expect(api.setModelBinding).toHaveBeenCalledExactlyOnceWith('old', 'vision', capable.id)
    expect(host.textContent).toContain('new vision')
    expect(selectFor(host).value).toBe('')
    expect(selectFor(host).disabled).toBe(false)
  })

  it('allows retrying a failed declaration load', async () => {
    api.getModelBindings.mockRejectedValueOnce(new Error('Temporary load failure'))
    const { host } = mountBindings()
    await flush()
    expect(host.textContent).toContain('Temporary load failure')
    host.querySelector<HTMLButtonElement>('button')?.click()
    await flush()
    expect(host.textContent).not.toContain('Temporary load failure')
    expect(selectFor(host)).toBeDefined()
  })
})
