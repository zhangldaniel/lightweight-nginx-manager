export type Role = 'admin' | 'operator' | 'viewer' | string
export type Tone = 'success' | 'warning' | 'danger' | 'info' | 'neutral'

export interface Session {
  authenticated: boolean
  username: string
  role: Role
  auth_source: string
  csrf_token: string
  expires_at: string | null
}

export interface ConfigEntry {
  id: string
  context: 'http' | 'stream'
  directory: string
  suffix: '.conf' | '.stream'
  default?: boolean
  label?: string
}

export interface NodeFacts {
  nginx_binary?: string
  nginx_root?: string
  nginx_config?: string
  nginx_service?: string
  managed_config_root?: string
  managed_certificate_root?: string
  config_entries?: ConfigEntry[]
  main_config_editable?: boolean
  log_roots?: string[]
  log_files?: string[]
  stub_status_url?: string
  log_stream_transport?: string
  lvs?: LvsManagementFacts
  ipvs?: LvsObservation
  [key: string]: unknown
}

export interface LvsMember {
  address: string
  port: number
  forwarding: 'nat' | 'dr' | 'tunnel' | 'local' | 'bypass' | 'unknown'
  weight: number
  active_connections: number
  inactive_connections: number
}

export interface LvsVirtualService {
  id: string
  kind: 'address' | 'fwmark'
  protocol: 'TCP' | 'UDP' | 'SCTP' | 'FWM'
  address?: string
  port?: number
  fwmark?: number
  scheduler: string
  one_packet: boolean
  persistence_seconds?: number
  active_connections: number
  inactive_connections: number
  destinations: LvsMember[]
}

export interface LvsStats {
  totals?: {
    connections?: number
    in_packets?: number
    out_packets?: number
    in_bytes?: number
    out_bytes?: number
  }
  rates?: {
    connections_per_second?: number
    in_packets_per_second?: number
    out_packets_per_second?: number
    in_bytes_per_second?: number
    out_bytes_per_second?: number
  }
}

export interface LvsObservation {
  available: boolean
  source: 'procfs'
  reason?: 'disabled' | 'not_loaded' | 'permission_denied' | 'unavailable' | 'invalid_format' | 'helper_unavailable'
  version?: string
  service_count?: number
  destination_count?: number
  partial?: boolean
  truncated?: boolean
  services?: LvsVirtualService[]
  stats?: LvsStats
}

export interface LvsListener {
  address: string
  port: number
  protocol: 'TCP' | 'UDP' | 'SCTP'
}

export interface LvsTcpMonitor {
  kind: 'tcp'
  connect_timeout: number
  retries: number
  delay_before_retry: number
  connect_port?: number
}

export interface LvsManagedMember {
  address: string
  port: number
  weight: number
  enabled: boolean
  monitor?: LvsTcpMonitor | null
}

export interface LvsManagedService {
  name: string
  listener: LvsListener
  scheduler: string
  forwarding: 'DR' | 'NAT' | 'TUN'
  delay_loop: number
  persistence_seconds?: number | null
  members: LvsManagedMember[]
  origin?: string
  editable?: boolean
  unsupported_directives?: string[]
}

export interface LvsManagementFacts {
  management_enabled: boolean
  config_hash?: string | null
  services?: LvsManagedService[]
  reason?: string
  [key: string]: unknown
}

export interface LvsUpsertIntent {
  kind: 'upsert_service'
  target: LvsListener
  service: LvsManagedService
  change_note?: string
}

export interface LvsDeleteIntent {
  kind: 'delete_service'
  target: LvsListener
  change_note?: string
}

export type LvsIntent = LvsUpsertIntent | LvsDeleteIntent

export interface LvsPlan {
  id: string
  plan_id: string
  plan_digest: string
  diff: unknown
  warnings: string[]
  expires_at: string | null
  expected_config_hashes?: Record<string, string>
  consumed_at?: string | null
  operation_id?: string | null
  [key: string]: unknown
}

export interface LvsApplyResult {
  status?: string
  no_changes?: boolean
  operation: OperationRecord | null
  jobs: JobRecord[]
  [key: string]: unknown
}

export interface NodeRecord {
  id: string
  node_name: string
  hostname: string
  labels: Record<string, string>
  status: string
  reported_status: string
  agent_version: string | null
  nginx_version: string | null
  config_hash: string | null
  capabilities: string[]
  facts: NodeFacts
  enrolled_at: string | null
  last_seen_at: string | null
  revoked_at: string | null
}

export type KeepalivedRole = 'MASTER' | 'BACKUP' | 'STANDALONE' | 'FAULT' | 'UNKNOWN'
export type KeepalivedJobAction = 'keepalived_inspect' | 'keepalived_validate'

export interface HighAvailabilityNodeRef {
  address: string
  nodeId?: string
  label?: string
}

export interface HighAvailabilityGroup {
  id: string
  name: string
  type: 'keepalived'
  vip: string
  nodes: HighAvailabilityNodeRef[]
}

export interface SiteRecord {
  id: string
  resourceType?: 'site' | 'generic'
  name?: string
  filename?: string
  domain?: string
  type?: string
  target?: string
  context?: 'http' | 'stream' | 'main'
  configMode?: 'guided' | 'conf' | 'generic'
  config: string
  /** @deprecated Retained only for older saved UI state. */
  environment?: string
  nodeIds: string[]
  certificateId?: string
  version: number
  candidateVersion?: number
  status: string
  note?: string
  changeNote?: string
  updatedAt?: string
  nodeHashes?: Record<string, string>
  nodeConfigPaths?: Record<string, string>
  nodeConfigs?: Record<string, string>
  nodeConfigEntryIds?: Record<string, string>
  history?: Array<Record<string, unknown>>
  pendingRemote?: Record<string, unknown>
  lastFailure?: {
    summary?: string
    stage?: string
    node?: string
    message?: string
    operation?: string
    operationId?: string
    completedNodes?: number
    totalNodes?: number
    failedNodeIds?: string[]
    candidateVersion?: number
    [key: string]: unknown
  }
  [key: string]: unknown
}

export interface CertificatePath {
  certificatePath: string
  keyPath: string
}

export interface CertificateRecord {
  id: string
  domain: string
  name?: string
  domains?: string[]
  issuer?: string
  source?: string
  expiresAt?: string
  daysLeft?: number
  status?: string
  nodeIds: string[]
  nodePaths?: Record<string, CertificatePath>
  nodeHashes?: Record<string, { certificateHash?: string; keyHash?: string }>
  linkedSiteIds?: string[]
  fingerprint?: string
  notAfter?: string
  note?: string
  [key: string]: unknown
}

export interface JobRecord {
  id: string
  batch_id: string
  operation_id: string | null
  node_id: string
  node_name: string | null
  action: string
  status: string
  created_at: string
  expires_at: string
  claimed_at: string | null
  completed_at: string | null
  created_by: string | null
  result: Record<string, unknown> | null
  sequence_no?: number
  [key: string]: unknown
}

export interface OperationRecord {
  id: string
  site_id: string
  kind: string
  status: string
  base_version: number
  candidate_revision_id: string | null
  created_by: string
  created_at: string
  updated_at: string
  completed_at: string | null
  execution_mode?: string
  metadata: Record<string, unknown>
}

export interface SiteRevision {
  id: string
  site_id: string
  version: number
  snapshot_sha256: string
  note: string
  created_by: string
  created_at: string
  published_at: string | null
  snapshot?: SiteRecord
}

export interface EnrollmentRecord {
  id: string
  node_id: string
  node_name: string
  hostname: string
  labels: Record<string, string>
  status: string
  requested_at: string
  updated_at: string
  expires_at: string
  decided_at: string | null
  decided_by: string | null
}

export interface MonitoringItem {
  node: NodeRecord
  sampled_at: string | null
  metrics: Record<string, unknown>
  health: { status: string; reasons: string[] }
}

export interface AuditRecord {
  id: number
  created_at: string
  actor_type: string
  actor_id: string
  event: string
  target_type: string
  target_id: string | null
  detail: Record<string, unknown>
}

export interface UiState {
  sites: SiteRecord[]
  certificates: CertificateRecord[]
  highAvailabilityGroups: HighAvailabilityGroup[]
  importedInventoryJobs: string[]
  importedCertificateInventoryJobs: string[]
  processedOperationIds: string[]
  [key: string]: unknown
}

export interface ToastItem {
  id: number
  type: Tone
  title: string
  message?: string
}
