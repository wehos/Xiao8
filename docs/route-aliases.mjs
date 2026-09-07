/**
 * Source-file rewrites keep human-readable filenames separate from stable URLs.
 * Every source-path consumer must use this map instead of deriving URLs directly.
 */
export const SOURCE_REWRITES = Object.freeze({
  'plugins/Getting Started with Plugin Development.md': 'plugins/plugin-development.md',
  'zh-CN/plugins/插件开发入门文档.md': 'zh-CN/plugins/plugin-development.md',
  'ja/plugins/プラグイン開発入門ガイド.md': 'ja/plugins/plugin-development.md',
})

/**
 * Public routes retained after overlapping plugin pages were merged.
 * These aliases are emitted as noindex client redirects during the docs build.
 */
export const LEGACY_REDIRECTS = Object.freeze({
  '/plugins/plugin-base': '/plugins/sdk-reference',
  '/plugins/lifecycle-config': '/plugins/decorators',
  '/plugins/safe-local-upgrades': '/plugins/plugin-toml',
  '/plugins/host-capability-gaps': '/plugins/sdk-reference',
  '/plugins/use-claw': '/architecture/agent-system',
  '/zh-CN/plugins/plugin-base': '/zh-CN/plugins/sdk-reference',
  '/zh-CN/plugins/lifecycle-config': '/zh-CN/plugins/decorators',
  '/zh-CN/plugins/safe-local-upgrades': '/zh-CN/plugins/plugin-toml',
  '/zh-CN/plugins/host-capability-gaps': '/zh-CN/plugins/sdk-reference',
  '/zh-CN/plugins/use-claw': '/zh-CN/architecture/agent-system',
  '/ja/plugins/plugin-base': '/ja/plugins/sdk-reference',
  '/ja/plugins/lifecycle-config': '/ja/plugins/decorators',
  '/ja/plugins/safe-local-upgrades': '/ja/plugins/plugin-toml',
})
