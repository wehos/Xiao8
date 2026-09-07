import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

const BACKEND_TARGET = process.env.VITE_BACKEND_URL || 'http://localhost:48916'
const isVitest = process.env.VITEST === 'true'
const elementPlusImportStyle = isVitest ? false : 'css'

// 提取代理目标的 hostname 用于运行时检测
// 当 API_BASE_URL 为空（通过 Vite 代理）时，前端需要知道代理目标是本地还是远程
let PROXY_TARGET_HOSTNAME = 'localhost'
try {
  PROXY_TARGET_HOSTNAME = new URL(BACKEND_TARGET).hostname
} catch {
  // 解析失败，假定为本地
  PROXY_TARGET_HOSTNAME = 'localhost'
}

// 组件测试普遍用 `app.component('el-tabs', stub)` 之类的全局桩替掉 Element Plus，
// 断言再去查桩渲染出的标记（如 data-tab-name）。ElementPlusResolver 会把模板里的
// <el-tabs> 编译成显式 `import { ElTabs } from 'element-plus'`，直接绕过全局注册，
// 于是桩失效、断言落空。测试环境关掉自动导入，让模板回到全局解析。
const autoImportComponents = isVitest
  ? []
  : [
      Components({
        dts: false,
        resolvers: [ElementPlusResolver({ importStyle: elementPlusImportStyle })],
      }),
    ]

// https://vite.dev/config/
export default defineConfig({
  base: '/ui/',
  define: {
    // 注入代理目标的 hostname，用于运行时检测本地/远程后端
    __VITE_PROXY_TARGET_HOSTNAME__: JSON.stringify(PROXY_TARGET_HOSTNAME)
  },
  plugins: [
    vue(),
    ...autoImportComponents,
    vueDevTools(),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    },
  },
  server: {
    port: 5173,
    fs: {
      // 允许访问父目录（更安全的替代方案，而不是完全禁用严格模式）
      // 这样可以访问项目根目录之外的必要文件，同时保持文件系统的安全防护
      allow: ['..']
    },
    proxy: {
      // Hosted surfaces open the main model-settings page through the parent
      // bridge. Keep this exact SPA-external route on the backend in dev.
      '^/api_key(?:\\?.*)?$': {
        target: BACKEND_TARGET,
        changeOrigin: true,
        secure: false
      },
      // Hosted document parsing uses a deliberately narrow API proxy. Do not
      // expose the entire /api namespace through the plugin-manager dev server.
      '/api/documents': {
        target: BACKEND_TARGET,
        changeOrigin: true,
        secure: false
      },
      // 代理所有插件服务器 API 请求
      '/plugin/': {
        target: BACKEND_TARGET,
        changeOrigin: true,
        secure: false
      },
      // Market Bridge API endpoints only. Keep the SPA route /market on Vite.
      '^/market/(status|bridge-token|install|installed|token-exchange|github-proxy/measure|catalog(?:/.*)?|oauth(?:/.*)?|tasks(?:/.*)?)(?:\\?.*)?$': {
        target: BACKEND_TARGET,
        changeOrigin: true,
        secure: false
      },
      // 只代理精确匹配 /plugins 的 API 请求（不带路径参数）
      // 使用 bypass 函数区分 API 请求和前端路由
      // 只代理带有 Accept: application/json 的请求（API 请求）
      '^/plugins$': {
        target: BACKEND_TARGET,
        changeOrigin: true,
        secure: false,
        bypass(req, res, options) {
          // 检查是否是 API 请求（通过 Accept 头判断）
          const acceptHeader = req.headers.accept || ''
          const method = req.method || 'GET'
          // 如果是 API 请求（包含 application/json），则代理
          // 或者是非 GET 请求（POST/PUT/DELETE 通常是 API 调用）
          if (acceptHeader.includes('application/json') || (method !== 'GET' && method !== 'HEAD')) {
            return null // 继续代理
          }
          // 否则返回原路径，让 Vite 处理（前端路由）
          return req.url
        }
      },
      '/server': {
        target: BACKEND_TARGET,
        changeOrigin: true,
        secure: false
      },
      '/health': {
        target: BACKEND_TARGET,
        changeOrigin: true,
        secure: false
      },
      '/available': {
        target: BACKEND_TARGET,
        changeOrigin: true,
        secure: false
      },
      // WebSocket 代理
      '/ws': {
        target: BACKEND_TARGET,
        changeOrigin: true,
        secure: false,
        ws: true, // 启用 WebSocket 代理
        configure: (proxy, _options) => {
          let suppressedErrorCount = 0
          // 处理 WebSocket 代理错误，避免在连接关闭后继续写入
          proxy.on('error', (err, _req, _res) => {
            // 忽略常见的 WebSocket 关闭错误
            if (err.message && (
              err.message.includes('socket has been ended') ||
              err.message.includes('ECONNRESET') ||
              err.message.includes('EPIPE')
            )) {
              suppressedErrorCount++
              if (process.env.DEBUG || suppressedErrorCount % 10 === 0) {
                console.debug(
                  `[Vite] WebSocket connection closed (suppressed ${suppressedErrorCount} times):`,
                  err.message
                )
              }
              return
            }
            console.error('[Vite] WebSocket proxy error:', err.message)
          })
          
          proxy.on('close', () => {
            // 连接关闭时的清理
          })
        }
      }
    }
  }
})
