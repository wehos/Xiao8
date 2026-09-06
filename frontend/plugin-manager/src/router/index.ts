/**
 * 路由配置
 */
import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import { i18n } from '@/i18n'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    component: () => import('@/components/layout/AppLayout.vue'),
    children: [
      {
        path: '',
        name: 'Dashboard',
        component: () => import('@/views/Dashboard.vue'),
        meta: {
          titleKey: 'nav.dashboard'
        }
      },
      {
        path: 'plugins',
        name: 'PluginList',
        component: () => import('@/views/PluginList.vue'),
        meta: {
          titleKey: 'nav.plugins'
        }
      },
      {
        path: 'plugins/:id',
        name: 'PluginDetail',
        component: () => import('@/views/PluginDetail.vue'),
        meta: {
          titleKey: 'plugins.pluginDetail'
        }
      },
      {
        path: 'model-api',
        name: 'ModelApi',
        component: () => import('@/views/ModelApi.vue'),
        meta: {
          titleKey: 'modelApi.title'
        }
      },
      {
        path: 'market',
        name: 'MarketBrowse',
        component: () => import('@/views/MarketBrowse.vue'),
        meta: {
          titleKey: 'nav.market'
        }
      },
      {
        path: 'logs',
        redirect: '/logs/_server'
      },
      {
        path: 'runs',
        name: 'Runs',
        component: () => import('@/views/Runs.vue'),
        meta: {
          titleKey: 'nav.runs'
        }
      },
      {
        path: 'packages',
        name: 'PackageManager',
        component: () => import('@/views/PackageManager.vue'),
        meta: {
          titleKey: 'nav.packageManager'
        }
      },
      {
        path: 'logs/:id',
        name: 'Logs',
        component: () => import('@/views/Logs.vue'),
        meta: {
          titleKey: 'nav.serverLogs'
        }
      },
      {
        path: 'adapter/:id/ui',
        name: 'AdapterUI',
        component: () => import('@/views/AdapterUI.vue'),
        meta: {
          titleKey: 'nav.adapterUI'
        }
      },
      {
        path: ':pathMatch(.*)*',
        redirect: '/'
      }
    ]
  }
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes
})

// 路由守卫
router.beforeEach((to, from, next) => {
  // 设置页面标题
  if (to.meta.titleKey) {
    const title = i18n.global.t(to.meta.titleKey as string)
    const suffix = i18n.global.t('app.titleSuffix')
    document.title = `${title} - ${suffix}`
  }

  next()
})

export default router
