<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Boxes,
  CheckCircle2,
  CircleDot,
  FileDiff,
  Import,
  LockKeyhole,
  Network,
  Pencil,
  Plus,
  RefreshCw,
  Route,
  Server,
  ShieldCheck,
  Trash2,
  Waypoints,
} from '@lucide/vue'
import { NButton, NCheckbox, NInput, NInputNumber, NModal, NSelect, NSwitch } from 'naive-ui'
import { ApiError, api } from '../api'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import { useConsoleStore } from '../stores/console'
import type {
  JobRecord,
  LvsIntent,
  LvsManagedMember,
  LvsManagedService,
  LvsMember,
  LvsPlan,
  NodeRecord,
  Tone,
} from '../types'
import {
  buildLvsOverview,
  buildLvsSemanticDiff,
  canonicalLvsService,
  lvsListenerKey,
  lvsManagedServiceFor,
  lvsManagement,
  lvsMemberKey,
  lvsObservation,
  lvsPlanSemanticDiff,
  lvsServiceEditable,
  type LvsSemanticDiffLine,
  type LvsVirtualServiceGroup,
} from '../utils/ipvs'

type ObjectMode = 'services' | 'pools' | 'members'

const store = useConsoleStore()
const objectMode = ref<ObjectMode>('services')
const query = ref('')
const directorId = ref('all')
const selectedKey = ref('')
const detailDirectorId = ref('all')
const refreshing = ref(false)
const editorOpen = ref(false)
const planOpen = ref(false)
const planning = ref(false)
const applying = ref(false)
const planConfirmed = ref(false)
const planned = ref<LvsPlan | null>(null)
const plannedIntent = ref<LvsIntent | null>(null)
const planRequestId = ref('')
const semanticDiff = ref<LvsSemanticDiffLine[]>([])
const publishResult = ref<{
  tone: Tone
  title: string
  message: string
  failures: Array<{ node: string; stage: string; rollback: string; message: string }>
} | null>(null)

type DraftMode = 'create' | 'edit' | 'takeover' | 'delete'
interface LvsDraftState {
  mode: DraftMode
  nodeIds: string[]
  baseService: LvsManagedService | null
  service: LvsManagedService
  changeNote: string
}

const draft = ref<LvsDraftState | null>(null)

const schedulerOptions = [
  ['wlc', 'WLC · 加权最少连接'], ['wrr', 'WRR · 加权轮询'], ['rr', 'RR · 轮询'],
  ['lc', 'LC · 最少连接'], ['sh', 'SH · 源地址哈希'], ['dh', 'DH · 目标地址哈希'],
  ['lblc', 'LBLC · 局部最少连接'], ['lblcr', 'LBLCR · 带复制局部最少连接'],
  ['sed', 'SED · 最短预期延迟'], ['nq', 'NQ · 永不排队'], ['mh', 'MH · Maglev 哈希'],
].map(([value, label]) => ({ value, label }))
const protocolOptions = ['TCP', 'UDP', 'SCTP'].map((value) => ({ value, label: value }))
const forwardingOptions = [
  { value: 'DR', label: 'DR · Direct Routing' },
  { value: 'NAT', label: 'NAT · 网络地址转换' },
  { value: 'TUN', label: 'TUN · IP 隧道' },
]

const overview = computed(() => buildLvsOverview(store.nodes))
const manageableNodes = computed(() => store.nodes.filter((node) =>
  node.capabilities.includes('lvs_manage_v1') &&
  lvsManagement(node)?.management_enabled === true,
))
const onlineManageableNodes = computed(() => manageableNodes.value.filter((node) => node.status !== 'offline'))

function lvsTopologyForNode(node: NodeRecord): 'vrrp' | 'standalone' {
  const keepalived = node.facts.keepalived
  return keepalived && typeof keepalived === 'object' && !Array.isArray(keepalived)
    && (keepalived as Record<string, unknown>).mode === 'standalone'
    ? 'standalone'
    : 'vrrp'
}

function lvsTopologyLabel(node: NodeRecord) {
  return lvsTopologyForNode(node) === 'standalone' ? '单 Director' : 'VRRP'
}

const draftTargetNodes = computed(() => {
  if (!draft.value) return []
  const selected = new Set(draft.value.nodeIds)
  return onlineManageableNodes.value.filter((node) => selected.has(node.id))
})
const directorOptions = computed(() => [
  { label: `全部 Director（${overview.value.availableNodes.length}）`, value: 'all' },
  ...overview.value.capableNodes.map((node) => ({
    label: `${node.node_name} · ${node.status === 'offline' ? '离线' : '在线'}`,
    value: node.id,
  })),
])

function listenerForGroup(group: LvsVirtualServiceGroup | null) {
  if (!group || !group.address || !group.port || group.protocol === 'FWM') return null
  if (!['TCP', 'UDP', 'SCTP'].includes(group.protocol)) return null
  return {
    address: group.address,
    port: group.port,
    protocol: group.protocol as 'TCP' | 'UDP' | 'SCTP',
  }
}

function managedEntries(group: LvsVirtualServiceGroup | null) {
  const listener = listenerForGroup(group)
  if (!group || !listener) return []
  return group.snapshots.flatMap((snapshot) => {
    const service = lvsManagedServiceFor(snapshot.node, listener)
    return service ? [{ node: snapshot.node, service }] : []
  })
}

function managementMeta(group: LvsVirtualServiceGroup) {
  const entries = managedEntries(group)
  if (!listenerForGroup(group)) return { label: '外部格式·只读', tone: 'neutral' as Tone, managed: false }
  if (!entries.length) return { label: '仅观测·只读', tone: 'info' as Tone, managed: false }
  if (entries.length !== group.snapshots.length) return { label: '部分接管', tone: 'warning' as Tone, managed: false }
  if (entries.some(({ service }) => !lvsServiceEditable(service))) {
    return { label: '含不支持指令·只读', tone: 'warning' as Tone, managed: false }
  }
  if (entries.some(({ service }) => service.origin !== 'managed')) {
    return { label: '外部配置', tone: 'info' as Tone, managed: false }
  }
  const hashes = new Set(entries.map(({ service }) => JSON.stringify(service)))
  if (hashes.size > 1) return { label: '托管配置漂移', tone: 'danger' as Tone, managed: false }
  return { label: '平台托管', tone: 'success' as Tone, managed: true }
}

function snapshots(group: LvsVirtualServiceGroup) {
  return directorId.value === 'all'
    ? group.snapshots
    : group.snapshots.filter((snapshot) => snapshot.node.id === directorId.value)
}

const filteredGroups = computed(() => {
  const needle = query.value.trim().toLowerCase()
  return overview.value.groups.filter((group) => {
    const visible = snapshots(group)
    if (!visible.length) return false
    if (!needle) return true
    const haystack = [
      group.label,
      group.protocol,
      group.scheduler,
      String(group.port ?? ''),
      String(group.fwmark ?? ''),
      ...visible.map((snapshot) => snapshot.node.node_name),
      ...visible.flatMap((snapshot) => snapshot.service.destinations.flatMap((member) => [
        member.address,
        String(member.port),
        member.forwarding,
      ])),
    ].join(' ').toLowerCase()
    return haystack.includes(needle)
  })
})

watch(filteredGroups, (groups) => {
  if (!groups.some((group) => group.key === selectedKey.value)) {
    selectedKey.value = groups[0]?.key || ''
  }
}, { immediate: true })

const selectedGroup = computed(() =>
  overview.value.groups.find((group) => group.key === selectedKey.value) || null,
)
const selectedManagement = computed(() => selectedGroup.value
  ? managementMeta(selectedGroup.value)
  : { label: '未选择', tone: 'neutral' as Tone, managed: false },
)
const selectedManagedEntries = computed(() => managedEntries(selectedGroup.value))
const selectedManagedService = computed(() => selectedManagedEntries.value[0]?.service || null)
const selectedHasExistingConfig = computed(() => selectedManagedEntries.value.some(
  ({ service }) => service.origin !== 'managed',
))
const selectedHasUnsupportedConfig = computed(() => selectedManagedEntries.value.some(
  ({ service }) => !lvsServiceEditable(service),
))
const canTakeOverSelected = computed(() => {
  const group = selectedGroup.value
  const listener = listenerForGroup(group)
  if (!group || group.drift || group.partial || !listener || !group.snapshots.length) return false
  const entries = managedEntries(group)
  if (entries.length !== group.snapshots.length || entries.some(({ service }) => service.origin !== 'existing')) {
    return false
  }
  const nodesReady = group.snapshots.every((snapshot) =>
    snapshot.node.status !== 'offline' &&
    snapshot.node.capabilities.includes('lvs_manage_v1') &&
    snapshot.node.capabilities.includes('lvs_adopt_v1') &&
    lvsManagement(snapshot.node)?.management_enabled === true,
  )
  const servicesEditable = group.snapshots.every((snapshot) => {
    const service = lvsManagedServiceFor(snapshot.node, listener)
    return !service || lvsServiceEditable(service)
  })
  const forwardingModes = new Set(group.snapshots.flatMap((snapshot) =>
    snapshot.service.destinations.map((member) => member.forwarding),
  ))
  return nodesReady && servicesEditable && forwardingModes.size <= 1
})

watch(selectedKey, () => {
  detailDirectorId.value = 'all'
})

interface MemberRow {
  key: string
  group: LvsVirtualServiceGroup
  member: LvsMember
  directors: string[]
  weights: number[]
  forwarding: string[]
  activeConnections: number
  inactiveConnections: number
}

function memberRows(group: LvsVirtualServiceGroup, onlyVisible = false): MemberRow[] {
  const rows = new Map<string, MemberRow>()
  const source = onlyVisible ? snapshots(group) : group.snapshots
  for (const snapshot of source) {
    for (const member of snapshot.service.destinations) {
      const key = lvsMemberKey(member)
      const row = rows.get(key) || {
        key,
        group,
        member,
        directors: [],
        weights: [],
        forwarding: [],
        activeConnections: 0,
        inactiveConnections: 0,
      }
      row.directors.push(snapshot.node.node_name)
      row.weights.push(member.weight)
      row.forwarding.push(member.forwarding)
      row.activeConnections = Math.max(row.activeConnections, member.active_connections)
      row.inactiveConnections = Math.max(row.inactiveConnections, member.inactive_connections)
      rows.set(key, row)
    }
  }
  return [...rows.values()].sort((left, right) => left.key.localeCompare(right.key, undefined, { numeric: true }))
}

const objectRows = computed(() => {
  if (objectMode.value === 'members') {
    return filteredGroups.value.flatMap((group) => memberRows(group, true).map((row) => ({
      id: `${group.key}|${row.key}`,
      group,
      title: `${row.member.address}:${row.member.port}`,
      subtitle: 'Pool Member · Real Server',
      relation: group.label,
      method: `${[...new Set(row.forwarding)].join(' / ')} · weight ${[...new Set(row.weights)].join(' / ')}`,
      directors: row.directors,
      active: row.activeConnections,
      inactive: row.inactiveConnections,
      state: row.weights.every((weight) => weight === 0) ? 'disabled' : group.drift ? 'drift' : group.partial ? 'partial' : 'observed',
    })))
  }
  return filteredGroups.value.map((group) => ({
    id: group.key,
    group,
    title: objectMode.value === 'pools' ? `Pool · ${group.label}` : group.label,
    subtitle: objectMode.value === 'pools' ? '派生 Backend Pool' : `${group.protocol} Virtual Service`,
    relation: objectMode.value === 'pools' ? group.label : `${group.memberCount} 个 Pool Member`,
    method: group.scheduler + (group.persistenceSeconds !== null ? ` · persistence ${group.persistenceSeconds}s` : ''),
    directors: snapshots(group).map((snapshot) => snapshot.node.node_name),
    active: group.activeConnections,
    inactive: group.inactiveConnections,
    state: group.drift ? 'drift' : group.partial ? 'partial' : 'observed',
  }))
})

const detailSnapshots = computed(() => {
  if (!selectedGroup.value) return []
  return detailDirectorId.value === 'all'
    ? selectedGroup.value.snapshots
    : selectedGroup.value.snapshots.filter((snapshot) => snapshot.node.id === detailDirectorId.value)
})
const detailMembers = computed(() => selectedGroup.value
  ? memberRows({ ...selectedGroup.value, snapshots: detailSnapshots.value })
  : [],
)

function stateMeta(state: string): { label: string; tone: Tone } {
  if (state === 'drift') return { label: '配置漂移', tone: 'danger' }
  if (state === 'disabled') return { label: '已停用', tone: 'warning' }
  if (state === 'partial') return { label: '观测不完整', tone: 'info' }
  return { label: '已观测', tone: 'info' }
}

function forwardingLabel(value: string) {
  return ({ nat: 'NAT', dr: 'DR', tunnel: 'TUN', local: 'LOCAL', bypass: 'BYPASS', unknown: '未知' } as Record<string, string>)[value] || value
}

function unavailableLabel(reason?: string) {
  return ({
    disabled: '观察器未启用',
    not_loaded: 'IPVS 模块未加载',
    permission_denied: 'procfs 无读取权限',
    unavailable: 'IPVS 状态不可用',
    invalid_format: 'IPVS 数据格式无法识别',
    helper_unavailable: '特权 Helper 不可用',
  } as Record<string, string>)[reason || ''] || '尚未收到有效观测'
}

function formatNumber(value: number) {
  return new Intl.NumberFormat('zh-CN').format(value || 0)
}

function formatEndpoint(member: LvsMember) {
  return member.address.includes(':') ? `[${member.address}]:${member.port}` : `${member.address}:${member.port}`
}

function cloneService(service: LvsManagedService): LvsManagedService {
  return JSON.parse(JSON.stringify(service)) as LvsManagedService
}

function emptyService(): LvsManagedService {
  return {
    name: 'service-443',
    listener: { address: '192.0.2.110', port: 443, protocol: 'TCP' },
    scheduler: 'wlc',
    forwarding: 'DR',
    delay_loop: 6,
    persistence_seconds: null,
    members: [],
  }
}

function openCreate() {
  publishResult.value = null
  draft.value = {
    mode: 'create',
    nodeIds: [],
    baseService: null,
    service: emptyService(),
    changeNote: '',
  }
  editorOpen.value = true
}

function openTakeover() {
  const group = selectedGroup.value
  const service = selectedManagedService.value
  if (!group || !service || !canTakeOverSelected.value) {
    store.notify('当前服务不能接管', 'warning', '需要所有 Director 在线、配置与转发模式一致，且启用 lvs_manage_v1。')
    return
  }
  draft.value = {
    mode: 'takeover',
    nodeIds: group.snapshots.map((snapshot) => snapshot.node.id),
    baseService: null,
    service: cloneService(service),
    changeNote: '从当前 IPVS 观测结果接管',
  }
  editorOpen.value = true
}

function openEdit() {
  const service = selectedManagedService.value
  if (!service || !selectedManagement.value.managed) {
    store.notify('该服务暂不可编辑', 'warning', '只有所有 Director 上均由平台托管且配置一致的服务可以直接编辑。')
    return
  }
  draft.value = {
    mode: 'edit',
    nodeIds: selectedManagedEntries.value.map(({ node }) => node.id),
    baseService: cloneService(service),
    service: cloneService(service),
    changeNote: '',
  }
  editorOpen.value = true
}

function openDelete() {
  const service = selectedManagedService.value
  if (!service || !selectedManagement.value.managed) return
  draft.value = {
    mode: 'delete',
    nodeIds: selectedManagedEntries.value.map(({ node }) => node.id),
    baseService: cloneService(service),
    service: cloneService(service),
    changeNote: '',
  }
  editorOpen.value = true
}

function toggleDraftNode(nodeId: string, checked: boolean) {
  if (!draft.value || draft.value.mode !== 'create') return
  const node = onlineManageableNodes.value.find((item) => item.id === nodeId)
  if (!node) return
  const selected = new Set(draft.value.nodeIds)
  if (checked && lvsTopologyForNode(node) === 'standalone') {
    draft.value.nodeIds = [nodeId]
    return
  }
  if (checked) {
    for (const selectedId of selected) {
      const selectedNode = onlineManageableNodes.value.find((item) => item.id === selectedId)
      if (selectedNode && lvsTopologyForNode(selectedNode) === 'standalone') selected.delete(selectedId)
    }
    selected.add(nodeId)
  }
  else selected.delete(nodeId)
  draft.value.nodeIds = [...selected]
}

function addMember() {
  if (!draft.value) return
  const listenerPort = draft.value.service.listener.port || 80
  draft.value.service.members.push({
    address: '192.0.2.10',
    port: listenerPort,
    weight: 100,
    enabled: true,
    monitor: null,
  })
}

function removeMember(index: number) {
  draft.value?.service.members.splice(index, 1)
}

function toggleMemberMonitor(member: LvsManagedMember, checked: boolean) {
  member.monitor = checked
    ? {
        kind: 'tcp',
        connect_timeout: 3,
        retries: 3,
        delay_before_retry: 3,
        connect_port: member.port,
      }
    : null
}

function isIpAddress(value: string) {
  const text = value.trim()
  if (text.includes(':')) return /^[0-9a-f:]+$/i.test(text) && text.includes(':')
  const fields = text.split('.')
  return fields.length === 4 && fields.every((field) => /^\d{1,3}$/.test(field) && Number(field) <= 255)
}

function draftValidationError() {
  const current = draft.value
  if (!current) return '没有可预览的草稿'
  if (!current.nodeIds.length) return '至少选择一个 Director'
  const invalidNode = current.nodeIds.some((id) => {
    const node = store.nodes.find((item) => item.id === id)
    return !node || node.status === 'offline' ||
      !node.capabilities.includes('lvs_manage_v1') ||
      lvsManagement(node)?.management_enabled !== true
  })
  if (invalidNode) return '目标 Director 必须在线且已启用 LVS 管理'
  const targetTopologies = new Set(current.nodeIds.map((id) => {
    const node = store.nodes.find((item) => item.id === id)
    return node ? lvsTopologyForNode(node) : 'unknown'
  }))
  if (targetTopologies.size > 1) return '单 Director 与 VRRP Director 不能混合发布'
  if (targetTopologies.has('standalone')) {
    if (current.nodeIds.length !== 1) return '单 Director 拓扑只能选择一个节点'
    const standalone = store.nodes.find((item) => item.id === current.nodeIds[0])
    if (!standalone?.capabilities.includes('lvs_standalone_v1')) return '该 Agent 版本尚不支持单 Director 安全发布'
  }
  if (current.mode === 'delete') return ''
  const service = current.service
  if (!/^[A-Za-z0-9._:-]{1,128}$/.test(service.name)) return '服务名称仅支持字母、数字、点、下划线、冒号和短横线'
  if (!isIpAddress(service.listener.address)) return '请输入有效的 Virtual Service IP'
  if (!Number.isInteger(service.listener.port) || service.listener.port < 1 || service.listener.port > 65535) return '监听端口必须在 1–65535 之间'
  if (!Number.isInteger(service.delay_loop) || service.delay_loop < 1 || service.delay_loop > 3600) return '检查周期必须在 1–3600 秒之间'
  if (service.persistence_seconds !== null && service.persistence_seconds !== undefined &&
      (!Number.isInteger(service.persistence_seconds) || service.persistence_seconds < 1 || service.persistence_seconds > 86400)) {
    return '会话保持时间必须留空或设置为 1–86400 秒'
  }
  if (!service.members.length) return '至少添加一个 Pool Member'
  if (!service.members.some((member) => member.enabled)) return '至少保留一个启用的 Pool Member'
  const memberKeys = new Set<string>()
  for (const member of service.members) {
    if (!isIpAddress(member.address)) return `成员 ${member.address || '（空）'} 的 IP 无效`
    if (!Number.isInteger(member.port) || member.port < 1 || member.port > 65535) return `成员 ${member.address} 的端口无效`
    if (!Number.isInteger(member.weight) || member.weight < 1 || member.weight > 65535) return `成员 ${member.address}:${member.port} 的权重必须在 1–65535 之间`
    if (member.monitor) {
      const monitor = member.monitor
      if (!Number.isInteger(monitor.connect_port) || Number(monitor.connect_port) < 1 || Number(monitor.connect_port) > 65535) return `成员 ${member.address}:${member.port} 的检查端口无效`
      if (!Number.isInteger(monitor.connect_timeout) || monitor.connect_timeout < 1 || monitor.connect_timeout > 300) return `成员 ${member.address}:${member.port} 的检查超时必须在 1–300 秒之间`
      if (!Number.isInteger(monitor.retries) || monitor.retries < 1 || monitor.retries > 20) return `成员 ${member.address}:${member.port} 的重试次数必须在 1–20 之间`
      if (!Number.isInteger(monitor.delay_before_retry) || monitor.delay_before_retry < 1 || monitor.delay_before_retry > 300) return `成员 ${member.address}:${member.port} 的重试间隔必须在 1–300 秒之间`
    }
    const key = `${member.address}|${member.port}`
    if (memberKeys.has(key)) return `成员 ${member.address}:${member.port} 重复`
    memberKeys.add(key)
  }
  return ''
}

function intentForDraft(current: LvsDraftState): LvsIntent {
  if (current.mode === 'delete') {
    return {
      kind: 'delete_service',
      target: current.baseService!.listener,
      change_note: current.changeNote.trim() || undefined,
    }
  }
  return {
    kind: 'upsert_service',
    target: current.baseService?.listener || current.service.listener,
    service: canonicalLvsService(current.service),
    change_note: current.changeNote.trim() || undefined,
  }
}

async function previewDraft() {
  if (!draft.value) return
  const validation = draftValidationError()
  if (validation) {
    store.notify('草稿还不能预览', 'warning', validation)
    return
  }
  planning.value = true
  try {
    const intent = intentForDraft(draft.value)
    const plan = await api.createLvsPlan(
      draft.value.nodeIds,
      intent,
      draft.value.mode === 'takeover',
    )
    planned.value = plan
    plannedIntent.value = intent
    planRequestId.value = requestId()
    planConfirmed.value = false
    const localDiff = buildLvsSemanticDiff(
      draft.value.baseService,
      intent.kind === 'upsert_service' ? intent.service : null,
    )
    semanticDiff.value = lvsPlanSemanticDiff(
      plan.diff,
      localDiff,
      Object.fromEntries(store.nodes.map((node) => [node.id, node.node_name])),
    )
    editorOpen.value = false
    planOpen.value = true
  } catch (error) {
    store.notify('无法生成 LVS 变更计划', 'danger', lvsApiMessage(error))
  } finally {
    planning.value = false
  }
}

function requestId() {
  if (typeof globalThis.crypto?.randomUUID === 'function') return `lvs-${globalThis.crypto.randomUUID()}`
  return `lvs-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`
}

function planIsExpired() {
  if (!planned.value?.expires_at) return false
  const expiresAt = Date.parse(planned.value.expires_at)
  return Number.isFinite(expiresAt) && expiresAt <= Date.now()
}

function lvsApiMessage(error: unknown) {
  if (!(error instanceof ApiError)) return store.apiMessage(error)
  const detail = error.body.detail
  const raw = typeof detail === 'string'
    ? detail
    : detail && typeof detail === 'object'
      ? String((detail as Record<string, unknown>).message || '')
      : ''
  const known: Record<string, string> = {
    'LVS plan expired': '变更计划已过期，请返回草稿重新生成计划。',
    'LVS plan digest mismatch': '计划摘要不一致，已阻止发布；请重新生成计划。',
    'LVS plan was already consumed': '该计划已被其他请求使用，请刷新执行记录。',
    'LVS configuration changed after planning': 'Director 配置在预览后发生了变化，已阻止过期计划发布。',
    'LVS HA group changed after planning': 'Keepalived 主备角色或 VIP 归属已变化，已中止发布；请刷新后重新预览。',
    'selected LVS nodes are not in the same Keepalived VIP/VRID group': '所选 Director 不属于同一个 Keepalived VIP/VRID 组。',
    'LVS nodes must report a complete Keepalived configuration summary': 'Director 尚未上报完整 Keepalived 配置摘要，请先升级 Agent 并刷新状态。',
    'all discovered Keepalived group members must be selected': '必须选择该 VRRP 组中所有已登记的 Director，不能只改其中一台。',
    'Keepalived unicast peers are not represented by discovered Agents': 'Keepalived 配置中仍有未接入平台的 unicast peer，请先安装并批准对应 Agent。',
    'registered Agent membership in the Keepalived group is ambiguous': '同一 VIP 下存在摘要不完整或归属不明确的 Agent，请先修正节点状态。',
    'Keepalived unicast peer identity is shared by multiple Agents': '多个 Agent 使用了同一个 HA 地址标签，请修正 --node-ip 后再发布。',
    'standalone LVS plans require exactly one node': '单 Director 拓扑一次只能选择一个节点。',
    'standalone LVS target address must be local to the selected node': 'Virtual Service 地址不属于该单 Director 的本机地址；请先配置本机地址或改用 VRRP。',
    'standalone LVS topology cannot manage a registered VRRP VIP': '该地址属于已登记的 VRRP 组，不能按单 Director 发布。',
    'selected LVS nodes use mixed deployment topologies': '单 Director 与 VRRP Director 不能混合发布。',
    'standalone LVS capability is required': '请先升级 Agent；当前节点尚不支持单 Director 安全发布。',
    'explicit LVS takeover acknowledgement is required': '该 Virtual Service 仍属于现有配置，请使用“接管”流程迁移到平台托管文件。',
    'LVS nodes have semantic configuration drift': 'Director 之间存在非目标配置漂移，需先处理漂移后再发布。',
  }
  return known[raw] || store.apiMessage(error)
}

const terminalJobStates = new Set(['succeeded', 'failed', 'expired', 'cancelled'])

const plannedHasChanges = computed(() => {
  if (!planned.value) return false
  const diff = planned.value.diff
  return Boolean(diff && typeof diff === 'object' && (diff as Record<string, unknown>).changed === true)
})

function failureRows(jobs: JobRecord[]) {
  const knownFailures: Record<string, string> = {
    lvs_takeover_required: '该服务尚未完成显式接管，Agent 已拒绝覆盖现有配置。',
    concurrent_change: '主备角色、VIP 归属或配置内容在发布期间发生变化，Agent 已停止覆盖。',
    ipvs_reconcile_timeout: '配置已执行但 IPVS 未在期限内收敛，Agent 已尝试恢复原配置。',
    keepalived_config_test_failed: 'Keepalived 配置校验未通过，未继续发布。',
    keepalived_reload_failed: 'Keepalived reload 失败，Agent 已尝试恢复原配置。',
  }
  return [...jobs]
    .sort((left, right) => (left.sequence_no ?? 0) - (right.sequence_no ?? 0))
    .filter((job) => ['failed', 'expired', 'cancelled'].includes(job.status)).map((job) => {
    const result = job.result || {}
    const failureCode = String(result.failure_code || '')
    const rollbackStatus = String(result.rollback_status || '')
    const rollback = rollbackStatus === 'restored'
      ? '已回滚'
      : rollbackStatus === 'unverified'
        ? '回滚未验证'
        : result.rolled_back === true
          ? '已回滚'
          : result.rolled_back === false
            ? '未回滚'
            : '回滚状态未知'
    return {
      node: job.node_name || store.nodes.find((node) => node.id === job.node_id)?.node_name || job.node_id,
      stage: String(result.failure_stage || result.stage || (job.status === 'expired' ? '任务过期' : '执行')),
      rollback,
      message: failureCode === 'dependency_failed'
        ? '前序 Director 发布失败，本节点未执行'
        : knownFailures[failureCode]
          || String(result.message || result.error || result.failure_reason || failureCode || job.status),
    }
  })
}

async function settleOperation(operationId: string, initialJobs: JobRecord[]) {
  let jobs = initialJobs
  for (let attempt = 0; attempt < 60 && !jobs.every((job) => terminalJobStates.has(job.status)); attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000))
    try {
      jobs = (await api.operation(operationId)).jobs
    } catch {
      publishResult.value = {
        tone: 'warning',
        title: '变更已提交，正在后台执行',
        message: `操作 ${operationId} 的实时状态暂时无法读取，请在执行记录中继续跟踪。`,
        failures: [],
      }
      return
    }
  }
  const failures = failureRows(jobs)
  if (failures.length) {
    publishResult.value = {
      tone: 'danger',
      title: 'LVS 发布未完成',
      message: `${failures.length} 个 Director 执行失败；请根据阶段与回滚状态处理。`,
      failures,
    }
    return
  }
  if (jobs.length && jobs.every((job) => job.status === 'succeeded')) {
    publishResult.value = {
      tone: 'success',
      title: 'LVS 变更已安全发布',
      message: `${jobs.length} 个 Director 已完成校验、原子写入与运行态确认。`,
      failures: [],
    }
    await store.refresh(false, true)
    return
  }
  publishResult.value = {
    tone: 'warning',
    title: 'LVS 变更仍在执行',
    message: `操作 ${operationId} 尚未结束，请在执行记录中继续跟踪。`,
    failures: [],
  }
}

async function applyPlan() {
  if (!planned.value) return
  if (planIsExpired()) {
    store.notify('变更计划已过期', 'warning', '请返回草稿并重新生成控制端计划。')
    return
  }
  if (!planRequestId.value) planRequestId.value = requestId()
  applying.value = true
  try {
    const result = await api.applyLvsPlan(planned.value.id, planned.value.plan_digest, planRequestId.value)
    planOpen.value = false
    editorOpen.value = false
    if (result.no_changes === true || result.operation === null) {
      applying.value = false
      publishResult.value = {
        tone: 'success',
        title: '配置无需变更',
        message: '当前 Director 已与计划一致，没有创建发布任务。',
        failures: [],
      }
      return
    }
    publishResult.value = {
      tone: 'info',
      title: 'LVS 变更已提交',
      message: 'Agent 正在逐节点执行；运行健康仍以 IPVS 实时观测为准。',
      failures: [],
    }
    void settleOperation(result.operation.id, result.jobs).finally(() => { applying.value = false })
  } catch (error) {
    applying.value = false
    store.notify('LVS 计划发布失败', 'danger', lvsApiMessage(error))
  }
}

function draftTitle() {
  const labels: Record<DraftMode, string> = {
    create: '新增 Virtual Service', edit: '编辑 Virtual Service',
    takeover: '接管现有 Virtual Service', delete: '删除 Virtual Service',
  }
  return draft.value ? labels[draft.value.mode] : 'LVS 草稿'
}

function selectedNodeNames() {
  const ids = draft.value?.nodeIds || []
  return ids.map((id) => store.nodes.find((node) => node.id === id)?.node_name || id)
}

async function refresh() {
  refreshing.value = true
  try {
    await store.refresh(false, true)
  } catch (error) {
    store.notify('LVS 状态刷新失败', 'danger', store.apiMessage(error))
  } finally {
    refreshing.value = false
  }
}
</script>

<template>
  <section class="page page-lvs">
    <PageHeader
      eyebrow="LAYER 4 DELIVERY"
      title="LVS"
      description="用 F5 风格对象管理 Keepalived Virtual Service，并与宿主机 IPVS 运行态分开核对。"
    >
      <StatusTag
        :label="manageableNodes.length ? `${manageableNodes.length} 个 Director 可管理` : '仅运行态观测'"
        :tone="manageableNodes.length ? 'success' : 'info'"
      />
      <NButton secondary :loading="refreshing" @click="refresh">
        <template #icon><RefreshCw :size="16" /></template>
        刷新状态
      </NButton>
      <NButton
        type="primary"
        :disabled="!store.canOperate || !onlineManageableNodes.length"
        title="只有启用 LVS 管理且在线的 Director 可以接收配置"
        @click="openCreate"
      >
        <template #icon><Plus :size="16" /></template>
        新增 Virtual Service
      </NButton>
    </PageHeader>

    <div class="lvs-scope-note">
      <ShieldCheck :size="17" />
      <div>
        <strong>配置管理与运行健康是两条独立证据链。</strong>
        <span>所有变更先保存为浏览器草稿，再由控制端生成语义计划；确认摘要后才会逐节点校验、原子发布并验证 IPVS。</span>
      </div>
    </div>

    <div v-if="publishResult" class="lvs-result-strip" :class="`tone-${publishResult.tone}`" role="status">
      <CheckCircle2 v-if="publishResult.tone === 'success'" :size="18" />
      <AlertTriangle v-else :size="18" />
      <div>
        <strong>{{ publishResult.title }}</strong>
        <span>{{ publishResult.message }}</span>
        <ul v-if="publishResult.failures.length">
          <li v-for="failure in publishResult.failures" :key="`${failure.node}-${failure.stage}`">
            <b>{{ failure.node }}</b> · 阶段 {{ failure.stage }} · {{ failure.rollback }} · {{ failure.message }}
          </li>
        </ul>
      </div>
    </div>

    <section class="lvs-kpi-rail" aria-label="LVS 概览">
      <article>
        <span><Waypoints :size="16" /> Virtual Services</span>
        <strong>{{ overview.virtualServiceCount }}</strong>
        <small>{{ overview.availableNodes.length }} 个 Director 已观测</small>
      </article>
      <article>
        <span><Boxes :size="16" /> Backend Pools</span>
        <strong>{{ overview.poolCount }}</strong>
        <small>每个服务派生一个后端池</small>
      </article>
      <article>
        <span><Server :size="16" /> Pool Members</span>
        <strong>{{ overview.memberCount }}</strong>
        <small v-if="overview.disabledMemberCount">{{ overview.disabledMemberCount }} 个权重为 0</small>
        <small v-else>存在不等于健康</small>
      </article>
      <article :class="{ attention: overview.driftCount }">
        <span><AlertTriangle :size="16" /> Director 对账</span>
        <strong>{{ overview.driftCount }}</strong>
        <small>{{ overview.driftCount ? '存在配置漂移' : '已观测规则一致' }}</small>
      </article>
      <article>
        <span><Activity :size="16" /> 活跃连接</span>
        <strong>{{ formatNumber(overview.activeConnections) }}</strong>
        <small>{{ formatNumber(overview.connectionsPerSecond) }} CPS · Director 合计</small>
      </article>
    </section>

    <div v-if="!overview.capableNodes.length" class="lvs-empty-state">
      <div class="empty-icon"><Route :size="30" /></div>
      <h2>还没有启用 LVS 观察器</h2>
      <p>在需要观测的 Director 上重新运行 Agent 安装命令，并添加 <code>--enable-lvs-observer</code>。</p>
      <span>观察器只读取宿主机命名空间的 /proc/net/ip_vs，不会加载模块或改变规则。</span>
    </div>

    <template v-else>
      <div v-if="overview.unavailableNodes.length" class="lvs-warning-strip">
        <AlertTriangle :size="17" />
        <div>
          <strong>{{ overview.unavailableNodes.length }} 个 Director 暂无 IPVS 数据</strong>
          <span v-for="node in overview.unavailableNodes" :key="node.id">
            {{ node.node_name }}：{{ unavailableLabel(lvsObservation(node)?.reason) }}
          </span>
        </div>
      </div>

      <div v-if="overview.availableNodes.length && !overview.groups.length" class="lvs-empty-state compact">
        <div class="empty-icon"><Network :size="28" /></div>
        <h2>IPVS 已就绪，但没有 Virtual Service</h2>
        <p>Agent 已成功读取内核状态；当前转发表为空，这不是故障。</p>
      </div>

      <div v-else-if="overview.groups.length" class="lvs-workbench">
        <section class="lvs-browser" aria-label="LVS 对象浏览器">
          <div class="lvs-browser-tabs" role="tablist" aria-label="LVS 对象类型">
            <button :class="{ active: objectMode === 'services' }" @click="objectMode = 'services'">
              Virtual Services <span>{{ overview.virtualServiceCount }}</span>
            </button>
            <button :class="{ active: objectMode === 'pools' }" @click="objectMode = 'pools'">
              Backend Pools <span>{{ overview.poolCount }}</span>
            </button>
            <button :class="{ active: objectMode === 'members' }" @click="objectMode = 'members'">
              Pool Members <span>{{ overview.memberCount }}</span>
            </button>
          </div>
          <div class="lvs-toolbar">
            <NInput v-model:value="query" clearable placeholder="搜索 VIP、端口、协议、Member IP 或 Director">
              <template #prefix><Waypoints :size="16" /></template>
            </NInput>
            <NSelect v-model:value="directorId" :options="directorOptions" />
          </div>

          <div class="lvs-table" role="table" aria-label="LVS 对象列表">
            <div class="lvs-table-head" role="row">
              <span>对象</span><span>关联对象</span><span>调度 / 转发</span><span>Director</span><span>连接</span><span>管理 / 运行</span>
            </div>
            <button
              v-for="row in objectRows"
              :key="row.id"
              class="lvs-table-row"
              :class="{ selected: row.group.key === selectedKey }"
              role="row"
              @click="selectedKey = row.group.key"
            >
              <span class="object-cell">
                <strong>{{ row.title }}</strong>
                <small>{{ row.subtitle }}</small>
              </span>
              <span><strong>{{ row.relation }}</strong></span>
              <span><code>{{ row.method }}</code></span>
              <span class="director-chips">
                <i v-for="name in row.directors.slice(0, 2)" :key="name"><CircleDot :size="10" /> {{ name }}</i>
                <i v-if="row.directors.length > 2">+{{ row.directors.length - 2 }}</i>
              </span>
              <span class="connection-cell"><strong>{{ formatNumber(row.active) }}</strong><small>active · {{ formatNumber(row.inactive) }} inactive</small></span>
              <span class="state-stack">
                <StatusTag :label="managementMeta(row.group).label" :tone="managementMeta(row.group).tone" />
                <StatusTag v-bind="stateMeta(row.state)" />
              </span>
            </button>
            <div v-if="!objectRows.length" class="lvs-no-results">没有匹配当前搜索和 Director 条件的对象。</div>
          </div>
        </section>

        <aside v-if="selectedGroup" class="lvs-detail" aria-label="Virtual Service 详情">
          <header class="lvs-detail-header">
            <div>
              <span class="detail-eyebrow">SELECTED VIRTUAL SERVICE</span>
              <h2>{{ selectedGroup.label }}</h2>
              <p>{{ selectedGroup.protocol }} · {{ selectedGroup.scheduler }} scheduler</p>
            </div>
            <div class="detail-statuses">
              <StatusTag :label="selectedManagement.label" :tone="selectedManagement.tone" />
              <StatusTag
                v-bind="stateMeta(selectedGroup.drift ? 'drift' : selectedGroup.partial ? 'partial' : 'observed')"
              />
            </div>
          </header>

          <div v-if="!selectedManagement.managed" class="takeover-banner">
            <LockKeyhole :size="17" />
            <div>
              <strong>当前服务来自外部配置，平台保持只读</strong>
              <span v-if="selectedHasUnsupportedConfig">配置包含平台无法无损表达的指令；平台不会改写或删除该服务。</span>
              <span v-else-if="selectedHasExistingConfig">检测到 Keepalived 现有配置。接管会先展示权威差异，再把该 virtual_server 迁入平台托管文件。</span>
              <span v-else>当前只有 IPVS 运行态证据，没有可安全迁移的源配置，因此保持只读。</span>
            </div>
            <NButton
              secondary
              :disabled="!store.isAdmin || !canTakeOverSelected"
              :title="!store.isAdmin ? '接管需要管理员权限' : canTakeOverSelected ? '预览显式接管计划' : '需所有 Director 在线、配置一致且存在可迁移的 Keepalived 配置块'"
              @click="openTakeover"
            >
              <template #icon><Import :size="15" /></template>
              {{ canTakeOverSelected ? '导入 / 接管' : '接管暂不可用' }}
            </NButton>
          </div>

          <div v-else class="lvs-actions">
            <NButton secondary :disabled="!store.canOperate" @click="openEdit">
              <template #icon><Pencil :size="15" /></template>
              编辑算法与成员
            </NButton>
            <NButton tertiary type="error" :disabled="!store.isAdmin" title="删除服务需要管理员权限" @click="openDelete">
              <template #icon><Trash2 :size="15" /></template>
              删除服务
            </NButton>
          </div>

          <div class="lvs-detail-facts">
            <div><span>Virtual Service</span><strong>{{ selectedGroup.label }}</strong></div>
            <div><span>Scheduler</span><strong>{{ selectedGroup.scheduler }}</strong></div>
            <div><span>部署拓扑</span><strong>{{ selectedGroup.snapshots.every((snapshot) => lvsTopologyForNode(snapshot.node) === 'standalone') ? '单 Director' : 'VRRP Director 组' }}</strong></div>
            <div><span>Persistence</span><strong>{{ selectedGroup.persistenceSeconds === null ? '未启用' : `${selectedGroup.persistenceSeconds}s` }}</strong></div>
            <div><span>管理状态</span><strong>{{ selectedManagement.label }}</strong></div>
            <div><span>运行状态</span><strong>{{ selectedGroup.partial ? '观测不完整' : 'IPVS 规则已观测' }}</strong></div>
            <div><span>Director 对账</span><strong>{{ selectedGroup.drift ? '配置漂移' : '规则一致' }}</strong></div>
            <div><span>故障接管</span><strong>{{ selectedGroup.snapshots.every((snapshot) => lvsTopologyForNode(snapshot.node) === 'standalone') ? '无主备接管' : '由 Keepalived 提供' }}</strong></div>
          </div>

          <section class="lvs-topology">
            <div class="section-title"><Network :size="16" /><strong>实时流量路径</strong><span>Observed State</span></div>
            <div class="topology-flow">
              <div class="topology-node client"><span>CLIENT</span><strong>客户端流量</strong></div>
              <ArrowRight class="flow-arrow" :size="20" />
              <div class="topology-node virtual"><span>VIRTUAL SERVICE</span><strong>{{ selectedGroup.label }}</strong><small>{{ selectedGroup.protocol }} · {{ selectedGroup.scheduler }}</small></div>
              <ArrowRight class="flow-arrow" :size="20" />
              <div class="topology-node pool"><span>DERIVED POOL</span><strong>{{ selectedGroup.memberCount }} Members</strong></div>
            </div>
            <div class="topology-members">
              <span v-for="member in detailMembers.slice(0, 8)" :key="member.key" :class="{ disabled: member.weights.every((weight) => weight === 0) }">
                <CircleDot :size="11" /> {{ formatEndpoint(member.member) }}
              </span>
              <span v-if="detailMembers.length > 8">+{{ detailMembers.length - 8 }} Members</span>
            </div>
          </section>

          <section class="lvs-detail-section">
            <div class="section-title"><Server :size="16" /><strong>Director 快照</strong><span>{{ selectedGroup.snapshots.length }} 份</span></div>
            <div class="snapshot-tabs">
              <button :class="{ active: detailDirectorId === 'all' }" @click="detailDirectorId = 'all'">对比全部</button>
              <button
                v-for="snapshot in selectedGroup.snapshots"
                :key="snapshot.node.id"
                :class="{ active: detailDirectorId === snapshot.node.id }"
                @click="detailDirectorId = snapshot.node.id"
              >{{ snapshot.node.node_name }}</button>
            </div>
            <div class="snapshot-grid">
              <article v-for="snapshot in detailSnapshots" :key="snapshot.node.id">
                <header><strong>{{ snapshot.node.node_name }}</strong><StatusTag :label="snapshot.node.status === 'offline' ? '数据已过期' : '在线'" :tone="snapshot.node.status === 'offline' ? 'warning' : 'success'" /></header>
                <dl>
                  <div><dt>Scheduler</dt><dd>{{ snapshot.service.scheduler }}</dd></div>
                  <div><dt>Members</dt><dd>{{ snapshot.service.destinations.length }}</dd></div>
                  <div><dt>Active</dt><dd>{{ formatNumber(snapshot.service.active_connections) }}</dd></div>
                  <div><dt>采样</dt><dd>{{ snapshot.node.last_seen_at || '—' }}</dd></div>
                </dl>
              </article>
            </div>
          </section>

          <section class="lvs-detail-section">
            <div class="section-title"><Boxes :size="16" /><strong>Pool Members</strong><span>健康状态需由外部 Monitor 证明</span></div>
            <div class="member-list">
              <article v-for="row in detailMembers" :key="row.key">
                <div class="member-identity"><CircleDot :size="13" /><strong>{{ formatEndpoint(row.member) }}</strong><small>Real Server</small></div>
                <div><span>Forward</span><strong>{{ [...new Set(row.forwarding)].map(forwardingLabel).join(' / ') }}</strong></div>
                <div><span>Weight</span><strong>{{ [...new Set(row.weights)].join(' / ') }}</strong></div>
                <div><span>Connections</span><strong>{{ formatNumber(row.activeConnections) }} / {{ formatNumber(row.inactiveConnections) }}</strong></div>
                <StatusTag :label="row.weights.every((weight) => weight === 0) ? '已停用' : '可调度'" :tone="row.weights.every((weight) => weight === 0) ? 'warning' : 'info'" />
              </article>
              <div v-if="!detailMembers.length" class="member-empty">该 Virtual Service 暂无 Pool Member。</div>
            </div>
          </section>
        </aside>
      </div>
    </template>

    <NModal
      v-model:show="editorOpen"
      preset="card"
      class="lvs-editor-modal"
      :title="draftTitle()"
      :bordered="false"
      :mask-closable="false"
    >
      <template v-if="draft">
        <div class="draft-ribbon">
          <FileDiff :size="18" />
          <div>
            <strong>当前仅为浏览器草稿，不会直接改变 Director</strong>
            <span>下一步先由控制端生成结构化计划；只有二次确认后才会发布。</span>
          </div>
          <StatusTag label="未发布草稿" tone="warning" />
        </div>

        <div v-if="draft.mode === 'delete'" class="delete-draft-warning">
          <AlertTriangle :size="22" />
          <div>
            <strong>准备删除 {{ draft.baseService?.listener.address }}:{{ draft.baseService?.listener.port }}</strong>
            <span>该操作只删除平台托管的 Virtual Service；生成计划后仍需再次确认。</span>
          </div>
        </div>

        <div class="lvs-editor-grid" :class="{ deleting: draft.mode === 'delete' }">
          <section class="draft-section target-section">
            <header><span>01</span><div><strong>{{ draft.mode === 'create' ? '发布范围' : '已锁定部署范围' }}</strong><small>Director Targets</small></div></header>
            <div v-if="draft.mode === 'create'" class="draft-node-list">
              <label
                v-for="node in onlineManageableNodes"
                :key="node.id"
                :class="{ selected: draft.nodeIds.includes(node.id) }"
              >
                <NCheckbox
                  :checked="draft.nodeIds.includes(node.id)"
                  @update:checked="(checked) => toggleDraftNode(node.id, checked)"
                />
                <span><strong>{{ node.node_name }}</strong><small>{{ node.hostname }} · {{ lvsTopologyForNode(node) === 'standalone' ? '无主备接管能力' : 'Keepalived 主备组' }}</small></span>
                <StatusTag :label="lvsTopologyLabel(node)" :tone="lvsTopologyForNode(node) === 'standalone' ? 'warning' : 'success'" />
              </label>
            </div>
            <div v-else class="draft-locked-targets">
              <div v-for="node in draftTargetNodes" :key="node.id">
                <LockKeyhole :size="15" />
                <span><strong>{{ node.node_name }}</strong><small>{{ node.hostname }} · 在线</small></span>
                <StatusTag label="已锁定" tone="neutral" />
              </div>
            </div>
            <p v-if="draft.mode !== 'create'" class="locked-hint">
              编辑不会改变部署范围；目标节点来自该服务当前的托管记录。
            </p>
            <p v-else-if="!draft.nodeIds.length" class="target-selection-warning">
              默认不选中 Director，请明确选择发布范围。
            </p>
            <p v-else-if="draftTargetNodes.some((node) => lvsTopologyForNode(node) === 'standalone')" class="target-selection-warning standalone">
              单 Director 发布没有 VRRP 主备接管能力，只会修改当前选中的一台节点。
            </p>
            <label class="field-block">
              <span>变更说明</span>
              <NInput
                v-model:value="draft.changeNote"
                type="textarea"
                :autosize="{ minRows: 3, maxRows: 5 }"
                maxlength="500"
                show-count
                placeholder="说明本次调整原因，便于执行记录审计"
              />
            </label>
          </section>

          <template v-if="draft.mode !== 'delete'">
            <section class="draft-section service-section">
              <header><span>02</span><div><strong>Virtual Service</strong><small>Listener & Policy</small></div></header>
              <div class="service-form-grid">
                <label class="field-block wide">
                  <span>服务名称</span>
                  <NInput v-model:value="draft.service.name" placeholder="例如 web-production-443" />
                </label>
                <label class="field-block wide">
                  <span>Virtual Service IP</span>
                  <NInput
                    v-model:value="draft.service.listener.address"
                    :disabled="draft.mode !== 'create'"
                    placeholder="192.0.2.110"
                  />
                </label>
                <label class="field-block">
                  <span>端口</span>
                  <NInputNumber
                    v-model:value="draft.service.listener.port"
                    :disabled="draft.mode !== 'create'"
                    :min="1"
                    :max="65535"
                  />
                </label>
                <label class="field-block">
                  <span>协议</span>
                  <NSelect
                    v-model:value="draft.service.listener.protocol"
                    :disabled="draft.mode !== 'create'"
                    :options="protocolOptions"
                  />
                </label>
                <label class="field-block wide">
                  <span>调度算法</span>
                  <NSelect v-model:value="draft.service.scheduler" :options="schedulerOptions" />
                </label>
                <label class="field-block wide">
                  <span>转发模式</span>
                  <NSelect v-model:value="draft.service.forwarding" :options="forwardingOptions" />
                </label>
                <label class="field-block">
                  <span>检查周期（秒）</span>
                  <NInputNumber v-model:value="draft.service.delay_loop" :min="1" :max="3600" />
                </label>
                <label class="field-block">
                  <span>会话保持（秒）</span>
                  <NInputNumber
                    v-model:value="draft.service.persistence_seconds"
                    clearable
                    placeholder="留空关闭"
                    :min="1"
                    :max="86400"
                  />
                </label>
              </div>
              <p v-if="draft.mode !== 'create'" class="locked-hint">
                为避免误建重复监听，已托管服务的 IP、端口和协议不可在编辑流程中修改。
              </p>
            </section>

            <section class="draft-section members-section">
              <header>
                <span>03</span>
                <div><strong>Pool Members</strong><small>Real Servers</small></div>
                <NButton secondary size="small" @click="addMember">
                  <template #icon><Plus :size="14" /></template>
                  新增成员
                </NButton>
              </header>
              <div class="member-editor-head">
                <span>状态</span><span>成员 IP</span><span>端口</span><span>权重</span><span>健康检查</span><span></span>
              </div>
              <div class="member-editor-list">
                <div v-for="(member, index) in draft.service.members" :key="index" class="member-editor-item">
                  <div class="member-editor-row">
                    <NSwitch v-model:value="member.enabled" aria-label="启用或停用成员">
                      <template #checked>启用</template><template #unchecked>停用</template>
                    </NSwitch>
                    <NInput v-model:value="member.address" placeholder="192.0.2.108" />
                    <NInputNumber v-model:value="member.port" :min="1" :max="65535" />
                    <NInputNumber v-model:value="member.weight" :min="1" :max="65535" />
                    <NCheckbox
                      :checked="Boolean(member.monitor)"
                      @update:checked="toggleMemberMonitor(member, $event)"
                    >TCP</NCheckbox>
                    <NButton quaternary type="error" title="删除成员" @click="removeMember(index)">
                      <template #icon><Trash2 :size="15" /></template>
                    </NButton>
                  </div>
                  <div v-if="member.monitor" class="member-monitor-row">
                    <label><span>检查端口</span><NInputNumber v-model:value="member.monitor.connect_port" :min="1" :max="65535" /></label>
                    <label><span>连接超时（秒）</span><NInputNumber v-model:value="member.monitor.connect_timeout" :min="1" :max="300" /></label>
                    <label><span>重试次数</span><NInputNumber v-model:value="member.monitor.retries" :min="1" :max="20" /></label>
                    <label><span>重试间隔（秒）</span><NInputNumber v-model:value="member.monitor.delay_before_retry" :min="1" :max="300" /></label>
                  </div>
                </div>
                <div v-if="!draft.service.members.length" class="member-editor-empty">
                  暂无成员。至少添加一个成员且保留一个启用状态，才能生成发布计划。
                </div>
              </div>
              <p class="member-editor-note">停用成员会保留配置身份并将运行权重置为 0；不会把“存在”误报为“健康”。</p>
            </section>
          </template>
        </div>
      </template>

      <template #footer>
        <div class="modal-footer">
          <NButton :disabled="planning" @click="editorOpen = false">取消</NButton>
          <NButton type="primary" :loading="planning" @click="previewDraft">
            <template #icon><FileDiff :size="16" /></template>
            {{ draft?.mode === 'delete' ? '预览删除计划' : '预览变更计划' }}
          </NButton>
        </div>
      </template>
    </NModal>

    <NModal
      v-model:show="planOpen"
      preset="card"
      class="lvs-plan-modal"
      title="确认 LVS 变更计划"
      :bordered="false"
      :mask-closable="false"
      :closable="!applying"
    >
      <template v-if="planned && plannedIntent">
        <div class="plan-identity">
          <ShieldCheck :size="22" />
          <div>
            <strong>计划已由控制端校验并锁定摘要</strong>
            <code>{{ planned.plan_digest }}</code>
          </div>
          <StatusTag :label="plannedIntent.kind === 'delete_service' ? '删除服务' : '新增 / 更新服务'" :tone="plannedIntent.kind === 'delete_service' ? 'danger' : 'success'" />
        </div>

        <section class="plan-targets">
          <span>目标 Director</span>
          <div><i v-for="name in selectedNodeNames()" :key="name"><CircleDot :size="10" /> {{ name }}</i></div>
          <small v-if="planned.expires_at">计划有效期至 {{ planned.expires_at }}</small>
          <small v-if="planned.expected_config_hashes">已锁定 {{ Object.keys(planned.expected_config_hashes).length }} 份 Director 配置摘要</small>
        </section>

        <section class="semantic-diff">
          <header><FileDiff :size="17" /><strong>控制端权威语义差异</strong><span>{{ semanticDiff.length }} 项</span></header>
          <article v-for="(line, index) in semanticDiff" :key="`${line.subject}-${index}`" :class="`diff-${line.kind}`">
            <b>{{ line.kind === 'add' ? '+' : line.kind === 'remove' ? '−' : '↻' }}</b>
            <strong>{{ line.subject }}</strong>
            <span v-if="line.before"><del>{{ line.before }}</del></span>
            <ArrowRight v-if="line.before && line.after" :size="14" />
            <span v-if="line.after"><ins>{{ line.after }}</ins></span>
          </article>
          <div v-if="!semanticDiff.length && plannedHasChanges" class="no-semantic-diff">业务语义未变化；本计划仅迁移配置所有权。</div>
          <div v-else-if="!semanticDiff.length" class="no-semantic-diff">没有检测到可发布的语义变化。</div>
        </section>

        <section v-if="planIsExpired()" class="plan-expired" role="alert">
          <AlertTriangle :size="17" />
          <div><strong>计划已过期</strong><span>Director 状态可能已变化，请返回草稿重新生成计划。</span></div>
        </section>

        <section v-if="planned.warnings.length" class="plan-warnings">
          <AlertTriangle :size="17" />
          <div><strong>发布前警告</strong><span v-for="warning in planned.warnings" :key="warning">{{ warning }}</span></div>
        </section>

        <label class="plan-confirmation">
          <NCheckbox v-model:checked="planConfirmed" />
          <span>我已核对目标 Director、监听地址和语义差异，确认发布此计划。</span>
        </label>
      </template>

      <template #footer>
        <div class="modal-footer">
          <NButton
            :disabled="applying"
            @click="planOpen = false; editorOpen = true"
          >返回草稿</NButton>
          <NButton
            :type="plannedIntent?.kind === 'delete_service' ? 'error' : 'primary'"
            :loading="applying"
            :disabled="!planConfirmed || !plannedHasChanges || planIsExpired()"
            @click="applyPlan"
          >
            <template #icon><ShieldCheck :size="16" /></template>
            二次确认并发布
          </NButton>
        </div>
      </template>
    </NModal>
  </section>
</template>

<style scoped>
.page-lvs { --lvs-accent: var(--green); }
.lvs-scope-note,.lvs-warning-strip { display:flex; align-items:flex-start; gap:12px; margin-bottom:14px; padding:12px 14px; border:1px solid var(--line); background:var(--surface-soft); color:var(--text-2); }
.lvs-scope-note svg { color:var(--green); }
.lvs-scope-note div,.lvs-warning-strip div { display:flex; flex-wrap:wrap; gap:4px 12px; }
.lvs-scope-note strong,.lvs-warning-strip strong { color:var(--text); }
.lvs-scope-note span,.lvs-warning-strip span { font-size:12px; }
.lvs-warning-strip { border-color:#e2c98c; background:var(--amber-soft); }
.lvs-warning-strip svg { color:var(--amber); }
.lvs-result-strip { display:flex; align-items:flex-start; gap:11px; margin-bottom:14px; padding:13px 15px; border:1px solid var(--line); background:#fff; }
.lvs-result-strip > div { display:grid; gap:2px; }
.lvs-result-strip span { color:var(--text-2); font-size:12px; }
.lvs-result-strip ul { display:grid; gap:4px; margin:7px 0 0; padding:0; list-style:none; }
.lvs-result-strip li { padding:7px 9px; border-left:3px solid var(--red); background:var(--red-soft); color:var(--text-2); font-size:11px; }
.lvs-result-strip.tone-success { border-color:#a9d2bf; background:#f1f8f5; }
.lvs-result-strip.tone-success svg { color:var(--green); }
.lvs-result-strip.tone-warning { border-color:#e2c98c; background:var(--amber-soft); }
.lvs-result-strip.tone-warning svg,.lvs-result-strip.tone-danger svg { color:var(--red); }
.lvs-result-strip.tone-danger { border-color:#e4b3b3; background:var(--red-soft); }
.lvs-kpi-rail { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); margin-bottom:16px; border:1px solid var(--line); }
.lvs-kpi-rail article { min-width:0; padding:15px 17px; border-right:1px solid var(--line); background:#fff; }
.lvs-kpi-rail article:last-child { border-right:0; }
.lvs-kpi-rail article.attention { box-shadow:inset 0 -3px 0 var(--red); }
.lvs-kpi-rail span { display:flex; align-items:center; gap:7px; color:var(--text-3); font-size:11px; font-weight:700; letter-spacing:.04em; text-transform:uppercase; }
.lvs-kpi-rail strong { display:block; margin:5px 0 0; color:var(--text); font-family:var(--font-accent); font-size:28px; line-height:1; }
.lvs-kpi-rail small { color:var(--text-3); font-size:11px; }
.lvs-empty-state { display:grid; min-height:360px; place-items:center; align-content:center; gap:8px; border:1px solid var(--line); background:var(--surface-soft); text-align:center; }
.lvs-empty-state.compact { min-height:240px; }
.lvs-empty-state .empty-icon { display:grid; width:60px; height:60px; place-items:center; border:1px solid var(--line); background:#fff; color:var(--green); }
.lvs-empty-state h2 { font-size:20px; }
.lvs-empty-state p,.lvs-empty-state span { max-width:640px; color:var(--text-2); font-size:13px; }
.lvs-empty-state code { padding:2px 5px; background:#eef2f1; color:var(--green); }
.lvs-workbench { display:grid; grid-template-columns:minmax(680px,1fr) minmax(390px,34%); align-items:start; gap:18px; }
.lvs-browser,.lvs-detail { min-width:0; border:1px solid var(--line); background:#fff; }
.lvs-browser-tabs { display:flex; border-bottom:1px solid var(--line); }
.lvs-browser-tabs button { position:relative; flex:1; min-height:48px; border:0; border-right:1px solid var(--line); background:#fff; color:var(--text-2); cursor:pointer; font-weight:650; }
.lvs-browser-tabs button:last-child { border-right:0; }
.lvs-browser-tabs button.active { color:var(--text); background:var(--surface-soft); }
.lvs-browser-tabs button.active::after { position:absolute; right:18px; bottom:-1px; left:18px; height:3px; background:var(--lime); content:''; box-shadow:0 -1px 0 var(--green); }
.lvs-browser-tabs button span { margin-left:6px; color:var(--text-3); font-size:11px; }
.lvs-toolbar { display:grid; grid-template-columns:minmax(0,1fr) 250px; gap:10px; padding:11px; border-bottom:1px solid var(--line); }
.lvs-table { width:100%; overflow:auto; }
.lvs-table-head,.lvs-table-row { display:grid; grid-template-columns:minmax(210px,1.25fr) minmax(150px,.85fr) minmax(145px,.8fr) minmax(145px,.8fr) 120px 155px; align-items:center; min-width:1040px; }
.lvs-table-head { min-height:38px; padding:0 14px; border-bottom:1px solid var(--line); background:var(--surface-soft); color:var(--text-3); font-size:10px; font-weight:750; letter-spacing:.04em; text-transform:uppercase; }
.lvs-table-row { width:100%; min-height:73px; padding:0 14px; border:0; border-bottom:1px solid var(--line); background:#fff; text-align:left; cursor:pointer; transition:background 140ms ease, box-shadow 140ms ease; }
.lvs-table-row:hover { background:#f8faf9; }
.lvs-table-row.selected { background:#eef6f2; box-shadow:inset 4px 0 0 var(--green); }
.lvs-table-row > span { min-width:0; padding-right:12px; color:var(--text-2); font-size:12px; }
.lvs-table-row strong { display:block; overflow:hidden; color:var(--text); text-overflow:ellipsis; white-space:nowrap; }
.lvs-table-row small { display:block; overflow:hidden; color:var(--text-3); text-overflow:ellipsis; white-space:nowrap; }
.lvs-table-row code { color:var(--text-2); font-size:11px; }
.director-chips { display:flex; flex-wrap:wrap; gap:4px; }
.director-chips i { display:inline-flex; align-items:center; gap:3px; padding:2px 5px; border:1px solid var(--line); color:var(--text-2); font-style:normal; font-size:10px; }
.director-chips svg { color:var(--green); fill:var(--green); }
.connection-cell strong { font-family:var(--font-mono); font-size:13px; }
.state-stack { display:flex; flex-wrap:wrap; gap:4px; }
.lvs-no-results { padding:50px 20px; color:var(--text-3); text-align:center; }
.lvs-detail { position:sticky; top:12px; max-height:calc(100vh - var(--topbar) - 38px); overflow:auto; scrollbar-gutter:stable; }
.lvs-detail-header { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; padding:20px; border-top:5px solid var(--lime); border-bottom:1px solid var(--line); }
.detail-eyebrow { color:var(--green); font-size:9px; font-weight:800; letter-spacing:.14em; }
.lvs-detail-header h2 { margin-top:3px; overflow-wrap:anywhere; font-size:22px; }
.lvs-detail-header p { color:var(--text-3); font-size:12px; }
.detail-statuses { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:5px; }
.takeover-banner { display:grid; grid-template-columns:auto minmax(0,1fr) auto; align-items:center; gap:10px; padding:13px 16px; border-bottom:1px solid var(--line); background:var(--surface-soft); }
.takeover-banner > svg { color:var(--text-3); }
.takeover-banner div { display:grid; gap:2px; }
.takeover-banner span { color:var(--text-2); font-size:10px; line-height:1.4; }
.lvs-actions { display:grid; grid-template-columns:1fr 1fr; gap:8px; padding:12px 16px; border-bottom:1px solid var(--line); }
.lvs-detail-facts { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); border-bottom:1px solid var(--line); }
.lvs-detail-facts div { min-width:0; padding:12px 16px; border-right:1px solid var(--line); border-bottom:1px solid var(--line); }
.lvs-detail-facts div:nth-child(even) { border-right:0; }
.lvs-detail-facts div:nth-last-child(-n+2) { border-bottom:0; }
.lvs-detail-facts span,.member-list span { display:block; color:var(--text-3); font-size:9px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; }
.lvs-detail-facts strong { display:block; overflow:hidden; margin-top:3px; text-overflow:ellipsis; white-space:nowrap; }
.lvs-topology,.lvs-detail-section { padding:17px 16px; border-bottom:1px solid var(--line); }
.section-title { display:flex; align-items:center; gap:7px; margin-bottom:12px; color:var(--text-2); }
.section-title svg { color:var(--green); }
.section-title strong { color:var(--text); }
.section-title span { margin-left:auto; color:var(--text-3); font-size:10px; }
.topology-flow { display:grid; grid-template-columns:minmax(95px,.75fr) 22px minmax(140px,1.2fr) 22px minmax(100px,.8fr); align-items:center; gap:5px; }
.topology-node { display:grid; min-height:68px; align-content:center; padding:9px 10px; border:1px solid var(--line-strong); background:#fff; }
.topology-node.virtual { border-color:var(--green); background:#f0f8f4; }
.topology-node span { color:var(--text-3); font-size:8px; font-weight:800; letter-spacing:.08em; }
.topology-node strong { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.topology-node small { color:var(--text-3); font-size:9px; }
.flow-arrow { color:var(--green); }
.topology-members { display:flex; flex-wrap:wrap; gap:5px; margin-top:10px; padding-left:calc(44% + 24px); }
.topology-members span { display:inline-flex; align-items:center; gap:4px; padding:3px 6px; border:1px solid #bcd5ca; background:#f4faf7; color:#356153; font-size:9px; }
.topology-members span.disabled { border-color:#dfc688; background:var(--amber-soft); color:#8a641e; }
.snapshot-tabs { display:flex; flex-wrap:wrap; gap:5px; margin-bottom:10px; }
.snapshot-tabs button { min-height:30px; padding:0 9px; border:1px solid var(--line); background:#fff; color:var(--text-2); cursor:pointer; font-size:10px; }
.snapshot-tabs button.active { border-color:var(--green); background:#eef6f2; color:var(--green); font-weight:700; }
.snapshot-grid { display:grid; gap:8px; }
.snapshot-grid article { padding:10px; border:1px solid var(--line); }
.snapshot-grid header { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.snapshot-grid dl { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin-top:9px; }
.snapshot-grid dl div { min-width:0; }
.snapshot-grid dt { color:var(--text-3); font-size:9px; text-transform:uppercase; }
.snapshot-grid dd { overflow:hidden; color:var(--text-2); font-size:11px; text-overflow:ellipsis; white-space:nowrap; }
.member-list { display:grid; gap:7px; }
.member-list article { display:grid; grid-template-columns:minmax(160px,1.2fr) .65fr .55fr .8fr auto; align-items:center; gap:9px; padding:9px 10px; border:1px solid var(--line); }
.member-list article > div { min-width:0; }
.member-list strong { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:11px; }
.member-identity { display:grid; grid-template-columns:16px minmax(0,1fr); align-items:center; }
.member-identity svg { grid-row:1 / 3; color:var(--green); fill:var(--green); }
.member-identity small { color:var(--text-3); font-size:9px; }
.member-empty { padding:24px; border:1px dashed var(--line); color:var(--text-3); text-align:center; }
:global(.n-card.lvs-editor-modal) { width:min(1240px,calc(100vw - 40px)); max-height:calc(100vh - 32px); }
:global(.n-card.lvs-plan-modal) { width:min(780px,calc(100vw - 40px)); max-height:calc(100vh - 32px); }
:global(.lvs-editor-modal .n-card__content),:global(.lvs-plan-modal .n-card__content) { overflow:auto; }
.draft-ribbon,.plan-identity { display:flex; align-items:center; gap:11px; margin:-2px 0 16px; padding:12px 14px; border:1px solid #b9d2c6; background:#f0f8f4; }
.draft-ribbon > svg,.plan-identity > svg { flex:0 0 auto; color:var(--green); }
.draft-ribbon > div,.plan-identity > div { display:grid; min-width:0; flex:1; gap:2px; }
.draft-ribbon span { color:var(--text-2); font-size:11px; }
.plan-identity code { overflow:hidden; color:var(--text-3); font-size:10px; text-overflow:ellipsis; white-space:nowrap; }
.delete-draft-warning { display:flex; align-items:flex-start; gap:12px; margin-bottom:14px; padding:15px; border:1px solid #e2a3a3; background:var(--red-soft); }
.delete-draft-warning svg { color:var(--red); }
.delete-draft-warning div { display:grid; gap:3px; }
.delete-draft-warning span { color:var(--text-2); font-size:12px; }
.lvs-editor-grid { display:grid; grid-template-columns:minmax(260px,.72fr) minmax(310px,.9fr) minmax(440px,1.35fr); align-items:start; gap:12px; }
.lvs-editor-grid.deleting { grid-template-columns:1fr; }
.draft-section { min-width:0; border:1px solid var(--line); background:#fff; }
.draft-section > header { display:flex; align-items:center; gap:9px; min-height:54px; padding:9px 12px; border-bottom:1px solid var(--line); background:var(--surface-soft); }
.draft-section > header > span { display:grid; width:28px; height:28px; flex:0 0 auto; place-items:center; background:var(--text); color:var(--lime); font-family:var(--font-mono); font-size:10px; }
.draft-section > header > div { display:grid; }
.draft-section > header small { color:var(--text-3); font-size:9px; letter-spacing:.07em; text-transform:uppercase; }
.draft-section > header .n-button { margin-left:auto; }
.draft-node-list { display:grid; gap:7px; padding:12px; }
.draft-node-list > label { display:grid; grid-template-columns:auto minmax(0,1fr) auto; align-items:center; gap:8px; padding:9px; border:1px solid var(--line); cursor:pointer; }
.draft-node-list > label.selected { border-color:var(--green); background:#f0f8f4; box-shadow:inset 3px 0 0 var(--lime); }
.draft-node-list > label.locked { cursor:default; }
.draft-node-list > label > span { display:grid; min-width:0; }
.draft-node-list small { overflow:hidden; color:var(--text-3); font-size:10px; text-overflow:ellipsis; white-space:nowrap; }
.draft-locked-targets { display:grid; gap:7px; padding:12px; }
.draft-locked-targets > div { display:grid; grid-template-columns:auto minmax(0,1fr) auto; align-items:center; gap:8px; padding:10px; border:1px solid var(--line); background:var(--surface-soft); }
.draft-locked-targets > div > svg { color:var(--text-3); }
.draft-locked-targets > div > span { display:grid; min-width:0; }
.draft-locked-targets small { color:var(--text-3); font-size:10px; }
.target-selection-warning { margin:0 12px 12px; padding:8px 9px; border-left:3px solid var(--amber); background:var(--amber-soft); color:var(--text-2); font-size:10px; }
.field-block { display:grid; gap:6px; padding:0 12px 12px; }
.field-block > span { color:var(--text-2); font-size:10px; font-weight:700; }
.locked-hint { margin:0 12px 12px; padding:8px 9px; border-left:3px solid var(--line-strong); background:var(--surface-soft); color:var(--text-3); font-size:10px; }
.service-form-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px 0; padding-top:12px; }
.field-block.wide { grid-column:span 2; }
.members-section { min-width:0; }
.member-editor-head,.member-editor-row { display:grid; grid-template-columns:72px minmax(120px,1fr) 92px 90px 92px 34px; align-items:center; gap:7px; }
.member-editor-head { min-height:32px; padding:0 11px; border-bottom:1px solid var(--line); color:var(--text-3); font-size:9px; font-weight:700; text-transform:uppercase; }
.member-editor-list { display:grid; }
.member-editor-item { border-bottom:1px solid var(--line); }
.member-editor-item:last-child { border-bottom:0; }
.member-editor-row { min-height:56px; padding:8px 11px; }
.member-monitor-row { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; padding:0 11px 11px 90px; }
.member-monitor-row label { display:grid; gap:4px; }
.member-monitor-row span { color:var(--text-3); font-size:9px; font-weight:700; }
.member-editor-empty { padding:30px 16px; color:var(--text-3); text-align:center; font-size:11px; }
.member-editor-note { margin:0; padding:9px 11px; border-top:1px solid var(--line); background:var(--surface-soft); color:var(--text-3); font-size:10px; }
.plan-targets { display:grid; grid-template-columns:110px 1fr; align-items:start; gap:7px 12px; margin-bottom:14px; padding:12px 14px; border:1px solid var(--line); }
.plan-targets > span { color:var(--text-3); font-size:10px; font-weight:700; text-transform:uppercase; }
.plan-targets div { display:flex; flex-wrap:wrap; gap:5px; }
.plan-targets i { display:inline-flex; align-items:center; gap:4px; padding:3px 6px; border:1px solid var(--line); font-style:normal; font-size:10px; }
.plan-targets i svg { color:var(--green); fill:var(--green); }
.plan-targets small { grid-column:2; color:var(--text-3); font-size:9px; }
.semantic-diff { border:1px solid var(--line); }
.semantic-diff > header { display:flex; align-items:center; gap:7px; min-height:44px; padding:0 13px; border-bottom:1px solid var(--line); background:var(--text); color:#fff; }
.semantic-diff > header svg { color:var(--lime); }
.semantic-diff > header span { margin-left:auto; color:#b9c3c8; font-size:10px; }
.semantic-diff article { display:grid; grid-template-columns:24px minmax(135px,.65fr) minmax(0,1fr) 18px minmax(0,1fr); align-items:center; gap:7px; min-height:48px; padding:7px 12px; border-bottom:1px solid var(--line); }
.semantic-diff article:last-child { border-bottom:0; }
.semantic-diff article > b:first-child { font-family:var(--font-mono); font-size:16px; }
.semantic-diff article > span { overflow:hidden; color:var(--text-2); text-overflow:ellipsis; white-space:nowrap; }
.semantic-diff del { color:#9b4e4e; text-decoration-color:var(--red); }
.semantic-diff ins { color:#276a52; text-decoration:none; }
.semantic-diff .diff-add > b:first-child { color:var(--green); }
.semantic-diff .diff-remove > b:first-child { color:var(--red); }
.semantic-diff .diff-change > b:first-child { color:var(--amber); }
.no-semantic-diff { padding:28px; color:var(--text-3); text-align:center; }
.plan-warnings { display:flex; align-items:flex-start; gap:9px; margin-top:12px; padding:11px 13px; border:1px solid #e2c98c; background:var(--amber-soft); }
.plan-warnings svg { color:var(--amber); }
.plan-warnings div { display:grid; gap:3px; }
.plan-warnings span { color:var(--text-2); font-size:11px; }
.plan-expired { display:flex; align-items:flex-start; gap:9px; margin-top:12px; padding:11px 13px; border:1px solid #e4b3b3; background:var(--red-soft); }
.plan-expired svg { flex:none; color:var(--red); }
.plan-expired div { display:grid; gap:3px; }
.plan-expired span { color:var(--text-2); font-size:11px; }
.plan-confirmation { display:flex; align-items:flex-start; gap:9px; margin-top:14px; padding:12px 13px; border:1px solid var(--line-strong); cursor:pointer; }
.plan-confirmation span { color:var(--text-2); font-size:12px; }
@media (max-width:1300px) { .lvs-kpi-rail { grid-template-columns:repeat(3,1fr); } .lvs-kpi-rail article:nth-child(3) { border-right:0; } .lvs-kpi-rail article:nth-child(-n+3) { border-bottom:1px solid var(--line); } .lvs-workbench { grid-template-columns:1fr; } .lvs-detail { position:relative; top:0; max-height:none; } }
@media (max-width:1080px) { .lvs-editor-grid { grid-template-columns:1fr 1fr; } .members-section { grid-column:span 2; } }
@media (max-width:760px) { .lvs-kpi-rail { grid-template-columns:1fr 1fr; } .lvs-kpi-rail article { border-bottom:1px solid var(--line); } .lvs-kpi-rail article:nth-child(even) { border-right:0; } .lvs-toolbar { grid-template-columns:1fr; } .lvs-browser-tabs { overflow:auto; } .lvs-browser-tabs button { min-width:170px; } .topology-flow { grid-template-columns:1fr; } .flow-arrow { transform:rotate(90deg); justify-self:center; } .topology-members { padding-left:0; } .member-list article { grid-template-columns:1fr 1fr; } .lvs-editor-grid { grid-template-columns:1fr; } .members-section { grid-column:auto; } .member-editor-head { display:none; } .member-editor-row { grid-template-columns:1fr 1fr; } .member-editor-row > :first-child { grid-column:span 2; justify-self:start; } .member-monitor-row { grid-template-columns:1fr 1fr; padding-left:11px; } .takeover-banner { grid-template-columns:auto 1fr; } .takeover-banner .n-button { grid-column:2; justify-self:start; } .semantic-diff article { grid-template-columns:22px 1fr; } .semantic-diff article > span,.semantic-diff article > svg { grid-column:2; } }
@media (prefers-reduced-motion:no-preference) { .topology-flow .flow-arrow { animation:flow-pulse 1.8s ease-in-out infinite; } @keyframes flow-pulse { 0%,100% { opacity:.35; transform:translateX(-2px); } 50% { opacity:1; transform:translateX(2px); } } }
</style>
