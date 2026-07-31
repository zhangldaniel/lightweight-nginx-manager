<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import {
  AlertTriangle,
  ArrowRightLeft,
  Check,
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
  useDialog,
} from 'naive-ui'
import PageHeader from '../components/PageHeader.vue'
import MetricCard from '../components/MetricCard.vue'
import ReleaseChannel from '../components/ReleaseChannel.vue'
import StatusTag from '../components/StatusTag.vue'
import { useConsoleStore } from '../stores/console'
import { api } from '../api'
import type { SiteRecord, SiteRevision } from '../types'
import {
  certificateDirectiveCounts,
  certificatePathsForNode,
  rewriteConfigCertificatePaths,
} from '../utils/certificateConfig'
import { certificateCoversDomain } from '../utils/certificateDomain'
import { defaultSiteConfig, nodeEntries, safeName, uid } from '../utils/config'
import { certificateDays, relativeTime, siteKind, siteStatus, siteTitle } from '../utils/format'
import {
  renderSiteTemplate,
  siteTemplates,
  type SiteTemplateKey,
} from '../utils/siteTemplates'

const store = useConsoleStore()
const dialog = useDialog()
const search = ref('')
const nodeFilter = ref('')
const statusFilter = ref('')
const editorOpen = ref(false)
const editorMode = ref<'create' | 'edit'>('create')
const editorTab = ref<'guided' | 'conf' | 'generic'>('guided')
const activeTemplate = ref<SiteTemplateKey>('http')
const templateManaged = ref(true)
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
const editorBaseline = ref('')
const editorCloseConfirming = ref(false)
const templateConfirming = ref(false)
const certificatePreviewConfirmed = ref(false)

const form = reactive({
  id: '',
  domain: '',
  name: '',
  filename: '',
  type: 'proxy',
  target: '',
  context: 'http' as 'http' | 'stream' | 'main',
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
const activeConfigScan = computed(() =>
  store.jobs.some(
    (job) => job.action === 'config_inventory' && ['queued', 'running'].includes(job.status),
  ),
)
const availableCertificates = computed(() => {
  const domain = form.domain.trim()
  return [
    { label: '暂不绑定（保留 Conf 内现有路径）', value: '' },
    ...store.certificates.map((certificate) => {
      const nodeCovered =
        form.nodeIds.length > 0 &&
        form.nodeIds.every(
          (id) => certificate.nodeIds.includes(id) && Boolean(certificate.nodePaths?.[id]),
        )
      const domainCovered = !domain || certificateCoversDomain(certificate, domain)
      const days = certificateDays(certificate)
      const unavailableReason = !form.nodeIds.length
        ? '请先选择部署节点'
        : !domainCovered
          ? '不覆盖当前域名'
          : !nodeCovered
            ? '部分节点缺少证书路径'
            : ''
      return {
        label: `${certificate.domain}${days === null ? '' : ` · 剩余 ${days} 天`}${unavailableReason ? ` · ${unavailableReason}` : ''}`,
        value: certificate.id,
        disabled: Boolean(unavailableReason),
      }
    }),
  ]
})
const selectedFormCertificate = computed(() =>
  store.certificates.find((certificate) => certificate.id === form.certificateId),
)
const certificatePathRows = computed(() => {
  const certificate = selectedFormCertificate.value
  if (!certificate) return []
  return form.nodeIds.map((nodeId) => {
    const node = store.nodes.find((item) => item.id === nodeId)
    return {
      nodeId,
      nodeName: node?.node_name || nodeId,
      paths: certificatePathsForNode(certificate, node),
    }
  })
})
const certificatePathsDiffer = computed(() => {
  const signatures = certificatePathRows.value
    .filter((row) => row.paths)
    .map((row) => `${row.paths!.certificatePath}\n${row.paths!.keyPath}`)
  return new Set(signatures).size > 1
})
const certificateDirectives = computed(() => certificateDirectiveCounts(form.config))
const certificateDirectivesComplete = computed(
  () =>
    certificateDirectives.value.certificate > 0 &&
    certificateDirectives.value.certificate === certificateDirectives.value.key,
)
const certificatePreviewNode = computed(() => {
  const row = certificatePathRows.value.find((item) => item.paths)
  return row ? store.nodes.find((item) => item.id === row.nodeId) : undefined
})
const certificatePreviewRewrite = computed(() => {
  const certificate = selectedFormCertificate.value
  const node = certificatePreviewNode.value
  if (!certificate || !node) return null
  return rewriteConfigCertificatePaths(form.config, certificate, node)
})
const certificatePreviewInSync = computed(
  () =>
    certificatePreviewConfirmed.value &&
    Boolean(selectedFormCertificate.value) &&
    certificateDirectivesComplete.value &&
    certificatePreviewRewrite.value?.content === form.config,
)

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


function operationalSnapshot(value: typeof form) {
  return JSON.stringify({
    domain: value.domain,
    name: value.name,
    filename: value.filename,
    type: value.type,
    target: value.target,
    context: value.context,
    nodeIds: [...value.nodeIds].sort(),
    certificateId: value.certificateId,
    config: value.config,
    nodeConfigEntryIds: value.nodeConfigEntryIds,
  })
}

function editorSnapshot() {
  const nodeConfigEntryIds = Object.fromEntries(
    Object.entries(form.nodeConfigEntryIds).sort(([left], [right]) => left.localeCompare(right)),
  )
  return JSON.stringify({
    id: form.id,
    domain: form.domain,
    name: form.name,
    filename: form.filename,
    type: form.type,
    target: form.target,
    context: form.context,
    nodeIds: [...form.nodeIds].sort(),
    certificateId: form.certificateId,
    note: form.note,
    changeNote: form.changeNote,
    config: form.config,
    nodeConfigEntryIds,
    editorTab: editorTab.value,
    activeTemplate: activeTemplate.value,
  })
}

const editorDirty = computed(
  () => editorOpen.value && editorSnapshot() !== editorBaseline.value,
)

function resetForm() {
  Object.assign(form, {
    id: uid('site'),
    domain: '',
    name: '',
    filename: '',
    type: 'proxy',
    target: '',
    context: 'http',
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
  editorTab.value = 'conf'
  activeTemplate.value = 'http'
  resetForm()
  originalOperational.value = ''
  templateManaged.value = true
  certificatePreviewConfirmed.value = false
  editorBaseline.value = editorSnapshot()
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
    nodeIds: [...(site.nodeIds || [])],
    certificateId: site.certificateId || '',
    note: site.note || '',
    changeNote: '',
    config: site.config || '',
    nodeConfigEntryIds: { ...(site.nodeConfigEntryIds || {}) },
  })
  certificatePreviewConfirmed.value = Boolean(site.certificateId)
  activeTemplate.value = inferTemplate(site)
  templateManaged.value = false
  originalOperational.value = operationalSnapshot(form)
  editorBaseline.value = editorSnapshot()
  editorOpen.value = true
}

function inferTemplate(site: SiteRecord): SiteTemplateKey {
  if (site.context === 'stream') return 'stream'
  if (site.resourceType === 'generic') {
    return /stub_status\s*;/m.test(site.config) ? 'stub-status' : 'custom'
  }
  if (site.type === 'static' || /\btry_files\b/m.test(site.config)) return 'static'
  if (/proxy_set_header\s+Upgrade\s+/m.test(site.config)) return 'websocket'
  if (/\bupstream\s+\S+\s*\{/m.test(site.config) && /listen\s+443\s+ssl/m.test(site.config)) {
    return 'balanced-https'
  }
  if (/listen\s+443\s+ssl/m.test(site.config)) return 'https'
  return 'http'
}

function applyTemplateNow(kind: SiteTemplateKey) {
  const template = siteTemplates.find((item) => item.key === kind)
  if (!template) return
  activeTemplate.value = kind
  templateManaged.value = true
  editorTab.value = template.resourceType === 'generic' ? 'generic' : 'conf'
  form.context = template.context
  form.type = template.type
  if (template.resourceType === 'generic') {
    form.name = template.defaultName || form.name
    form.filename = template.defaultFilename || form.filename
  }
  if (!['https', 'balanced-https', 'websocket'].includes(kind)) {
    form.certificateId = ''
    certificatePreviewConfirmed.value = false
  }
  form.config = renderSiteTemplate(kind, form.domain, form.target)
  applySelectedCertificateToPreview(false)
  for (const nodeId of form.nodeIds) ensureEntrySelection(nodeId)
}

function applyTemplate(kind: SiteTemplateKey) {
  if (saving.value || templateConfirming.value) return
  if (templateManaged.value || !form.config.trim()) {
    applyTemplateNow(kind)
    return
  }
  templateConfirming.value = true
  dialog.warning({
    title: '替换当前 Conf？',
    content: '你已经手动编辑过当前 Conf。应用模板会完整替换右侧内容，且无法自动撤销。',
    positiveText: '确认替换',
    negativeText: '保留当前内容',
    onPositiveClick() {
      templateConfirming.value = false
      applyTemplateNow(kind)
    },
    onNegativeClick() {
      templateConfirming.value = false
    },
    onClose() {
      templateConfirming.value = false
    },
  })
}

function applySelectedCertificateToPreview(notifyWhenMissing = true) {
  const certificate = selectedFormCertificate.value
  if (!certificate) return
  const node = certificatePreviewNode.value
  if (!node) {
    certificatePreviewConfirmed.value = false
    if (notifyWhenMissing) {
      store.notify('证书路径尚不可用', 'warning', '所选节点没有上报这张证书的证书路径和私钥路径。')
    }
    return
  }
  const rewritten = rewriteConfigCertificatePaths(form.config, certificate, node)
  form.config = rewritten.content
  certificatePreviewConfirmed.value = true
  if (!certificateDirectivesComplete.value && notifyWhenMissing) {
    store.notify(
      '证书已绑定，但 Conf 没有证书指令',
      'info',
      '请先选择 HTTPS 模板，或在 Conf 中加入 ssl_certificate 与 ssl_certificate_key。',
    )
  }
}

function selectCertificate(value: string | null) {
  form.certificateId = String(value || '')
  certificatePreviewConfirmed.value = false
  if (form.certificateId) applySelectedCertificateToPreview()
}

function handleConfigInput() {
  templateManaged.value = false
  certificatePreviewConfirmed.value = false
}

watch(
  () => [form.domain, form.target],
  () => {
    if (!editorOpen.value || !templateManaged.value) return
    form.config = renderSiteTemplate(activeTemplate.value, form.domain, form.target)
    applySelectedCertificateToPreview(false)
  },
)

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
  if (form.certificateId) applySelectedCertificateToPreview(false)
}


function validateForm() {
  if (!form.nodeIds.length) return '请选择至少一个部署节点'
  if (editorTab.value !== 'generic' && !form.domain.trim()) return '请输入域名'
  if (editorTab.value === 'generic' && !form.name.trim()) return '请输入配置名称'
  if (!form.config.trim()) return 'Nginx Conf 不能为空'
  if (/\/apps\/nginx\/cert\/example\.com\.(?:pem|key)/.test(form.config)) {
    return '请绑定真实证书，或把 HTTPS 模板中的示例证书路径替换为实际路径'
  }
  if (form.certificateId) {
    const certificate = selectedFormCertificate.value
    if (!certificate) return '所选证书已不存在，请重新选择'
    if (!certificateCoversDomain(certificate, form.domain.trim())) return '所选证书不覆盖当前域名'
    if (!certificateDirectivesComplete.value) {
      return '绑定证书时，Conf 必须包含数量一致的 ssl_certificate 与 ssl_certificate_key'
    }
    const missing = certificatePathRows.value.filter((row) => !row.paths)
    if (missing.length) return `${missing.map((row) => row.nodeName).join('、')} 缺少所选证书的路径`
    if (!certificatePreviewInSync.value) {
      return '当前 Conf 中的证书路径与绑定证书不一致，请先点击“同步右侧预览”'
    }
  }
  return ''
}

function closeEditorNow() {
  editorOpen.value = false
  editorCloseConfirming.value = false
  templateConfirming.value = false
}

function requestCloseEditor() {
  if (saving.value || editorCloseConfirming.value || !editorOpen.value) return
  if (!editorDirty.value) {
    closeEditorNow()
    return
  }
  editorCloseConfirming.value = true
  dialog.warning({
    title: '放弃未保存的修改？',
    content: '当前表单、备注或 Nginx Conf 已发生变化。关闭后这些修改不会保存。',
    positiveText: '放弃修改',
    negativeText: '继续编辑',
    onPositiveClick() {
      closeEditorNow()
    },
    onNegativeClick() {
      editorCloseConfirming.value = false
    },
    onClose() {
      editorCloseConfirming.value = false
    },
  })
}

function handleEditorVisibility(show: boolean) {
  if (show) {
    editorOpen.value = true
    return
  }
  requestCloseEditor()
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
    const saved = await store.upsertSite(site)
    if (!saved) return
    closeEditorNow()
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
  if (activeConfigScan.value) {
    store.notify('配置扫描正在进行', 'info', '无需重复提交，完成后页面会自动同步结果。')
    return
  }
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
        :loading="scanning || activeConfigScan"
        :disabled="!store.canOperate || activeConfigScan"
        @click="scanSites"
      >
        {{ activeConfigScan ? '扫描进行中' : '导入节点现有配置' }}
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

    <ReleaseChannel :site="selected" />

    <div class="master-detail">
      <section class="data-panel">
        <div class="filter-bar">
          <NInput v-model:value="search" clearable placeholder="搜索域名、配置备注或变更说明">
            <template #prefix><Search :size="17" /></template>
          </NInput>
          <NSelect
            v-model:value="nodeFilter"
            :options="nodeOptions"
            aria-label="按 Agent 筛选"
          />
          <NSelect
            v-model:value="statusFilter"
            :options="statusOptions"
            aria-label="按状态筛选"
          />
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
              <span
                v-for="nodeId in site.nodeIds.slice(0, 2)"
                :key="nodeId"
                class="node-chip"
                :class="{ offline: store.nodes.find((item) => item.id === nodeId)?.status === 'offline' }"
                :title="store.nodes.find((item) => item.id === nodeId)?.status === 'offline' ? '节点离线' : '节点在线'"
              >
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
            <p>{{ siteKind(selected) }} · 配置 v{{ selected.version }}</p>
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
              <span
                class="online-dot"
                :class="{ offline: store.nodes.find((item) => item.id === nodeId)?.status === 'offline' }"
              ></span>
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
      :show="editorOpen"
      preset="card"
      class="site-editor-modal"
      :title="editorMode === 'create' ? '新增站点' : `编辑 ${selected ? siteTitle(selected) : ''}`"
      :bordered="false"
      :mask-closable="false"
      :closable="!saving"
      :close-on-esc="!saving"
      @update:show="handleEditorVisibility"
    >
      <div
        class="site-editor-grid"
        :class="{ 'is-saving': saving }"
        :aria-busy="saving"
        :inert="saving"
      >
        <aside class="template-rail" aria-label="配置模板">
          <div class="template-rail-head">
            <strong>配置模板</strong>
            <small>选择后会替换右侧 Conf</small>
          </div>
          <button
            v-for="template in siteTemplates"
            :key="template.key"
            type="button"
            class="template-card"
            :class="{ active: activeTemplate === template.key }"
            :aria-pressed="activeTemplate === template.key"
            @click="applyTemplate(template.key)"
          >
            <span class="template-card-icon"><FileCode2 :size="17" /></span>
            <span class="template-card-copy">
              <strong>{{ template.label }}</strong>
              <small>{{ template.description }}</small>
            </span>
            <span class="template-context">{{ template.context.toUpperCase() }}</span>
          </button>
        </aside>

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
          <label v-else>
            <span>域名</span>
            <NInput v-model:value="form.domain" placeholder="api.example.com" />
          </label>

          <label v-if="editorTab !== 'generic' && form.type !== 'static'">
            <span>上游地址或站点目录</span>
            <NInput
              v-model:value="form.target"
              :placeholder="activeTemplate === 'balanced-https' ? '10.0.0.21:8080, 10.0.0.22:8080' : 'http://10.0.0.21:8080'"
            />
            <small v-if="templateManaged && activeTemplate === 'balanced-https'">
              多个上游用逗号分隔；输入会实时同步到右侧模板。
            </small>
            <small v-else-if="templateManaged">输入会实时同步到右侧模板；手动修改 Conf 后停止自动同步。</small>
            <small v-else>当前 Conf 已手动编辑；域名和上游不会再自动覆盖正文。</small>
          </label>

          <fieldset>
            <legend>部署节点</legend>
            <div class="choice-grid">
              <button
                v-for="node in store.nodes"
                :key="node.id"
                type="button"
                class="choice-card"
                :class="{ selected: form.nodeIds.includes(node.id), offline: node.status === 'offline' }"
                :disabled="node.status === 'offline' || form.context === 'main'"
                :aria-pressed="form.nodeIds.includes(node.id)"
                @click="toggleNode(node.id, !form.nodeIds.includes(node.id))"
              >
                <span class="choice-card-indicator" aria-hidden="true">
                  <Check v-if="form.nodeIds.includes(node.id)" :size="14" />
                </span>
                <span>
                  <strong>{{ node.node_name }}</strong>
                  <small>{{ node.hostname }} · {{ node.status === 'offline' ? '离线' : '在线' }}</small>
                </span>
              </button>
            </div>
            <p v-if="!store.nodes.length" class="field-empty-hint">
              尚未接入 Agent，请先到“节点 Agent”页面完成接入。
            </p>
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

          <div v-if="editorTab !== 'generic'" class="editor-field certificate-field">
            <span id="site-certificate-label">绑定证书</span>
            <NSelect
              :value="form.certificateId"
              :options="availableCertificates"
              filterable
              aria-labelledby="site-certificate-label"
              @update:value="selectCertificate"
            />
            <small>先选择部署节点；只可绑定覆盖域名且在全部目标节点都有路径的证书。</small>
            <div
              v-if="selectedFormCertificate"
              class="certificate-path-preview"
              :class="{
                warning:
                  !form.nodeIds.length ||
                  !certificatePreviewNode ||
                  !certificateDirectivesComplete ||
                  !certificatePreviewInSync,
              }"
            >
              <div class="certificate-path-summary">
                <ShieldCheck :size="16" />
                <span v-if="!form.nodeIds.length">
                  尚未选择部署节点，无法确定发布时使用的证书和私钥路径。
                </span>
                <span v-else-if="!certificatePreviewNode">
                  所选节点缺少这张证书的证书或私钥路径，当前不能保存。
                </span>
                <span v-else-if="!certificateDirectivesComplete">
                  当前 Conf 缺少成对的 ssl_certificate 与 ssl_certificate_key，保存前必须补齐。
                </span>
                <span v-else-if="!certificatePreviewInSync">
                  当前 Conf 与绑定证书不同步。发布时会按每个节点的真实路径落地，请先同步右侧预览。
                </span>
                <span v-else-if="certificatePathsDiffer">
                  右侧预览显示首个节点路径；发布时会逐节点替换为各自的证书和私钥路径。
                </span>
                <span v-else>右侧 Conf 已同步为下方节点上的真实证书和私钥路径。</span>
              </div>
              <NButton
                v-if="certificatePreviewNode && certificateDirectivesComplete && !certificatePreviewInSync"
                class="certificate-sync-button"
                size="tiny"
                secondary
                :disabled="saving"
                @click="applySelectedCertificateToPreview()"
              >同步右侧预览</NButton>
              <div
                v-for="row in certificatePathRows"
                :key="row.nodeId"
                class="certificate-path-row"
                :class="{ missing: !row.paths }"
              >
                <strong>{{ row.nodeName }}</strong>
                <template v-if="row.paths">
                  <span class="certificate-path-kind">证书</span>
                  <code :title="row.paths.certificatePath">{{ row.paths.certificatePath }}</code>
                  <span class="certificate-path-kind">私钥</span>
                  <code :title="row.paths.keyPath">{{ row.paths.keyPath }}</code>
                </template>
                <small v-else>缺少证书或私钥路径</small>
              </div>
            </div>
          </div>

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
              <small>
                {{ siteTemplates.find((item) => item.key === activeTemplate)?.label || '自定义配置' }}
                · 保存草稿不会触碰节点文件
              </small>
            </div>
            <span class="editor-context-badge">{{ form.context.toUpperCase() }}</span>
          </div>
          <textarea
            v-model="form.config"
            class="conf-editor"
            @input="handleConfigInput"
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
          <NButton :disabled="saving" @click="requestCloseEditor">取消</NButton>
          <NButton
            type="primary"
            :loading="saving"
            :disabled="saving"
            @click="saveDraft"
          >保存草稿</NButton>
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
