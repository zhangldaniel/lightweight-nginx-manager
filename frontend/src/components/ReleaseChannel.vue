<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { useConsoleStore } from '../stores/console'
import type { JobRecord, SiteRecord } from '../types'
import { siteTitle } from '../utils/format'

const props = withDefaults(
  defineProps<{
    site: SiteRecord | null | undefined
    variant?: 'summary' | 'flow'
    additionalActiveCount?: number
  }>(),
  { variant: 'flow', additionalActiveCount: 0 },
)

const store = useConsoleStore()

type PendingJobReference = {
  id: string
  nodeId: string
  candidateHash?: string
}

type ReleaseJobSlot = PendingJobReference & {
  job?: JobRecord
}

type ChannelState = 'idle' | 'running' | 'partial' | 'success'
type StageStatus =
  | 'ready'
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'expired'
  | 'syncing'
  | 'skipped'

type ReleaseStage = {
  id: 'certificate' | 'candidate' | 'agent' | 'reload'
  label: string
  detail: string
  status: StageStatus
}

const activeStatuses = new Set(['queued', 'claimed', 'running'])
const failedStatuses = new Set(['failed', 'expired'])

const pendingJobReferences = computed<PendingJobReference[]>(() => {
  const jobs = props.site?.pendingRemote?.jobs
  if (!Array.isArray(jobs)) return []

  const references: PendingJobReference[] = []
  const seen = new Set<string>()
  for (const item of jobs) {
    if (!item || typeof item !== 'object') continue
    const value = item as Record<string, unknown>
    const id = typeof value.id === 'string' ? value.id.trim() : ''
    const nodeId = typeof value.nodeId === 'string' ? value.nodeId.trim() : ''
    if (!id || !nodeId || seen.has(id)) continue
    seen.add(id)
    references.push({
      id,
      nodeId,
      candidateHash:
        typeof value.candidateHash === 'string' ? value.candidateHash.trim() : undefined,
    })
  }
  return references
})

const jobSlots = computed<ReleaseJobSlot[]>(() => {
  const jobsById = new Map(store.jobs.map((job) => [job.id, job]))
  return pendingJobReferences.value.map((reference) => {
    const job = jobsById.get(reference.id)
    return {
      ...reference,
      // A matching id is not sufficient: node identity is part of the saved reference.
      job: job?.node_id === reference.nodeId ? job : undefined,
    }
  })
})

const jobs = computed<JobRecord[]>(() =>
  jobSlots.value.flatMap((slot) => (slot.job ? [slot.job] : [])),
)
const totalCount = computed(() => pendingJobReferences.value.length)
const syncingCount = computed(() => Math.max(0, totalCount.value - jobs.value.length))
const succeededCount = computed(
  () => jobs.value.filter((job) => job.status === 'succeeded').length,
)
const failedCount = computed(
  () => jobs.value.filter((job) => failedStatuses.has(job.status)).length,
)
const activeCount = computed(
  () => jobs.value.filter((job) => activeStatuses.has(job.status)).length,
)
const runningCount = computed(
  () => jobs.value.filter((job) => ['claimed', 'running'].includes(job.status)).length,
)
const queuedCount = computed(() => jobs.value.filter((job) => job.status === 'queued').length)
const terminalCount = computed(() => succeededCount.value + failedCount.value)
const taskProgress = computed(() =>
  totalCount.value ? Math.round((terminalCount.value / totalCount.value) * 100) : 0,
)

const onlineAgentCount = computed(() => store.onlineCount)
const agentCount = computed(() => store.nodes.length)
const availability = computed(() =>
  agentCount.value ? Math.round((onlineAgentCount.value / agentCount.value) * 100) : 0,
)

const operation = computed(() => {
  const value = props.site?.pendingRemote?.operation
  return typeof value === 'string' ? value : ''
})

const operationLabel = computed(() => {
  const labels: Record<string, string> = {
    publish: '配置发布',
    validate: '逐节点校验',
    reload: 'Nginx reload',
    delete: '从节点移除',
    transfer: '复制 / 迁移',
  }
  return labels[operation.value] || (totalCount.value ? '节点任务' : '发布通道')
})
const parallelTaskNote = computed(() =>
  props.additionalActiveCount > 0 ? `另有 ${props.additionalActiveCount} 个任务` : '',
)

const channel = computed<{ state: ChannelState; label: string; detail: string }>(() => {
  if (!totalCount.value) {
    return {
      state: 'idle',
      label: '当前无发布任务',
      detail: agentCount.value
        ? `${onlineAgentCount.value} / ${agentCount.value} Agent 在线`
        : '尚未接入 Agent',
    }
  }

  if (activeCount.value || syncingCount.value) {
    return {
      state: 'running',
      label: failedCount.value ? '执行中 · 已有失败' : syncingCount.value ? '同步任务状态' : '执行中',
      detail: failedCount.value
        ? `${terminalCount.value} / ${totalCount.value} 节点已结束，${failedCount.value} 个失败`
        : syncingCount.value
          ? `${syncingCount.value} 个节点的任务记录正在同步`
          : `${terminalCount.value} / ${totalCount.value} 节点已结束`,
    }
  }

  if (failedCount.value) {
    return {
      state: 'partial',
      label: failedCount.value === totalCount.value ? '任务未完成' : '部分节点失败',
      detail: `${succeededCount.value} 成功 · ${failedCount.value} 未完成`,
    }
  }

  if (succeededCount.value === totalCount.value) {
    return {
      state: 'success',
      label: '执行成功',
      detail: `${succeededCount.value} 个节点全部完成`,
    }
  }

  return {
    state: 'idle',
    label: '等待状态更新',
    detail: '任务记录尚未进入可识别状态',
  }
})

const summary = computed(() => {
  if (totalCount.value) {
    return {
      percent: taskProgress.value,
      ratio: `${terminalCount.value} / ${totalCount.value}`,
      metric: '节点已结束',
      note: `${succeededCount.value} 成功${failedCount.value ? ` · ${failedCount.value} 未完成` : ''}`,
    }
  }
  return {
    percent: availability.value,
    ratio: agentCount.value ? `${onlineAgentCount.value} / ${agentCount.value}` : '0 / 0',
    metric: 'Agent 在线',
    note: agentCount.value ? '当前展示在线可用度' : '尚未接入 Agent',
  }
})

function resultValue(job: JobRecord, key: string) {
  return job.result && typeof job.result === 'object' ? job.result[key] : undefined
}

function failureStage(job: JobRecord) {
  const value = resultValue(job, 'failure_stage')
  return typeof value === 'string' ? value : ''
}

function certificateStage(): ReleaseStage {
  const site = props.site
  if (!site) {
    return { id: 'certificate', label: '证书引用', detail: '未选择配置', status: 'queued' }
  }
  if (['delete', 'reload'].includes(operation.value)) {
    return {
      id: 'certificate',
      label: '证书引用',
      detail: '本操作不涉及',
      status: 'skipped',
    }
  }
  if (!site.certificateId) {
    return {
      id: 'certificate',
      label: '证书引用',
      detail: '无需平台证书',
      status: 'skipped',
    }
  }
  const certificate = store.certificates.find((item) => item.id === site.certificateId)
  if (!certificate) {
    return { id: 'certificate', label: '证书引用', detail: '证书记录不存在', status: 'failed' }
  }
  const targetNodeIds = pendingJobReferences.value.length
    ? pendingJobReferences.value.map((item) => item.nodeId)
    : site.nodeIds
  const covered = targetNodeIds.filter((nodeId) => {
    const paths = certificate.nodePaths?.[nodeId]
    return Boolean(paths?.certificatePath && paths?.keyPath)
  }).length
  if (covered !== targetNodeIds.length) {
    return {
      id: 'certificate',
      label: '证书引用',
      detail: `${covered} / ${targetNodeIds.length} 节点路径就绪`,
      status: 'failed',
    }
  }
  return {
    id: 'certificate',
    label: '证书引用',
    detail: targetNodeIds.length ? `${covered} / ${targetNodeIds.length} 节点路径已映射` : '证书已绑定',
    status: 'ready',
  }
}

function candidateStage(): ReleaseStage {
  if (!totalCount.value) {
    return { id: 'candidate', label: '候选配置', detail: '等待发布', status: 'queued' }
  }
  if (operation.value === 'reload') {
    return { id: 'candidate', label: '候选配置', detail: '配置未变化', status: 'ready' }
  }
  const candidateCount = pendingJobReferences.value.filter((item) => item.candidateHash).length
  if (candidateCount) {
    return {
      id: 'candidate',
      label: '候选配置',
      detail: `${candidateCount} / ${totalCount.value} 节点候选已生成`,
      status: candidateCount === totalCount.value ? 'ready' : 'syncing',
    }
  }
  const wording = operation.value === 'delete' ? '删除任务已提交' : operation.value === 'transfer' ? '迁移任务已提交' : '任务已提交'
  return { id: 'candidate', label: '候选配置', detail: wording, status: 'ready' }
}

function agentStage(): ReleaseStage {
  if (!totalCount.value) {
    return { id: 'agent', label: 'Agent 执行', detail: '当前空闲', status: 'queued' }
  }
  if (syncingCount.value) {
    return {
      id: 'agent',
      label: 'Agent 执行',
      detail: `${syncingCount.value} 个节点同步中`,
      status: 'syncing',
    }
  }
  if (activeCount.value) {
    return {
      id: 'agent',
      label: 'Agent 执行',
      detail: runningCount.value
        ? `${runningCount.value} 执行中${queuedCount.value ? ` · ${queuedCount.value} 排队` : ''}`
        : `${queuedCount.value} 个节点等待领取`,
      status: runningCount.value ? 'running' : 'queued',
    }
  }
  if (failedCount.value) {
    const allExpired = jobs.value.every((job) => job.status === 'expired')
    return {
      id: 'agent',
      label: 'Agent 执行',
      detail: `${succeededCount.value} 成功 · ${failedCount.value} 未完成`,
      status: allExpired ? 'expired' : 'failed',
    }
  }
  if (succeededCount.value === totalCount.value) {
    return {
      id: 'agent',
      label: 'Agent 执行',
      detail: `${succeededCount.value} / ${totalCount.value} 节点完成`,
      status: 'succeeded',
    }
  }
  return { id: 'agent', label: 'Agent 执行', detail: '等待状态更新', status: 'syncing' }
}

function reloadStage(): ReleaseStage {
  if (!totalCount.value) {
    return { id: 'reload', label: 'reload 结果', detail: '等待发布', status: 'queued' }
  }
  if (operation.value === 'validate') {
    return { id: 'reload', label: 'reload 结果', detail: '仅校验，不 reload', status: 'skipped' }
  }
  if (syncingCount.value) {
    return { id: 'reload', label: 'reload 结果', detail: '等待任务状态', status: 'syncing' }
  }
  if (activeCount.value) {
    // config_apply is one atomic Agent job; its internal phase is not observable here.
    return { id: 'reload', label: 'reload 结果', detail: '等待 Agent 返回结果', status: 'queued' }
  }
  if (failedCount.value) {
    const stages = new Set(jobs.value.filter((job) => failedStatuses.has(job.status)).map(failureStage))
    if (stages.has('reload')) {
      return { id: 'reload', label: 'reload 结果', detail: 'reload 失败', status: 'failed' }
    }
    if (stages.has('health_check')) {
      return { id: 'reload', label: 'reload 结果', detail: '健康检查失败', status: 'failed' }
    }
    if (jobs.value.some((job) => job.status === 'expired')) {
      return { id: 'reload', label: 'reload 结果', detail: '任务过期，结果待核对', status: 'expired' }
    }
    return { id: 'reload', label: 'reload 结果', detail: '发布未完成', status: 'failed' }
  }
  const reloaded = jobs.value.filter((job) => resultValue(job, 'reloaded') === true).length
  if (reloaded === totalCount.value) {
    return {
      id: 'reload',
      label: 'reload 结果',
      detail: `${reloaded} / ${totalCount.value} 节点完成`,
      status: 'succeeded',
    }
  }
  return { id: 'reload', label: 'reload 结果', detail: '结果尚未确认', status: 'syncing' }
}

const stages = computed<ReleaseStage[]>(() => [
  certificateStage(),
  candidateStage(),
  agentStage(),
  reloadStage(),
])
</script>

<template>
  <section
    v-if="variant === 'summary'"
    class="release-channel release-channel-summary"
    :data-state="channel.state"
    :style="{ '--release-progress': `${summary.percent}%` }"
    :aria-busy="channel.state === 'running'"
    aria-label="发布通道摘要"
    aria-live="polite"
  >
    <header class="release-identity release-summary-identity">
      <span class="release-signal" aria-hidden="true"></span>
      <div>
        <strong>{{ channel.label }}</strong>
        <small>{{ channel.detail }}{{ parallelTaskNote ? ` · ${parallelTaskNote}` : '' }}</small>
      </div>
    </header>

    <div class="release-flow release-summary-copy">
      <div>
        <strong>{{ operationLabel }}</strong>
        <small>{{ site ? siteTitle(site) : summary.note }}</small>
      </div>
    </div>

    <div class="release-result release-summary-result">
      <strong>{{ summary.percent }}%</strong>
      <small>{{ summary.metric }}</small>
    </div>

    <div class="release-tools release-summary-tools">
      <strong>{{ summary.ratio }}</strong>
      <RouterLink to="/records">查看执行记录</RouterLink>
    </div>
  </section>

  <section
    v-else
    class="release-channel release-channel-flow"
    :data-state="channel.state"
    :style="{ '--release-progress': `${taskProgress}%` }"
    :aria-busy="channel.state === 'running'"
    aria-label="发布任务状态"
    aria-live="polite"
  >
    <header class="release-identity">
      <span class="release-signal" aria-hidden="true"></span>
      <div>
        <strong>{{ operationLabel }}</strong>
        <small>
          {{ site ? siteTitle(site) : '未选择配置' }}{{ parallelTaskNote ? ` · ${parallelTaskNote}` : '' }}
        </small>
      </div>
    </header>

    <div class="release-flow release-stage-flow">
      <span
        v-for="stage in stages"
        :key="stage.id"
        class="release-job release-stage"
        :class="`release-stage-${stage.id}`"
        :data-status="stage.status"
      >
        <strong>{{ stage.label }}</strong>
        <small>{{ stage.detail }}</small>
      </span>
    </div>

    <div class="release-result">
      <strong>{{ totalCount ? `${terminalCount} / ${totalCount}` : '—' }}</strong>
      <small>{{ totalCount ? '节点已结束' : '当前空闲' }}</small>
    </div>

    <div class="release-tools">
      <RouterLink to="/records">查看执行记录</RouterLink>
    </div>
  </section>
</template>
