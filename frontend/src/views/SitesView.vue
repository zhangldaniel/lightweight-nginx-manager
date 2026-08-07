<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  AlertTriangle,
  Check,
  Copy,
  FileCode2,
  History,
  Plus,
  RotateCcw,
  Search,
  ShieldCheck,
  X,
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
import ReleaseChannel from '../components/ReleaseChannel.vue'
import SiteScreenshotAttachments from '../components/SiteScreenshotAttachments.vue'
import StatusTag from '../components/StatusTag.vue'
import { useConsoleStore } from '../stores/console'
import { api } from '../api'
import type { NodeRecord, SiteRecord, SiteRevision } from '../types'
import {
  certificateDirectiveCounts,
  certificatePathsForNode,
  rewriteConfigCertificatePaths,
} from '../utils/certificateConfig'
import { certificateCoversDomain } from '../utils/certificateDomain'
import {
  configPathForEntry,
  defaultSiteConfig,
  managedConfigPath,
  nodeEntries,
  safeName,
  uid,
} from '../utils/config'
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
const configManuallyEdited = ref(false)
const saving = ref(false)
const running = ref(false)
const scanning = ref(false)
const transferOpen = ref(false)
const transferring = ref(false)
const transferMode = ref<'create' | 'replace'>('create')
const deploymentAction = ref<'add' | 'migrate' | 'remove'>('add')
const transferNodeIds = ref<string[]>([])
const transferEntryIds = reactive<Record<string, string>>({})
const deleteRecordAfterRemoval = ref(false)
const removeConfirmation = ref('')
const historyOpen = ref(false)
const historyLoading = ref(false)
const revisions = ref<SiteRevision[]>([])
const editorBaseline = ref('')
const editorCloseConfirming = ref(false)
const templateConfirming = ref(false)
const detailScroll = ref<HTMLElement | null>(null)
const detailPanel = ref<HTMLElement | null>(null)
const detailCloseButton = ref<HTMLButtonElement | null>(null)
const detailDrawerOpen = ref(false)
const detailDrawerSiteId = ref('')
const narrowDetail = ref(false)
const attachmentRefreshToken = ref(0)
let detailMediaQuery: MediaQueryList | undefined
let detailReturnFocus: HTMLElement | null = null

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

function normalizedSearchTerms(value: string) {
  return value
    .trim()
    .toLocaleLowerCase()
    .split(/\s+/)
    .filter(Boolean)
}

function siteSearchSources(site: SiteRecord) {
  return [
    siteTitle(site),
    site.domain,
    site.name,
    site.filename,
    site.type,
    site.target,
    site.context,
    site.note,
    site.changeNote,
    site.config,
    ...Object.values(site.nodeConfigs || {}),
    ...Object.values(site.nodeConfigPaths || {}),
  ]
    .filter(Boolean)
    .map((value) => String(value))
}

function searchSourceIncludesTerm(value: string, term: string) {
  if (/^\d+$/.test(term)) return new RegExp(`(?:^|\\D)${term}(?:\\D|$)`).test(value)
  return value.includes(term)
}

function siteMatchesSearch(site: SiteRecord, terms: string[]) {
  if (!terms.length) return true
  const sources = siteSearchSources(site).map((value) => value.toLocaleLowerCase())
  return terms.every((term) => sources.some((value) => searchSourceIncludesTerm(value, term)))
}

function clippedSearchLine(value: string) {
  const compact = value.trim().replace(/\s+/g, ' ')
  return compact.length > 96 ? `${compact.slice(0, 93)}…` : compact
}

function searchMatchHint(site: SiteRecord, terms: string[]) {
  if (!terms.length) return ''

  const visibleMetadata = [
    siteTitle(site),
    site.domain,
    site.name,
    site.filename,
    site.note,
    site.changeNote,
  ]
    .filter(Boolean)
    .map((value) => String(value).toLocaleLowerCase())
  if (
    terms.every((term) =>
      visibleMetadata.some((value) => searchSourceIncludesTerm(value, term)),
    )
  ) return ''

  const candidates = [
    { label: '命中目标', values: [site.target] },
    {
      label: '命中配置',
      values: [site.config, ...Object.values(site.nodeConfigs || {})]
        .filter(Boolean)
        .flatMap((value) => String(value).split(/\r?\n/)),
    },
    { label: '命中路径', values: Object.values(site.nodeConfigPaths || {}) },
  ]
  for (const candidate of candidates) {
    const line = candidate.values
      .filter(Boolean)
      .map((value) => String(value))
      .find((value) => {
        const normalized = value.toLocaleLowerCase()
        return terms.some((term) => searchSourceIncludesTerm(normalized, term))
      })
    if (line) return `${candidate.label} · ${clippedSearchLine(line)}`
  }
  return ''
}

const filteredSites = computed(() => {
  const terms = normalizedSearchTerms(search.value)
  return store.sites.filter((site) => {
    const matchedKeyword = siteMatchesSearch(site, terms)
    const matchedNode = !nodeFilter.value || site.nodeIds.includes(nodeFilter.value)
    const matchedStatus = !statusFilter.value || site.status === statusFilter.value
    return matchedKeyword && matchedNode && matchedStatus
  })
})
const siteSearchHints = computed(() => {
  const terms = normalizedSearchTerms(search.value)
  return new Map(filteredSites.value.map((site) => [site.id, searchMatchHint(site, terms)]))
})

const selected = computed(() => store.selectedSite)
function refreshAttachmentsFor(siteId: string) {
  const selectedMatches = selected.value?.id === siteId
  const editorMatches = editorOpen.value && editorMode.value === 'edit' && form.id === siteId
  if (selectedMatches || editorMatches) attachmentRefreshToken.value += 1
}
const siteBusyTitle = '当前配置有任务执行中，请等待任务完成'
function isSiteBusy(site: SiteRecord | null | undefined) {
  return Boolean(site && store.isSiteOperationBusy(site.id))
}
const selectedSiteBusy = computed(() => isSiteBusy(selected.value))
function siteOutstandingCandidateVersion(site: SiteRecord) {
  const explicit = Number(site.candidateVersion || 0)
  if (explicit === Number(site.version || 0) + 1) return explicit
  const legacy = Number(site.lastFailure?.candidateVersion || 0)
  if (site.lastFailure?.operation === 'publish' && legacy === Number(site.version || 0) + 1) {
    return legacy
  }
  return site.status === 'draft' ? Number(site.version || 0) + 1 : 0
}
const selectedRunBlocker = computed(() => {
  const site = selected.value
  if (!store.canOperate) return '当前账号只有查看权限'
  if (!site) return '配置不存在'
  if (selectedSiteBusy.value) return siteBusyTitle
  if (!site.nodeIds.length) return '请先通过“调整部署范围”添加节点'
  const readOnly = site.nodeReadOnly as Record<string, boolean> | undefined
  for (const nodeId of site.nodeIds) {
    const node = store.nodes.find((item) => item.id === nodeId)
    if (!node) return `节点 ${nodeId} 不存在`
    if (node.status === 'offline') return `${node.node_name} 的 Agent 离线`
    if (readOnly?.[nodeId]) return `${node.node_name} 的配置为只读`
    if (!node.capabilities.includes('config_apply')) {
      return `${node.node_name} 的 Agent 不支持配置写入`
    }
  }
  return ''
})
const editingSiteBusy = computed(() => {
  if (editorMode.value !== 'edit') return false
  return isSiteBusy(store.sites.find((site) => site.id === form.id))
})
const jobsById = computed(() => new Map(store.jobs.map((job) => [job.id, job])))
const deploymentActionOptions = [
  { label: '添加节点', value: 'add' },
  { label: '迁移配置目录', value: 'migrate' },
  { label: '移除节点', value: 'remove' },
]
const deploymentCandidateNodes = computed(() => {
  const site = selected.value
  if (!site) return []
  if (deploymentAction.value === 'add') {
    return store.nodes.filter((node) => !site.nodeIds.includes(node.id))
  }
  return store.nodes.filter((node) => site.nodeIds.includes(node.id))
})
const removesLastDeployment = computed(() => {
  const site = selected.value
  if (!site?.nodeIds.length || deploymentAction.value !== 'remove') return false
  const selectedNodes = new Set(transferNodeIds.value)
  return site.nodeIds.every((nodeId) => selectedNodes.has(nodeId))
})
const removeConfirmationMatches = computed(
  () => !selected.value || removeConfirmation.value.trim() === siteTitle(selected.value),
)
const finalCandidateRemovalBlocked = computed(
  () =>
    Boolean(
      removesLastDeployment.value &&
        selected.value &&
        siteOutstandingCandidateVersion(selected.value) &&
        !deleteRecordAfterRemoval.value,
    ),
)
const deploymentActionLead = computed(() => {
  if (deploymentAction.value === 'add') {
    return '把当前配置发布到新的节点。每个节点需要选择 Agent 允许的配置入口。'
  }
  if (deploymentAction.value === 'migrate') {
    return '在已部署节点内原子迁移配置文件；源文件只会在目标文件写入并校验成功后移除。'
  }
  return '从所选节点安全删除受托管配置，并逐节点执行 nginx -t 和 reload。'
})
const deploymentSubmitLabel = computed(() => {
  if (deploymentAction.value === 'add') return '提交添加节点'
  if (deploymentAction.value === 'migrate') return '提交目录迁移'
  return deleteRecordAfterRemoval.value ? '移除并删除平台记录' : '提交移除节点'
})
const deploymentPreviews = computed(() =>
  transferNodeIds.value
    .map((nodeId) => deploymentPreview(nodeId))
    .filter((preview): preview is NonNullable<typeof preview> => Boolean(preview)),
)
const draftCount = computed(
  () => store.sites.filter((site) => ['draft', 'failed', 'drift'].includes(site.status)).length,
)
const pendingCount = computed(() => store.sites.filter((site) => Boolean(site.pendingRemote)).length)
const releaseSite = computed(
  () =>
    (selected.value?.pendingRemote ? selected.value : null) ||
    store.sites.find((site) => Boolean(site.pendingRemote)) ||
    selected.value,
)
const additionalPendingCount = computed(() =>
  Math.max(0, pendingCount.value - (releaseSite.value?.pendingRemote ? 1 : 0)),
)
const offlineCount = computed(() => Math.max(0, store.nodes.length - store.onlineCount))
const heroHeadline = computed(() => {
  if (pendingCount.value) {
    return `${store.onlineCount} 个节点在线，${pendingCount.value} 个配置操作正在收尾。`
  }
  if (offlineCount.value) {
    return `${store.onlineCount} 个节点在线，${offlineCount.value} 个连接需要处理。`
  }
  return `${store.onlineCount} 个节点在线，发布通道当前空闲。`
})
const activeConfigScan = computed(() =>
  store.jobs.some(
    (job) => job.action === 'config_inventory' && ['queued', 'claimed', 'running'].includes(job.status),
  ),
)
const visibleSiteTemplates = computed(() => {
  if (editorMode.value === 'create') return siteTemplates
  const existing = store.sites.find((site) => site.id === form.id)
  if (!existing || existing.context === 'main') return []
  return siteTemplates.filter((template) => template.context === existing.context)
})

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
}

function numericValue(source: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = Number(source[key])
    if (Number.isFinite(value) && value >= 0) return value
  }
  return 0
}

function arrayLength(source: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = source[key]
    if (Array.isArray(value)) return value.length
  }
  return 0
}

function operationName(source: Record<string, unknown>) {
  if (typeof source.operation === 'string') return source.operation
  return source.publish === true ? 'publish' : ''
}

function publishProgress(source: Record<string, unknown>, includeLiveJobs: boolean) {
  const references = Array.isArray(source.jobs)
    ? source.jobs.map(objectValue).filter((item) => Object.keys(item).length)
    : []
  const alreadyCompleted = numericValue(source, ['alreadyCompleted'])
  const total =
    numericValue(source, ['totalTargets', 'totalNodes', 'totalCount', 'totalNodeCount', 'targetCount']) ||
    arrayLength(source, ['targetNodeIds', 'nodeIds']) ||
    references.length + alreadyCompleted
  let succeeded =
    arrayLength(source, ['successfulNodeIds', 'succeededNodeIds']) ||
    numericValue(source, [
      'completedNodes',
      'succeededCount',
      'successCount',
      'successfulCount',
      'alreadyCompleted',
    ])

  if (references.length) {
    const referencedSucceeded = references.filter((reference) => {
      const id = typeof reference.id === 'string' ? reference.id : ''
      const liveJob = includeLiveJobs && id ? jobsById.value.get(id) : undefined
      return (liveJob?.status || reference.status) === 'succeeded'
    }).length
    if (includeLiveJobs || !succeeded) succeeded = alreadyCompleted + referencedSucceeded
  }
  return total > 0 ? `${Math.min(succeeded, total)}/${total}` : ''
}

function siteVersionPresentation(site: SiteRecord) {
  const pending = objectValue(site.pendingRemote)
  const failure = objectValue(site.lastFailure)
  const pendingPublish = operationName(pending) === 'publish'
  const failedPublish = operationName(failure) === 'publish'
  const explicitCandidateVersion = Number(site.candidateVersion || 0)
  const hasCandidate =
    explicitCandidateVersion > Number(site.version || 0) ||
    site.status === 'draft' ||
    pendingPublish ||
    failedPublish
  const candidateSource = pendingPublish ? pending : failedPublish ? failure : {}
  const candidateVersion =
    explicitCandidateVersion ||
    numericValue(candidateSource, ['candidateVersion']) ||
    site.version + 1
  return {
    label: hasCandidate ? `v${site.version} → v${candidateVersion}` : `v${site.version}`,
    progress: pendingPublish
      ? publishProgress(pending, true)
      : failedPublish
        ? publishProgress(failure, false)
        : '',
  }
}

function siteVersionLabel(site: SiteRecord) {
  return siteVersionPresentation(site).label
}

function siteVersionProgress(site: SiteRecord) {
  return siteVersionPresentation(site).progress
}
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

function resetDetailScroll() {
  void nextTick(() => {
    if (detailScroll.value) detailScroll.value.scrollTop = 0
  })
}

function selectSite(siteId: string) {
  if (narrowDetail.value) {
    if (!detailDrawerOpen.value) {
      detailReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    }
    detailDrawerSiteId.value = siteId
    detailDrawerOpen.value = true
    if (document.activeElement !== detailCloseButton.value) {
      void nextTick(() => detailCloseButton.value?.focus())
    }
  }
  store.selectedSiteId = siteId
  resetDetailScroll()
}

function closeDetailDrawer() {
  const shouldRestoreFocus = narrowDetail.value && detailDrawerOpen.value
  const returnFocus = detailReturnFocus
  detailDrawerOpen.value = false
  detailDrawerSiteId.value = ''
  detailReturnFocus = null
  if (shouldRestoreFocus && returnFocus) void nextTick(() => returnFocus.focus())
}

function trapDetailFocus(event: KeyboardEvent) {
  if (!narrowDetail.value || !detailDrawerOpen.value || event.key !== 'Tab') return
  const focusable = Array.from(
    detailPanel.value?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ) || [],
  ).filter((element) => element.offsetParent !== null)
  if (!focusable.length) {
    event.preventDefault()
    detailPanel.value?.focus()
    return
  }
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

function syncDetailBreakpoint(matches: boolean) {
  narrowDetail.value = matches
  if (!matches) closeDetailDrawer()
}

function handleDetailBreakpoint(event: MediaQueryListEvent) {
  syncDetailBreakpoint(event.matches)
}

function handleDetailEscape(event: KeyboardEvent) {
  if (event.key === 'Escape' && detailDrawerOpen.value) closeDetailDrawer()
}

onMounted(() => {
  detailMediaQuery = window.matchMedia('(max-width: 1220px)')
  syncDetailBreakpoint(detailMediaQuery.matches)
  detailMediaQuery.addEventListener('change', handleDetailBreakpoint)
  window.addEventListener('keydown', handleDetailEscape)
})

onBeforeUnmount(() => {
  detailMediaQuery?.removeEventListener('change', handleDetailBreakpoint)
  window.removeEventListener('keydown', handleDetailEscape)
})

watch(
  () => store.selectedSiteId,
  () => resetDetailScroll(),
)

watch(selected, (site) => {
  if (
    !site ||
    (narrowDetail.value &&
      detailDrawerOpen.value &&
      detailDrawerSiteId.value &&
      detailDrawerSiteId.value !== site.id)
  ) {
    closeDetailDrawer()
  }
})

watch(removesLastDeployment, (removesLast) => {
  if (removesLast) return
  deleteRecordAfterRemoval.value = false
  removeConfirmation.value = ''
})


function operationalSnapshot(value: typeof form) {
  return JSON.stringify({
    domain: value.domain,
    name: value.name,
    type: value.type,
    target: value.target,
    certificateId: value.certificateId,
    config: value.config,
  })
}

function editorSnapshot() {
  const createDeployment =
    editorMode.value === 'create'
      ? {
          filename: form.filename,
          context: form.context,
          nodeIds: [...form.nodeIds].sort(),
          nodeConfigEntryIds: Object.fromEntries(
            Object.entries(form.nodeConfigEntryIds).sort(([left], [right]) =>
              left.localeCompare(right),
            ),
          ),
        }
      : {}
  return JSON.stringify({
    id: form.id,
    domain: form.domain,
    name: form.name,
    type: form.type,
    target: form.target,
    certificateId: form.certificateId,
    note: form.note,
    changeNote: form.changeNote,
    config: form.config,
    editorTab: editorTab.value,
    activeTemplate: activeTemplate.value,
    ...createDeployment,
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
  configManuallyEdited.value = false
  editorBaseline.value = editorSnapshot()
  editorOpen.value = true
}

function openEdit(site: SiteRecord) {
  if (isSiteBusy(site)) {
    store.notify('配置操作执行中', 'warning', '请等待当前发布或部署范围调整完成后再编辑。')
    return
  }
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
  activeTemplate.value = inferTemplate(site)
  templateManaged.value = false
  configManuallyEdited.value = false
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
  configManuallyEdited.value = false
  editorTab.value = template.resourceType === 'generic' ? 'generic' : 'conf'
  form.context = template.context
  form.type = template.type
  if (template.resourceType === 'generic') {
    form.name = template.defaultName || form.name
    form.filename = template.defaultFilename || form.filename
  }
  if (!['https', 'balanced-https', 'websocket'].includes(kind)) {
    form.certificateId = ''
  }
  form.config = renderSiteTemplate(kind, form.domain, form.target)
  applySelectedCertificateToPreview(false)
  for (const nodeId of form.nodeIds) ensureEntrySelection(nodeId)
}

function applyTemplate(kind: SiteTemplateKey) {
  if (saving.value || templateConfirming.value) return
  if (!configManuallyEdited.value || !form.config.trim()) {
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
    if (notifyWhenMissing) {
      store.notify('证书路径尚不可用', 'warning', '所选节点没有上报这张证书的证书路径和私钥路径。')
    }
    return
  }
  const rewritten = rewriteConfigCertificatePaths(form.config, certificate, node)
  form.config = rewritten.content
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
  if (form.certificateId) applySelectedCertificateToPreview()
}

function handleConfigInput() {
  templateManaged.value = false
  configManuallyEdited.value = true
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

function editorDeploymentPath(nodeId: string) {
  const site = store.sites.find((item) => item.id === form.id)
  const node = store.nodes.find((item) => item.id === nodeId)
  const savedPath = site?.nodeConfigPaths?.[nodeId]
  if (savedPath) return savedPath
  if (node && site?.context !== 'main') {
    const context = site?.context === 'stream' ? 'stream' : 'http'
    const entry = nodeEntries(node, context).find(
      (item) => item.id === site?.nodeConfigEntryIds?.[nodeId],
    )
    if (entry) return `${entry.directory}/*${entry.suffix}`
  }
  return node?.facts.managed_config_root || '等待 Agent 上报路径'
}

function initialNodeBlocker(node: NodeRecord) {
  if (form.context === 'main') return '主配置不通过新增站点入口部署'
  if (node.status === 'offline') return 'Agent 离线'
  if (!node.capabilities.includes('config_apply')) return 'Agent 不支持配置写入'
  if (!nodeEntries(node, form.context).length) return 'Agent 没有上报可用配置目录'
  return ''
}

function toggleNode(nodeId: string, checked: boolean) {
  const node = store.nodes.find((item) => item.id === nodeId)
  if (checked && (!node || initialNodeBlocker(node))) return
  if (checked && !form.nodeIds.includes(nodeId)) form.nodeIds.push(nodeId)
  if (!checked) form.nodeIds = form.nodeIds.filter((item) => item !== nodeId)
  ensureEntrySelection(nodeId)
  if (form.certificateId) applySelectedCertificateToPreview(false)
}


function validateForm() {
  if (editorMode.value === 'create' && !form.nodeIds.length) return '请选择至少一个首次部署节点'
  if (editorMode.value === 'create') {
    for (const nodeId of form.nodeIds) {
      const node = store.nodes.find((item) => item.id === nodeId)
      if (!node) return '首次部署节点已不存在，请重新选择'
      const blocker = initialNodeBlocker(node)
      if (blocker) return `${node.node_name}：${blocker}`
    }
  }
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
  if (editingSiteBusy.value) {
    store.notify('配置操作执行中', 'warning', siteBusyTitle)
    return
  }
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
    const deploymentNodeIds = previous ? [...previous.nodeIds] : [...form.nodeIds]
    const deploymentEntryIds = previous
      ? { ...(previous.nodeConfigEntryIds || {}) }
      : { ...form.nodeConfigEntryIds }
    const deploymentContext = previous?.context || form.context
    const deploymentFilename = previous
      ? previous.filename
      : resourceType === 'generic'
        ? form.filename.trim() ||
          `${safeName(form.name)}.${deploymentContext === 'stream' ? 'stream' : 'conf'}`
        : undefined
    const site: SiteRecord = {
      ...(previous || {}),
      id: form.id,
      resourceType,
      name: resourceType === 'generic' ? form.name.trim() : undefined,
      filename: deploymentFilename,
      domain: resourceType === 'site' ? form.domain.trim() : undefined,
      type: resourceType === 'generic' ? 'custom' : form.type,
      target: form.target.trim(),
      context: deploymentContext,
      configMode: resourceType === 'generic' ? 'generic' : editorTab.value === 'guided' ? 'guided' : 'conf',
      config: form.config,
      nodeIds: deploymentNodeIds,
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
      nodeConfigEntryIds: deploymentEntryIds,
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

function clearDeploymentSelection() {
  transferNodeIds.value = []
  for (const key of Object.keys(transferEntryIds)) delete transferEntryIds[key]
  deleteRecordAfterRemoval.value = false
  removeConfirmation.value = ''
}

function openDeploymentAdjustment() {
  if (!selected.value || selected.value.context === 'main') return
  if (isSiteBusy(selected.value)) {
    store.notify('配置操作执行中', 'warning', '请等待当前任务完成后再调整部署范围。')
    return
  }
  deploymentAction.value = 'add'
  transferMode.value = 'create'
  clearDeploymentSelection()
  transferOpen.value = true
}

function selectDeploymentAction(value: string | number | null) {
  if (!['add', 'migrate', 'remove'].includes(String(value))) return
  deploymentAction.value = String(value) as 'add' | 'migrate' | 'remove'
  clearDeploymentSelection()
}

function transferEntries(nodeId: string) {
  const site = selected.value
  const node = store.nodes.find((item) => item.id === nodeId)
  if (!site || !node || site.context === 'main') return []
  return nodeEntries(node, site.context === 'stream' ? 'stream' : 'http')
}

function deploymentEligibilityBlockers(nodeId: string) {
  const site = selected.value
  const node = store.nodes.find((item) => item.id === nodeId)
  if (!site || !node) return ['节点不存在']
  const blockers: string[] = []
  if (node.status === 'offline') blockers.push('Agent 离线')
  const nodeReadOnly = site.nodeReadOnly as Record<string, boolean> | undefined
  if (nodeReadOnly?.[nodeId]) blockers.push('当前节点配置为只读')
  if (
    deploymentAction.value !== 'remove' &&
    siteOutstandingCandidateVersion(site)
  ) {
    blockers.push('当前有未发布候选配置，请先完成发布')
  }
  if (deploymentAction.value !== 'add' && !site.nodeHashes?.[nodeId]) {
    blockers.push('缺少当前 Hash')
  }
  const requiredCapabilities =
    deploymentAction.value === 'add'
      ? [['config_apply', 'Agent 不支持配置写入']]
      : deploymentAction.value === 'remove'
        ? [['config_delete', 'Agent 不支持配置删除']]
        : [
            ['config_apply', 'Agent 不支持配置写入'],
            ['config_delete', 'Agent 不支持配置删除'],
            ['config_move', 'Agent 不支持原子迁移'],
          ]
  for (const [capability, message] of requiredCapabilities) {
    if (!node.capabilities.includes(capability)) blockers.push(message)
  }
  if (deploymentAction.value !== 'remove' && site.certificateId) {
    const certificate = store.certificates.find((item) => item.id === site.certificateId)
    if (!certificatePathsForNode(certificate, node)) blockers.push('目标节点缺少证书路径')
  }
  if (deploymentAction.value === 'remove') return blockers
  const entries = transferEntries(nodeId)
  if (!entries.length) blockers.push('没有可用配置目录')
  if (deploymentAction.value === 'add') return blockers
  const currentEntry = selected.value?.nodeConfigEntryIds?.[nodeId]
  if (!entries.some((entry) => entry.id !== currentEntry)) blockers.push('没有其他可迁移目录')
  return blockers
}

function deploymentNodeDisabled(nodeId: string) {
  return deploymentEligibilityBlockers(nodeId).length > 0
}

function deploymentNodeHint(nodeId: string) {
  const blockers = deploymentEligibilityBlockers(nodeId)
  if (blockers.length) return blockers.join('；')
  if (deploymentAction.value === 'add') return '未部署 · 添加到该节点'
  if (deploymentAction.value === 'migrate') return '已部署 · 选择其他入口后迁移'
  return '已部署 · 从该节点安全移除'
}

function transferEntryOptions(nodeId: string) {
  const currentEntry = selected.value?.nodeConfigEntryIds?.[nodeId]
  return transferEntries(nodeId).map((entry) => ({
    label: `${entry.label || entry.id} · ${entry.directory}/*${entry.suffix}`,
    value: entry.id,
    disabled: deploymentAction.value === 'migrate' && currentEntry === entry.id,
  }))
}

function deploymentPreview(nodeId: string) {
  const site = selected.value
  const node = store.nodes.find((item) => item.id === nodeId)
  if (!site || !node) return null
  const actionLabels = {
    add: '添加节点',
    migrate: '迁移目录',
    remove: '移除节点',
  }
  const hash = site.nodeHashes?.[nodeId] || ''
  const sourcePath = deploymentAction.value === 'add' ? '未部署' : managedConfigPath(site, node)
  let targetPath = '删除当前文件'
  if (deploymentAction.value !== 'remove') {
    const entryId = transferEntryIds[nodeId]
    try {
      targetPath = entryId ? configPathForEntry(site, node, entryId) : '尚未选择目标目录'
    } catch {
      targetPath = '目标目录不可用'
    }
  }

  const blockers = deploymentEligibilityBlockers(nodeId)
  if (deploymentAction.value === 'migrate' && sourcePath === targetPath) {
    blockers.push('目标路径与源路径相同')
  }
  if (deploymentAction.value !== 'remove' && !transferEntryIds[nodeId]) {
    blockers.push('未选择目标目录')
  }

  return {
    nodeId,
    nodeName: node.node_name,
    action: actionLabels[deploymentAction.value],
    sourcePath,
    targetPath,
    hash: hash || (deploymentAction.value === 'add' ? '新节点，无当前 Hash' : '缺失'),
    safe: blockers.length === 0,
    safety:
      blockers.join('；') ||
      (deploymentAction.value === 'remove'
        ? '按当前 Hash 安全删除'
        : transferMode.value === 'replace'
          ? '目标存在时允许替换'
          : '目标存在时停止，不覆盖'),
  }
}

function toggleTransferNode(nodeId: string, checked: boolean) {
  if (checked && !transferNodeIds.value.includes(nodeId)) {
    if (deploymentNodeDisabled(nodeId)) return
    transferNodeIds.value.push(nodeId)
    if (deploymentAction.value !== 'remove') {
      const current = selected.value?.nodeConfigEntryIds?.[nodeId]
      const entries = transferEntries(nodeId)
      transferEntryIds[nodeId] =
        entries.find((entry) => deploymentAction.value !== 'migrate' || entry.id !== current)?.id ||
        ''
    }
  }
  if (!checked) {
    transferNodeIds.value = transferNodeIds.value.filter((item) => item !== nodeId)
    delete transferEntryIds[nodeId]
  }
}

async function submitDeploymentAdjustment() {
  if (!selected.value || !transferNodeIds.value.length) {
    store.notify('请选择至少一个节点', 'warning')
    return
  }
  if (isSiteBusy(selected.value)) {
    store.notify('配置操作执行中', 'warning', siteBusyTitle)
    return
  }
  if (deploymentAction.value !== 'remove' && transferNodeIds.value.some((id) => !transferEntryIds[id])) {
    store.notify('请为每个目标节点选择配置目录', 'warning')
    return
  }
  if (deploymentPreviews.value.some((preview) => !preview.safe)) {
    store.notify('变更预检未通过', 'warning', '请先处理摘要中标记的节点问题。')
    return
  }
  if (deleteRecordAfterRemoval.value && !removesLastDeployment.value) {
    store.notify('只有移除最后部署节点时才能同时删除平台记录', 'warning')
    return
  }
  if (deleteRecordAfterRemoval.value && !removeConfirmationMatches.value) {
    store.notify('请输入完整配置名称以确认删除', 'warning')
    return
  }
  if (finalCandidateRemovalBlocked.value) {
    store.notify(
      '当前还有未发布候选配置',
      'warning',
      '请先完成发布；如果确定不再保留该配置，也可以勾选同时删除平台记录。',
    )
    return
  }
  transferring.value = true
  try {
    if (deploymentAction.value === 'remove') {
      await store.removeSiteFromNodes(
        selected.value.id,
        [...transferNodeIds.value],
        deleteRecordAfterRemoval.value,
      )
    } else {
      await store.transferSite(
        selected.value.id,
        transferNodeIds.value.map((nodeId) => ({
          nodeId,
          entryId: transferEntryIds[nodeId],
        })),
        transferMode.value,
      )
    }
    transferOpen.value = false
  } catch (error) {
    const actionLabel =
      deploymentAction.value === 'add'
        ? '添加节点'
        : deploymentAction.value === 'migrate'
          ? '目录迁移'
          : '移除节点'
    store.notify(`${actionLabel}任务未提交`, 'danger', store.apiMessage(error))
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
  if (isSiteBusy(selected.value)) {
    store.notify('配置操作执行中', 'warning', siteBusyTitle)
    return
  }
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

function deleteRecord() {
  if (!selected.value) return
  const site = selected.value
  if (isSiteBusy(site)) {
    store.notify('配置操作执行中', 'warning', siteBusyTitle)
    return
  }
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
    <header class="sites-hero">
      <div class="sites-hero-copy">
        <span class="sites-eyebrow"><i aria-hidden="true"></i> Configuration desk</span>
        <h1>{{ heroHeadline }}</h1>
        <p>证书、配置、Agent 和 reload 状态集中在一条链路里。</p>
      </div>
      <ReleaseChannel
        :site="releaseSite"
        variant="summary"
        :additional-active-count="additionalPendingCount"
      />
    </header>

    <section class="sites-fact-command" aria-label="配置概览与快捷操作">
      <div class="site-fact">
        <strong>{{ store.sites.length }}</strong>
        <span>托管配置<small>站点与通用 Conf</small></span>
      </div>
      <div class="site-fact">
        <strong>{{ store.certificates.length }}</strong>
        <span>域名证书<small>已纳管证书</small></span>
      </div>
      <div class="site-fact" :data-tone="draftCount ? 'warning' : 'neutral'">
        <strong>{{ draftCount }}</strong>
        <span>需要处理<small>草稿、漂移与失败</small></span>
      </div>
      <div class="site-fact" :data-tone="pendingCount ? 'active' : 'neutral'">
        <strong>{{ pendingCount }}</strong>
        <span>执行中<small>{{ pendingCount ? '配置操作待完成' : '当前空闲' }}</small></span>
      </div>
      <div class="sites-command-actions">
        <NButton
          secondary
          :loading="scanning || activeConfigScan"
          :disabled="!store.canOperate || activeConfigScan"
          @click="scanSites"
        >
          {{ activeConfigScan ? '扫描进行中' : '导入节点配置' }}
        </NButton>
        <NButton type="primary" :disabled="!store.canOperate" @click="openCreate">
          <template #icon><Plus :size="17" /></template>
          新增站点
        </NButton>
      </div>
    </section>

    <ReleaseChannel
      :site="releaseSite"
      variant="flow"
      :additional-active-count="additionalPendingCount"
    />

    <div class="master-detail">
      <section class="data-panel sites-config-table">
        <div class="filter-bar">
          <NInput
            v-model:value="search"
            clearable
            placeholder="搜索域名、端口、IP、Nginx 指令或备注"
          >
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
            :aria-current="store.selectedSiteId === site.id ? 'true' : undefined"
            @click="selectSite(site.id)"
          >
            <span class="site-primary">
              <strong :title="siteTitle(site)">{{ siteTitle(site) }}</strong>
              <small :title="siteSearchHints.get(site.id) || site.note || siteKind(site)">
                {{ siteSearchHints.get(site.id) || site.note || siteKind(site) }}
              </small>
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
            <span>
              <strong>{{ siteVersionLabel(site) }}</strong>
              <small>{{ siteVersionProgress(site) || relativeTime(site.updatedAt) }}</small>
            </span>
            <span><StatusTag v-bind="siteStatus(site)" :pulse="Boolean(site.pendingRemote)" /></span>
          </button>
        </div>
        <div v-else class="empty-state">
          <Search :size="26" />
          <strong>没有匹配的配置</strong>
          <span>调整筛选条件，或创建一个新站点。</span>
        </div>
      </section>

      <button
        v-if="narrowDetail && detailDrawerOpen"
        type="button"
        class="sites-detail-backdrop"
        aria-label="关闭详情"
        @click="closeDetailDrawer"
      ></button>

      <aside
        v-if="selected"
        ref="detailPanel"
        class="detail-panel sites-detail-panel"
        :class="{ 'is-drawer-open': detailDrawerOpen }"
        :role="narrowDetail && detailDrawerOpen ? 'dialog' : undefined"
        :aria-modal="narrowDetail && detailDrawerOpen ? 'true' : undefined"
        :inert="narrowDetail && !detailDrawerOpen"
        :aria-label="`${siteTitle(selected)} 配置详情`"
        :tabindex="narrowDetail ? -1 : undefined"
        @keydown="trapDetailFocus"
      >
        <div class="sites-detail-fixed">
          <button
            ref="detailCloseButton"
            type="button"
            class="sites-detail-close"
            aria-label="关闭详情"
            @click="closeDetailDrawer"
          >
            <X :size="18" />
          </button>
          <div class="detail-head">
            <div>
              <span class="detail-eyebrow">Selected configuration</span>
              <h2>{{ siteTitle(selected) }}</h2>
              <p>
                {{ siteKind(selected) }} · 配置 {{ siteVersionLabel(selected) }}
                <template v-if="siteVersionProgress(selected)">
                  · {{ siteVersionProgress(selected) }} 节点成功
                </template>
              </p>
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
            <NButton
              :disabled="!store.canOperate || selectedSiteBusy"
              :title="selectedSiteBusy ? '当前操作完成后才能编辑' : '编辑配置内容与备注'"
              @click="openEdit(selected)"
            >编辑配置</NButton>
            <NButton
              :disabled="
                !store.canOperate ||
                selected.context === 'main' ||
                selectedSiteBusy
              "
              :title="
                selectedSiteBusy
                  ? siteBusyTitle
                  : selected.context === 'main'
                  ? '主配置不支持增删节点或迁移目录'
                  : '添加、迁移或移除部署节点'
              "
              @click="openDeploymentAdjustment"
            >
              <template #icon><Copy :size="16" /></template>
              调整部署范围
            </NButton>
            <NButton @click="openHistory">
              <template #icon><History :size="16" /></template>
              版本记录
            </NButton>
            <NButton
              :loading="running"
              :disabled="Boolean(selectedRunBlocker)"
              :title="selectedRunBlocker || '在全部部署节点执行 nginx -t'"
              @click="run(false)"
            >
              逐节点校验
            </NButton>
            <NButton
              type="primary"
              class="detail-publish-action"
              :loading="running"
              :disabled="Boolean(selectedRunBlocker)"
              :title="selectedRunBlocker || '校验并发布到全部部署节点'"
              @click="run(true)"
            >
              校验并发布
            </NButton>
            <NButton
              v-if="!selected.nodeIds.length"
              type="error"
              secondary
              class="detail-danger-action"
              :disabled="!store.canOperate || selectedSiteBusy"
              :title="selectedSiteBusy ? siteBusyTitle : '删除未部署的平台记录'"
              @click="deleteRecord"
            >
              删除平台记录
            </NButton>
          </div>
        </div>

        <div ref="detailScroll" class="sites-detail-scroll">
          <div class="detail-context-grid">
          <div>
            <h3>配置备注</h3>
            <p>{{ selected.note || '尚未填写配置用途和负责人。' }}</p>
          </div>
          <div>
            <h3>最近变更</h3>
            <p>{{ selected.changeNote || '暂无变更说明' }}</p>
          </div>
        </div>

        <SiteScreenshotAttachments
          :site-id="selected.id"
          :editable="store.canOperate && !selectedSiteBusy"
          :refresh-token="attachmentRefreshToken"
          compact
          @changed="refreshAttachmentsFor"
        />

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
            <small>
              {{
                editorMode === 'edit' && form.context === 'main'
                  ? '主配置仅支持手写 Conf'
                  : editorMode === 'edit'
                    ? '仅显示当前上下文模板'
                    : '选择后会替换右侧 Conf'
              }}
            </small>
          </div>
          <button
            v-for="template in visibleSiteTemplates"
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
                :disabled="editorMode === 'edit' || form.context === 'main'"
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

          <fieldset v-if="editorMode === 'create'">
            <legend>首次部署节点</legend>
            <div class="choice-grid">
              <button
                v-for="node in store.nodes"
                :key="node.id"
                type="button"
                class="choice-card"
                :class="{
                  selected: form.nodeIds.includes(node.id),
                  offline: Boolean(initialNodeBlocker(node)),
                }"
                :disabled="Boolean(initialNodeBlocker(node))"
                :title="initialNodeBlocker(node) || '点击选择首次部署节点'"
                :aria-pressed="form.nodeIds.includes(node.id)"
                @click="toggleNode(node.id, !form.nodeIds.includes(node.id))"
              >
                <span class="choice-card-indicator" aria-hidden="true">
                  <Check v-if="form.nodeIds.includes(node.id)" :size="14" />
                </span>
                <span>
                  <strong>{{ node.node_name }}</strong>
                  <small>
                    {{ node.hostname }} · {{ initialNodeBlocker(node) || '在线，可部署' }}
                  </small>
                </span>
              </button>
            </div>
            <p v-if="!store.nodes.length" class="field-empty-hint">
              尚未接入 Agent，请先到“节点 Agent”页面完成接入。
            </p>
          </fieldset>

          <fieldset v-else>
            <legend>当前部署范围（只读）</legend>
            <div v-if="form.nodeIds.length" class="deployment-list">
              <article v-for="nodeId in form.nodeIds" :key="nodeId">
                <span
                  class="online-dot"
                  :class="{
                    offline: store.nodes.find((item) => item.id === nodeId)?.status === 'offline',
                  }"
                ></span>
                <div>
                  <strong>{{ store.nodes.find((item) => item.id === nodeId)?.node_name || nodeId }}</strong>
                  <code :title="editorDeploymentPath(nodeId)">{{ editorDeploymentPath(nodeId) }}</code>
                </div>
                <StatusTag label="只读" tone="neutral" />
              </article>
            </div>
            <p v-else class="field-empty-hint">当前配置未部署到任何节点。</p>
            <p class="field-empty-hint">节点增删和目录迁移请在详情页使用“调整部署范围”。</p>
          </fieldset>

          <div
            v-if="editorMode === 'create' && form.nodeIds.length && form.context !== 'main'"
            class="entry-targets"
          >
            <h3>首次部署目录</h3>
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
          <SiteScreenshotAttachments
            :site-id="editorMode === 'edit' ? form.id : ''"
            :editable="store.canOperate && editorMode === 'edit' && !editingSiteBusy"
            :refresh-token="attachmentRefreshToken"
            @changed="refreshAttachmentsFor"
          />
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
            :disabled="saving || editingSiteBusy"
            :title="editingSiteBusy ? siteBusyTitle : '保存草稿'"
            @click="saveDraft"
          >保存草稿</NButton>
        </div>
      </template>
    </NModal>

    <NModal
      v-model:show="transferOpen"
      preset="card"
      class="action-modal"
      title="调整部署范围"
      :bordered="false"
      :mask-closable="false"
      :closable="!transferring"
      :close-on-esc="!transferring"
    >
      <p class="modal-lead">
        {{ deploymentActionLead }} 拓扑调整不会增加配置版本。
      </p>
      <label>
        <span>调整动作</span>
        <NSelect
          :value="deploymentAction"
          :options="deploymentActionOptions"
          @update:value="selectDeploymentAction"
        />
      </label>
      <label v-if="deploymentAction !== 'remove'">
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
          v-for="node in deploymentCandidateNodes"
          :key="node.id"
          class="transfer-target"
          :class="{
            selected: transferNodeIds.includes(node.id),
            disabled: deploymentNodeDisabled(node.id),
          }"
        >
          <NCheckbox
            :checked="transferNodeIds.includes(node.id)"
            :disabled="deploymentNodeDisabled(node.id)"
            @update:checked="(checked) => toggleTransferNode(node.id, checked)"
          />
          <span>
            <strong>{{ node.node_name }}</strong>
            <small>{{ deploymentNodeHint(node.id) }}</small>
          </span>
          <NSelect
            v-if="transferNodeIds.includes(node.id) && deploymentAction !== 'remove'"
            v-model:value="transferEntryIds[node.id]"
            :options="transferEntryOptions(node.id)"
          />
        </label>
        <div v-if="!deploymentCandidateNodes.length" class="empty-state">
          {{
            deploymentAction === 'add'
              ? '没有可添加的节点。'
              : deploymentAction === 'migrate'
                ? '当前配置没有可迁移目录的部署节点。'
                : '当前配置没有部署节点可移除。'
          }}
        </div>
      </div>

      <div v-if="deploymentPreviews.length" class="entry-targets">
        <h3>变更预检摘要</h3>
        <div class="deployment-list">
          <article v-for="preview in deploymentPreviews" :key="preview.nodeId">
            <ShieldCheck v-if="preview.safe" :size="15" aria-hidden="true" />
            <AlertTriangle v-else :size="15" aria-hidden="true" />
            <div>
              <strong>{{ preview.nodeName }} · {{ preview.action }}</strong>
              <code :title="`${preview.sourcePath} → ${preview.targetPath}`">
                {{ preview.sourcePath }} → {{ preview.targetPath }}
              </code>
              <small>当前 Hash：{{ preview.hash }} · {{ preview.safety }}</small>
            </div>
            <StatusTag
              :label="preview.safe ? '本地预检通过' : '需要处理'"
              :tone="preview.safe ? 'success' : 'danger'"
            />
          </article>
        </div>
      </div>

      <div v-if="removesLastDeployment" class="security-banner">
        <AlertTriangle :size="20" />
        <div>
          <NCheckbox v-model:checked="deleteRecordAfterRemoval">
            移除成功后同时删除平台记录
          </NCheckbox>
          <p>默认只将配置保留为“未部署”。勾选后，全部目标节点移除成功时平台记录也会删除。</p>
          <p v-if="finalCandidateRemovalBlocked" class="danger-copy">
            当前还有未发布候选配置。为避免留下无法发布的草稿，请先完成发布，或勾选同时删除平台记录。
          </p>
          <label v-if="deleteRecordAfterRemoval">
            <span>输入“{{ selected ? siteTitle(selected) : '' }}”确认</span>
            <NInput
              v-model:value="removeConfirmation"
              :placeholder="selected ? siteTitle(selected) : ''"
              autocomplete="off"
            />
          </label>
        </div>
      </div>
      <template #footer>
        <div class="modal-footer">
          <NButton :disabled="transferring" @click="transferOpen = false">取消</NButton>
          <NButton
            type="primary"
            :loading="transferring"
            :disabled="
              !transferNodeIds.length ||
              selectedSiteBusy ||
              deploymentPreviews.some((preview) => !preview.safe) ||
              finalCandidateRemovalBlocked ||
              (deleteRecordAfterRemoval && !removeConfirmationMatches)
            "
            :title="selectedSiteBusy ? siteBusyTitle : deploymentSubmitLabel"
            @click="submitDeploymentAdjustment"
          >
            {{ deploymentSubmitLabel }}
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
            :disabled="
              !store.canOperate || selectedSiteBusy || revision.version === selected?.version
            "
            :title="selectedSiteBusy ? siteBusyTitle : '恢复此版本为草稿'"
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
