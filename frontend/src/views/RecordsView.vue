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
            <span>动作 / 目标</span><span>状态</span><span>操作者</span><span>开始时间</span><span>完成时间</span>
          </div>
          <button
            v-for="job in jobs"
            :key="job.id"
            type="button"
            class="record-table record-table-jobs record-row"
            @click="showJob(job)"
          >
            <span><strong>{{ actionLabel(job.action) }}</strong><small>{{ job.node_name || job.node_id }}</small></span>
            <span><StatusTag :label="statusLabel(job.status)" :tone="statusTone(job.status)" /></span>
            <span>{{ job.created_by || '系统' }}</span>
            <span>{{ dateTime(job.created_at) }}</span>
            <span>{{ dateTime(job.completed_at) }}</span>
          </button>
        </NTabPane>

        <NTabPane name="operations" :tab="`平台操作（${operations.length}）`">
          <div class="record-table table-head">
            <span>操作 / 站点</span><span>状态</span><span>基础版本</span><span>操作者</span><span>时间</span>
          </div>
          <article v-for="operation in operations" :key="operation.id" class="record-table record-row">
            <span><strong>{{ actionLabel(operation.kind) }}</strong><small>{{ operation.site_id }}</small></span>
            <span><StatusTag :label="statusLabel(operation.status)" :tone="statusTone(operation.status)" /></span>
            <span>v{{ operation.base_version }}</span>
            <span>{{ operation.created_by }}</span>
            <span>{{ dateTime(operation.created_at) }}</span>
          </article>
        </NTabPane>

        <NTabPane name="audit" :tab="`审计事件（${audits.length}）`">
          <div class="record-table audit-table table-head">
            <span>事件</span><span>操作者</span><span>目标</span><span>时间</span>
          </div>
          <article v-for="item in audits" :key="item.id" class="record-table audit-table record-row">
            <span><strong>{{ item.event }}</strong><small>#{{ item.id }}</small></span>
            <span>{{ item.actor_id }}</span>
            <span>{{ item.target_type }} · {{ item.target_id || '—' }}</span>
            <span>{{ dateTime(item.created_at) }}</span>
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
