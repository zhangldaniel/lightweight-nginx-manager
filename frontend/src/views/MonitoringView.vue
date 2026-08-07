<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
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
const selectedCapabilities = computed(() => selectedSummary.value?.node.capabilities || [])
const pureLvsNode = computed(
  () => selectedCapabilities.value.includes('ipvs_observer_v1') &&
    !selectedCapabilities.value.includes('nginx_test'),
)
const ipvs = computed<Record<string, unknown>>(
  () => (selectedSummary.value?.node.facts.ipvs as Record<string, unknown> | undefined) || {},
)
const ipvsAvailable = computed(() => ipvs.value.available === true)
const ipvsServices = computed(
  () => (ipvs.value.services as Array<Record<string, unknown>> | undefined) || [],
)
const ipvsActiveConnections = computed(() => {
  if (!ipvsServices.value.length) return null
  return ipvsServices.value.reduce((total, service) => {
    const value = Number(service.active_connections)
    return total + (Number.isFinite(value) ? value : 0)
  }, 0)
})
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
const monitoringDescription = computed(() => pureLvsNode.value
  ? '宿主机资源与 IPVS；页面每 20 秒自动更新。'
  : '宿主机资源与 Nginx Stub Status；页面每 20 秒自动更新。')
const healthSummary = computed(() => {
  const reasons = selectedSummary.value?.health.reasons || []
  if (reasons.length) return reasons.join('；')
  if (selectedSummary.value?.health.status === 'healthy') {
    if (pureLvsNode.value) {
      return ipvsAvailable.value ? '宿主机指标正常；IPVS 运行表已观测' : '宿主机指标正常；IPVS 暂无观测数据'
    }
    return '宿主机与 Nginx 指标处于正常范围'
  }
  if (selectedSummary.value?.health.status === 'offline') return '节点目前无法连接，请检查 Agent 状态'
  return '当前采样不足，等待 Agent 补充数据'
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

function metricTone(path: string, warning: number, critical: number) {
  const value = valueAt(path)
  if (value === null) return 'neutral'
  if (value >= critical) return 'danger'
  if (value >= warning) return 'warning'
  return 'healthy'
}

function chartTone(path: string, warning: number, critical: number): 'blue' | 'amber' | 'red' {
  const value = valueAt(path)
  if (value !== null && value >= critical) return 'red'
  if (value !== null && value >= warning) return 'amber'
  return 'blue'
}

function valueAt(path: string) {
  const value = metric(metrics.value, path, Number.NaN)
  return Number.isFinite(value) ? value : null
}

function ipvsValue(path: string) {
  const value = metric(ipvs.value, path, Number.NaN)
  return Number.isFinite(value) ? value : null
}

function displayIpvs(path: string, digits = 0, suffix = '') {
  const value = ipvsValue(path)
  return value === null ? '—' : `${value.toFixed(digits)}${suffix}`
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
    <PageHeader title="运行监控" :description="monitoringDescription">
      <div class="monitor-toolbar" aria-label="监控筛选">
        <label class="monitor-filter">
          <span>观察节点</span>
          <NSelect
            v-model:value="selectedNodeId"
            class="monitor-node-select"
            :options="nodeOptions"
            aria-label="观察节点"
          />
        </label>
        <label class="monitor-filter">
          <span>时间范围</span>
          <NSelect
            v-model:value="rangeSeconds"
            class="range-select"
            :options="[
              { label: '最近 1 小时', value: 3600 },
              { label: '最近 6 小时', value: 21600 },
              { label: '最近 24 小时', value: 86400 },
            ]"
            aria-label="时间范围"
          />
        </label>
        <NButton
          type="primary"
          class="monitor-refresh"
          :loading="refreshingSummary || loadingHistory"
          @click="refreshNow"
        >
          <template #icon><RefreshCw :size="16" /></template>
          刷新数据
        </NButton>
      </div>
    </PageHeader>

    <div v-if="summaryError" class="inline-error" role="alert">
      <strong>监控摘要暂时无法更新</strong>
      <span>{{ summaryError }}</span>
    </div>

    <template v-if="selectedSummary">
      <section class="monitor-signal" :data-state="selectedSummary.health.status">
        <div class="monitor-signal-node">
          <span class="monitor-kicker">当前节点</span>
          <div class="monitor-node-row">
            <span class="node-avatar"><Server :size="21" /></span>
            <div class="monitor-node-copy">
              <h2>{{ selectedSummary.node.node_name }}</h2>
              <p>{{ selectedSummary.node.hostname }}</p>
            </div>
            <StatusTag
              :label="healthLabel(selectedSummary.health.status)"
              :tone="healthTone(selectedSummary.health.status)"
            />
          </div>
        </div>

        <div class="monitor-signal-health">
          <span class="monitor-health-icon" aria-hidden="true">
            <CheckCircle2 v-if="selectedSummary.health.status === 'healthy'" :size="22" />
            <AlertTriangle v-else :size="22" />
          </span>
          <div class="monitor-health-copy">
            <span>运行结论</span>
            <strong>{{ healthSummary }}</strong>
          </div>
          <div class="monitor-freshness">
            <span>最近采样</span>
            <strong>{{ relativeTime(selectedSummary.sampled_at) }}</strong>
            <small>20 秒自动更新</small>
          </div>
        </div>
      </section>

      <section class="monitor-kpi-rail" aria-label="核心指标">
        <article class="monitor-kpi" :data-tone="metricTone('cpu.percent', 85, 95)">
          <header><Cpu :size="17" /><span>CPU 使用率</span></header>
          <strong>{{ display('cpu.percent', 1, '%') }}</strong>
          <p>{{ valueAt('cpu.count') === null ? '尚无核心数采样' : `${display('cpu.count', 0)} 个核心` }}</p>
          <progress
            v-if="valueAt('cpu.percent') !== null"
            :value="valueAt('cpu.percent') || 0"
            max="100"
            aria-label="CPU 使用率"
          ></progress>
        </article>
        <article class="monitor-kpi" :data-tone="metricTone('memory.percent', 90, 95)">
          <header><MemoryStick :size="17" /><span>内存使用率</span></header>
          <strong>{{ display('memory.percent', 1, '%') }}</strong>
          <p>{{ valueAt('memory.used_bytes') === null ? '尚无内存采样' : `${displayBytes('memory.used_bytes')} 已用` }}</p>
          <progress
            v-if="valueAt('memory.percent') !== null"
            :value="valueAt('memory.percent') || 0"
            max="100"
            aria-label="内存使用率"
          ></progress>
        </article>
        <template v-if="pureLvsNode">
          <article
            class="monitor-kpi"
            data-tone="neutral"
          >
            <header><Network :size="17" /><span>Virtual Service</span></header>
            <strong>{{ displayIpvs('service_count') }}</strong>
            <p v-if="ipvsAvailable && ipvsValue('destination_count') !== null">
              {{ displayIpvs('destination_count') }} 个 Pool Member
            </p>
            <p v-else>{{ ipvsAvailable ? '暂未报告 Virtual Service 数量' : '暂无 IPVS 观测数据' }}</p>
          </article>
          <article
            class="monitor-kpi"
            :data-tone="ipvsAvailable && ipvsValue('stats.rates.connections_per_second') !== null ? 'info' : 'neutral'"
          >
            <header><Gauge :size="17" /><span>IPVS 连接速率</span></header>
            <strong>{{ displayIpvs('stats.rates.connections_per_second', 0, '/s') }}</strong>
            <p>{{ ipvsAvailable ? '内核 IPVS 运行表采样' : '暂无 IPVS 观测数据' }}</p>
          </article>
        </template>
        <template v-else>
          <article class="monitor-kpi" :data-tone="stubAvailable ? 'healthy' : 'warning'">
            <header><Activity :size="17" /><span>活跃连接</span></header>
            <strong>{{ stubAvailable ? display('stub_status.active', 0) : '—' }}</strong>
            <p>{{ stubAvailable ? 'Nginx Stub Status' : stubReason() }}</p>
          </article>
          <article class="monitor-kpi" :data-tone="stubAvailable ? 'info' : 'neutral'">
            <header><Gauge :size="17" /><span>请求速率</span></header>
            <strong>{{ stubAvailable ? display('stub_status.requests_per_second', 1, '/s') : '—' }}</strong>
            <p>{{ stubAvailable ? 'Agent 按采样增量计算' : '没有可用采样' }}</p>
          </article>
        </template>
      </section>

      <section class="monitor-trends">
        <header class="monitor-section-head">
          <div>
            <span class="monitor-kicker">历史采样</span>
            <h2>资源趋势</h2>
            <p>{{ rangeLabel }}</p>
          </div>
          <div class="monitor-trend-status" aria-live="polite">
            <span>20 秒自动刷新</span>
            <StatusTag
              :label="loadingHistory ? '正在刷新' : historyError ? '刷新失败' : `${history.length} 个采样点`"
              :tone="historyError ? 'danger' : loadingHistory ? 'info' : 'neutral'"
              :pulse="loadingHistory"
            />
          </div>
        </header>
        <div v-if="historyError" class="trend-error" role="alert">
          <span>{{ historyError }}</span>
          <NButton size="small" @click="loadHistory()">重试</NButton>
        </div>
        <div class="monitor-charts" :class="{ loading: loadingHistory && !history.length }">
          <LineChart
            label="CPU"
            :values="series('cpu.percent')"
            suffix="%"
            :tone="chartTone('cpu.percent', 85, 95)"
            :ceiling="100"
            :warning="85"
          />
          <LineChart
            label="内存"
            :values="series('memory.percent')"
            suffix="%"
            :tone="chartTone('memory.percent', 90, 95)"
            :ceiling="100"
            :warning="90"
          />
          <LineChart
            label="Load / Core"
            :values="series('cpu.load_per_core')"
            :tone="chartTone('cpu.load_per_core', 1, 1.5)"
            :warning="1"
          />
          <LineChart
            v-if="!pureLvsNode"
            label="请求速率"
            :values="series('stub_status.requests_per_second')"
            suffix="/s"
            tone="green"
          />
          <LineChart v-if="!pureLvsNode" label="活跃连接" :values="series('stub_status.active')" tone="green" />
          <LineChart
            v-if="pureLvsNode"
            label="网络发送"
            :values="scaledSeries('network.tx_bytes_per_second', 1024)"
            suffix=" KB/s"
          />
          <LineChart
            v-if="pureLvsNode"
            label="磁盘写入"
            :values="scaledSeries('disk_io.write_bytes_per_second', 1024)"
            suffix=" KB/s"
          />
          <LineChart label="网络接收" :values="scaledSeries('network.rx_bytes_per_second', 1024)" suffix=" KB/s" />
        </div>
      </section>

      <section class="monitor-details">
        <header class="monitor-section-head">
          <div>
            <span class="monitor-kicker">即时明细</span>
            <h2>运行指标</h2>
            <p>用于定位资源、进程与磁盘问题的当前采样值</p>
          </div>
          <StatusTag
            :label="pureLvsNode
              ? ipvsAvailable ? 'IPVS 已观测' : 'IPVS 暂无数据'
              : stubAvailable ? 'Stub 可用' : stubConfigured ? 'Stub 异常' : 'Stub 未配置'"
            :tone="pureLvsNode
              ? 'neutral'
              : stubAvailable ? 'success' : 'warning'"
          />
        </header>

        <div class="monitor-detail-grid">
          <article class="monitor-data-block">
            <header>
              <span><Cpu :size="18" /></span>
              <div><strong>宿主机</strong><small>系统与 I/O</small></div>
            </header>
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

          <article v-if="pureLvsNode" class="monitor-data-block">
            <header>
              <span><Network :size="18" /></span>
              <div><strong>IPVS 观测</strong><small>内核运行表</small></div>
            </header>
            <p class="data-card-note">
              运行表是观测事实，不代表成员健康。
            </p>
            <dl>
              <div><dt>Virtual Service</dt><dd>{{ displayIpvs('service_count') }}</dd></div>
              <div><dt>Pool Member</dt><dd>{{ displayIpvs('destination_count') }}</dd></div>
              <div><dt>活跃连接</dt><dd>{{ ipvsActiveConnections ?? '—' }}</dd></div>
              <div><dt>连接速率</dt><dd>{{ displayIpvs('stats.rates.connections_per_second', 0, '/s') }}</dd></div>
              <div><dt>入站包速率</dt><dd>{{ displayIpvs('stats.rates.in_packets_per_second', 0, '/s') }}</dd></div>
              <div><dt>出站包速率</dt><dd>{{ displayIpvs('stats.rates.out_packets_per_second', 0, '/s') }}</dd></div>
              <div><dt>数据来源</dt><dd>{{ String(ipvs.source || '—') }}</dd></div>
              <div><dt>IPVS 版本</dt><dd>{{ String(ipvs.version || '—') }}</dd></div>
            </dl>
          </article>

          <article v-else class="monitor-data-block">
            <header>
              <span><Network :size="18" /></span>
              <div><strong>Nginx</strong><small>进程与连接</small></div>
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

          <article class="monitor-data-block filesystem-card">
            <header>
              <span><HardDrive :size="18" /></span>
              <div><strong>文件系统</strong><small>{{ filesystems.length }} 个挂载点</small></div>
            </header>
            <div v-if="filesystems.length" class="monitor-filesystems">
              <div
                v-for="filesystem in filesystems"
                :key="String(filesystem.mount || filesystem.path)"
                class="filesystem-row"
                :data-tone="Number(filesystem.percent || 0) >= 90 ? 'danger' : Number(filesystem.percent || 0) >= 80 ? 'warning' : 'healthy'"
              >
                <div>
                  <strong>{{ filesystem.mount || filesystem.path }}</strong>
                  <span>{{ bytes(filesystem.used_bytes) }} / {{ bytes(filesystem.total_bytes) }}</span>
                  <b>{{ filesystem.percent }}%</b>
                </div>
                <progress
                  :value="Number(filesystem.percent || 0)"
                  max="100"
                  :aria-label="`${filesystem.mount || filesystem.path} 使用率`"
                ></progress>
              </div>
            </div>
            <div v-else class="monitor-inline-empty">
              <HardDrive :size="20" />
              <span>尚未收到文件系统指标</span>
            </div>
          </article>
        </div>
      </section>
    </template>

    <section v-else-if="refreshingSummary" class="empty-state large monitor-empty-state" aria-live="polite">
      <span class="monitor-empty-icon loading"><RefreshCw :size="30" /></span>
      <span class="monitor-kicker">正在建立观测</span>
      <h2>读取节点监控数据</h2>
      <p>正在获取最新心跳、资源指标与 Nginx 运行状态。</p>
    </section>

    <section v-else class="empty-state large monitor-empty-state">
      <span class="monitor-empty-icon"><Activity :size="32" /></span>
      <span class="monitor-kicker">暂无遥测信号</span>
      <h2>尚未收到监控数据</h2>
      <p>依次确认 Agent 在线、节点具备指标能力，并等待下一次心跳。</p>
      <ol>
        <li><strong>01</strong><span>确认 Agent 在线</span></li>
        <li><strong>02</strong><span>检查 metrics_v1 能力</span></li>
        <li><strong>03</strong><span>等待下一次采样</span></li>
      </ol>
      <NButton type="primary" :loading="refreshingSummary" @click="refreshNow">
        <template #icon><RefreshCw :size="16" /></template>
        重新获取
      </NButton>
    </section>
  </section>
</template>
