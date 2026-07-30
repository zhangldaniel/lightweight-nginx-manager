<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Activity, Cpu, Gauge, HardDrive, MemoryStick, Network, Server } from '@lucide/vue'
import { NSelect } from 'naive-ui'
import PageHeader from '../components/PageHeader.vue'
import MetricCard from '../components/MetricCard.vue'
import StatusTag from '../components/StatusTag.vue'
import LineChart from '../components/LineChart.vue'
import { api } from '../api'
import { useConsoleStore } from '../stores/console'
import { bytes, metric, relativeTime } from '../utils/format'

const store = useConsoleStore()
const selectedNodeId = ref(store.selectedNodeId || store.nodes[0]?.id || '')
const rangeSeconds = ref(3600)
const loadingHistory = ref(false)
const history = ref<Array<{ sampled_at: string; metrics: Record<string, unknown> }>>([])

const selectedSummary = computed(
  () =>
    store.monitoring.find((item) => item.node.id === selectedNodeId.value) ||
    store.monitoring[0] ||
    null,
)
const metrics = computed(() => selectedSummary.value?.metrics || {})
const stubAvailable = computed(
  () =>
    (metrics.value.stub_status as Record<string, unknown> | undefined)?.available === true,
)
const nodeOptions = computed(() =>
  store.monitoring.map((item) => ({
    label: `${item.node.node_name} · ${healthLabel(item.health.status)}`,
    value: item.node.id,
  })),
)

function healthLabel(status: string) {
  if (status === 'healthy') return '正常'
  if (status === 'warning') return '需关注'
  if (status === 'critical') return '严重'
  if (status === 'offline') return '离线'
  return '无数据'
}

function healthTone(status: string) {
  if (status === 'healthy') return 'success' as const
  if (status === 'warning') return 'warning' as const
  if (status === 'critical' || status === 'offline') return 'danger' as const
  return 'neutral' as const
}

async function refreshSummary() {
  try {
    const response = await api.monitoringSummary()
    store.monitoring = response.items
    if (!selectedNodeId.value && response.items.length) selectedNodeId.value = response.items[0].node.id
  } catch (error) {
    store.notify('监控数据读取失败', 'danger', store.apiMessage(error))
  }
}

async function loadHistory() {
  if (!selectedNodeId.value) return
  loadingHistory.value = true
  try {
    const response = await api.monitoringHistory(selectedNodeId.value, rangeSeconds.value)
    history.value = response.items
  } catch (error) {
    store.notify('监控趋势读取失败', 'danger', store.apiMessage(error))
  } finally {
    loadingHistory.value = false
  }
}

function series(path: string) {
  return history.value.map((item) => metric(item.metrics, path, 0))
}

watch([selectedNodeId, rangeSeconds], loadHistory)
onMounted(async () => {
  await refreshSummary()
  await loadHistory()
})
</script>

<template>
  <section class="page page-monitoring">
    <PageHeader title="监控" description="宿主机资源和 Nginx Stub Status 状态，数据由 Agent 周期上报。">
      <NSelect v-model:value="selectedNodeId" class="monitor-node-select" :options="nodeOptions" />
      <NSelect
        v-model:value="rangeSeconds"
        class="range-select"
        :options="[
          { label: '最近 1 小时', value: 3600 },
          { label: '最近 6 小时', value: 21600 },
          { label: '最近 24 小时', value: 86400 },
        ]"
      />
    </PageHeader>

    <div v-if="selectedSummary" class="monitor-overview">
      <div class="monitor-identity">
        <span class="node-avatar"><Server :size="23" /></span>
        <div>
          <h2>{{ selectedSummary.node.node_name }}</h2>
          <p>{{ selectedSummary.node.hostname }} · 采样于 {{ relativeTime(selectedSummary.sampled_at) }}</p>
        </div>
        <StatusTag
          :label="healthLabel(selectedSummary.health.status)"
          :tone="healthTone(selectedSummary.health.status)"
        />
      </div>
      <div v-if="selectedSummary.health.reasons.length" class="health-reasons">
        <span v-for="reason in selectedSummary.health.reasons" :key="reason">{{ reason }}</span>
      </div>
    </div>

    <div v-if="selectedSummary" class="metrics-grid monitor-metrics">
      <MetricCard
        label="CPU 使用率"
        :value="`${metric(metrics, 'cpu.percent').toFixed(1)}%`"
        note="当前采样"
        :icon="Cpu"
        :tone="metric(metrics, 'cpu.percent') >= 85 ? 'warning' : 'info'"
        featured
      />
      <MetricCard
        label="内存使用率"
        :value="`${metric(metrics, 'memory.percent').toFixed(1)}%`"
        :note="bytes(metric(metrics, 'memory.used_bytes'))"
        :icon="MemoryStick"
        :tone="metric(metrics, 'memory.percent') >= 90 ? 'warning' : 'success'"
      />
      <MetricCard
        label="活跃连接"
        :value="metric(metrics, 'stub_status.active').toFixed(0)"
        note="Nginx Stub Status"
        :icon="Activity"
        tone="success"
      />
      <MetricCard
        label="请求速率"
        :value="`${metric(metrics, 'stub_status.requests_per_second').toFixed(1)}/s`"
        note="Agent 计算增量"
        :icon="Gauge"
      />
    </div>

    <div v-if="selectedSummary" class="monitor-charts" :class="{ loading: loadingHistory }">
      <LineChart label="CPU" :values="series('cpu.percent')" suffix="%" />
      <LineChart label="内存" :values="series('memory.percent')" suffix="%" tone="amber" />
      <LineChart
        label="请求速率"
        :values="series('stub_status.requests_per_second')"
        suffix="/s"
        tone="green"
      />
      <LineChart label="活跃连接" :values="series('stub_status.active')" tone="green" />
      <LineChart
        label="网络接收"
        :values="series('network.rx_bytes_per_second')"
        suffix=" B/s"
      />
      <LineChart
        label="磁盘写入"
        :values="series('disk_io.write_bytes_per_second')"
        suffix=" B/s"
        tone="amber"
      />
    </div>

    <div v-if="selectedSummary" class="monitor-detail-grid">
      <article class="data-card">
        <header><Network :size="18" /><strong>Nginx Stub Status</strong></header>
        <dl>
          <div><dt>状态</dt><dd>{{ stubAvailable ? '可用' : '不可用' }}</dd></div>
          <div><dt>接受连接</dt><dd>{{ metric(metrics, 'stub_status.accepts').toFixed(0) }}</dd></div>
          <div><dt>已处理连接</dt><dd>{{ metric(metrics, 'stub_status.handled').toFixed(0) }}</dd></div>
          <div><dt>累计请求</dt><dd>{{ metric(metrics, 'stub_status.requests').toFixed(0) }}</dd></div>
        </dl>
      </article>
      <article class="data-card">
        <header><HardDrive :size="18" /><strong>文件系统</strong></header>
        <div
          v-for="filesystem in (metrics.filesystems as Array<Record<string, unknown>> || [])"
          :key="String(filesystem.mount || filesystem.path)"
          class="filesystem-row"
        >
          <div><strong>{{ filesystem.mount || filesystem.path }}</strong><span>{{ filesystem.percent }}%</span></div>
          <progress :value="Number(filesystem.percent || 0)" max="100"></progress>
        </div>
        <p v-if="!(metrics.filesystems as unknown[])?.length">尚未收到文件系统指标。</p>
      </article>
    </div>

    <div v-else class="empty-state large">
      <Activity :size="34" />
      <strong>尚未收到监控数据</strong>
      <span>检查 Agent 是否配置了 Stub Status URL，并等待下一次心跳。</span>
    </div>
  </section>
</template>
