import type { LvsMember, LvsObservation, LvsVirtualService, NodeRecord } from '../types'

export interface LvsSnapshot {
  node: NodeRecord
  observation: LvsObservation
  service: LvsVirtualService
  signature: string
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

export function buildLvsOverview(nodes: NodeRecord[]): LvsOverview {
  const capableNodes = nodes.filter((node) => node.capabilities.includes('ipvs_observer_v1'))
  const unsupportedNodes = nodes.filter((node) => !node.capabilities.includes('ipvs_observer_v1'))
  const availableNodes = capableNodes.filter((node) => lvsObservation(node)?.available === true)
  const unavailableNodes = capableNodes.filter((node) => lvsObservation(node)?.available !== true)
  const expectedDirectors = availableNodes.filter((node) => node.status !== 'offline').length
  const snapshotsByKey = new Map<string, LvsSnapshot[]>()

  for (const node of availableNodes) {
    const observation = lvsObservation(node)
    if (!observation) continue
    for (const service of observation.services || []) {
      const key = lvsServiceKey(service)
      const snapshots = snapshotsByKey.get(key) || []
      snapshots.push({ node, observation, service, signature: serviceSignature(service) })
      snapshotsByKey.set(key, snapshots)
    }
  }

  const groups = [...snapshotsByKey.entries()].map(([key, snapshots]) => {
    const primary = snapshots[0].service
    const signatures = new Set(snapshots.map((snapshot) => snapshot.signature))
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
      partial: snapshots.some((snapshot) => snapshot.observation.partial || snapshot.observation.truncated || snapshot.node.status === 'offline'),
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
