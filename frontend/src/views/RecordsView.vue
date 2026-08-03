<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Clock3, FileClock, Search } from '@lucide/vue'
import { NButton, NInput, NModal, NSelect, NTabPane, NTabs } from 'naive-ui'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import { api } from '../api'
import { useConsoleStore } from '../stores/console'
import type { JobRecord } from '../types'
import { dateTime } from '../utils/format'

const store = useConsoleStore()
const activeTab = ref('jobs')
const search = ref('')
const statusFilter = ref('')
const selectedJob = ref<JobRecord | null>(null)
const detailOpen = ref(false)

const jobs = computed(() =>
  store.jobs.filter((job) => {
    const keyword = search.value.trim().toLowerCase()
    return (
      (!statusFilter.value || job.status === statusFilter.value) &&
      (!keyword ||
        [job.node_name, job.action, job.created_by, job.id]
          .filter(Boolean)
          .some((value) => String(value).toLowerCase().includes(keyword)))
    )
  }),
)
const operations = computed(() =>
  store.operations.filter((operation) => {
    const keyword = search.value.trim().toLowerCase()
    return (
      (!statusFilter.value || operation.status === statusFilter.value) &&
      (!keyword ||
        [operation.site_id, operation.kind, operation.created_by, operation.id]
          .some((value) => String(value).toLowerCase().includes(keyword)))
    )
  }),
)
const audits = computed(() =>
  store.audit.filter((item) => {
    const keyword = search.value.trim().toLowerCase()
    return (
      !keyword ||
      [item.actor_id, item.event, item.target_type, item.target_id]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(keyword))
    )
  }),
)

function statusTone(status: string) {
  if (status === 'succeeded') return 'success' as const
  if (['failed', 'expired'].includes(status)) return 'danger' as const
  if (status === 'partial') return 'warning' as const
  if (['queued', 'running', 'claimed'].includes(status)) return 'info' as const
  return 'neutral' as const
}

function statusLabel(status: string) {
  const labels: Record<string, string> = {
    succeeded: '成功',
    failed: '失败',
    expired: '已过期',
    partial: '部分成功',
    queued: '排队中',
    running: '执行中',
    claimed: '已领取',
  }
  return labels[status] || status
}

function actionLabel(action: string) {
  const labels: Record<string, string> = {
    config_apply: '配置发布',
    config_delete: '删除配置',
    config_move: '迁移配置',
    config_inventory: '配置扫描',
    certificate_inventory: '证书扫描',
    certificate_apply: '证书替换',
    nginx_test: 'Nginx 校验',
    nginx_reload: 'Nginx reload',
    inspect: '节点探测',
  }
  return labels[action] || action
}

function rowState(status: string) {
  if (['failed', 'expired'].includes(status)) return 'is-failed'
  if (status === 'partial') return 'is-warning'
  if (['queued', 'running', 'claimed'].includes(status)) return 'is-active'
  return ''
}

function payloadMessage(payload: Record<string, unknown> | null | undefined) {
  if (!payload) return ''
  const keys = ['error', 'message', 'summary', 'failure_reason', 'stderr', 'failure_stage']
  for (const key of keys) {
    const value = payload[key]
    if (typeof value === 'string' && value.trim()) {
      return value.trim().split(/\r?\n/, 1)[0]
    }
    if (typeof value === 'number') return String(value)
  }
  return ''
}

function statusHint(status: string, payload?: Record<string, unknown> | null) {
  const message = payloadMessage(payload)
  if (message) return message
  const hints: Record<string, string> = {
    failed: '打开详情查看失败输出',
    expired: '任务未在有效期内完成',
    partial: '部分目标未完成',
    queued: '等待 Agent 领取',
    claimed: 'Agent 已领取任务',
    running: 'Agent 正在执行',
    succeeded: '任务已正常完成',
  }
  return hints[status] || '状态已记录'
}

function durationLabel(start: string | null | undefined, end: string | null | undefined, status: string) {
  if (!end) return ['queued', 'claimed', 'running'].includes(status) ? '进行中' : '—'
  const startAt = Date.parse(start || '')
  const endAt = Date.parse(end)
  if (!Number.isFinite(startAt) || !Number.isFinite(endAt)) return '—'
  const seconds = Math.max(0, Math.round((endAt - startAt) / 1000))
  if (seconds < 1) return '< 1 秒'
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 时 ${minutes % 60} 分`
  return `${Math.floor(hours / 24)} 天 ${hours % 24} 时`
}

function showJob(job: JobRecord) {
  selectedJob.value = job
  detailOpen.value = true
}

async function refreshAudit() {
  try {
    const response = await api.audit()
    store.audit = response.items
  } catch (error) {
    store.notify('审计记录读取失败', 'danger', store.apiMessage(error))
  }
}

onMounted(refreshAudit)
</script>

<template>
  <section class="page page-records">
    <PageHeader title="执行记录" description="追踪操作、Agent 任务和登录审批等审计事件。">
      <NButton @click="store.refresh(true)">刷新记录</NButton>
    </PageHeader>

    <div class="records-toolbar">
      <NInput v-model:value="search" clearable placeholder="搜索节点、动作、站点或操作者">
        <template #prefix><Search :size="17" /></template>
      </NInput>
      <NSelect
        v-model:value="statusFilter"
        :options="[
          { label: '全部状态', value: '' },
          { label: '成功', value: 'succeeded' },
          { label: '失败', value: 'failed' },
          { label: '部分成功', value: 'partial' },
          { label: '执行中', value: 'running' },
          { label: '排队中', value: 'queued' },
        ]"
      />
    </div>

    <section class="records-panel">
      <NTabs v-model:value="activeTab" type="line" animated>
        <NTabPane name="jobs" :tab="`Agent 任务（${jobs.length}）`">
          <div class="record-table record-table-jobs table-head">
            <span>任务 / 目标</span><span>状态</span><span>耗时</span><span>操作者</span><span>开始 / 完成</span>
          </div>
          <button
            v-for="job in jobs"
            :key="job.id"
            type="button"
            class="record-table record-table-jobs record-row"
            :class="rowState(job.status)"
            :aria-label="`查看 ${actionLabel(job.action)}，${job.node_name || job.node_id} 的任务详情`"
            @click="showJob(job)"
          >
            <span class="record-primary">
              <strong>{{ actionLabel(job.action) }}</strong>
              <small :title="job.node_name || job.node_id">{{ job.node_name || job.node_id }}</small>
            </span>
            <span class="record-status-cell">
              <StatusTag :label="statusLabel(job.status)" :tone="statusTone(job.status)" />
              <small :title="statusHint(job.status, job.result)">{{ statusHint(job.status, job.result) }}</small>
            </span>
            <span class="record-duration-cell">
              <strong>{{ durationLabel(job.claimed_at || job.created_at, job.completed_at, job.status) }}</strong>
              <small>{{ job.completed_at ? '执行耗时' : '尚未完成' }}</small>
            </span>
            <span class="record-operator-cell">
              <small class="record-mobile-label">操作者</small>
              {{ job.created_by || '系统' }}
            </span>
            <span class="record-time-cell">
              <span class="record-time-line"><small>开始</small><time :datetime="job.created_at">{{ dateTime(job.created_at) }}</time></span>
              <span class="record-time-line"><small>完成</small><time :datetime="job.completed_at || undefined">{{ dateTime(job.completed_at) }}</time></span>
            </span>
          </button>
        </NTabPane>

        <NTabPane name="operations" :tab="`平台操作（${operations.length}）`">
          <div class="record-table record-table-operations table-head">
            <span>操作 / 站点</span><span>状态</span><span>耗时</span><span>操作者</span><span>开始 / 完成</span>
          </div>
          <article
            v-for="operation in operations"
            :key="operation.id"
            class="record-table record-table-operations record-row"
            :class="rowState(operation.status)"
          >
            <span class="record-primary">
              <span class="record-title-line">
                <strong>{{ actionLabel(operation.kind) }}</strong>
                <em>基线 v{{ operation.base_version }}</em>
              </span>
              <small :title="operation.site_id">{{ operation.site_id }}</small>
            </span>
            <span class="record-status-cell">
              <StatusTag :label="statusLabel(operation.status)" :tone="statusTone(operation.status)" />
              <small :title="statusHint(operation.status, operation.metadata)">{{ statusHint(operation.status, operation.metadata) }}</small>
            </span>
            <span class="record-duration-cell">
              <strong>{{ durationLabel(operation.created_at, operation.completed_at, operation.status) }}</strong>
              <small>{{ operation.completed_at ? '总耗时' : '尚未完成' }}</small>
            </span>
            <span class="record-operator-cell">
              <small class="record-mobile-label">操作者</small>
              {{ operation.created_by || '系统' }}
            </span>
            <span class="record-time-cell">
              <span class="record-time-line"><small>开始</small><time :datetime="operation.created_at">{{ dateTime(operation.created_at) }}</time></span>
              <span class="record-time-line"><small>完成</small><time :datetime="operation.completed_at || undefined">{{ dateTime(operation.completed_at) }}</time></span>
            </span>
          </article>
        </NTabPane>

        <NTabPane name="audit" :tab="`审计事件（${audits.length}）`">
          <div class="record-table record-table-audit table-head">
            <span>事件</span><span>操作者</span><span>目标</span><span>时间</span>
          </div>
          <article v-for="item in audits" :key="item.id" class="record-table record-table-audit record-row">
            <span class="record-primary"><strong>{{ item.event }}</strong><small>#{{ item.id }}</small></span>
            <span class="record-operator-cell"><small class="record-mobile-label">操作者</small>{{ item.actor_id }}</span>
            <span class="record-audit-target"><small class="record-mobile-label">目标</small>{{ item.target_type }} · {{ item.target_id || '—' }}</span>
            <span class="record-audit-time"><time :datetime="item.created_at">{{ dateTime(item.created_at) }}</time></span>
          </article>
        </NTabPane>
      </NTabs>

      <div
        v-if="
          (activeTab === 'jobs' && !jobs.length) ||
          (activeTab === 'operations' && !operations.length) ||
          (activeTab === 'audit' && !audits.length)
        "
        class="empty-state"
      >
        <FileClock :size="28" />
        <strong>没有匹配的记录</strong>
        <span>调整搜索和状态条件。</span>
      </div>
    </section>

    <NModal
      v-model:show="detailOpen"
      preset="card"
      class="record-detail-modal"
      title="Agent 任务详情"
      :bordered="false"
    >
      <div v-if="selectedJob" class="job-detail">
        <div class="job-detail-head">
          <span class="section-icon"><Clock3 :size="19" /></span>
          <div>
            <h3>{{ actionLabel(selectedJob.action) }} · {{ selectedJob.node_name }}</h3>
            <code>{{ selectedJob.id }}</code>
          </div>
          <StatusTag :label="statusLabel(selectedJob.status)" :tone="statusTone(selectedJob.status)" />
        </div>
        <dl>
          <div><dt>创建时间</dt><dd>{{ dateTime(selectedJob.created_at) }}</dd></div>
          <div><dt>领取时间</dt><dd>{{ dateTime(selectedJob.claimed_at) }}</dd></div>
          <div><dt>完成时间</dt><dd>{{ dateTime(selectedJob.completed_at) }}</dd></div>
          <div><dt>操作者</dt><dd>{{ selectedJob.created_by || '系统' }}</dd></div>
        </dl>
        <div class="detail-section">
          <h3>Agent 输出</h3>
          <pre class="code-panel"><code>{{ JSON.stringify(selectedJob.result || {}, null, 2) }}</code></pre>
        </div>
      </div>
    </NModal>
  </section>
</template>
