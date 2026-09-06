import { beforeEach, describe, expect, it, vi } from 'vitest'
import { deleteModelBinding, getModelBindings, getModelUsage, listModelSlots, setModelBinding, testModelSlot, updateModelSlot } from './models'

const request = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/utils/request', () => ({ default: request }))

beforeEach(() => vi.clearAllMocks())

describe('plugin model management API', () => {
  it('keeps all requests in the independent model configuration namespace', async () => {
    await listModelSlots()
    await updateModelSlot('slot_saved', { name: 'renamed' })
    await testModelSlot('slot_saved')
    expect(request.get).toHaveBeenCalledWith('/api/model-config/slots', expect.objectContaining({ suppressErrorMessage: true }))
    expect(request.patch).toHaveBeenCalledWith('/api/model-config/slots/slot_saved', { name: 'renamed' }, expect.any(Object))
    expect(request.post).toHaveBeenCalledWith('/api/model-config/slots/slot_saved/test', undefined, expect.objectContaining({ timeout: 35000 }))
  })
  it('encodes plugin and usage identifiers and binds stable slot IDs', async () => {
    await getModelBindings('plugin/a')
    await setModelBinding('plugin/a', 'use/b', 'slot_id')
    await deleteModelBinding('plugin/a', 'use/b')
    expect(request.get).toHaveBeenCalledWith('/api/model-config/plugins/plugin%2Fa/bindings', expect.any(Object))
    expect(request.put).toHaveBeenCalledWith('/api/model-config/plugins/plugin%2Fa/bindings/use%2Fb', { slot_id: 'slot_id' }, expect.any(Object))
    expect(request.delete).toHaveBeenCalledWith('/api/model-config/plugins/plugin%2Fa/bindings/use%2Fb', expect.any(Object))
  })
  it('passes explicit usage filters without adding write operations', async () => {
    await getModelUsage({ plugin_id: 'example', slot_id: 'slot_id', limit: 100 })
    expect(request.get).toHaveBeenCalledWith('/api/model-config/usage', { suppressErrorMessage: true, params: { plugin_id: 'example', slot_id: 'slot_id', limit: 100 } })
    expect(request.post).not.toHaveBeenCalled()
  })
})
