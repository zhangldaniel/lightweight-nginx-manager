<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { LogOut, RefreshCw } from '@lucide/vue'
import { NButton, NDropdown } from 'naive-ui'
import { useConsoleStore } from '../stores/console'

const route = useRoute()
const store = useConsoleStore()
const labels: Record<string, string> = {
  sites: '站点与配置',
  certificates: '证书',
  nodes: '节点 Agent',
  logs: '实时日志',
  monitoring: '运行监控',
  records: '执行记录',
}

const pageName = computed(() => labels[String(route.name)] || '控制台')
const accountOptions = computed(() => [
  {
    key: 'identity',
    label: `${store.session?.username} · ${roleLabel(store.session?.role || '')}`,
    disabled: true,
  },
  { key: 'density', label: store.density === 'comfortable' ? '切换紧凑模式' : '切换舒适模式' },
  { key: 'logout', label: '退出登录' },
])

function roleLabel(role: string) {
  if (role === 'admin') return '管理员'
  if (role === 'operator') return '操作员'
  return '只读'
}

async function handleAccount(key: string) {
  if (key === 'density') {
    store.setDensity(store.density === 'comfortable' ? 'compact' : 'comfortable')
  } else if (key === 'logout') {
    await store.logout()
  }
}

async function refresh() {
  try {
    await store.refresh(route.name === 'monitoring' || route.name === 'records')
    store.notify('数据已刷新', 'success')
  } catch (error) {
    store.notify('刷新失败', 'danger', store.apiMessage(error))
  }
}
</script>

<template>
  <header class="top-bar">
    <div class="breadcrumb">
      <span>Nginx 托管</span>
      <b>/</b>
      <strong>{{ pageName }}</strong>
    </div>
    <div class="top-actions">
      <span class="agent-online-pill">
        <span class="online-dot"></span>
        {{ store.onlineCount }} / {{ store.nodes.length }} Agent 在线
      </span>
      <NButton quaternary :loading="store.loading" aria-label="刷新数据" @click="refresh">
        <template #icon><RefreshCw :size="17" /></template>
        刷新
      </NButton>
      <NDropdown :options="accountOptions" trigger="click" @select="handleAccount">
        <button class="account-button" type="button">
          <span class="account-avatar">{{ store.session?.username.slice(0, 1).toUpperCase() }}</span>
          <span>{{ store.session?.username }}</span>
        </button>
      </NDropdown>
      <button class="sr-only" type="button" @click="store.logout">
        <LogOut :size="16" />退出登录
      </button>
    </div>
  </header>
</template>
