<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import {
  AlertTriangle,
  ArrowRightLeft,
  Copy,
  FileCode2,
  History,
  Plus,
  RotateCcw,
  Search,
  Server,
  ShieldCheck,
} from '@lucide/vue'
import {
  NButton,
  NCheckbox,
  NDialog,
  NInput,
  NModal,
  NSelect,
  NTabPane,
  NTabs,
  useDialog,
} from 'naive-ui'
import PageHeader from '../components/PageHeader.vue'
import MetricCard from '../components/MetricCard.vue'
import StatusTag from '../components/StatusTag.vue'
import { useConsoleStore } from '../stores/console'
import { api } from '../api'
import type { SiteRecord, SiteRevision } from '../types'
import { defaultSiteConfig, nodeEntries, safeName, uid } from '../utils/config'
import { certificateDays, relativeTime, siteKind, siteStatus, siteTitle } from '../utils/format'

const store = useConsoleStore()
const dialog = useDialog()
const search = ref('')
const nodeFilter = ref('')
const statusFilter = ref('')
const editorOpen = ref(false)
const editorMode = ref<'create' | 'edit'>('create')
const editorTab = ref<'guided' | 'conf' | 'generic'>('guided')
const saving = ref(false)
const running = ref(false)
const scanning = ref(false)
const transferOpen = ref(false)
const transferring = ref(false)
const transferMode = ref<'create' | 'replace'>('create')
const transferNodeIds = ref<string[]>([])
const transferEntryIds = reactive<Record<string, string>>({})
const historyOpen = ref(false)
const historyLoading = ref(false)
const revisions = ref<SiteRevision[]>([])

const form = reactive({
  id: '',
  domain: '',
  name: '',
  filename: '',
  type: 'proxy',
  target: '',
  context: 'http' as 'http' | 'stream' | 'main',
  environment: '生产',
  nodeIds: [] as string[],
  certificateId: '',
  note: '',
  changeNote: '',
  config: '',
  nodeConfigEntryIds: {} as Record<string, string>,
})
const originalOperational = ref('')

const nodeOptions = computed(() => [
  { label: `全部 Agent（${store.sites.length}）`, value: '' },
  ...store.nodes.map((node) => ({
    label: `${node.node_name}（${store.sites.filter((site) => site.nodeIds.includes(node.id)).length}）`,
    value: node.id,
  })),
])
const statusOptions = [
  { label: '全部状态', value: '' },
  { label: '已发布', value: 'published' },
  { label: '有草稿', value: 'draft' },
  { label: '配置漂移', value: 'drift' },
  { label: '发布失败', value: 'failed' },
  { label: '未部署', value: 'unassigned' },
]

const filteredSites = computed(() => {
  const keyword = search.value.trim().toLowerCase()
  return store.sites.filter((site) => {
    const matchedKeyword =
      !keyword ||
      [siteTitle(site), site.note, site.changeNote, site.filename]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(keyword))
    const matchedNode = !nodeFilter.value || site.nodeIds.includes(nodeFilter.value)
    const matchedStatus = !statusFilter.value || site.status === statusFilter.value
    return matchedKeyword && matchedNode && matchedStatus
  })
})

const selected = computed(() => store.selectedSite)
const draftCount = computed(
  () => store.sites.filter((site) => ['draft', 'failed', 'drift'].includes(site.status)).length,
)
const pendingCount = computed(() => store.sites.filter((site) => Boolean(site.pendingRemote)).length)
const availableCertificates = computed(() => {
  const domain = form.domain.trim()
  return [
    { label: '暂不绑定（保留 Conf 内现有路径）', value: '' },
    ...store.certificates.map((certificate) => {
      const nodeCovered = form.nodeIds.every((id) => certificate.nodeIds.includes(id))
      const domainCovered = !domain || coversDomain(certificate.domain, domain)
      const days = certificateDays(certificate)
      return {
        label: `${certificate.domain}${days === null ? '' : ` · 剩余 ${days} 天`}`,
        value: certificate.id,
        disabled: !nodeCovered || !domainCovered,
      }
    }),
  ]
})

watch(
  () => filteredSites.value.map((item) => item.id).join(','),
  () => {
    if (
      filteredSites.value.length &&
      !filteredSites.value.some((item) => item.id === store.selectedSiteId)
    ) {
      store.selectedSiteId = filteredSites.value[0].id
    }
  },
)

function coversDomain(pattern: string, domain: string) {
  const expected = pattern.toLowerCase().replace(/\.$/, '')
  const actual = domain.toLowerCase().replace(/\.$/, '')
  if (expected === actual) return true
  if (!expected.startsWith('*.')) return false
  const suffix = expected.slice(2)
  return actual.endsWith(`.${suffix}`) && actual.split('.').length === suffix.split('.').length + 1
}

function operationalSnapshot(value: typeof form) {
  return JSON.stringify({
    domain: value.domain,
    name: value.name,
    filename: value.filename,
    type: value.type,
    target: value.target,
    context: value.context,
    environment: value.environment,
    nodeIds: [...value.nodeIds].sort(),
    certificateId: value.certificateId,
    config: value.config,
    nodeConfigEntryIds: value.nodeConfigEntryIds,
  })
}

function resetForm() {
  Object.assign(form, {
    id: uid('site'),
    domain: '',
    name: '',
    filename: '',
    type: 'proxy',
    target: '',
    context: 'http',
    environment: '生产',
    nodeIds: [],
    certificateId: '',
    note: '',
    changeNote: '',
    config: defaultSiteConfig('api.example.com', '127.0.0.1:8080'),
    nodeConfigEntryIds: {},
  })
}

function openCreate() {
  editorMode.value = 'create'
  editorTab.value = 'guided'
  resetForm()
  originalOperational.value = ''
  editorOpen.value = true
}

function openEdit(site: SiteRecord) {
  editorMode.value = 'edit'
  editorTab.value =
    site.resourceType === 'generic' ? 'generic' : site.configMode === 'guided' ? 'guided' : 'conf'
  Object.assign(form, {
    id: site.id,
    domain: site.domain || '',
    name: site.name || '',
    filename: site.filename || '',
    type: site.type || 'proxy',
    target: site.target || '',
    context: site.context === 'main' ? 'main' : site.context === 'stream' ? 'stream' : 'http',
    environment: site.environment || '生产',
    nodeIds: [...(site.nodeIds || [])],
    certificateId: site.certificateId || '',
    note: site.note || '',
    changeNote: '',
    config: site.config || '',
    nodeConfigEntryIds: { ...(site.nodeConfigEntryIds || {}) },
  })
  originalOperational.value = operationalSnapshot(form)
  editorOpen.value = true
}

function applyTemplate(kind: string) {
  if (kind === 'https') {
    form.config = [
      'server {',
      '  listen 443 ssl;',
      `  server_name ${form.domain || 'api.example.com'};`,
      '',
      '  ssl_certificate     /apps/nginx/cert/example.com.pem;',
      '  ssl_certificate_key /apps/nginx/cert/example.com.key;',
      '',
      '  location / {',
      `    proxy_pass ${form.target || 'http://127.0.0.1:8080'};`,
      '    proxy_http_version 1.1;',
      '    proxy_set_header Host $host;',
      '    proxy_set_header X-Real-IP $remote_addr;',
      '    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
      '  }',
      '}',
    ].join('\n')
  } else if (kind === 'websocket') {
    form.config = [
      'server {',
      '  listen 443 ssl;',
      `  server_name ${form.domain || 'ws.example.com'};`,
      '',
      '  location / {',
      `    proxy_pass ${form.target || 'http://127.0.0.1:8080'};`,
      '    proxy_http_version 1.1;',
      '    proxy_set_header Upgrade $http_upgrade;',
      '    proxy_set_header Connection "upgrade";',
      '    proxy_set_header Host $host;',
      '    proxy_set_header X-Real-IP $remote_addr;',
      '  }',
      '}',
    ].join('\n')
  } else if (kind === 'stream') {
    form.context = 'stream'
    form.config = [
      'upstream tcp_backend {',
      '  server 127.0.0.1:3306;',
      '}',
      '',
      'server {',
      '  listen 13306;',
      '  proxy_pass tcp_backend;',
      '}',
    ].join('\n')
  } else {
    form.config = defaultSiteConfig(form.domain, form.target)
  }
}

function ensureEntrySelection(nodeId: string) {
  if (form.context === 'main') return
  const node = store.nodes.find((item) => item.id === nodeId)
  if (!node) return
  const entries = nodeEntries(node, form.context)
  if (!entries.some((item) => item.id === form.nodeConfigEntryIds[nodeId])) {
    const entry = entries.find((item) => item.default) || entries[0]
    if (entry) form.nodeConfigEntryIds[nodeId] = entry.id
  }
}

function toggleNode(nodeId: string, checked: boolean) {
  if (checked && !form.nodeIds.includes(nodeId)) form.nodeIds.push(nodeId)
  if (!checked) form.nodeIds = form.nodeIds.filter((item) => item !== nodeId)
  ensureEntrySelection(nodeId)
}

function validateForm() {
  if (!form.nodeIds.length) return '请选择至少一个部署节点'
  if (editorTab.value !== 'generic' && !form.domain.trim()) return '请输入域名'
  if (editorTab.value === 'generic' && !form.name.trim()) return '请输入配置名称'
  if (!form.config.trim()) return 'Nginx Conf 不能为空'
  return ''
}

async function saveDraft() {
  const validation = validateForm()
  if (validation) {
    store.notify(validation, 'warning')
    return
  }
  saving.value = true
  try {
    const previous = store.sites.find((item) => item.id === form.id)
    const operationalChanged = operationalSnapshot(form) !== originalOperational.value
    const resourceType = editorTab.value === 'generic' ? 'generic' : 'site'
    const site: SiteRecord = {
      ...(previous || {}),
      id: form.id,
      resourceType,
      name: resourceType === 'generic' ? form.name.trim() : undefined,
      filename:
        resourceType === 'generic'
          ? form.filename.trim() || `${safeName(form.name)}.${form.context === 'stream' ? 'stream' : 'conf'}`
          : previous?.filename,
      domain: resourceType === 'site' ? form.domain.trim() : undefined,
      type: resourceType === 'generic' ? 'custom' : form.type,
      target: form.target.trim(),
      context: form.context,
      configMode: resourceType === 'generic' ? 'generic' : editorTab.value === 'guided' ? 'guided' : 'conf',
      config: form.config,
      environment: form.environment,
      nodeIds: [...form.nodeIds],
      certificateId: resourceType === 'generic' ? '' : form.certificateId,
      version: Number(previous?.version || 0),
      status:
        previous && !operationalChanged
          ? previous.status
          : previous?.status === 'unassigned'
            ? 'draft'
            : 'draft',
      note: form.note.trim(),
      changeNote: form.changeNote.trim(),
      updatedAt: new Date().toISOString(),
      nodeHashes: { ...(previous?.nodeHashes || {}) },
      nodeConfigPaths: { ...(previous?.nodeConfigPaths || {}) },
      nodeConfigs: { ...(previous?.nodeConfigs || {}) },
      nodeConfigEntryIds: { ...form.nodeConfigEntryIds },
      history: previous?.history || [],
    }
    await store.upsertSite(site)
    editorOpen.value = false
  } catch (error) {
    store.notify('保存失败', 'danger', store.apiMessage(error))
  } finally {
    saving.value = false
  }
}

function openTransfer() {
  if (!selected.value || selected.value.context === 'main') return
  transferNodeIds.value = []
  transferMode.value = 'create'
  for (const key of Object.keys(transferEntryIds)) delete transferEntryIds[key]
  transferOpen.value = true
}

function transferEntries(nodeId: string) {
  const site = selected.value
  const node = store.nodes.find((item) => item.id === nodeId)
  if (!site || !node || site.context === 'main') return []
  return nodeEntries(node, site.context === 'stream' ? 'stream' : 'http')
}

function toggleTransferNode(nodeId: string, checked: boolean) {
  if (checked && !transferNodeIds.value.includes(nodeId)) {
    transferNodeIds.value.push(nodeId)
    const site = selected.value
    const entries = transferEntries(nodeId)
    const current = site?.nodeConfigEntryIds?.[nodeId]
    transferEntryIds[nodeId] =
      entries.find((entry) => entry.id !== current)?.id || entries[0]?.id || ''
  }
  if (!checked) {
    transferNodeIds.value = transferNodeIds.value.filter((item) => item !== nodeId)
    delete transferEntryIds[nodeId]
  }
}

async function submitTransfer() {
  if (!selected.value || !transferNodeIds.value.length) {
    store.notify('请选择至少一个目标节点', 'warning')
    return
  }
  transferring.value = true
  try {
    await store.transferSite(
      selected.value.id,
      transferNodeIds.value.map((nodeId) => ({
        nodeId,
        entryId: transferEntryIds[nodeId],
      })),
      transferMode.value,
    )
    transferOpen.value = false
  } catch (error) {
    store.notify('复制 / 迁移任务未提交', 'danger', store.apiMessage(error))
  } finally {
    transferring.value = false
  }
}

async function openHistory() {
  if (!selected.value) return
  historyOpen.value = true
  historyLoading.value = true
  try {
    revisions.value = (await api.siteRevisions(selected.value.id)).items
  } catch (error) {
    store.notify('版本记录读取失败', 'danger', store.apiMessage(error))
  } finally {
    historyLoading.value = false
  }
}

async function restoreRevision(version: number) {
  const current = selected.value
  if (!current) return
  historyLoading.value = true
  try {
    const revision = await api.siteRevision(current.id, version)
    if (!revision.snapshot) throw new Error('该版本没有可恢复的配置快照')
    const restored: SiteRecord = {
      ...current,
      ...revision.snapshot,
      id: current.id,
      version: current.version,
      nodeIds: [...current.nodeIds],
      nodeHashes: { ...(current.nodeHashes || {}) },
      nodeConfigPaths: { ...(current.nodeConfigPaths || {}) },
      nodeConfigEntryIds: { ...(current.nodeConfigEntryIds || {}) },
      nodeConfigs: { ...(current.nodeConfigs || {}) },
      status: 'draft',
      changeNote: `从已发布版本 v${version} 恢复为草稿`,
      updatedAt: new Date().toISOString(),
    }
    delete restored.pendingRemote
    delete restored.lastFailure
    await store.upsertSite(restored)
    historyOpen.value = false
    store.notify(`v${version} 已恢复为草稿`, 'success', '节点文件尚未改变，请核对后再发布。')
  } catch (error) {
    store.notify('版本恢复失败', 'danger', store.apiMessage(error))
  } finally {
    historyLoading.value = false
  }
}

async function run(publish: boolean) {
  if (!selected.value) return
  running.value = true
  try {
    await store.runSite(selected.value.id, publish)
  } catch (error) {
    store.notify(publish ? '发布未提交' : '校验未提交', 'danger', store.apiMessage(error))
  } finally {
    running.value = false
  }
}

async function scanSites() {
  scanning.value = true
  try {
    await store.scanInventory('config_inventory')
  } catch (error) {
    store.notify('配置扫描未提交', 'danger', store.apiMessage(error))
  } finally {
    scanning.value = false
  }
}

function removeFromNodes() {
  if (!selected.value) return
  const site = selected.value
  dialog.warning({
    title: `从节点移除 ${siteTitle(site)}`,
    content: `将从 ${site.nodeIds.length} 个节点删除受托管配置，并执行 nginx -t 和 reload。平台记录与证书不会删除。`,
    positiveText: '确认移除',
    negativeText: '取消',
    async onPositiveClick() {
      try {
        await store.removeSiteFromNodes(site.id, [...site.nodeIds])
      } catch (error) {
        store.notify('移除任务未提交', 'danger', store.apiMessage(error))
      }
    },
  })
}

function deleteRecord() {
  if (!selected.value) return
  const site = selected.value
  dialog.error({
    title: `删除平台记录 ${siteTitle(site)}`,
    content: '此操作只允许用于已没有部署节点的记录，删除后无法从平台恢复。',
    positiveText: '确认删除',
    negativeText: '取消',
    async onPositiveClick() {
      try {
        await store.removeSiteRecord(site.id)
      } catch (error) {
        store.notify('删除失败', 'danger', store.apiMessage(error))
      }
    },
  })
}
</script>

<template>
  <section class="page page-sites">
    <PageHeader title="站点与配置" description="以站点为中心管理配置、证书和多节点发布。">
      <NButton
        secondary
        :loading="scanning"
        :disabled="!store.canOperate"
        @click="scanSites"
      >
        导入节点现有配置
      </NButton>
      <NButton type="primary" :disabled="!store.canOperate" @click="openCreate">
        <template #icon><Plus :size="18" /></template>
        新增站点
      </NButton>
    </PageHeader>

    <div class="metrics-grid">
      <MetricCard
        label="托管配置"
        :value="store.sites.length"
        note="站点、Stream 与通用 Conf"
        :icon="FileCode2"
        featured
      />
      <MetricCard
        label="在线 Agent"
        :value="`${store.onlineCount}/${store.nodes.length}`"
        note="主动出站连接"
        :icon="Server"
        tone="success"
      />
      <MetricCard
        label="待处理"
        :value="draftCount"
        note="草稿、漂移与失败"
        :icon="AlertTriangle"
        :tone="draftCount ? 'warning' : 'neutral'"
      />
      <MetricCard
        label="执行中"
        :value="pendingCount"
        note="Agent 正在处理"
        :icon="ArrowRightLeft"
        :tone="pendingCount ? 'info' : 'neutral'"
      />
    </div>

    <div class="master-detail">
      <section class="data-panel">
        <div class="filter-bar">
          <NInput v-model:value="search" clearable placeholder="搜索域名、配置备注或变更说明">
            <template #prefix><Search :size="17" /></template>
          </NInput>
          <NSelect v-model:value="nodeFilter" :options="nodeOptions" />
          <NSelect v-model:value="statusFilter" :options="statusOptions" />
        </div>

        <div class="site-table table-head" aria-hidden="true">
          <span>站点 / 配置备注</span>
          <span>目标节点</span>
          <span>证书</span>
          <span>版本</span>
          <span>状态</span>
        </div>
        <div v-if="filteredSites.length" class="site-list">
          <button
            v-for="site in filteredSites"
            :key="site.id"
            type="button"
            class="site-table site-row"
            :class="{ selected: store.selectedSiteId === site.id }"
            @click="store.selectedSiteId = site.id"
          >
            <span class="site-primary">
              <strong>{{ siteTitle(site) }}</strong>
              <small>{{ site.note || siteKind(site) }}</small>
            </span>
            <span class="node-chip-stack">
              <span v-for="nodeId in site.nodeIds.slice(0, 2)" :key="nodeId" class="node-chip">
                {{ store.nodes.find((item) => item.id === nodeId)?.node_name || nodeId }}
              </span>
              <span v-if="site.nodeIds.length > 2" class="node-chip">+{{ site.nodeIds.length - 2 }}</span>
              <small v-if="!site.nodeIds.length">未部署</small>
            </span>
            <span>
              <strong>{{
                store.certificates.find((item) => item.id === site.certificateId)?.domain || '未绑定'
              }}</strong>
              <small>{{ site.certificateId ? '节点已有证书' : '仅 HTTP / Conf 自管' }}</small>
            </span>
            <span><strong>v{{ site.version }}</strong><small>{{ relativeTime(site.updatedAt) }}</small></span>
            <span><StatusTag v-bind="siteStatus(site)" :pulse="Boolean(site.pendingRemote)" /></span>
          </button>
        </div>
        <div v-else class="empty-state">
          <Search :size="26" />
          <strong>没有匹配的配置</strong>
          <span>调整筛选条件，或创建一个新站点。</span>
        </div>
      </section>

      <aside v-if="selected" class="detail-panel">
        <div class="detail-head">
          <div>
            <h2>{{ siteTitle(selected) }}</h2>
            <p>{{ selected.environment || '生产' }} · {{ siteKind(selected) }} · 配置 v{{ selected.version }}</p>
          </div>
          <StatusTag v-bind="siteStatus(selected)" :pulse="Boolean(selected.pendingRemote)" />
        </div>

        <div v-if="selected.lastFailure" class="failure-card">
          <AlertTriangle :size="20" />
          <div>
            <strong>上次操作未完成</strong>
            <p>{{ selected.lastFailure.summary || selected.lastFailure.message || 'Agent 返回失败' }}</p>
            <small>
              {{ selected.lastFailure.node || '目标节点' }} ·
              {{ selected.lastFailure.stage || '执行阶段未知' }}
            </small>
          </div>
        </div>

        <div class="detail-actions">
          <NButton :disabled="!store.canOperate" @click="openEdit(selected)">编辑配置</NButton>
          <NButton
            v-if="selected.context !== 'main'"
            :disabled="!store.canOperate"
            @click="openTransfer"
          >
            <template #icon><Copy :size="16" /></template>
            复制 / 迁移
          </NButton>
          <NButton @click="openHistory">
            <template #icon><History :size="16" /></template>
            版本记录
          </NButton>
          <NButton :loading="running" :disabled="!store.canOperate" @click="run(false)">
            逐节点校验
          </NButton>
          <NButton
            type="primary"
            :loading="running"
            :disabled="!store.canOperate || !selected.nodeIds.length"
            @click="run(true)"
          >
            校验并发布
          </NButton>
          <NButton
            v-if="selected.nodeIds.length"
            type="error"
            secondary
            :disabled="!store.canOperate"
            @click="removeFromNodes"
          >
            从节点移除
          </NButton>
          <NButton
            v-else
            type="error"
            secondary
            :disabled="!store.canOperate"
            @click="deleteRecord"
          >
            删除平台记录
          </NButton>
        </div>

        <div class="detail-section">
          <h3>配置备注</h3>
          <p>{{ selected.note || '尚未填写配置用途和负责人。' }}</p>
          <small v-if="selected.changeNote">最近变更：{{ selected.changeNote }}</small>
        </div>

        <div class="detail-section">
          <h3>部署节点</h3>
          <div class="deployment-list">
            <article v-for="nodeId in selected.nodeIds" :key="nodeId">
              <span class="online-dot"></span>
              <div>
                <strong>{{ store.nodes.find((item) => item.id === nodeId)?.node_name || nodeId }}</strong>
                <code>{{
                  selected.nodeConfigPaths?.[nodeId] ||
                  store.nodes.find((item) => item.id === nodeId)?.facts.managed_config_root ||
                  '等待 Agent 上报路径'
                }}</code>
              </div>
              <StatusTag
                :label="
                  store.nodes.find((item) => item.id === nodeId)?.status === 'offline' ? '离线' : '在线'
                "
                :tone="
                  store.nodes.find((item) => item.id === nodeId)?.status === 'offline'
                    ? 'danger'
                    : 'success'
                "
              />
            </article>
          </div>
        </div>

        <div class="detail-section code-preview-section">
          <h3>配置预览</h3>
          <pre class="code-panel"><code>{{ selected.config }}</code></pre>
        </div>
      </aside>
    </div>

    <NModal
      v-model:show="editorOpen"
      preset="card"
      class="site-editor-modal"
      :title="editorMode === 'create' ? '新增站点' : `编辑 ${selected ? siteTitle(selected) : ''}`"
      :bordered="false"
      :mask-closable="false"
    >
      <NTabs v-model:value="editorTab" type="segment" animated>
        <NTabPane name="guided" tab="向导模式" :disabled="form.context === 'main'">
          <span class="tab-help">填写域名和上游，自动生成基础配置。</span>
        </NTabPane>
        <NTabPane name="conf" tab="站点 Conf" :disabled="form.context === 'main'">
          <span class="tab-help">直接编辑站点级 Nginx Conf。</span>
        </NTabPane>
        <NTabPane name="generic" tab="通用 Conf">
          <span class="tab-help">托管 upstream、map、状态页等 HTTP/Stream 片段。</span>
        </NTabPane>
      </NTabs>

      <div class="site-editor-grid">
        <div class="editor-fields">
          <div v-if="editorTab === 'generic'" class="field-grid">
            <label>
              <span>配置名称</span>
              <NInput v-model:value="form.name" placeholder="例如 Nginx Stub Status" />
            </label>
            <label>
              <span>文件名</span>
              <NInput
                v-model:value="form.filename"
                placeholder="nginx-status.conf"
                :disabled="form.context === 'main'"
              />
            </label>
          </div>
          <div v-else class="field-grid">
            <label>
              <span>域名</span>
              <NInput v-model:value="form.domain" placeholder="api.example.com" />
            </label>
            <label>
              <span>环境</span>
              <NSelect
                v-model:value="form.environment"
                :options="[
                  { label: '生产', value: '生产' },
                  { label: '预发布', value: '预发布' },
                  { label: '测试', value: '测试' },
                ]"
              />
            </label>
          </div>

          <label v-if="editorTab === 'guided'">
            <span>上游地址或站点目录</span>
            <NInput v-model:value="form.target" placeholder="http://10.0.0.21:8080" />
          </label>

          <fieldset>
            <legend>部署节点</legend>
            <div class="choice-grid">
              <label
                v-for="node in store.nodes"
                :key="node.id"
                class="choice-card"
                :class="{ selected: form.nodeIds.includes(node.id), offline: node.status === 'offline' }"
              >
                <NCheckbox
                  :checked="form.nodeIds.includes(node.id)"
                  :disabled="node.status === 'offline' || form.context === 'main'"
                  @update:checked="(checked) => toggleNode(node.id, checked)"
                />
                <span>
                  <strong>{{ node.node_name }}</strong>
                  <small>{{ node.hostname }} · {{ node.status === 'offline' ? '离线' : '在线' }}</small>
                </span>
              </label>
            </div>
          </fieldset>

          <div v-if="form.nodeIds.length && form.context !== 'main'" class="entry-targets">
            <h3>配置目录</h3>
            <label v-for="nodeId in form.nodeIds" :key="nodeId">
              <span>{{ store.nodes.find((item) => item.id === nodeId)?.node_name }}</span>
              <NSelect
                v-model:value="form.nodeConfigEntryIds[nodeId]"
                :options="
                  nodeEntries(
                    store.nodes.find((item) => item.id === nodeId)!,
                    form.context,
                  ).map((entry) => ({
                    label: `${entry.label || entry.id} · ${entry.directory}/*${entry.suffix}`,
                    value: entry.id,
                  }))
                "
                placeholder="选择 Agent 允许的配置目录"
              />
            </label>
          </div>

          <label v-if="editorTab !== 'generic'">
            <span>绑定证书</span>
            <NSelect
              v-model:value="form.certificateId"
              :options="availableCertificates"
              filterable
            />
            <small>只显示覆盖域名并且已存在于全部目标节点的证书。</small>
          </label>

          <label>
            <span>配置备注</span>
            <NInput
              v-model:value="form.note"
              type="textarea"
              :autosize="{ minRows: 3, maxRows: 5 }"
              placeholder="这个配置服务什么业务、负责人是谁"
            />
          </label>
          <label>
            <span>本次变更说明</span>
            <NInput
              v-model:value="form.changeNote"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 4 }"
              placeholder="可留空；发布时会按实际操作补充记录"
            />
          </label>
        </div>

        <div class="editor-code">
          <div class="editor-code-head">
            <div>
              <strong>Nginx Conf</strong>
              <small>保存草稿不会触碰节点文件</small>
            </div>
            <NSelect
              v-if="form.context !== 'main'"
              class="template-select"
              placeholder="应用模板"
              :options="[
                { label: 'HTTP 反向代理', value: 'http' },
                { label: '标准 HTTPS 反向代理', value: 'https' },
                { label: 'WebSocket 长连接', value: 'websocket' },
                { label: 'Stream TCP 代理', value: 'stream' },
              ]"
              @update:value="applyTemplate"
            />
          </div>
          <textarea
            v-model="form.config"
            class="conf-editor"
            spellcheck="false"
            aria-label="Nginx Conf"
          ></textarea>
          <div class="editor-note">
            <ShieldCheck :size="17" />
            {{
              form.context === 'main'
                ? '这是受保护的 nginx.conf；只有 Agent 安装时显式允许主配置编辑才可发布，且始终不能删除或迁移。'
                : '发布时由 Agent 原子写入；失败会恢复原文件。平台不修改正文格式。'
            }}
          </div>
        </div>
      </div>

      <template #footer>
        <div class="modal-footer">
          <NButton @click="editorOpen = false">取消</NButton>
          <NButton type="primary" :loading="saving" @click="saveDraft">保存草稿</NButton>
        </div>
      </template>
    </NModal>

    <NModal
      v-model:show="transferOpen"
      preset="card"
      class="action-modal"
      title="复制或迁移配置"
      :bordered="false"
      :mask-closable="false"
    >
      <p class="modal-lead">
        新节点会复制配置；已部署节点会把文件原子迁移到另一个配置入口。版本号不会因此增加。
      </p>
      <label>
        <span>目标已存在文件时</span>
        <NSelect
          v-model:value="transferMode"
          :options="[
            { label: '安全新建（目标存在则停止）', value: 'create' },
            { label: '确认替换目标文件', value: 'replace' },
          ]"
        />
      </label>
      <div class="transfer-target-list">
        <label
          v-for="node in store.nodes"
          :key="node.id"
          class="transfer-target"
          :class="{
            selected: transferNodeIds.includes(node.id),
            disabled:
              node.status === 'offline' ||
              !transferEntries(node.id).length ||
              (selected?.nodeIds.includes(node.id) && transferEntries(node.id).length < 2),
          }"
        >
          <NCheckbox
            :checked="transferNodeIds.includes(node.id)"
            :disabled="
              node.status === 'offline' ||
              !transferEntries(node.id).length ||
              (selected?.nodeIds.includes(node.id) && transferEntries(node.id).length < 2)
            "
            @update:checked="(checked) => toggleTransferNode(node.id, checked)"
          />
          <span>
            <strong>{{ node.node_name }}</strong>
            <small>
              {{
                selected?.nodeIds.includes(node.id)
                  ? '已部署 · 选择其他入口后迁移'
                  : '未部署 · 复制到该节点'
              }}
            </small>
          </span>
          <NSelect
            v-if="transferNodeIds.includes(node.id)"
            v-model:value="transferEntryIds[node.id]"
            :options="
              transferEntries(node.id).map((entry) => ({
                label: `${entry.label || entry.id} · ${entry.directory}/*${entry.suffix}`,
                value: entry.id,
                disabled:
                  selected?.nodeIds.includes(node.id) &&
                  selected?.nodeConfigEntryIds?.[node.id] === entry.id,
              }))
            "
          />
        </label>
      </div>
      <template #footer>
        <div class="modal-footer">
          <NButton @click="transferOpen = false">取消</NButton>
          <NButton
            type="primary"
            :loading="transferring"
            :disabled="!transferNodeIds.length"
            @click="submitTransfer"
          >
            提交原子复制 / 迁移
          </NButton>
        </div>
      </template>
    </NModal>

    <NModal
      v-model:show="historyOpen"
      preset="card"
      class="action-modal revision-modal"
      title="已发布版本"
      :bordered="false"
    >
      <div v-if="historyLoading" class="empty-state">正在读取版本记录…</div>
      <div v-else-if="revisions.length" class="revision-list">
        <article v-for="revision in revisions" :key="revision.id">
          <span class="revision-number">v{{ revision.version }}</span>
          <div>
            <strong>{{ revision.note || '未填写变更说明' }}</strong>
            <small>{{ revision.created_by }} · {{ revision.created_at }}</small>
            <code>{{ revision.snapshot_sha256.slice(0, 16) }}…</code>
          </div>
          <NButton
            secondary
            :disabled="!store.canOperate || revision.version === selected?.version"
            @click="restoreRevision(revision.version)"
          >
            <template #icon><RotateCcw :size="15" /></template>
            恢复为草稿
          </NButton>
        </article>
      </div>
      <div v-else class="empty-state">这个配置还没有已发布版本记录。</div>
    </NModal>
  </section>
</template>
