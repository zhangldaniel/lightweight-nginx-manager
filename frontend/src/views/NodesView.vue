<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  Activity,
  Check,
  CircleSlash,
  FileSearch,
  RefreshCw,
  Server,
  ShieldCheck,
  ShieldOff,
  X,
} from '@lucide/vue'
import { NButton, NSelect, useDialog } from 'naive-ui'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import { useConsoleStore } from '../stores/console'
import { relativeTime } from '../utils/format'

const store = useConsoleStore()
const dialog = useDialog()
const busyId = ref('')
const statusFilter = ref('')

const filteredNodes = computed(() =>
  store.nodes.filter((node) => !statusFilter.value || node.status === statusFilter.value),
)

async function decide(id: string, action: 'approve' | 'reject') {
  busyId.value = id
  try {
    await store.decideEnrollment(id, action)
  } catch (error) {
    store.notify('审批失败', 'danger', store.apiMessage(error))
  } finally {
    busyId.value = ''
  }
}

async function run(nodeId: string, action: 'inspect' | 'nginx_test' | 'nginx_reload' | 'config_inventory' | 'certificate_inventory') {
  busyId.value = `${nodeId}:${action}`
  try {
    await store.quickNodeAction(nodeId, action)
  } catch (error) {
    store.notify('任务提交失败', 'danger', store.apiMessage(error))
  } finally {
    busyId.value = ''
  }
}

function revoke(nodeId: string, nodeName: string) {
  dialog.error({
    title: `吊销 ${nodeName} 的 Agent 身份`,
    content:
      '吊销后，该 Agent 的机器凭据立即失效，排队中的任务会过期。重新接入需要在客户端重新安装并再次审批。',
    positiveText: '确认吊销',
    negativeText: '取消',
    async onPositiveClick() {
      busyId.value = `${nodeId}:revoke`
      try {
        await store.revokeNode(nodeId)
      } catch (error) {
        store.notify('Agent 吊销失败', 'danger', store.apiMessage(error))
      } finally {
        busyId.value = ''
      }
    },
  })
}
</script>

<template>
  <section class="page page-nodes">
    <PageHeader title="节点 Agent" description="Agent 主动连接控制端，只执行明确白名单内的 Nginx 操作。">
      <NSelect
        v-model:value="statusFilter"
        class="header-select"
        :options="[
          { label: '全部节点', value: '' },
          { label: '在线', value: 'online' },
          { label: '离线', value: 'offline' },
        ]"
      />
    </PageHeader>

    <section v-if="store.enrollments.length" class="enrollment-section">
      <div class="section-heading">
        <div>
          <span class="section-icon warning"><ShieldCheck :size="19" /></span>
          <div>
            <h2>待审批接入</h2>
            <p>Agent 已主动连接，但尚未获得节点操作权限。</p>
          </div>
        </div>
        <StatusTag :label="`${store.enrollments.length} 个待处理`" tone="warning" />
      </div>
      <div class="enrollment-grid">
        <article v-for="item in store.enrollments" :key="item.id" class="enrollment-card">
          <span class="node-avatar pending">?</span>
          <div class="enrollment-copy">
            <strong>{{ item.node_name }}</strong>
            <span>{{ item.hostname }}</span>
            <small>申请于 {{ relativeTime(item.requested_at) }}</small>
          </div>
          <div class="enrollment-actions">
            <NButton
              type="primary"
              :loading="busyId === item.id"
              :disabled="!store.isAdmin"
              @click="decide(item.id, 'approve')"
            >
              <template #icon><Check :size="16" /></template>
              批准
            </NButton>
            <NButton
              :disabled="!store.isAdmin"
              @click="decide(item.id, 'reject')"
            >
              <template #icon><X :size="16" /></template>
              拒绝
            </NButton>
          </div>
        </article>
      </div>
    </section>

    <div v-if="filteredNodes.length" class="node-grid">
      <article
        v-for="node in filteredNodes"
        :key="node.id"
        class="node-card spotlight-card"
        :class="{ online: node.status !== 'offline', offline: node.status === 'offline' }"
      >
        <header class="node-card-head">
          <span class="node-avatar"><Server :size="22" /></span>
          <div>
            <h2>{{ node.node_name }}</h2>
            <p>{{ node.hostname }}</p>
          </div>
          <StatusTag
            :label="node.status === 'offline' ? '离线' : '在线'"
            :tone="node.status === 'offline' ? 'danger' : 'success'"
            :pulse="node.status !== 'offline'"
          />
        </header>

        <dl class="node-facts">
          <div>
            <dt>Agent</dt>
            <dd>{{ node.agent_version || '未知' }}</dd>
          </div>
          <div>
            <dt>Nginx</dt>
            <dd>{{ node.nginx_version || '未知' }}</dd>
          </div>
          <div>
            <dt>最后心跳</dt>
            <dd>{{ relativeTime(node.last_seen_at) }}</dd>
          </div>
          <div>
            <dt>托管配置</dt>
            <dd>{{ store.sites.filter((site) => site.nodeIds.includes(node.id)).length }} 份</dd>
          </div>
        </dl>

        <div class="node-paths">
          <div>
            <span>主配置</span>
            <code>{{ node.facts.nginx_config || '未上报' }}</code>
          </div>
          <div>
            <span>证书目录</span>
            <code>{{ node.facts.managed_certificate_root || '未上报' }}</code>
          </div>
          <div class="config-entry-summary">
            <span>配置入口</span>
            <div>
              <code v-for="entry in node.facts.config_entries || []" :key="entry.id">
                {{ entry.context.toUpperCase() }} · {{ entry.directory }}/*{{ entry.suffix }}
              </code>
            </div>
          </div>
        </div>

        <footer class="node-actions">
          <NButton
            :loading="busyId === `${node.id}:inspect`"
            :disabled="node.status === 'offline' || !store.canOperate"
            @click="run(node.id, 'inspect')"
          >
            <template #icon><Activity :size="16" /></template>
            探测
          </NButton>
          <NButton
            :loading="busyId === `${node.id}:nginx_test`"
            :disabled="node.status === 'offline' || !store.canOperate"
            @click="run(node.id, 'nginx_test')"
          >
            <template #icon><FileSearch :size="16" /></template>
            nginx -t
          </NButton>
          <NButton
            type="primary"
            secondary
            :loading="busyId === `${node.id}:nginx_reload`"
            :disabled="node.status === 'offline' || !store.canOperate"
            @click="run(node.id, 'nginx_reload')"
          >
            <template #icon><RefreshCw :size="16" /></template>
            reload
          </NButton>
          <NButton
            :loading="busyId === `${node.id}:config_inventory`"
            :disabled="node.status === 'offline' || !store.canOperate"
            @click="run(node.id, 'config_inventory')"
          >
            扫描配置
          </NButton>
          <NButton
            :loading="busyId === `${node.id}:certificate_inventory`"
            :disabled="node.status === 'offline' || !store.canOperate"
            @click="run(node.id, 'certificate_inventory')"
          >
            扫描证书
          </NButton>
          <NButton
            type="error"
            quaternary
            :loading="busyId === `${node.id}:revoke`"
            :disabled="!store.isAdmin"
            @click="revoke(node.id, node.node_name)"
          >
            <template #icon><ShieldOff :size="16" /></template>
            吊销
          </NButton>
        </footer>
      </article>
    </div>

    <div v-else class="empty-state large">
      <CircleSlash :size="34" />
      <strong>没有匹配的 Agent</strong>
      <span>调整状态筛选，或使用安装脚本接入新节点。</span>
    </div>
  </section>
</template>
