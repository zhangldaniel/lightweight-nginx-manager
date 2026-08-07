<script setup lang="ts">
import {
  FileTerminal,
  Gauge,
  History,
  Network,
  PanelsTopLeft,
  ServerCog,
  ShieldCheck,
  Waypoints,
} from '@lucide/vue'
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useConsoleStore } from '../stores/console'
import { buildLvsOverview } from '../utils/ipvs'

const route = useRoute()
const store = useConsoleStore()
const lvs = computed(() => buildLvsOverview(store.nodes))

const items = computed(() => [
  { to: '/sites', label: '站点与配置', icon: PanelsTopLeft, count: store.sites.length },
  {
    to: '/certificates',
    label: '证书',
    icon: ShieldCheck,
    count: store.certificates.length,
    alert: store.riskyCertificateCount > 0,
  },
  {
    to: '/nodes',
    label: '节点 Agent',
    icon: ServerCog,
    count: store.nodes.length,
    pendingCount: store.enrollments.length,
  },
  { to: '/logs', label: '实时日志', icon: FileTerminal },
  { to: '/monitoring', label: '运行监控', icon: Gauge, alert: store.unhealthyCount > 0 },
  { to: '/high-availability', label: '高可用', icon: Network },
  {
    to: '/lvs',
    label: 'LVS',
    icon: Waypoints,
    count: lvs.value.virtualServiceCount,
    alert: lvs.value.driftCount > 0 || lvs.value.unavailableNodes.length > 0,
  },
  { to: '/records', label: '执行记录', icon: History, count: store.jobs.length },
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
        <span class="nav-icon" aria-hidden="true">
          <component :is="item.icon" :size="19" stroke-width="2" />
        </span>
        <span class="nav-label">{{ item.label }}</span>
        <span
          class="nav-item-meta"
          :aria-live="item.pendingCount !== undefined ? 'polite' : undefined"
          :aria-atomic="item.pendingCount !== undefined ? 'true' : undefined"
        >
          <span v-if="item.alert" class="nav-alert" aria-label="存在需处理项目">!</span>
          <span v-else-if="item.count !== undefined" class="nav-count">{{ item.count }}</span>
          <span
            v-if="item.pendingCount"
            class="nav-pending"
            :aria-label="`${item.pendingCount} 个待审批 Agent`"
            :title="`${item.pendingCount} 个待审批 Agent`"
          >待 {{ item.pendingCount }}</span>
        </span>
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
