<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  Activity,
  Cpu,
  Gauge,
  HardDrive,
  MemoryStick,
  Network,
  RefreshCw,
  Server,
} from '@lucide/vue'
import { NButton, NSelect } from 'naive-ui'
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
const refreshingSummary = ref(false)
const historyError = ref('')
const summaryError = ref('')
const history = ref<Array<{ sampled_at: string; metrics: Record<string, unknown> }>>([])
let refreshTimer: number | undefined
let summaryRequest = 0
let historyRequest = 0
let autoRefreshInFlight = false
let monitoringActive = false

const selectedSummary = computed(
  () => store.monitoring.find((item) => item.node.id === selectedNodeId.value) || null,
)
const metrics = computed<Record<string, unknown>>(
  () => (selectedSummary.value?.metrics || {}) as Record<string, unknown>,
)
const stub = computed<Record<string, unknown>>(
  () => (metrics.value.stub_status as Record<string, unknown> | undefined) || {},
)
const nginx = computed<Record<string, unknown>>(
  () => (metrics.value.nginx as Record<string, unknown> | undefined) || {},
)
const filesystems = computed(
  () => (metrics.value.filesystems as Array<Record<string, unknown>> | undefined) || [],
)
const stubAvailable = computed(() => stub.value.available === true)
const stubConfigured = computed(() => stub.value.configured === true)
const nodeOptions = computed(() =>
  store.monitoring.map((item) => ({
    label: `${item.node.node_name} · ${healthLabel(item.health.status)}`,
    value: item.node.id,
  })),
)
const rangeLabel = computed(() => {
  if (rangeSeconds.value === 3600) return '最近 1 小时 · 原始采样'
  if (rangeSeconds.value === 21600) return '最近 6 小时 · 分钟采样'
  return '最近 24 小时 · 分钟采样'
})

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

function valueAt(path: string) {
  const value = metric(metrics.value, path, Number.NaN)
  return Number.isFinite(value) ? value : null
}

function display(path: string, digits = 1, suffix = '') {
  const value = valueAt(path)
  return value === null ? '—' : `${value.toFixed(digits)}${suffix}`
}

function displayBytes(path: string) {
  const value = valueAt(path)
  return value === null ? '—' : bytes(value)
}

function series(path: string) {
  return history.value.map((item) => {
    const value = metric(item.metrics, path, Number.NaN)
    return Number.isFinite(value) ? value : null
  })
}

function scaledSeries(path: string, divisor: number) {
  return series(path).map((value) => (value === null ? null : value / divisor))
}

function stubReason() {
  const reason = String(stub.value.reason || '')
  if (!stubConfigured.value) return 'Agent 未配置 Stub Status URL'
  if (reason === 'invalid_format') return '返回内容不是标准 Stub Status 格式'
  if (reason.startsWith('http_')) return `接口返回 ${reason.replace('http_', 'HTTP ')}`
  if (reason) return `采集失败：${reason}`
  return 'Stub Status 暂不可用'
}

function uptime(value: unknown) {
  if (value === null || value === undefined) return '—'
  const seconds = Number(value)
  if (!Number.isFinite(seconds)) return '—'
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  return days ? `${days} 天 ${hours} 小时` : `${hours} 小时`
}

async function refreshSummary(quiet = false) {
  const request = ++summaryRequest
  refreshingSummary.value = true
  if (!quiet) summaryError.value = ''
  try {
    const response = await api.monitoringSummary()
    if (request !== summaryRequest) return
    store.monitoring = response.items
    summaryError.value = ''
    if (!response.items.some((item) => item.node.id === selectedNodeId.value)) {
      selectedNodeId.value = response.items[0]?.node.id || ''
    }
  } catch (error) {
    if (request !== summaryRequest) return
    summaryError.value = store.apiMessage(error)
    if (!quiet) store.notify('监控数据读取失败', 'danger', summaryError.value)
  } finally {
    if (request === summaryRequest) refreshingSummary.value = false
  }
}

async function loadHistory(quiet = false) {
  if (!selectedNodeId.value) return
  const request = ++historyRequest
  const nodeId = selectedNodeId.value
  const range = rangeSeconds.value
  loadingHistory.value = true
  if (!quiet) historyError.value = ''
  try {
    const response = await api.monitoringHistory(nodeId, range)
    if (request !== historyRequest || nodeId !== selectedNodeId.value || range !== rangeSeconds.value) return
    history.value = response.items
    historyError.value = ''
  } catch (error) {
    if (request !== historyRequest) return
    historyError.value = store.apiMessage(error)
    if (!quiet) store.notify('监控趋势读取失败', 'danger', historyError.value)
  } finally {
    if (request === historyRequest) loadingHistory.value = false
  }
}

async function refreshNow() {
  await refreshSummary()
  await loadHistory()
}

function scheduleRefresh() {
  if (!monitoringActive) return
  if (refreshTimer !== undefined) window.clearTimeout(refreshTimer)
  refreshTimer = window.setTimeout(async () => {
    if (monitoringActive && !document.hidden && !autoRefreshInFlight) {
      autoRefreshInFlight = true
      try {
        await refreshSummary(true)
        if (monitoringActive) await loadHistory(true)
      } finally {
        autoRefreshInFlight = false
      }
    }
    if (monitoringActive) scheduleRefresh()
  }, 20_000)
}

watch([selectedNodeId, rangeSeconds], () => {
  history.value = []
  void loadHistory()
})

onMounted(async () => {
  monitoringActive = true
  await refreshSummary()
  await loadHistory()
  scheduleRefresh()
})

onBeforeUnmount(() => {
  monitoringActive = false
  summaryRequest += 1
  historyRequest += 1
  if (refreshTimer !== undefined) window.clearTimeout(refreshTimer)
})
</script>

<template>
  <section class="page page-monitoring">
    <PageHeader title="运行监控" description="宿主机资源与 Nginx Stub Status；页面每 20 秒自动更新。">
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
      <NButton :loading="refreshingSummary || loadingHistory" @click="refreshNow">
        <template #icon><RefreshCw :size="16" /></template>
        刷新
      </NButton>
    </PageHeader>

    <div v-if="summaryError" class="inline-error" role="alert">
      <strong>监控摘要暂时无法更新</strong>
      <span>{{ summaryError }}</span>
    </div>

    <div v-if="selectedSummary" class="monitor-overview" :data-state="selectedSummary.health.status">
      <div class="monitor-identity">
        <span class="node-avatar"><Server :size="21" /></span>
        <div>
          <h2>{{ selectedSummary.node.node_name }}</h2>
          <p>
            {{ selectedSummary.node.hostname }} · 采样于 {{ relativeTime(selectedSummary.sampled_at) }}
          </p>
        </div>
        <StatusTag
          :label="healthLabel(selectedSummary.health.status)"
          :tone="healthTone(selectedSummary.health.status)"
        />
      </div>
      <div class="health-reasons">
        <span v-for="reason in selectedSummary.health.reasons" :key="reason">{{ reason }}</span>
        <span v-if="!selectedSummary.health.reasons.length" class="healthy-reason">
          宿主机与 Nginx 指标在正常范围内
        </span>
      </div>
    </div>

    <div v-if="selectedSummary" class="metrics-grid monitor-metrics">
      <MetricCard
        label="CPU 使用率"
        :value="display('cpu.percent', 1, '%')"
        :note="valueAt('cpu.count') === null ? '尚无采样' : `${display('cpu.count', 0)} 个核心`"
        :icon="Cpu"
        :tone="valueAt('cpu.percent') === null ? 'neutral' : (valueAt('cpu.percent') || 0) >= 85 ? 'warning' : 'info'"
        featured
      />
      <MetricCard
        label="内存使用率"
        :value="display('memory.percent', 1, '%')"
        :note="valueAt('memory.used_bytes') === null ? '尚无采样' : displayBytes('memory.used_bytes')"
        :icon="MemoryStick"
        :tone="valueAt('memory.percent') === null ? 'neutral' : (valueAt('memory.percent') || 0) >= 90 ? 'warning' : 'success'"
      />
      <MetricCard
        label="活跃连接"
        :value="stubAvailable ? display('stub_status.active', 0) : '—'"
        :note="stubAvailable ? 'Nginx Stub Status' : stubReason()"
        :icon="Activity"
        :tone="stubAvailable ? 'success' : 'warning'"
      />
      <MetricCard
        label="请求速率"
        :value="stubAvailable ? display('stub_status.requests_per_second', 1, '/s') : '—'"
        :note="stubAvailable ? 'Agent 计算增量' : '没有可用采样'"
        :icon="Gauge"
        :tone="stubAvailable ? 'info' : 'neutral'"
      />
    </div>

    <section v-if="selectedSummary" class="monitor-trends">
      <header class="monitor-section-head">
        <div>
          <h2>资源趋势</h2>
          <p>{{ rangeLabel }}</p>
        </div>
        <StatusTag
          :label="loadingHistory ? '正在刷新' : historyError ? '刷新失败' : `${history.length} 个采样点`"
          :tone="historyError ? 'danger' : loadingHistory ? 'info' : 'neutral'"
          :pulse="loadingHistory"
        />
      </header>
      <div v-if="historyError" class="trend-error" role="alert">
        <span>{{ historyError }}</span>
        <NButton size="small" @click="loadHistory()">重试</NButton>
      </div>
      <div class="monitor-charts" :class="{ loading: loadingHistory && !history.length }">
        <LineChart label="CPU" :values="series('cpu.percent')" suffix="%" :ceiling="100" :warning="85" />
        <LineChart label="内存" :values="series('memory.percent')" suffix="%" tone="amber" :ceiling="100" :warning="90" />
        <LineChart label="Load / Core" :values="series('cpu.load_per_core')" :warning="1" tone="amber" />
        <LineChart label="请求速率" :values="series('stub_status.requests_per_second')" suffix="/s" tone="green" />
        <LineChart label="活跃连接" :values="series('stub_status.active')" tone="green" />
        <LineChart label="网络接收" :values="scaledSeries('network.rx_bytes_per_second', 1024)" suffix=" KB/s" />
      </div>
    </section>

    <div v-if="selectedSummary" class="monitor-detail-grid">
      <article class="data-card">
        <header><Cpu :size="18" /><strong>宿主机运行状态</strong></header>
        <dl>
          <div><dt>Load 1 / 5 / 15</dt><dd>{{ display('cpu.load1', 2) }} / {{ display('cpu.load5', 2) }} / {{ display('cpu.load15', 2) }}</dd></div>
          <div><dt>Swap 使用</dt><dd>{{ display('memory.swap_percent', 1, '%') }}</dd></div>
          <div><dt>网络发送</dt><dd>{{ displayBytes('network.tx_bytes_per_second') }}<template v-if="valueAt('network.tx_bytes_per_second') !== null">/s</template></dd></div>
          <div><dt>网络错误</dt><dd>{{ display('network.errors', 0) }}</dd></div>
          <div><dt>磁盘读取</dt><dd>{{ displayBytes('disk_io.read_bytes_per_second') }}<template v-if="valueAt('disk_io.read_bytes_per_second') !== null">/s</template></dd></div>
          <div><dt>磁盘写入</dt><dd>{{ displayBytes('disk_io.write_bytes_per_second') }}<template v-if="valueAt('disk_io.write_bytes_per_second') !== null">/s</template></dd></div>
          <div><dt>系统运行时间</dt><dd>{{ uptime(valueAt('system.uptime_seconds')) }}</dd></div>
          <div><dt>内核</dt><dd>{{ String((metrics.system as Record<string, unknown> | undefined)?.kernel || '—') }}</dd></div>
        </dl>
      </article>

      <article class="data-card">
        <header>
          <Network :size="18" />
          <strong>Nginx 运行状态</strong>
          <StatusTag
            :label="stubAvailable ? 'Stub 可用' : stubConfigured ? 'Stub 异常' : 'Stub 未配置'"
            :tone="stubAvailable ? 'success' : 'warning'"
          />
        </header>
        <p v-if="!stubAvailable" class="data-card-note">{{ stubReason() }}</p>
        <dl>
          <div><dt>Nginx 进程</dt><dd>{{ nginx.running === true ? `${nginx.processes || 0} 个` : nginx.running === false ? '未运行' : '—' }}</dd></div>
          <div><dt>Worker</dt><dd>{{ nginx.workers ?? '—' }}</dd></div>
          <div><dt>Nginx RSS</dt><dd>{{ bytes(nginx.rss_bytes) }}</dd></div>
          <div><dt>Reading / Writing</dt><dd>{{ stubAvailable ? `${stub.reading ?? 0} / ${stub.writing ?? 0}` : '—' }}</dd></div>
          <div><dt>Waiting</dt><dd>{{ stubAvailable ? stub.waiting ?? 0 : '—' }}</dd></div>
          <div><dt>接受 / 已处理</dt><dd>{{ stubAvailable ? `${stub.accepts ?? 0} / ${stub.handled ?? 0}` : '—' }}</dd></div>
          <div><dt>累计请求</dt><dd>{{ stubAvailable ? stub.requests ?? 0 : '—' }}</dd></div>
          <div><dt>丢弃连接</dt><dd>{{ stubAvailable ? stub.dropped_connections ?? 0 : '—' }}</dd></div>
        </dl>
      </article>

      <article class="data-card filesystem-card">
        <header><HardDrive :size="18" /><strong>文件系统</strong></header>
        <div
          v-for="filesystem in filesystems"
          :key="String(filesystem.mount || filesystem.path)"
          class="filesystem-row"
        >
          <div>
            <strong>{{ filesystem.mount || filesystem.path }}</strong>
            <span>{{ bytes(filesystem.used_bytes) }} / {{ bytes(filesystem.total_bytes) }} · {{ filesystem.percent }}%</span>
          </div>
          <progress :value="Number(filesystem.percent || 0)" max="100"></progress>
        </div>
        <p v-if="!filesystems.length">尚未收到文件系统指标。</p>
      </article>
    </div>

    <div v-else class="empty-state large">
      <Activity :size="34" />
      <strong>尚未收到监控数据</strong>
      <span>检查 Agent 连接与 Stub Status 配置，并等待下一次心跳。</span>
    </div>
  </section>
</template>
