<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  CircleSlash,
  Clock3,
  FileCheck2,
  Network,
  RefreshCw,
  Server,
  ShieldCheck,
} from '@lucide/vue'
import { NButton } from 'naive-ui'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import { api } from '../api'
import { useConsoleStore } from '../stores/console'
import type {
  HighAvailabilityNodeRef,
  JobRecord,
  KeepalivedJobAction,
  KeepalivedRole,
  NodeRecord,
  Tone,
} from '../types'
import { relativeTime } from '../utils/format'

const store = useConsoleStore()
const busyAction = ref<KeepalivedJobAction | ''>('')
const pageError = ref('')
const historyError = ref('')
const loadingHistory = ref(false)
const historicalJobs = ref<JobRecord[]>([])
let pollTimer: number | undefined

const group = computed(() => store.ui.highAvailabilityGroups[0] || null)
const availableJobs = computed(() => {
  const jobs = new Map<string, JobRecord>()
  for (const job of [...historicalJobs.value, ...store.jobs]) jobs.set(job.id, job)
  return [...jobs.values()].sort((left, right) => jobTime(right) - jobTime(left))
})

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

function jobTime(job: JobRecord) {
  const value = Date.parse(job.completed_at || job.created_at)
  return Number.isFinite(value) ? value : 0
}

function latestJob(nodeId: string, action: KeepalivedJobAction) {
  return availableJobs.value.find((job) => job.node_id === nodeId && job.action === action) || null
}

function jobDetails(job: JobRecord | null, key: KeepalivedJobAction) {
  const result = asRecord(job?.result)
  const nested = asRecord(result[key])
  const generic = asRecord(result.keepalived)
  return Object.keys(nested).length ? nested : Object.keys(generic).length ? generic : result
}

function nodeMatchesAddress(node: NodeRecord, address: string) {
  const exact = [
    node.id,
    node.node_name,
    node.hostname,
    ...Object.values(node.labels || {}),
    String(node.facts.ip_address || ''),
    String(node.facts.primary_ip || ''),
  ]
  if (exact.some((value) => value === address)) return true
  const escaped = address.replaceAll('.', '\\.')
  const addressPattern = new RegExp(`(^|[^\\d.])${escaped}([^\\d.]|$)`)
  if (addressPattern.test(JSON.stringify(node.facts))) return true
  const suffix = address.split('.').at(-1) || ''
  return Boolean(suffix && new RegExp(`(^|\\D)${suffix}(\\D|$)`).test(`${node.node_name} ${node.hostname}`))
}

function resolveNode(reference: HighAvailabilityNodeRef, used: Set<string>) {
  const explicit = reference.nodeId
    ? store.nodes.find((node) => node.id === reference.nodeId)
    : undefined
  const matched = explicit || store.nodes.find((node) => !used.has(node.id) && nodeMatchesAddress(node, reference.address))
  if (matched) used.add(matched.id)
  return matched || null
}

function normalizedRole(value: unknown): KeepalivedRole {
  const role = String(value || '').toUpperCase()
  return ['MASTER', 'BACKUP', 'FAULT'].includes(role) ? (role as KeepalivedRole) : 'UNKNOWN'
}

function boolOrNull(value: unknown) {
  return typeof value === 'boolean' ? value : null
}

function observation(node: NodeRecord | null) {
  const inspectJob = node ? latestJob(node.id, 'keepalived_inspect') : null
  const validateJob = node ? latestJob(node.id, 'keepalived_validate') : null
  const facts = asRecord(node?.facts.keepalived)
  const inspected = inspectJob?.status === 'succeeded'
    ? jobDetails(inspectJob, 'keepalived_inspect')
    : {}
  const factsTime = Date.parse(node?.last_seen_at || '')
  const inspectTime = inspectJob ? jobTime(inspectJob) : 0
  const useInspectJob = Object.keys(inspected).length > 0 && (
    !Number.isFinite(factsTime) || inspectTime > factsTime
  )
  const data = useInspectJob ? { ...facts, ...inspected } : facts
  const service = asRecord(data.service)
  const hasInspection = node?.status !== 'offline' && Object.keys(data).length > 0
  const serviceActive = hasInspection ? boolOrNull(service.active) : null
  const explicitRole = normalizedRole(data.role)
  const role = !hasInspection ? 'UNKNOWN' : serviceActive === false ? 'FAULT' : explicitRole
  const vipOwned = hasInspection ? boolOrNull(data.vip_owned) : null
  const summary = asRecord(data.config_summary)
  const instances = Array.isArray(summary.instances)
    ? summary.instances.map(asRecord).filter((item) => Object.keys(item).length)
    : []
  const validation = jobDetails(validateJob, 'keepalived_validate')
  const validationHash = String(validation.keepalived_config_hash || '')
  const validationValid = validateJob?.status === 'succeeded' && summary.summary_complete === true && summary.truncated !== true && validationHash && validationHash === String(data.keepalived_config_hash || '')
    ? boolOrNull(validation.valid ?? validation.validated)
    : null
  const validationFailed = Boolean(validateJob && ['failed', 'expired'].includes(validateJob.status))
  return {
    role,
    vipOwned,
    vip: String(data.vip || ''),
    serviceActive,
    serviceLabel:
      serviceActive === true
        ? '运行中'
        : serviceActive === false
          ? '已停止'
          : inspectJob && ['failed', 'expired'].includes(inspectJob.status)
            ? '检查失败'
            : '未知',
    serviceDetail: hasInspection
      ? [service.active_state, service.sub_state].filter(Boolean).join(' / ') || '等待 Agent 上报'
      : '等待 Agent 重新上线并上报',
    configPath: String(data.config_path || ''),
    configHash: String(data.keepalived_config_hash || data.config_hash || ''),
    keepalivedVersion: String(data.keepalived_version || validation.keepalived_version || ''),
    instances,
    summaryComplete: summary.summary_complete === true && summary.truncated !== true,
    hasInspection,
    inspectJob,
    validateJob,
    validationValid,
    validationFailed,
    lastObservedAt: !hasInspection
      ? null
      : useInspectJob
        ? inspectJob?.completed_at || inspectJob?.created_at || null
        : node?.last_seen_at || null,
  }
}

const nodeEntries = computed(() => {
  const used = new Set<string>()
  return (group.value?.nodes || []).map((reference) => {
    const node = resolveNode(reference, used)
    return { reference, node, observation: observation(node) }
  })
})

const targetNodeIds = computed(() => nodeEntries.value.flatMap((entry) => entry.node ? [entry.node.id] : []))
const observedCount = computed(() => nodeEntries.value.filter((entry) => entry.observation.hasInspection).length)
const vipMismatches = computed(() =>
  nodeEntries.value.filter(
    (entry) => entry.observation.vip && entry.observation.vip !== group.value?.vip,
  ),
)
const owners = computed(() =>
  nodeEntries.value.filter(
    (entry) =>
      entry.observation.vip === group.value?.vip &&
      entry.observation.vipOwned === true,
  ),
)
const pendingActions = computed(() =>
  availableJobs.value.filter(
    (job) =>
      targetNodeIds.value.includes(job.node_id) &&
      ['keepalived_inspect', 'keepalived_validate'].includes(job.action) &&
      ['queued', 'claimed', 'running'].includes(job.status),
  ),
)
const inspecting = computed(
  () => busyAction.value === 'keepalived_inspect' || pendingActions.value.some((job) => job.action === 'keepalived_inspect'),
)
const validating = computed(
  () => busyAction.value === 'keepalived_validate' || pendingActions.value.some((job) => job.action === 'keepalived_validate'),
)

function commonConfigSignature(entry: (typeof nodeEntries.value)[number]) {
  const instance = targetInstance(entry)
  if (!entry.observation.summaryComplete || !instance) return ''
  return JSON.stringify(
    {
      name: String(instance.name || ''),
      virtualRouterId: String(instance.virtual_router_id ?? ''),
      advertInt: String(instance.advert_int ?? ''),
      virtualIps: Array.isArray(instance.virtual_ips) ? instance.virtual_ips.map(String).sort() : [],
      authType: String(instance.auth_type || ''),
    },
  )
}

function targetInstance(entry: (typeof nodeEntries.value)[number]) {
  const expectedVip = group.value?.vip
  if (!expectedVip) return null
  return entry.observation.instances.find((instance) =>
    Array.isArray(instance.virtual_ips) && instance.virtual_ips.some(
      (address) => String(address).split('/', 1)[0] === expectedVip,
    ),
  ) || null
}

const configConsistency = computed(() => {
  const completeEntries = nodeEntries.value.filter((entry) => entry.observation.summaryComplete)
  if (completeEntries.some((entry) => !targetInstance(entry))) {
    return { state: 'invalid', label: '目标 VIP 未出现在配置', tone: 'danger' as Tone }
  }
  const signatures = nodeEntries.value.map(commonConfigSignature).filter(Boolean)
  if (signatures.length !== nodeEntries.value.length || signatures.length < 2) {
    return { state: 'unknown', label: '等待完整数据', tone: 'neutral' as Tone }
  }
  if (new Set(signatures).size === 1) {
    const usesUnicast = nodeEntries.value.some((entry) => {
      const instance = targetInstance(entry)
      return Boolean(instance?.unicast_src_ip) || (Array.isArray(instance?.unicast_peers) && instance.unicast_peers.length > 0)
    })
    if (usesUnicast) {
      const topologyMatches = nodeEntries.value.every((entry) => {
        const instance = targetInstance(entry)
        const peers = Array.from(new Set(
          Array.isArray(instance?.unicast_peers) ? instance.unicast_peers.map(String) : [],
        )).sort()
        const expectedPeers = Array.from(new Set(nodeEntries.value
          .filter((candidate) => candidate.reference.address !== entry.reference.address)
          .map((candidate) => candidate.reference.address))).sort()
        return instance?.unicast_src_ip === entry.reference.address &&
          peers.length === expectedPeers.length &&
          expectedPeers.every((address, index) => peers[index] === address)
      })
      if (!topologyMatches) {
        return { state: 'drift', label: '单播对端配置不匹配', tone: 'warning' as Tone }
      }
    }
    return { state: 'consistent', label: '公共参数一致', tone: 'success' as Tone }
  }
  return { state: 'drift', label: '发现配置差异', tone: 'warning' as Tone }
})

const vipState = computed(() => {
  if (vipMismatches.value.length) return { state: 'mismatch', label: '节点 VIP 配置不一致', tone: 'danger' as Tone }
  if (owners.value.length > 1) return { state: 'split', label: '检测到双持有', tone: 'danger' as Tone }
  if (owners.value.length === 1) return { state: 'owned', label: 'VIP 正常持有', tone: 'success' as Tone }
  if (observedCount.value === nodeEntries.value.length && observedCount.value > 0) {
    return { state: 'missing', label: 'VIP 未被持有', tone: 'danger' as Tone }
  }
  return { state: 'unknown', label: '持有者未知', tone: 'neutral' as Tone }
})

const groupHealth = computed(() => {
  if (
    vipState.value.tone === 'danger' ||
    configConsistency.value.tone === 'danger' ||
    nodeEntries.value.some((entry) => entry.observation.role === 'FAULT')
  ) {
    return { label: '异常', tone: 'danger' as Tone }
  }
  if (
    ['drift', 'unknown'].includes(configConsistency.value.state) ||
    nodeEntries.value.some((entry) => entry.node?.status === 'offline')
  ) {
    return { label: '需关注', tone: 'warning' as Tone }
  }
  if (vipState.value.state === 'owned' && observedCount.value === nodeEntries.value.length) {
    return { label: '运行正常', tone: 'success' as Tone }
  }
  return { label: '数据待补全', tone: 'neutral' as Tone }
})

const latestInspectionAt = computed(() => {
  const timestamps = nodeEntries.value
    .map((entry) => entry.observation.lastObservedAt)
    .filter(Boolean)
    .map((value) => ({ value: value as string, time: Date.parse(value as string) }))
    .filter((item) => Number.isFinite(item.time))
    .sort((left, right) => right.time - left.time)
  return timestamps[0]?.value || null
})

const partialMessage = computed(() => {
  const missingNodes = nodeEntries.value.filter((entry) => !entry.node).map((entry) => entry.reference.address)
  if (missingNodes.length) return `尚未匹配 ${missingNodes.join('、')} 的 Agent；对应状态保持为未知。`
  const offline = nodeEntries.value.filter((entry) => entry.node?.status === 'offline').map((entry) => entry.reference.address)
  if (offline.length) return `${offline.join('、')} 的 Agent 离线；当前结论基于其余可用数据。`
  if (observedCount.value < nodeEntries.value.length) return 'Keepalived 状态尚未完整上报，可点击“刷新状态”发起只读检查。'
  return ''
})

const latestFailedJob = computed(() =>
  targetNodeIds.value
    .flatMap((nodeId) => [
      latestJob(nodeId, 'keepalived_inspect'),
      latestJob(nodeId, 'keepalived_validate'),
    ])
    .filter((job): job is JobRecord => Boolean(job))
    .sort((left, right) => jobTime(right) - jobTime(left))
    .find((job) => ['failed', 'expired'].includes(job.status)) || null,
)

const operationError = computed(() => {
  if (pageError.value) return pageError.value
  const job = latestFailedJob.value
  if (!job) return historyError.value
  const result = asRecord(job.result)
  const node = store.nodes.find((item) => item.id === job.node_id)
  const action = job.action === 'keepalived_validate' ? '配置校验' : '状态检查'
  const reason = String(result.failure_code || result.failure_stage || (job.status === 'expired' ? '任务已过期' : 'Agent 返回失败'))
  return `${node?.node_name || job.node_name || job.node_id} 的${action}未完成：${reason}`
})

function roleTone(role: KeepalivedRole): Tone {
  if (role === 'MASTER') return 'success'
  if (role === 'BACKUP') return 'info'
  if (role === 'FAULT') return 'danger'
  return 'neutral'
}

function topologyRole(entry: (typeof nodeEntries.value)[number]): KeepalivedRole {
  return entry.observation.vip && entry.observation.vip !== group.value?.vip
    ? 'UNKNOWN'
    : entry.observation.role
}

function nodeRoleLabel(entry: (typeof nodeEntries.value)[number]) {
  return entry.observation.vip && entry.observation.vip !== group.value?.vip
    ? `${entry.observation.role} · VIP 不匹配`
    : entry.observation.role
}

function signalForAgent(node: NodeRecord | null) {
  if (!node) return { label: '未匹配', tone: 'neutral' as Tone }
  return node.status === 'offline'
    ? { label: '离线', tone: 'danger' as Tone }
    : { label: '在线', tone: 'success' as Tone }
}

function signalForKeepalived(entry: (typeof nodeEntries.value)[number]) {
  if (entry.observation.serviceActive === true) return { label: '运行中', tone: 'success' as Tone }
  if (entry.observation.serviceActive === false) return { label: '已停止', tone: 'danger' as Tone }
  return { label: entry.observation.serviceLabel, tone: 'neutral' as Tone }
}

function signalForNginx(node: NodeRecord | null) {
  if (!node || node.status === 'offline') return { label: '未知', tone: 'neutral' as Tone }
  const summary = store.monitoring.find((item) => item.node.id === node.id)
  const nginx = asRecord(summary?.metrics.nginx)
  if (nginx.running === true) return { label: '运行中', tone: 'success' as Tone }
  if (nginx.running === false) return { label: '已停止', tone: 'danger' as Tone }
  return node.nginx_version
    ? { label: `已发现 ${node.nginx_version}`, tone: 'info' as Tone }
    : { label: '未知', tone: 'neutral' as Tone }
}

function valueOrUnknown(value: unknown, suffix = '') {
  return value === undefined || value === null || value === '' ? '未知' : `${String(value)}${suffix}`
}

function eligibleNodeIds(action: KeepalivedJobAction) {
  return nodeEntries.value.flatMap((entry) =>
    entry.node && entry.node.status !== 'offline' && entry.node.capabilities.includes(action)
      ? [entry.node.id]
      : [],
  )
}

function actionTitle(action: KeepalivedJobAction) {
  if (!store.canOperate) return '当前账号只有查看权限'
  const eligible = eligibleNodeIds(action).length
  if (!eligible) return '没有在线且支持此检查的 Agent'
  if (eligible < nodeEntries.value.length) return `将检查 ${eligible} 个可用节点，其余节点保持未知`
  return action === 'keepalived_inspect' ? '从两台 Agent 获取实时状态' : '在两台 Agent 上校验现有配置'
}

async function loadHighAvailabilityJobs(quiet = false) {
  loadingHistory.value = true
  if (!quiet) historyError.value = ''
  try {
    historicalJobs.value = (await api.highAvailabilityJobs()).items
    historyError.value = ''
  } catch (error) {
    historyError.value = store.apiMessage(error)
    if (!quiet) store.notify('高可用记录读取失败', 'danger', historyError.value)
  } finally {
    loadingHistory.value = false
  }
}

function schedulePoll(remaining = 8) {
  if (pollTimer !== undefined) window.clearTimeout(pollTimer)
  if (!remaining) return
  pollTimer = window.setTimeout(async () => {
    await loadHighAvailabilityJobs(true)
    if (pendingActions.value.length) schedulePoll(remaining - 1)
  }, 1800)
}

async function runCheck(action: KeepalivedJobAction) {
  pageError.value = ''
  busyAction.value = action
  try {
    await store.runHighAvailabilityCheck(eligibleNodeIds(action), action)
    await loadHighAvailabilityJobs(true)
    schedulePoll()
  } catch (error) {
    pageError.value = store.apiMessage(error)
    store.notify(action === 'keepalived_inspect' ? '状态检查提交失败' : '配置校验提交失败', 'danger', pageError.value)
  } finally {
    busyAction.value = ''
  }
}

onMounted(() => void loadHighAvailabilityJobs())
onBeforeUnmount(() => {
  if (pollTimer !== undefined) window.clearTimeout(pollTimer)
})
</script>

<template>
  <section class="page page-ha">
    <PageHeader
      title="高可用"
      description="观察 Keepalived VRRP 主备状态、VIP 归属与配置一致性；当前页面仅执行只读检查。"
    >
      <div class="ha-header-actions">
        <StatusTag label="只读纳管" tone="info" />
        <NButton
          :loading="inspecting"
          :disabled="!eligibleNodeIds('keepalived_inspect').length || !store.canOperate"
          :title="actionTitle('keepalived_inspect')"
          @click="runCheck('keepalived_inspect')"
        >
          <template #icon><RefreshCw :size="16" /></template>
          刷新状态
        </NButton>
        <NButton
          type="primary"
          secondary
          :loading="validating"
          :disabled="!eligibleNodeIds('keepalived_validate').length || !store.canOperate"
          :title="actionTitle('keepalived_validate')"
          @click="runCheck('keepalived_validate')"
        >
          <template #icon><FileCheck2 :size="16" /></template>
          校验配置
        </NButton>
      </div>
    </PageHeader>

    <div v-if="operationError" class="ha-notice danger" role="alert">
      <AlertTriangle :size="18" aria-hidden="true" />
      <div><strong>高可用检查存在异常</strong><span>{{ operationError }}</span></div>
    </div>
    <div v-if="partialMessage" class="ha-notice partial" role="status">
      <Activity :size="18" aria-hidden="true" />
      <div><strong>当前为部分数据</strong><span>{{ partialMessage }}</span></div>
    </div>

    <div v-if="!group" class="empty-state large">
      <CircleSlash :size="34" />
      <strong>还没有高可用组</strong>
      <span>在本地 UI state 中添加 Keepalived 组后，这里会显示实时架构。</span>
    </div>

    <template v-else>
      <section class="ha-overview" :data-tone="groupHealth.tone">
        <div class="ha-identity">
          <span class="ha-kicker"><Network :size="14" /> KEEPALIVED · VRRP</span>
          <div class="ha-title-row">
            <div>
              <h2>{{ group.name }}</h2>
              <p>{{ group.nodes.length }} 个 Nginx 节点组成主备高可用组</p>
            </div>
            <StatusTag :label="groupHealth.label" :tone="groupHealth.tone" />
          </div>
        </div>
        <div class="vip-summary">
          <span>虚拟入口 VIP</span>
          <strong>{{ group.vip }}</strong>
          <StatusTag :label="vipState.label" :tone="vipState.tone" />
        </div>
        <dl class="ha-summary-grid">
          <div>
            <dt>当前持有者</dt>
            <dd>{{ owners.length === 1 ? owners[0]?.reference.address : owners.length > 1 ? `${owners.length} 个节点` : '未知' }}</dd>
          </div>
          <div>
            <dt>配置一致性</dt>
            <dd>{{ configConsistency.label }}</dd>
          </div>
          <div>
            <dt>最近观测</dt>
            <dd>{{ relativeTime(latestInspectionAt) }}</dd>
          </div>
          <div>
            <dt>观测覆盖</dt>
            <dd>{{ observedCount }} / {{ nodeEntries.length }} 节点</dd>
          </div>
        </dl>
      </section>

      <section class="ha-panel topology-panel" :aria-busy="inspecting || loadingHistory">
        <header class="ha-panel-head">
          <div>
            <span class="section-icon"><Network :size="19" /></span>
            <div><h2>实时架构</h2><p>连线高亮表示当前 VIP 流量归属；角色以 Agent 实际观测为准。</p></div>
          </div>
          <div class="role-legend" aria-label="VRRP 角色图例">
            <span data-role="master">MASTER</span>
            <span data-role="backup">BACKUP</span>
            <span data-role="fault">FAULT</span>
            <span data-role="unknown">UNKNOWN</span>
          </div>
        </header>

        <div class="topology-stage" :class="{ loading: inspecting || loadingHistory }">
          <div class="traffic-source"><Network :size="18" /><strong>客户端流量</strong><span>统一访问入口</span></div>
          <svg class="topology-trunk" viewBox="0 0 100 44" preserveAspectRatio="none" aria-hidden="true">
            <path d="M50 0 V44" />
          </svg>
          <div class="vip-gateway" :data-state="vipState.state">
            <span>VIP</span><strong>{{ group.vip }}</strong><small>{{ vipState.label }}</small>
          </div>
          <svg class="topology-split" viewBox="0 0 1000 90" preserveAspectRatio="none" aria-hidden="true">
            <path
              d="M500 0 V22 C500 50 250 36 250 90"
              :data-role="nodeEntries[0] ? topologyRole(nodeEntries[0]).toLowerCase() : 'unknown'"
            />
            <path
              d="M500 0 V22 C500 50 750 36 750 90"
              :data-role="nodeEntries[1] ? topologyRole(nodeEntries[1]).toLowerCase() : 'unknown'"
            />
            <circle cx="500" cy="22" r="5" />
          </svg>

          <div class="topology-nodes">
            <article
              v-for="entry in nodeEntries"
              :key="entry.reference.address"
              class="topology-node"
              :data-role="topologyRole(entry).toLowerCase()"
            >
              <header>
                <span class="node-server"><Server :size="21" /></span>
                <div><h3>{{ entry.reference.label || entry.reference.address }}</h3><code>{{ entry.reference.address }}</code></div>
                <StatusTag
                  :label="nodeRoleLabel(entry)"
                  :tone="entry.observation.vip && entry.observation.vip !== group.vip ? 'danger' : roleTone(entry.observation.role)"
                />
              </header>
              <div class="vip-ownership">
                <CheckCircle2 v-if="entry.observation.vipOwned === true" :size="16" />
                <AlertTriangle v-else-if="entry.observation.role === 'FAULT'" :size="16" />
                <Clock3 v-else :size="16" />
                <span>{{ entry.observation.vip && entry.observation.vip !== group.vip
                  ? `节点配置为 ${entry.observation.vip}`
                  : entry.observation.vipOwned === true
                    ? '当前持有 VIP'
                    : entry.observation.vipOwned === false
                      ? '未持有 VIP'
                      : 'VIP 归属未知' }}</span>
              </div>
              <dl class="service-signals">
                <div>
                  <dt>Agent</dt>
                  <dd><StatusTag v-bind="signalForAgent(entry.node)" /></dd>
                </div>
                <div>
                  <dt>Keepalived</dt>
                  <dd><StatusTag v-bind="signalForKeepalived(entry)" /></dd>
                </div>
                <div>
                  <dt>Nginx</dt>
                  <dd><StatusTag v-bind="signalForNginx(entry.node)" /></dd>
                </div>
              </dl>
              <footer>
                <span>{{ entry.observation.serviceDetail }}</span>
                <time v-if="entry.observation.lastObservedAt" :datetime="entry.observation.lastObservedAt">
                  {{ relativeTime(entry.observation.lastObservedAt) }}
                </time>
                <span v-else>等待首次检查</span>
              </footer>
            </article>
          </div>
          <div class="peer-link" aria-label="节点间 VRRP 心跳">
            <span></span><strong>VRRP 心跳 / 主备通告</strong><span></span>
          </div>
        </div>
      </section>

      <div class="ha-detail-grid">
        <section class="ha-panel config-panel">
          <header class="ha-panel-head compact">
            <div>
              <span class="section-icon"><ShieldCheck :size="19" /></span>
              <div><h2>配置一致性</h2><p>比较 VRRP 公共参数，节点优先级允许不同。</p></div>
            </div>
            <StatusTag :label="configConsistency.label" :tone="configConsistency.tone" />
          </header>
          <div class="config-node-list">
            <article v-for="entry in nodeEntries" :key="entry.reference.address">
              <header><strong>{{ entry.reference.address }}</strong><span>{{ entry.observation.keepalivedVersion || '版本未知' }}</span></header>
              <dl>
                <div><dt>实例</dt><dd>{{ valueOrUnknown(targetInstance(entry)?.name) }}</dd></div>
                <div><dt>VRID</dt><dd>{{ valueOrUnknown(targetInstance(entry)?.virtual_router_id) }}</dd></div>
                <div><dt>优先级</dt><dd>{{ valueOrUnknown(targetInstance(entry)?.priority) }}</dd></div>
                <div><dt>通告间隔</dt><dd>{{ valueOrUnknown(targetInstance(entry)?.advert_int, ' 秒') }}</dd></div>
              </dl>
              <code :title="entry.observation.configPath || undefined">{{ entry.observation.configPath || '配置路径未上报' }}</code>
            </article>
          </div>
          <div class="validation-line">
            <FileCheck2 :size="17" />
            <div>
              <strong>最近校验</strong>
              <span>
                {{ nodeEntries.every((entry) => entry.observation.validationValid === true)
                  ? '两端现有配置均通过校验'
                  : nodeEntries.some((entry) => entry.observation.validationFailed)
                    ? '至少一端校验未通过，请查看执行记录'
                    : '尚未取得完整校验结果' }}
              </span>
            </div>
          </div>
        </section>

        <section class="ha-panel switch-panel">
          <header class="ha-panel-head compact">
            <div>
              <span class="section-icon"><Activity :size="19" /></span>
              <div><h2>观测边界</h2><p>当前版本读取状态，不执行主备切换。</p></div>
            </div>
          </header>
          <div class="switch-empty">
            <Activity :size="28" />
            <strong>{{ observedCount }} / {{ nodeEntries.length }} 节点有实时数据</strong>
            <span>角色和 VIP 归属来自 Agent 心跳；本期不推断或保存历史切换时间。</span>
          </div>
          <div class="safety-note">
            <ShieldCheck :size="17" />
            <p><strong>观察模式</strong><span>此页面不会停止服务、漂移 VIP 或修改 Keepalived 配置。</span></p>
          </div>
        </section>
      </div>
    </template>
  </section>
</template>

<style scoped>
.page-ha { --ha-master: #16866a; --ha-backup: #3978d4; --ha-fault: #c54048; }
.ha-header-actions { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.ha-notice { display: flex; gap: 11px; align-items: flex-start; margin-bottom: 14px; padding: 12px 14px; border: 1px solid; border-radius: var(--radius-md); }
.ha-notice div { display: grid; gap: 1px; }
.ha-notice strong { font-size: 13px; }
.ha-notice span { color: var(--text-2); font-size: 12px; }
.ha-notice.danger { border-color: rgba(220, 60, 77, .24); background: rgba(255, 240, 241, .8); color: var(--red); }
.ha-notice.partial { border-color: rgba(217, 119, 6, .2); background: rgba(255, 246, 231, .76); color: var(--amber); }
.ha-overview, .ha-panel { border: 1px solid rgba(124, 151, 189, .2); background: rgba(255, 255, 255, .78); box-shadow: var(--shadow-sm); backdrop-filter: blur(18px); }
.ha-overview { display: grid; grid-template-columns: minmax(250px, 1.1fr) minmax(220px, .62fr) minmax(440px, 1.3fr); gap: 22px; align-items: stretch; padding: 20px; border-radius: var(--radius-lg); }
.ha-identity { display: grid; align-content: center; gap: 8px; }
.ha-kicker { display: inline-flex; gap: 6px; align-items: center; color: var(--blue); font-family: var(--font-mono); font-size: 11px; font-weight: 600; letter-spacing: .08em; }
.ha-title-row { display: flex; gap: 14px; align-items: flex-start; justify-content: space-between; }
.ha-title-row h2 { font-size: 21px; letter-spacing: -.02em; }
.ha-title-row p { margin-top: 2px; color: var(--text-3); font-size: 12px; }
.vip-summary { display: grid; place-content: center; justify-items: center; min-height: 112px; padding: 15px; border: 1px solid rgba(22, 119, 255, .18); border-radius: var(--radius-md); background: linear-gradient(145deg, rgba(234, 243, 255, .95), rgba(239, 252, 255, .9)); }
.vip-summary > span { color: var(--text-3); font-size: 11px; }
.vip-summary > strong { margin: 3px 0 8px; font-family: var(--font-mono); font-size: clamp(20px, 2vw, 28px); letter-spacing: -.04em; }
.ha-summary-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
.ha-summary-grid div { display: grid; align-content: center; min-width: 0; padding: 11px 13px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface-soft); }
.ha-summary-grid dt { color: var(--text-3); font-size: 10px; }
.ha-summary-grid dd { overflow: hidden; color: var(--text); font-family: var(--font-mono); font-size: 12px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.ha-panel { margin-top: 16px; border-radius: var(--radius-lg); }
.ha-panel-head { display: flex; gap: 16px; align-items: center; justify-content: space-between; padding: 17px 19px; border-bottom: 1px solid var(--line); }
.ha-panel-head > div:first-child { display: flex; gap: 10px; align-items: center; }
.ha-panel-head h2 { font-size: 16px; }
.ha-panel-head p { color: var(--text-3); font-size: 11px; }
.ha-panel-head.compact { min-height: 74px; }
.section-icon { display: grid; width: 36px; height: 36px; flex: 0 0 auto; place-items: center; border-radius: 10px; background: var(--blue-soft); color: var(--blue); }
.role-legend { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.role-legend span { padding: 3px 7px; border-radius: 999px; background: #f1f4f8; color: var(--text-3); font-family: var(--font-mono); font-size: 9px; font-weight: 600; }
.role-legend [data-role="master"] { background: var(--green-soft); color: var(--ha-master); }
.role-legend [data-role="backup"] { background: var(--blue-soft); color: var(--ha-backup); }
.role-legend [data-role="fault"] { background: var(--red-soft); color: var(--ha-fault); }
.topology-stage { position: relative; display: grid; justify-items: center; padding: 22px 28px 20px; overflow: hidden; }
.topology-stage::after { position: absolute; inset: 0; pointer-events: none; background: linear-gradient(100deg, transparent 30%, rgba(255,255,255,.55) 48%, transparent 66%); content: ""; opacity: 0; transform: translateX(-100%); }
.topology-stage.loading::after { opacity: 1; animation: topology-sheen 1.5s linear infinite; }
.traffic-source { display: grid; grid-template-columns: auto auto; column-gap: 8px; align-items: center; min-width: 170px; padding: 9px 13px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface-raised); box-shadow: var(--shadow-xs); }
.traffic-source svg { grid-row: 1 / 3; color: var(--blue); }
.traffic-source strong { font-size: 12px; }
.traffic-source span { color: var(--text-3); font-size: 10px; }
.topology-trunk { width: 120px; height: 38px; overflow: visible; }
.topology-trunk path, .topology-split path { fill: none; stroke: #90a9c6; stroke-width: 2; vector-effect: non-scaling-stroke; }
.vip-gateway { z-index: 1; display: grid; min-width: 244px; justify-items: center; padding: 12px 22px; border: 1px solid rgba(22,119,255,.28); border-radius: 12px; background: #f5faff; box-shadow: 0 10px 30px rgba(49, 105, 182, .12); }
.vip-gateway span { color: var(--blue); font-family: var(--font-mono); font-size: 9px; letter-spacing: .14em; }
.vip-gateway strong { font-family: var(--font-mono); font-size: 18px; }
.vip-gateway small { color: var(--text-3); font-size: 10px; }
.vip-gateway[data-state="missing"], .vip-gateway[data-state="split"] { border-color: rgba(220,60,77,.34); background: var(--red-soft); }
.topology-split { width: min(100%, 1000px); height: 76px; overflow: visible; }
.topology-split circle { fill: #8ea7c4; }
.topology-split path[data-role="master"] { stroke: var(--ha-master); stroke-width: 3; stroke-dasharray: 8 6; animation: route-flow 1s linear infinite; }
.topology-split path[data-role="fault"] { stroke: var(--ha-fault); stroke-dasharray: 3 5; }
.topology-split path[data-role="unknown"] { stroke-dasharray: 2 6; }
.topology-nodes { display: grid; width: min(100%, 1000px); grid-template-columns: repeat(2, minmax(0, 1fr)); gap: clamp(22px, 8vw, 90px); }
.topology-node { position: relative; min-width: 0; padding: 16px; border: 1px solid var(--line); border-top: 3px solid #a8b5c5; border-radius: 12px; background: rgba(255,255,255,.93); box-shadow: var(--shadow-xs); }
.topology-node[data-role="master"] { border-top-color: var(--ha-master); box-shadow: 0 14px 38px rgba(22,134,106,.12); }
.topology-node[data-role="backup"] { border-top-color: var(--ha-backup); }
.topology-node[data-role="fault"] { border-top-color: var(--ha-fault); }
.topology-node > header { display: flex; gap: 10px; align-items: center; }
.topology-node > header > div { min-width: 0; margin-right: auto; }
.topology-node h3 { overflow: hidden; font-size: 14px; text-overflow: ellipsis; white-space: nowrap; }
.topology-node code { color: var(--text-3); font-size: 10px; }
.node-server { display: grid; width: 36px; height: 36px; flex: 0 0 auto; place-items: center; border-radius: 9px; background: #f0f5fb; color: var(--text-2); }
.vip-ownership { display: flex; gap: 7px; align-items: center; margin-top: 13px; padding: 8px 10px; border-radius: 8px; background: var(--surface-soft); color: var(--text-2); font-size: 11px; }
.topology-node[data-role="master"] .vip-ownership { background: var(--green-soft); color: var(--ha-master); }
.topology-node[data-role="fault"] .vip-ownership { background: var(--red-soft); color: var(--ha-fault); }
.service-signals { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; margin-top: 10px; }
.service-signals div { display: grid; gap: 4px; min-width: 0; padding: 8px; border: 1px solid var(--line); border-radius: 8px; }
.service-signals dt { color: var(--text-3); font-size: 9px; }
.service-signals dd { min-width: 0; }
.service-signals :deep(.status-tag) { max-width: 100%; overflow: hidden; font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.topology-node footer { display: flex; gap: 10px; justify-content: space-between; margin-top: 10px; color: var(--text-3); font-size: 9px; }
.topology-node footer span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.topology-node footer time { flex: 0 0 auto; }
.peer-link { display: grid; width: min(70%, 630px); grid-template-columns: 1fr auto 1fr; gap: 10px; align-items: center; margin-top: 16px; color: var(--text-3); font-size: 9px; }
.peer-link span { height: 1px; background: repeating-linear-gradient(90deg, #9eafc3 0 5px, transparent 5px 9px); }
.ha-detail-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(310px, .75fr); gap: 16px; }
.config-node-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; padding: 14px; }
.config-node-list article { min-width: 0; padding: 12px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface-soft); }
.config-node-list article > header { display: flex; gap: 10px; justify-content: space-between; }
.config-node-list article > header strong { font-family: var(--font-mono); font-size: 12px; }
.config-node-list article > header span { color: var(--text-3); font-size: 9px; }
.config-node-list dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; margin: 11px 0; }
.config-node-list dl div { display: grid; gap: 1px; }
.config-node-list dt { color: var(--text-3); font-size: 9px; }
.config-node-list dd { font-family: var(--font-mono); font-size: 10px; }
.config-node-list article > code { display: block; overflow: hidden; color: var(--text-3); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.validation-line, .safety-note { display: flex; gap: 10px; align-items: flex-start; margin: 0 14px 14px; padding: 11px 12px; border-radius: 9px; background: var(--blue-soft); color: var(--blue); }
.validation-line div { display: grid; }
.validation-line strong, .safety-note strong { font-size: 11px; }
.validation-line span, .safety-note span { color: var(--text-2); font-size: 10px; }
.switch-panel { display: flex; flex-direction: column; }
.switch-event { display: flex; gap: 12px; align-items: center; margin: 16px; padding: 14px; border-left: 2px solid var(--green); background: var(--green-soft); }
.switch-marker { display: grid; width: 34px; height: 34px; place-items: center; border-radius: 50%; background: #fff; color: var(--green); }
.switch-event div { display: grid; }
.switch-event strong { font-size: 12px; }
.switch-event time { color: var(--text-3); font-family: var(--font-mono); font-size: 10px; }
.switch-empty { display: grid; flex: 1; min-height: 145px; place-content: center; justify-items: center; padding: 24px; color: var(--text-4); text-align: center; }
.switch-empty strong { margin-top: 6px; color: var(--text-2); font-size: 12px; }
.switch-empty span { max-width: 290px; color: var(--text-3); font-size: 10px; }
.safety-note { margin-top: auto; background: var(--green-soft); color: var(--green); }
.safety-note p { display: grid; }
@keyframes route-flow { to { stroke-dashoffset: -14; } }
@keyframes topology-sheen { to { transform: translateX(100%); } }
@media (max-width: 1250px) {
  .ha-overview { grid-template-columns: 1fr .72fr; }
  .ha-summary-grid { grid-column: 1 / -1; }
}
@media (max-width: 900px) {
  .ha-overview, .ha-detail-grid { grid-template-columns: minmax(0, 1fr); }
  .vip-summary { min-height: 100px; }
  .topology-stage { padding-inline: 15px; }
  .topology-nodes { gap: 14px; }
  .service-signals { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 680px) {
  .ha-header-actions { align-items: stretch; }
  .ha-header-actions :deep(.n-button) { flex: 1; }
  .ha-summary-grid, .topology-nodes, .config-node-list { grid-template-columns: minmax(0, 1fr); }
  .topology-split { display: none; }
  .topology-nodes { margin-top: 26px; }
  .topology-node::before { position: absolute; top: -27px; left: 50%; width: 1px; height: 24px; border-left: 2px dashed #90a9c6; content: ""; }
  .peer-link { width: 90%; }
  .role-legend { justify-content: flex-start; }
  .ha-panel-head { align-items: flex-start; flex-direction: column; }
}
@media (prefers-reduced-motion: reduce) {
  .topology-split path[data-role="master"], .topology-stage.loading::after { animation: none; }
}
</style>
