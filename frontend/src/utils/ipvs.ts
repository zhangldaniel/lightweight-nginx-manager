import type {
  LvsListener,
  LvsManagedMember,
  LvsManagedService,
  LvsManagementFacts,
  LvsMember,
  LvsObservation,
  LvsVirtualService,
  NodeRecord,
} from '../types'

export interface LvsSnapshot {
  node: NodeRecord
  observation: LvsObservation
  service: LvsVirtualService
  signature: string
  directorGroupKey: string
  directorGroupKnown: boolean
}

export interface LvsVirtualServiceGroup {
  key: string
  label: string
  protocol: string
  address: string
  port: number | null
  fwmark: number | null
  scheduler: string
  persistenceSeconds: number | null
  snapshots: LvsSnapshot[]
  memberCount: number
  activeConnections: number
  inactiveConnections: number
  drift: boolean
  partial: boolean
  missingDirectorCount: number
}

export interface LvsOverview {
  capableNodes: NodeRecord[]
  unsupportedNodes: NodeRecord[]
  availableNodes: NodeRecord[]
  unavailableNodes: NodeRecord[]
  groups: LvsVirtualServiceGroup[]
  virtualServiceCount: number
  poolCount: number
  memberCount: number
  disabledMemberCount: number
  driftCount: number
  activeConnections: number
  connectionsPerSecond: number
}

function isObservation(value: unknown): value is LvsObservation {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value) && typeof (value as LvsObservation).available === 'boolean')
}

export function lvsObservation(node: NodeRecord): LvsObservation | null {
  return isObservation(node.facts.ipvs) ? node.facts.ipvs : null
}

function isManagementFacts(value: unknown): value is LvsManagementFacts {
  return Boolean(
    value &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    typeof (value as LvsManagementFacts).management_enabled === 'boolean',
  )
}

export function lvsManagement(node: NodeRecord): LvsManagementFacts | null {
  return isManagementFacts(node.facts.lvs) ? node.facts.lvs : null
}

export function lvsListenerKey(listener: LvsListener) {
  return `${listener.protocol.toUpperCase()}|${listener.address}|${listener.port}`
}

export function lvsManagedServiceFor(node: NodeRecord, listener: LvsListener) {
  const management = lvsManagement(node)
  if (!management?.management_enabled) return null
  const target = lvsListenerKey(listener)
  return (management.services || []).find((service) => lvsListenerKey(service.listener) === target) || null
}

export function lvsServiceEditable(service: LvsManagedService | null | undefined) {
  return Boolean(
    service &&
    service.editable !== false &&
    (!service.unsupported_directives || service.unsupported_directives.length === 0),
  )
}

export function canonicalLvsService(service: LvsManagedService): LvsManagedService {
  return {
    name: service.name,
    listener: {
      address: service.listener.address,
      port: service.listener.port,
      protocol: service.listener.protocol,
    },
    scheduler: service.scheduler,
    forwarding: service.forwarding,
    delay_loop: service.delay_loop,
    persistence_seconds: service.persistence_seconds ?? null,
    members: service.members.map((member) => ({
      address: member.address,
      port: member.port,
      weight: member.weight,
      enabled: member.enabled,
      monitor: member.monitor ? { ...member.monitor } : null,
    })),
  }
}

export function lvsServiceKey(service: LvsVirtualService) {
  return service.kind === 'fwmark'
    ? `fwmark|${service.fwmark ?? 0}`
    : `${service.protocol}|${service.address || ''}|${service.port ?? 0}`
}

export function lvsServiceLabel(service: LvsVirtualService) {
  return service.kind === 'fwmark'
    ? `Firewall Mark ${service.fwmark ?? 0}`
    : `${service.address || '—'}:${service.port ?? 0}`
}

export function lvsMemberKey(member: LvsMember) {
  return `${member.address}|${member.port}`
}

export function managedServiceFromObserved(service: LvsVirtualService): LvsManagedService | null {
  if (service.kind !== 'address' || !service.address || !service.port || service.protocol === 'FWM') return null
  const forwarding = String(service.destinations[0]?.forwarding || 'dr').toUpperCase()
  return {
    name: `vs-${service.address.replace(/[^a-zA-Z0-9]+/g, '-')}-${service.port}`,
    listener: {
      address: service.address,
      port: service.port,
      protocol: service.protocol,
    },
    scheduler: service.scheduler || 'wlc',
    forwarding: forwarding === 'NAT' || forwarding === 'TUN' ? forwarding : 'DR',
    delay_loop: 6,
    persistence_seconds: service.persistence_seconds ?? null,
    members: service.destinations.map((member) => ({
      address: member.address,
      port: member.port,
      weight: Math.max(1, member.weight || 1),
      enabled: member.weight > 0,
      monitor: null,
    })),
  }
}

export interface LvsSemanticDiffLine {
  kind: 'add' | 'change' | 'remove'
  subject: string
  before?: string
  after?: string
}

function displayMonitor(member: LvsManagedMember) {
  if (!member.monitor) return '无健康检查'
  const port = member.monitor.connect_port ?? member.port
  return `TCP ${port} · 超时 ${member.monitor.connect_timeout}s · 重试 ${member.monitor.retries} 次/${member.monitor.delay_before_retry}s`
}

function displayPersistence(value?: number | null) {
  return value ? `${value} 秒` : '关闭'
}

export function buildLvsSemanticDiff(
  before: LvsManagedService | null,
  after: LvsManagedService | null,
): LvsSemanticDiffLine[] {
  if (!before && after) {
    return [
      { kind: 'add', subject: 'Virtual Service', after: `${after.listener.address}:${after.listener.port} / ${after.listener.protocol}` },
      { kind: 'add', subject: '调度与转发', after: `${after.scheduler.toUpperCase()} / ${after.forwarding}` },
      { kind: 'add', subject: 'Pool Members', after: `${after.members.length} 个成员` },
    ]
  }
  if (before && !after) {
    return [{ kind: 'remove', subject: 'Virtual Service', before: `${before.listener.address}:${before.listener.port} / ${before.listener.protocol}` }]
  }
  if (!before || !after) return []
  const changes: LvsSemanticDiffLine[] = []
  const addChange = (subject: string, left: string, right: string) => {
    if (left !== right) changes.push({ kind: 'change', subject, before: left, after: right })
  }
  addChange('名称', before.name, after.name)
  addChange('监听地址', `${before.listener.address}:${before.listener.port}`, `${after.listener.address}:${after.listener.port}`)
  addChange('协议', before.listener.protocol, after.listener.protocol)
  addChange('调度算法', before.scheduler.toUpperCase(), after.scheduler.toUpperCase())
  addChange('转发模式', before.forwarding, after.forwarding)
  addChange('检查周期', `${before.delay_loop} 秒`, `${after.delay_loop} 秒`)
  addChange('会话保持', displayPersistence(before.persistence_seconds), displayPersistence(after.persistence_seconds))

  const beforeMembers = new Map(before.members.map((member) => [`${member.address}|${member.port}`, member]))
  const afterMembers = new Map(after.members.map((member) => [`${member.address}|${member.port}`, member]))
  for (const [key, member] of afterMembers) {
    const previous = beforeMembers.get(key)
    const endpoint = `${member.address}:${member.port}`
    if (!previous) {
      changes.push({
        kind: 'add',
        subject: `成员 ${endpoint}`,
        after: `权重 ${member.weight} · ${member.enabled ? '启用' : '停用'} · ${displayMonitor(member)}`,
      })
      continue
    }
    addChange(
      `成员 ${endpoint}`,
      `权重 ${previous.weight} · ${previous.enabled ? '启用' : '停用'} · ${displayMonitor(previous)}`,
      `权重 ${member.weight} · ${member.enabled ? '启用' : '停用'} · ${displayMonitor(member)}`,
    )
  }
  for (const [key, member] of beforeMembers) {
    if (!afterMembers.has(key)) {
      changes.push({ kind: 'remove', subject: `成员 ${member.address}:${member.port}`, before: `权重 ${member.weight}` })
    }
  }
  return changes
}

/**
 * Treat the control-plane plan as the source of truth. The fallback is used only
 * for compatibility with an older server that did not yet return a semantic diff.
 */
export function lvsPlanSemanticDiff(
  value: unknown,
  fallback: LvsSemanticDiffLine[] = [],
  nodeLabels: Record<string, string> = {},
): LvsSemanticDiffLine[] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fallback
  const record = value as Record<string, unknown>
  if (typeof record.action !== 'string' || !('before' in record) || !('after' in record)) return fallback
  if (record.changed === false) return []
  const perNodeBefore = record.per_node_before
  const changedNodeIds = record.changed_node_ids
  if (
    perNodeBefore && typeof perNodeBefore === 'object' && !Array.isArray(perNodeBefore)
    && Array.isArray(changedNodeIds) && changedNodeIds.length > 0
  ) {
    const after = record.after && typeof record.after === 'object'
      ? record.after as LvsManagedService
      : null
    const lines = changedNodeIds.flatMap((nodeId) => {
      if (typeof nodeId !== 'string') return []
      const rawBefore = (perNodeBefore as Record<string, unknown>)[nodeId]
      const before = rawBefore && typeof rawBefore === 'object'
        ? rawBefore as LvsManagedService
        : null
      const label = nodeLabels[nodeId] || nodeId
      return buildLvsSemanticDiff(before, after).map((line) => ({
        ...line,
        subject: `${label} · ${line.subject}`,
      }))
    })
    if (lines.length) return lines
  }
  const before = record.before && typeof record.before === 'object'
    ? record.before as LvsManagedService
    : null
  const after = record.after && typeof record.after === 'object'
    ? record.after as LvsManagedService
    : null
  return buildLvsSemanticDiff(before, after)
}

function serviceSignature(service: LvsVirtualService) {
  const members = [...(service.destinations || [])]
    .map((member) => [member.address, member.port, member.forwarding, member.weight].join('|'))
    .sort()
  return JSON.stringify({
    scheduler: service.scheduler,
    onePacket: Boolean(service.one_packet),
    persistence: service.persistence_seconds ?? null,
    members,
  })
}

function safeNumber(value: unknown) {
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function normalizedAddress(value: unknown) {
  if (typeof value !== 'string') return ''
  return value.trim().split('/', 1)[0].toLowerCase()
}

function directorGroupFor(node: NodeRecord, service: LvsVirtualService) {
  const isolated = { key: `node|${node.id}`, known: true }
  const keepalived = asRecord(node.facts.keepalived)
  if (!keepalived) return isolated
  const summary = asRecord(keepalived.config_summary)
  const instances = Array.isArray(summary?.instances) ? summary.instances : []
  if (keepalived.mode === 'standalone') {
    return {
      ...isolated,
      known: summary?.summary_complete === true
        && summary.truncated !== true
        && Number(summary.instance_count) === 0
        && instances.length === 0,
    }
  }
  const vip = normalizedAddress(keepalived.vip)
  if (!vip || summary?.summary_complete !== true || summary.truncated === true) {
    return { ...isolated, known: false }
  }
  const target = service.kind === 'address' ? normalizedAddress(service.address) : vip
  const matching = instances.flatMap((value) => {
    const instance = asRecord(value)
    const vrid = instance?.virtual_router_id
    const virtualIps = Array.isArray(instance?.virtual_ips)
      ? instance.virtual_ips.map(normalizedAddress)
      : []
    return instance && Number.isInteger(vrid) && Number(vrid) >= 1 && Number(vrid) <= 255
      && virtualIps.includes(vip)
      ? [{ vrid: Number(vrid), targetAssigned: virtualIps.includes(target) }]
      : []
  })
  return matching.length === 1
    ? { key: `vrrp|${vip}|${matching[0].vrid}`, known: matching[0].targetAssigned }
    : { ...isolated, known: false }
}

export function buildLvsOverview(nodes: NodeRecord[]): LvsOverview {
  const capableNodes = nodes.filter((node) => node.capabilities.includes('ipvs_observer_v1'))
  const unsupportedNodes = nodes.filter((node) => !node.capabilities.includes('ipvs_observer_v1'))
  const availableNodes = capableNodes.filter((node) => lvsObservation(node)?.available === true)
  const unavailableNodes = capableNodes.filter((node) => lvsObservation(node)?.available !== true)
  const snapshotsByKey = new Map<string, LvsSnapshot[]>()

  for (const node of availableNodes) {
    const observation = lvsObservation(node)
    if (!observation) continue
    for (const service of observation.services || []) {
      const directorGroup = directorGroupFor(node, service)
      const key = `${directorGroup.key}|${lvsServiceKey(service)}`
      const snapshots = snapshotsByKey.get(key) || []
      snapshots.push({
        node,
        observation,
        service,
        signature: serviceSignature(service),
        directorGroupKey: directorGroup.key,
        directorGroupKnown: directorGroup.known,
      })
      snapshotsByKey.set(key, snapshots)
    }
  }

  const groups = [...snapshotsByKey.entries()].map(([key, snapshots]) => {
    const primary = snapshots[0].service
    const signatures = new Set(snapshots.map((snapshot) => snapshot.signature))
    const expectedDirectors = availableNodes.filter((node) =>
      node.status !== 'offline'
      && directorGroupFor(node, primary).key === snapshots[0].directorGroupKey,
    ).length
    const groupEvidenceIncomplete = availableNodes.some((node) => {
      const directorGroup = directorGroupFor(node, primary)
      return node.status !== 'offline'
        && directorGroup.key === snapshots[0].directorGroupKey
        && !directorGroup.known
    })
    const missingDirectorCount = Math.max(0, expectedDirectors - snapshots.filter((item) => item.node.status !== 'offline').length)
    const memberKeys = new Set(snapshots.flatMap((snapshot) => snapshot.service.destinations.map(lvsMemberKey)))
    return {
      key,
      label: lvsServiceLabel(primary),
      protocol: primary.protocol,
      address: primary.address || '',
      port: primary.kind === 'address' ? primary.port ?? 0 : null,
      fwmark: primary.kind === 'fwmark' ? primary.fwmark ?? 0 : null,
      scheduler: primary.scheduler,
      persistenceSeconds: primary.persistence_seconds ?? null,
      snapshots,
      memberCount: memberKeys.size,
      activeConnections: Math.max(...snapshots.map((snapshot) => safeNumber(snapshot.service.active_connections)), 0),
      inactiveConnections: Math.max(...snapshots.map((snapshot) => safeNumber(snapshot.service.inactive_connections)), 0),
      drift: signatures.size > 1 || missingDirectorCount > 0,
      partial: snapshots.some((snapshot) =>
        !snapshot.directorGroupKnown
        || snapshot.observation.partial
        || snapshot.observation.truncated
        || snapshot.node.status === 'offline',
      ) || groupEvidenceIncomplete,
      missingDirectorCount,
    }
  }).sort((left, right) => left.label.localeCompare(right.label, undefined, { numeric: true }))

  const memberKeys = new Set<string>()
  let disabledMemberCount = 0
  for (const group of groups) {
    const statusByMember = new Map<string, number[]>()
    for (const snapshot of group.snapshots) {
      for (const member of snapshot.service.destinations) {
        const key = `${group.key}|${lvsMemberKey(member)}`
        memberKeys.add(key)
        const weights = statusByMember.get(key) || []
        weights.push(member.weight)
        statusByMember.set(key, weights)
      }
    }
    disabledMemberCount += [...statusByMember.values()].filter((weights) => weights.every((weight) => weight === 0)).length
  }

  return {
    capableNodes,
    unsupportedNodes,
    availableNodes,
    unavailableNodes,
    groups,
    virtualServiceCount: groups.length,
    poolCount: groups.length,
    memberCount: memberKeys.size,
    disabledMemberCount,
    driftCount: groups.filter((group) => group.drift).length,
    activeConnections: groups.reduce((total, group) => total + group.activeConnections, 0),
    connectionsPerSecond: availableNodes.reduce((total, node) => total + safeNumber(lvsObservation(node)?.stats?.rates?.connections_per_second), 0),
  }
}
