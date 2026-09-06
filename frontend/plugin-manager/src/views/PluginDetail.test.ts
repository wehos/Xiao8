// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import PluginDetail from './PluginDetail.vue'
import type { PluginUiSurface } from '@/types/api'
import { usePluginStore } from '@/stores/plugin'

const apiMocks = vi.hoisted(() => ({
  getPluginUiSurfaceInfo: vi.fn(),
  get: vi.fn(),
  getPlugins: vi.fn(),
  getPluginStatus: vi.fn(),
}))
const routerMocks = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  route: { params: { id: 'study_companion' }, query: {} as Record<string, string> },
}))
const hostedFrameMocks = vi.hoisted(() => ({ refreshContext: vi.fn() }))

vi.mock('@/api/plugins', () => ({
  getPluginUiSurfaceInfo: apiMocks.getPluginUiSurfaceInfo,
  getPlugins: apiMocks.getPlugins,
  getPluginStatus: apiMocks.getPluginStatus,
}))
vi.mock('@/api', () => ({ get: apiMocks.get }))
vi.mock('@/i18n', () => ({ getLocale: () => 'en-US' }))
vi.mock('vue-router', () => ({
  useRoute: () => routerMocks.route,
  useRouter: () => ({ push: routerMocks.push, replace: routerMocks.replace }),
}))
vi.mock('vue-i18n', () => ({
  useI18n: () => ({ locale: ref('en-US'), t: (key: string) => key }),
}))
vi.mock('@/components/plugin/PluginActions.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent(() => () => h('div', { 'data-testid': 'plugin-actions' })) }
})
vi.mock('@/components/plugin/HostedSurfaceFrame.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      props: { surface: Object, active: Boolean, activationRevision: Number },
      emits: ['message'],
      setup(props, { emit, expose }) {
        expose({
          sendSurfaceMessage: vi.fn(),
          refreshContext: () => hostedFrameMocks.refreshContext((props.surface as PluginUiSurface)?.id),
        })
        return () => h('button', {
          'data-surface-id': (props.surface as PluginUiSurface)?.id,
          'data-active': String(props.active),
          'data-activation-revision': String(props.activationRevision),
          onClick: () => emit('message', {
            type: 'neko-study-open-surface',
            payload: {
              surfaceId: 'practice',
              kind: 'panel',
              activationRevision: 7,
            },
          }),
          onDblclick: () => emit('message', {
            type: 'neko-study-open-surface',
            payload: {
              surfaceId: 'practice',
              kind: 'panel',
              activationRevision: '7',
              prompt: 'do not forward this free text',
            },
          }),
          onContextmenu: () => emit('message', { type: 'neko-plugin-context-invalidated' }),
        })
      },
    }),
  }
})
vi.mock('@/components/plugin/PluginUIFrame.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent(() => () => h('div', { 'data-testid': 'legacy-ui' })) }
})
vi.mock('@/components/common/StatusIndicator.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent(() => () => h('div')) }
})
vi.mock('@/components/plugin/EntryList.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent(() => () => h('div')) }
})
vi.mock('@/components/metrics/MetricsCard.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent(() => () => h('div')) }
})
vi.mock('@/components/plugin/PluginConfigEditor.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent(() => () => h('div')) }
})
vi.mock('@/components/plugin/PluginModelBindings.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({
    props: { pluginId: String },
    setup(props) {
      return () => h('div', { 'data-model-bindings-plugin': props.pluginId })
    },
  }) }
})
vi.mock('@/components/logs/LogViewer.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent(() => () => h('div')) }
})
vi.mock('@/components/common/EmptyState.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent(() => () => h('div')) }
})

type MountedDetail = { container: HTMLDivElement; unmount: () => void }

function surface(overrides: Partial<PluginUiSurface>): PluginUiSurface {
  return {
    id: 'main',
    kind: 'panel',
    mode: 'hosted-tsx',
    title: 'Hosted panel',
    available: true,
    ...overrides,
  }
}

async function mountDetail(surfaces: PluginUiSurface[]): Promise<MountedDetail> {
  apiMocks.getPluginUiSurfaceInfo.mockResolvedValue({ surfaces, warnings: [] })
  apiMocks.get.mockResolvedValue({ has_ui: true })
  const plugin = {
    id: 'study_companion',
    name: 'Study Companion',
    description: 'Study Companion',
    version: '1.0.0',
    status: 'running',
  }
  apiMocks.getPlugins.mockResolvedValue({ plugins: [plugin] })
  apiMocks.getPluginStatus.mockResolvedValue({ plugin_id: plugin.id, status: { status: 'running' } })
  const container = document.createElement('div')
  document.body.appendChild(container)
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePluginStore()
  store.plugins = [plugin]
  const app = createApp(PluginDetail)
  app.use(pinia)
  app.config.globalProperties.$t = (key: string) => key
  const passthrough = defineComponent({
    setup(_props, { slots }) {
      return () => h('div', slots.default?.())
    },
  })
  const card = defineComponent({
    setup(_props, { slots }) {
      return () => h('div', [slots.header?.(), slots.default?.()])
    },
  })
  const tabPane = defineComponent({
    props: { label: String, name: String },
    setup(props, { slots }) {
      return () => h('section', { 'data-tab-name': props.name, 'data-tab-label': props.label }, slots.default?.())
    },
  })
  app.component('el-card', card)
  app.component('el-tabs', passthrough)
  app.component('el-tab-pane', tabPane)
  app.component('el-alert', passthrough)
  app.component('el-descriptions', passthrough)
  app.component('el-descriptions-item', passthrough)
  app.component('el-tag', passthrough)
  app.component('el-icon', passthrough)
  app.component('el-button', passthrough)
  app.mount(container)
  for (let index = 0; index < 10; index += 1) {
    await Promise.resolve()
    await nextTick()
  }
  return { container, unmount: () => { app.unmount(); container.remove() } }
}

describe('PluginDetail surface selection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiMocks.getPluginUiSurfaceInfo.mockReset()
    apiMocks.get.mockReset()
    apiMocks.getPlugins.mockReset()
    apiMocks.getPluginStatus.mockReset()
    routerMocks.push.mockReset()
    routerMocks.replace.mockReset()
    hostedFrameMocks.refreshContext.mockReset()
  })

  it('keeps legacy compatibility main without adding a duplicate static UI tab when hosted panels exist', async () => {
    const mounted = await mountDetail([
      surface({ id: 'main' }),
      surface({ id: 'legacy-main', mode: 'static', legacy_static_compat: true }),
    ])

    expect(mounted.container.querySelector('[data-surface-id="main"]')).not.toBeNull()
    expect(mounted.container.querySelector('[data-surface-id="legacy-main"]')).not.toBeNull()
    expect(mounted.container.querySelector('[data-tab-name="ui"]')).toBeNull()
    expect(mounted.container.querySelector('[data-testid="plugin-actions"]')).not.toBeNull()
    expect(mounted.container.querySelector('[data-tab-name="config"] [data-model-bindings-plugin="study_companion"]')).not.toBeNull()
    mounted.unmount()
  })

  it('renders only the legacy compatibility panel when no declared hosted panel exists', async () => {
    const mounted = await mountDetail([
      surface({ id: 'legacy-main', mode: 'static', legacy_static_compat: true }),
    ])

    expect(mounted.container.querySelector('[data-surface-id="legacy-main"]')).not.toBeNull()
    expect(mounted.container.querySelectorAll('[data-tab-name="panel"]')).toHaveLength(1)
    expect(mounted.container.querySelector('[data-tab-name="ui"]')).toBeNull()
    mounted.unmount()
  })

  it('activates the requested surface with a validated activation revision', async () => {
    const mounted = await mountDetail([
      surface({ id: 'knowledge-map' }),
      surface({ id: 'practice' }),
    ])

    const source = mounted.container.querySelector('[data-surface-id="knowledge-map"]') as HTMLButtonElement
    source.click()
    await nextTick()

    expect(mounted.container.querySelector('[data-surface-id="knowledge-map"]')?.getAttribute('data-active')).toBe('false')
    expect(mounted.container.querySelector('[data-surface-id="practice"]')?.getAttribute('data-active')).toBe('true')
    expect(mounted.container.querySelector('[data-surface-id="practice"]')?.getAttribute('data-activation-revision')).toBe('7')
    mounted.unmount()
  })

  it('does not accept a string revision or forward free-form activation data', async () => {
    const mounted = await mountDetail([
      surface({ id: 'knowledge-map' }),
      surface({ id: 'practice' }),
    ])

    const source = mounted.container.querySelector('[data-surface-id="knowledge-map"]') as HTMLButtonElement
    source.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await nextTick()

    expect(mounted.container.querySelector('[data-surface-id="practice"]')?.getAttribute('data-active')).toBe('true')
    expect(mounted.container.querySelector('[data-surface-id="practice"]')?.getAttribute('data-activation-revision')).toBe('0')
    mounted.unmount()
  })

  it('refreshes every mounted panel context when a static UI invalidates plugin context', async () => {
    const mounted = await mountDetail([
      surface({ id: 'legacy-main', mode: 'static' }),
      surface({ id: 'note-exporter' }),
    ])

    mounted.container.querySelector<HTMLButtonElement>('[data-surface-id="legacy-main"]')
      ?.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    await nextTick()

    expect(hostedFrameMocks.refreshContext).toHaveBeenCalledWith('legacy-main')
    expect(hostedFrameMocks.refreshContext).toHaveBeenCalledWith('note-exporter')
    mounted.unmount()
  })

  it('refreshes each frame exactly once on invalidation, with no retry chain', async () => {
    const mounted = await mountDetail([
      surface({ id: 'legacy-main', mode: 'static' }),
      surface({ id: 'note-exporter' }),
    ])
    hostedFrameMocks.refreshContext.mockClear()

    // The plugin that emitted this is alive and already done with its
    // mutation; the boot-latency retry chain would only triple the IPC.
    vi.useFakeTimers()
    mounted.container.querySelector<HTMLButtonElement>('[data-surface-id="legacy-main"]')
      ?.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    await nextTick()
    await vi.runAllTimersAsync()
    vi.useRealTimers()

    expect(hostedFrameMocks.refreshContext).toHaveBeenCalledTimes(2)
    mounted.unmount()
  })

  it.each([
    ['single guide', [{ id: 'quickstart', kind: 'guide' as const }]],
    ['tabbed guides', [{ id: 'quickstart', kind: 'guide' as const }, { id: 'faq', kind: 'guide' as const }]],
  ])('also refreshes hosted guide contexts (%s)', async (_label, guides) => {
    // Guides get a context id just like panels and can be hosted-tsx, so a
    // runtime change leaves them just as stale as a panel.
    const mounted = await mountDetail([
      surface({ id: 'legacy-main', mode: 'static' }),
      ...guides.map((guide) => surface(guide)),
    ])

    mounted.container.querySelector<HTMLButtonElement>('[data-surface-id="legacy-main"]')
      ?.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    await nextTick()

    for (const guide of guides) {
      expect(hostedFrameMocks.refreshContext).toHaveBeenCalledWith(guide.id)
    }
    mounted.unmount()
  })
})
