<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { useConsoleStore } from '../stores/console'
import type { JobRecord, SiteRecord } from '../types'
import { siteTitle } from '../utils/format'

const props = defineProps<{
  site: SiteRecord | null | undefined
}>()

const store = useConsoleStore()

type PendingJobReference = {
  id: string
  nodeId: string
}

type ReleaseJobSlot = PendingJobReference & {
  job?: JobRecord
}

type ChannelState = 'idle' | 'running' | 'partial' | 'success'

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
    references.push({ id, nodeId })
  }
  return references
})

const jobSlots = computed<ReleaseJobSlot[]>(() => {
  const jobsById = new Map(store.jobs.map((job) => [job.id, job]))
  return pendingJobReferences.value.map((reference) => {
    const job = jobsById.get(reference.id)
    return {
      ...reference,
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
const terminalCount = computed(() => succeededCount.value + failedCount.value)
const progress = computed(() =>
  totalCount.value ? Math.round((terminalCount.value / totalCount.value) * 100) : 0,
)

const channel = computed<{ state: ChannelState; label: string; detail: string }>(() => {
  if (!totalCount.value) {
    return {
      state: 'idle',
      label: '当前空闲',
      detail: '没有待处理的节点任务',
    }
  }

  if (activeCount.value || syncingCount.value) {
    return {
      state: 'running',
      label: failedCount.value ? '执行中 · 有失败' : syncingCount.value ? '同步任务中' : '执行中',
      detail: failedCount.value
        ? `${activeCount.value + syncingCount.value} 个节点待完成，${failedCount.value} 个已失败`
        : syncingCount.value
          ? `${syncingCount.value} 个节点正在同步任务状态`
          : `${activeCount.value} 个节点正在处理`,
    }
  }

  if (failedCount.value) {
    return {
      state: 'partial',
      label: failedCount.value === totalCount.value ? '任务失败' : '部分失败',
      detail: `${failedCount.value} 个节点未完成`,
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

const operationLabel = computed(() => {
  const operation = props.site?.pendingRemote?.operation
  const labels: Record<string, string> = {
    publish: '配置发布',
    validate: '逐节点校验',
    reload: 'Nginx reload',
    delete: '从节点移除',
    transfer: '复制 / 迁移',
  }
  return typeof operation === 'string' ? labels[operation] || '节点任务' : '节点任务'
})

function jobStatusLabel(status: string) {
  const labels: Record<string, string> = {
    queued: '排队中',
    claimed: '执行中',
    running: '执行中',
    succeeded: '成功',
    failed: '失败',
    expired: '已过期',
  }
  return labels[status] || status
}

function slotNodeName(slot: ReleaseJobSlot) {
  return slot.job?.node_name || store.nodes.find((node) => node.id === slot.nodeId)?.node_name || slot.nodeId
}
</script>

<template>
  <section
    class="release-channel"
    :data-state="channel.state"
    :style="{ '--release-progress': `${progress}%` }"
    :aria-busy="channel.state === 'running'"
    aria-label="节点任务状态"
    aria-live="polite"
  >
    <header class="release-identity">
      <span class="release-signal" aria-hidden="true"></span>
      <div>
        <strong>{{ operationLabel }}</strong>
        <small>{{ site ? siteTitle(site) : '未选择配置' }}</small>
      </div>
    </header>

    <div class="release-flow">
      <span
        v-for="slot in jobSlots"
        :key="slot.id"
        class="release-job"
        :data-status="slot.job?.status || 'syncing'"
      >
        <strong>{{ slotNodeName(slot) }}</strong>
        <small>{{ slot.job ? jobStatusLabel(slot.job.status) : '同步中' }}</small>
      </span>
      <span v-if="!jobSlots.length" class="release-job release-job-empty">
        <strong>{{ channel.label }}</strong>
        <small>{{ channel.detail }}</small>
      </span>
    </div>

    <div class="release-result">
      <strong>{{ totalCount ? `${terminalCount} / ${totalCount}` : '—' }}</strong>
      <small>{{ channel.label }}</small>
    </div>

    <div class="release-tools">
      <RouterLink to="/records">查看执行记录</RouterLink>
    </div>
  </section>
</template>
