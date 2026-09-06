<template>
  <nav class="sidebar">
    <div class="sidebar-brand">
      <img src="@/assets/neko-logo.png" alt="N.E.K.O" class="sidebar-brand__logo" draggable="false" />
      <span class="sidebar-brand__text">N.E.K.O</span>
    </div>
    <div class="sidebar-nav">
      <router-link
        v-for="item in navItems"
        :key="item.path"
        :to="item.path"
        custom
        v-slot="{ isExactActive, navigate }"
      >
        <button
          class="nav-item"
          :class="{ 'nav-item--active': isExactActive || isRouteActive(item.path) }"
          :aria-current="isExactActive || isRouteActive(item.path) ? 'page' : undefined"
          :data-yui-guide-id="item.guideId || null"
          @click="navigate"
        >
          <el-icon class="nav-item__icon"><component :is="item.icon" /></el-icon>
          <span class="nav-item__label">{{ item.label }}</span>
        </button>
      </router-link>

      <!-- Adapters group -->
      <template v-if="adapters.length > 0">
        <div class="nav-divider" />
        <span class="nav-group-label">{{ $t('nav.adapters') }}</span>
        <router-link
          v-for="adapter in adapters"
          :key="adapter.id"
          :to="`/adapter/${adapter.id}/ui`"
          custom
          v-slot="{ isActive, navigate }"
        >
          <button
            class="nav-item nav-item--sub"
            :class="{ 'nav-item--active': isActive }"
            @click="navigate"
          >
            <el-icon class="nav-item__icon"><Link /></el-icon>
            <span class="nav-item__label">{{ adapter.name }}</span>
          </button>
        </router-link>
      </template>
    </div>
  </nav>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { usePluginStore } from '@/stores/plugin'
import { Odometer, Box, VideoPlay, Monitor, Link, Connection } from '@element-plus/icons-vue'

const route = useRoute()
const { t } = useI18n()
const pluginStore = usePluginStore()

const adapters = computed(() => pluginStore.pluginsWithStatus.filter((p) => p.type === 'adapter'))

const navItems = computed(() => [
  { path: '/', icon: Odometer, label: t('nav.dashboard'), guideId: 'sidebar-dashboard' },
  { path: '/plugins', icon: Box, label: t('nav.plugins'), guideId: 'sidebar-plugins' },
  { path: '/model-api', icon: Connection, label: t('modelApi.title') },
  { path: '/runs', icon: VideoPlay, label: t('nav.runs'), guideId: 'sidebar-runs' },
  { path: '/logs/_server', icon: Monitor, label: t('nav.serverLogs'), guideId: 'sidebar-server-logs' },
])

function isRouteActive(path: string): boolean {
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}

onMounted(() => {
  if (pluginStore.pluginsWithStatus.length === 0) {
    pluginStore.fetchPlugins()
  }
})
</script>

<style scoped>
.sidebar {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 14px 10px;
  gap: 6px;
  background: color-mix(in srgb, var(--el-bg-color) 92%, transparent);
  backdrop-filter: blur(12px);
}

.sidebar-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px 16px;
}

.sidebar-brand__logo {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  object-fit: contain;
  flex-shrink: 0;
}

.sidebar-brand__text {
  font-size: 17px;
  font-weight: 800;
  color: var(--el-text-color-primary);
  letter-spacing: 0.5px;
}

.sidebar-nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 14px;
  border: none;
  border-radius: 12px;
  background: transparent;
  color: var(--el-text-color-regular);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  text-align: left;
  position: relative;
  transition:
    background-color 0.2s ease,
    color 0.2s ease,
    transform 0.18s ease,
    box-shadow 0.2s ease;
}

.nav-item:hover {
  background: color-mix(in srgb, var(--el-color-primary) 6%, transparent);
  color: var(--el-color-primary);
  transform: translateX(2px);
}

.nav-item--active {
  background: color-mix(in srgb, var(--el-color-primary) 12%, transparent);
  color: var(--el-color-primary);
  font-weight: 600;
  box-shadow: inset 3px 0 0 var(--el-color-primary);
}

.nav-item--active:hover {
  transform: none;
}

.nav-item--sub {
  padding-left: 22px;
  font-size: 13px;
}

.nav-item__icon {
  font-size: 17px;
  flex-shrink: 0;
  width: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: inherit;
}

.nav-item__label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.nav-divider {
  height: 1px;
  margin: 8px 12px;
  background: color-mix(in srgb, var(--el-border-color) 40%, transparent);
}

.nav-group-label {
  padding: 4px 14px;
  font-size: 11px;
  font-weight: 700;
  color: var(--el-text-color-secondary);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
</style>
