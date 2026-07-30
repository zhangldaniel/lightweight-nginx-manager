<script setup lang="ts">
import {
  Activity,
  FileClock,
  FileKey2,
  FileText,
  Logs,
  Server,
} from '@lucide/vue'
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useConsoleStore } from '../stores/console'

const route = useRoute()
const store = useConsoleStore()

const items = computed(() => [
  { to: '/sites', label: '站点与配置', icon: FileText, count: store.sites.length },
  {
    to: '/certificates',
    label: '证书',
    icon: FileKey2,
    count: store.certificates.length,
    alert: store.riskyCertificateCount > 0,
  },
  { to: '/nodes', label: '节点 Agent', icon: Server, count: store.nodes.length },
  { to: '/logs', label: '实时日志', icon: Logs },
  { to: '/monitoring', label: '监控', icon: Activity, alert: store.unhealthyCount > 0 },
  { to: '/records', label: '执行记录', icon: FileClock, count: store.jobs.length },
])
</script>

<template>
  <aside class="side-nav" aria-label="主要导航">
    <RouterLink class="brand" to="/sites" aria-label="Nginx Manager 首页">
      <span class="brand-mark"><span>N</span></span>
      <span class="brand-copy">
        <strong>NGINX MANAGER</strong>
        <small>CONTROL PLANE</small>
      </span>
    </RouterLink>

    <div class="nav-section-label">托管资源</div>
    <nav class="nav-list">
      <RouterLink
        v-for="item in items"
        :key="item.to"
        :to="item.to"
        class="nav-item"
        :class="{ active: route.path === item.to }"
      >
        <component :is="item.icon" :size="18" stroke-width="1.8" aria-hidden="true" />
        <span>{{ item.label }}</span>
        <span v-if="item.alert" class="nav-alert" aria-label="存在需处理项目">!</span>
        <span v-else-if="item.count !== undefined" class="nav-count">{{ item.count }}</span>
      </RouterLink>
    </nav>

    <div class="nav-health">
      <span class="online-dot" aria-hidden="true"></span>
      <div>
        <strong>{{ store.onlineCount }} / {{ store.nodes.length }} Agent 在线</strong>
        <small>主动出站连接</small>
      </div>
    </div>
  </aside>
</template>
