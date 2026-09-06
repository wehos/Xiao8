// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, markRaw, nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import ElementPlus, { ElMessageBox } from 'element-plus'
import type { MessageBoxData } from 'element-plus'
import ModelApi from './ModelApi.vue'
import { modelApiMessages } from '@/i18n/model-api'
import { maskedKeyCopy, modelSlotForm, modelSlotPayload, needsModelKeyUpdate } from './model-api-form'
import type { ModelSlot, ModelUsageResult } from '@/types/model-api'

const api = vi.hoisted(() => ({ listModelSlots: vi.fn(), createModelSlot: vi.fn(), updateModelSlot: vi.fn(), deleteModelSlot: vi.fn(), deleteModelBinding: vi.fn(), testModelSlot: vi.fn(), getModelUsage: vi.fn() }))
vi.mock('@/api/models', () => api)
vi.mock('@/utils/request', () => ({ formatHttpError: (error: Error) => error.message }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }) }))

const saved: ModelSlot = { id: 'slot_123', name: 'Vision model', protocol: 'openai_chat', base_url: 'https://example.test/v1', model: 'model-a', api_key: '__NEKO_SECRET_MASKED__', api_key_preview: 'sk-abc......wxyz', capabilities: ['text', 'image_input'], defaults: { temperature: null, max_output_tokens: null }, timeout_seconds: 60, fallback_slot_id: null, bound_by: [{ plugin_id: 'example', usage_id: 'vision' }] }
const emptyUsage: ModelUsageResult = { requests: [], filters: { plugin_id: null, slot_id: null }, summary: { window: 'recent_retained', retained_request_count: 0, logical_request_count: 0, upstream_attempt_count: 0, usage_counts: { reported: 0, partial: 0, unknown: 0 }, status_counts: { success: 0, error: 0, timeout: 0, cancelled: 0 }, tokens: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cached_tokens: 0 } } }
let cleanup: (() => void) | undefined
const NativeMutationObserver = globalThis.MutationObserver
async function flush() { for (let i = 0; i < 5; i++) { await Promise.resolve(); await nextTick() } }
async function mount() {
  const container = document.createElement('div')
  document.body.append(container)
  const app = createApp(ModelApi)
  app.use(ElementPlus)
  app.use(createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': { modelApi: modelApiMessages['en-US'], common: { edit: 'Edit', save: 'Save', cancel: 'Cancel', delete: 'Delete', refresh: 'Refresh' } } } }))
  app.component('RouterLink', defineComponent({ props: ['to'], setup: (_, { slots }) => () => h('a', slots.default?.()) }))
  app.mount(container)
  cleanup = () => { app.unmount(); container.remove() }
  await flush()
  return container
}
async function clickText(text: string) {
  const button = [...document.querySelectorAll('button')].find(element => element.textContent?.trim() === text)
  expect(button, `Missing button ${text}`).toBeTruthy()
  button!.click()
  await flush()
}
function input(testId: string): HTMLInputElement { return document.querySelector(`[data-testid="${testId}"] input, input[data-testid="${testId}"]`) as HTMLInputElement }
async function fill(testId: string, value: string) { const element = input(testId); element.value = value; element.dispatchEvent(new Event('input', { bubbles: true })); await flush() }

beforeEach(() => {
  // Element Plus stores observers in reactive state; happy-dom's private fields cannot be proxied.
  vi.stubGlobal('MutationObserver', class extends NativeMutationObserver {
    constructor(callback: MutationCallback) { super(callback); markRaw(this) }
  })
  vi.clearAllMocks()
  api.listModelSlots.mockResolvedValue({ schema_version: 1, slots: [structuredClone(saved)] })
  api.getModelUsage.mockResolvedValue(structuredClone(emptyUsage))
  api.updateModelSlot.mockResolvedValue(saved)
  api.createModelSlot.mockResolvedValue(saved)
  api.testModelSlot.mockResolvedValue({ slot_id: saved.id, status: 'success', duration_ms: 20, usage_status: 'unknown', usage: null })
})
afterEach(() => { cleanup?.(); cleanup = undefined; document.body.innerHTML = ''; vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('model slot credentials', () => {
  it('keeps the stored credential out of form inputs and update payloads', () => {
    const form = modelSlotForm(saved)
    expect(form.api_key).toBe('sk-abc......wxyz')
    expect(modelSlotPayload(form)).not.toHaveProperty('api_key')
    expect(JSON.stringify(modelSlotPayload(form))).not.toContain('__NEKO_SECRET_MASKED__')
  })
  it('requires explicit credential choice when changing the destination or protocol', () => {
    const form = modelSlotForm(saved)
    form.base_url = 'https://other.test/v1'
    expect(needsModelKeyUpdate(form, saved)).toBe(true)
    form.api_key = ''
    expect(needsModelKeyUpdate(form, saved)).toBe(false)
    expect(modelSlotPayload(form).api_key).toBe('')
    form.api_key = ' new-secret '
    expect(modelSlotPayload(form).api_key).toBe('new-secret')
    form.api_key = form.initial_api_key; form.base_url = saved.base_url; form.protocol = 'anthropic_messages'
    expect(needsModelKeyUpdate(form, saved)).toBe(true)
  })
  it('accepts a normalized equivalent endpoint and preserves explicit zero temperature', () => {
    const form = modelSlotForm(saved)
    form.base_url = 'https://EXAMPLE.test:443/v1/'
    form.temperature = 0
    expect(needsModelKeyUpdate(form, saved)).toBe(false)
    expect(modelSlotPayload(form).defaults).toEqual({ temperature: 0, max_output_tokens: null })
  })
})

describe('Plugin API page', () => {
  it('loads slots and usage without calling a model or mutating configuration', async () => {
    const container = await mount()
    expect(container.textContent).toContain('Vision model')
    expect(container.textContent).toContain('example / vision')
    expect(container.textContent).not.toContain('__NEKO_SECRET_MASKED__')
    expect(api.testModelSlot).not.toHaveBeenCalled()
    expect(api.createModelSlot).not.toHaveBeenCalled()
    expect(container.querySelector('.usage-summary strong')?.textContent).toBe('0')
    expect(container.querySelectorAll('.usage-summary strong')[2]?.textContent).toBe('—')
  })
  it('renames the same slot while preserving the saved key and bindings', async () => {
    await mount(); await clickText('Edit')
    expect(input('slot-key').value).toBe('sk-abc......wxyz')
    await fill('slot-name', 'Renamed')
    await clickText('Save')
    expect(api.updateModelSlot).toHaveBeenCalledWith(saved.id, expect.objectContaining({ name: 'Renamed' }))
    expect(api.updateModelSlot.mock.calls[0]![1]).not.toHaveProperty('api_key')
    expect(api.createModelSlot).not.toHaveBeenCalled()
    expect(api.testModelSlot).not.toHaveBeenCalled()
  })
  it('blocks saving a changed destination until the user replaces or clears the key', async () => {
    await mount(); await clickText('Edit'); await fill('slot-endpoint', 'https://other.test/v1')
    expect((document.querySelector('[data-testid="save-slot"]') as HTMLButtonElement).disabled).toBe(true)
    expect(document.body.textContent).toContain('The endpoint or protocol changed')
    await fill('slot-key', ''); await clickText('Save')
    expect(api.updateModelSlot).toHaveBeenCalledWith(saved.id, expect.objectContaining({ api_key: '', base_url: 'https://other.test/v1' }))
  })
  it('reports saved-slot connection failures inline and prevents deleting a bound slot', async () => {
    api.testModelSlot.mockRejectedValue(new Error('Invalid provider credential'))
    const container = await mount()
    const deleteButton = [...container.querySelectorAll('button')].find(el => el.textContent?.trim() === 'Delete')!
    expect(deleteButton.disabled).toBe(true)
    await clickText('Test connection')
    expect(api.testModelSlot).toHaveBeenCalledExactlyOnceWith(saved.id)
    expect(container.textContent).toContain('Invalid provider credential')
  })
  it('shows configuration read failures without disguising them as an empty list', async () => {
    api.listModelSlots.mockRejectedValue(new Error('Configuration is invalid'))
    const container = await mount()
    expect(container.textContent).toContain('Configuration is invalid')
    expect(container.textContent).not.toContain('No model slots yet')
  })
  it('removes a stale plugin usage binding and enables slot deletion after refreshing', async () => {
    api.listModelSlots.mockResolvedValue({ schema_version: 1, slots: [{ ...saved, bound_by: [{ plugin_id: 'uninstalled_plugin', usage_id: 'removed_usage' }] }] })
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as MessageBoxData)
    api.deleteModelBinding.mockResolvedValue({ success: true })
    const container = await mount()
    const deleteButton = () => [...container.querySelectorAll('button')].find(el => el.textContent?.trim() === 'Delete')!
    expect(deleteButton().disabled).toBe(true)
    api.listModelSlots.mockResolvedValue({ schema_version: 1, slots: [{ ...saved, bound_by: [] }] })
    await clickText('Unbind')
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('uninstalled_plugin / removed_usage'), 'Unbind', expect.objectContaining({ confirmButtonText: 'Unbind' }))
    expect(api.deleteModelBinding).toHaveBeenCalledExactlyOnceWith('uninstalled_plugin', 'removed_usage')
    expect(api.listModelSlots).toHaveBeenCalledTimes(2)
    expect(deleteButton().disabled).toBe(false)
    expect(container.textContent).not.toContain('uninstalled_plugin / removed_usage')
  })
  it('keeps a binding when unbinding is cancelled and reports failed writes inline', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')
    const container = await mount()
    await clickText('Unbind')
    expect(api.deleteModelBinding).not.toHaveBeenCalled()
    confirm.mockResolvedValue('confirm' as MessageBoxData)
    api.deleteModelBinding.mockRejectedValue(new Error('Binding could not be saved'))
    await clickText('Unbind')
    expect(container.textContent).toContain('Binding could not be saved')
    expect(container.textContent).toContain('example / vision')
  })
  it('labels partial counters and does not invent missing totals for an interrupted attempt', async () => {
    const data = structuredClone(emptyUsage)
    data.summary.logical_request_count = 1
    data.summary.usage_counts.partial = 1
    data.summary.tokens.prompt_tokens = 20
    data.requests = [{ request_id: 'request-1', plugin_id: 'example', usage_id: 'vision', slot_id: saved.id, started_at: 1, duration_ms: 40, status: 'error', error_code: 'upstream_error', attempts: [{ attempt_id: 'attempt-1', slot_id: saved.id, protocol: 'openai_chat', model: 'model-a', duration_ms: 30, status: 'error', error_code: 'upstream_error', upstream_started: true, usage_status: 'partial', usage: { prompt_tokens: 20 } }] }]
    api.getModelUsage.mockResolvedValue(data)
    const container = await mount()
    expect(container.querySelectorAll('.usage-summary strong')[2]?.textContent).toBe('—')
    const expand = container.querySelector('.el-table__expand-icon') as HTMLElement
    expect(expand).toBeTruthy()
    expand.click(); await flush()
    expect(container.textContent).toContain('Partial usage · Input 20 · Output — · Total —')
  })
})


describe('masked clipboard', () => {
  it('copies only a preview, including while entering a replacement', async () => {
    await mount(); await clickText('Edit')
    expect(document.querySelectorAll('input[type="radio"]').length).toBe(0)
    const clipboard = { setData: vi.fn() }
    const copy = new Event('copy', { bubbles: true, cancelable: true })
    Object.defineProperty(copy, 'clipboardData', { value: clipboard })
    input('slot-key').dispatchEvent(copy)
    expect(copy.defaultPrevented).toBe(true)
    expect(clipboard.setData).toHaveBeenLastCalledWith('text/plain', 'sk-abc......wxyz')
    await fill('slot-key', 'sk-new-very-private-value-1234')
    const cut = new Event('cut', { bubbles: true, cancelable: true })
    Object.defineProperty(cut, 'clipboardData', { value: clipboard })
    input('slot-key').dispatchEvent(cut)
    expect(clipboard.setData).toHaveBeenLastCalledWith('text/plain', 'sk-new......1234')
    expect(input('slot-key').type).toBe('password')
  })
  it('fully masks short keys and never sends an edited preview as a new key', async () => {
    expect(maskedKeyCopy('short-key')).toBe('******')
    await mount(); await clickText('Edit')
    await fill('slot-key', 'sk-abc......wxyz-edited')
    await clickText('Save')
    expect(api.updateModelSlot).not.toHaveBeenCalled()
  })
})
