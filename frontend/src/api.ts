import type {
  AuditRecord,
  EnrollmentRecord,
  JobRecord,
  KeepalivedJobAction,
  LvsApplyResult,
  LvsIntent,
  LvsPlan,
  MonitoringItem,
  NodeRecord,
  OperationRecord,
  Session,
  SiteAttachment,
  SiteRevision,
  UiState,
} from './types'

export class ApiError extends Error {
  status: number
  body: Record<string, unknown>

  constructor(message: string, status: number, body: Record<string, unknown>) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

let csrfToken = ''

export function setCsrfToken(value: string) {
  csrfToken = value
}

function errorMessage(body: Record<string, unknown>, status: number) {
  const detail = body.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    const message = (detail as Record<string, unknown>).message
    if (typeof message === 'string') return message
  }
  return `请求失败（HTTP ${status}）`
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  const method = (init.method || 'GET').toUpperCase()
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && csrfToken) {
    headers.set('X-CSRF-Token', csrfToken)
  }
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(path, {
    ...init,
    headers,
    credentials: 'same-origin',
  })
  let body: Record<string, unknown> = {}
  try {
    body = (await response.json()) as Record<string, unknown>
  } catch {
    body = {}
  }
  if (!response.ok) throw new ApiError(errorMessage(body, response.status), response.status, body)
  return body as T
}

export function jsonBody(value: unknown) {
  return JSON.stringify(value)
}

function normalizedLvsPlan(value: Record<string, unknown>): LvsPlan {
  const id = String(value.plan_id || value.id || '')
  const planDigest = String(value.plan_digest || '')
  if (!id || !planDigest) throw new Error('控制端返回的 LVS 变更计划不完整')
  return {
    ...value,
    id,
    plan_id: id,
    plan_digest: planDigest,
    diff: value.diff ?? [],
    warnings: Array.isArray(value.warnings)
      ? value.warnings.map((item) => {
        if (typeof item === 'string') return item
        if (item && typeof item === 'object') {
          const record = item as Record<string, unknown>
          return String(record.message || record.summary || record.code || '控制端返回一条发布警告')
        }
        return String(item)
      })
      : [],
    expires_at: typeof value.expires_at === 'string' ? value.expires_at : null,
  }
}

function normalizedLvsApplyResult(value: Record<string, unknown>): LvsApplyResult {
  const operation = value.operation && typeof value.operation === 'object'
    ? value.operation as unknown as OperationRecord
    : value.no_changes === true
      ? null
      : value as unknown as OperationRecord
  return {
    ...value,
    operation,
    jobs: Array.isArray(value.jobs) ? value.jobs as JobRecord[] : [],
  }
}

interface SiteAttachmentPayload {
  id: string
  site_id: string
  file_name: string
  content_type: string
  size_bytes: number
  created_at: string
  content_url: string
}

interface SiteAttachmentListPayload {
  items: SiteAttachmentPayload[]
  max_items?: number
  remaining?: number
  max_bytes?: number
}

function normalizedSiteAttachment(value: SiteAttachmentPayload): SiteAttachment {
  return {
    id: value.id,
    site_id: value.site_id,
    filename: value.file_name,
    content_type: value.content_type,
    size: value.size_bytes,
    created_at: value.created_at,
    url: value.content_url,
  }
}

function normalizedNonNegativeInteger(value: unknown, fallback: number) {
  const number = Number(value)
  return Number.isInteger(number) && number >= 0 ? number : fallback
}

function normalizedPositiveInteger(value: unknown, fallback: number) {
  const number = Number(value)
  return Number.isInteger(number) && number > 0 ? number : fallback
}

export const api = {
  session: () => request<Session>('/api/v1/auth/session'),
  login: (username: string, password: string) =>
    request<Session>('/api/v1/auth/login', {
      method: 'POST',
      body: jsonBody({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>('/api/v1/auth/logout', { method: 'POST' }),
  uiState: () => request<{ revision: number; state: UiState }>('/api/v1/admin/ui-state'),
  saveUiState: (revision: number, state: UiState, reconciledOperationIds: string[] = []) =>
    request<{ revision: number; state: UiState; reconciled_operation_ids?: string[] }>(
      '/api/v1/admin/ui-state',
      {
        method: 'PUT',
        body: jsonBody({
          revision,
          state,
          reconciled_operation_ids: reconciledOperationIds,
        }),
      },
    ),
  nodes: () => request<{ items: NodeRecord[] }>('/api/v1/admin/nodes'),
  jobs: (limit = 200) => request<{ items: JobRecord[] }>(`/api/v1/admin/jobs?limit=${limit}`),
  operations: (limit = 200) =>
    request<{ items: OperationRecord[] }>(`/api/v1/admin/operations?limit=${limit}`),
  reconciliationOperations: (limit = 500) =>
    request<{ items: OperationRecord[] }>(
      `/api/v1/admin/operations?reconciliation_status=pending&limit=${limit}`,
    ),
  operation: (operationId: string) =>
    request<{ operation: OperationRecord; jobs: JobRecord[] }>(
      `/api/v1/admin/operations/${encodeURIComponent(operationId)}`,
    ),
  enrollments: () =>
    request<{ items: EnrollmentRecord[] }>('/api/v1/admin/enrollments?status=pending&limit=100'),
  decideEnrollment: (id: string, decision: 'approve' | 'reject') =>
    request<Record<string, unknown>>(
      `/api/v1/admin/enrollments/${encodeURIComponent(id)}/${decision}`,
      { method: 'POST' },
    ),
  createJobs: (nodeIds: string[], action: string, payload: Record<string, unknown> = {}) =>
    request<{ batch_id: string; jobs: JobRecord[] }>('/api/v1/admin/jobs', {
      method: 'POST',
      body: jsonBody({ node_ids: nodeIds, action, payload, ttl_seconds: 120 }),
    }),
  highAvailabilityCheck: (nodeIds: string[], action: KeepalivedJobAction) =>
    request<{ batch_id: string; jobs: JobRecord[] }>('/api/v1/admin/jobs', {
      method: 'POST',
      body: jsonBody({ node_ids: nodeIds, action, payload: {}, ttl_seconds: 120 }),
    }),
  highAvailabilityJobs: async (limit = 100) => {
    const [inspections, validations] = await Promise.all([
      request<{ items: JobRecord[] }>(`/api/v1/admin/jobs?action=keepalived_inspect&limit=${limit}`),
      request<{ items: JobRecord[] }>(`/api/v1/admin/jobs?action=keepalived_validate&limit=${limit}`),
    ])
    return { items: [...inspections.items, ...validations.items] }
  },
  createLvsPlan: async (nodeIds: string[], intent: LvsIntent, adoptExisting = false) => {
    const response = await request<Record<string, unknown>>('/api/v1/admin/lvs/plans', {
      method: 'POST',
      body: jsonBody({ node_ids: nodeIds, intent, adopt_existing: adoptExisting }),
    })
    const plan = response.plan && typeof response.plan === 'object'
      ? response.plan as Record<string, unknown>
      : response
    return normalizedLvsPlan(plan)
  },
  applyLvsPlan: async (planId: string, planDigest: string, requestId: string) =>
    normalizedLvsApplyResult(
      await request<Record<string, unknown>>(
        `/api/v1/admin/lvs/plans/${encodeURIComponent(planId)}/apply`,
        {
          method: 'POST',
          body: jsonBody({ plan_digest: planDigest, request_id: requestId }),
        },
      ),
    ),
  createOperation: (body: Record<string, unknown>) =>
    request<{
      operation: OperationRecord
      jobs: JobRecord[]
      idempotent: boolean
    }>('/api/v1/admin/operations', {
      method: 'POST',
      body: jsonBody({ ...body, reconciliation_protocol: 'ui-state-v1' }),
    }),
  monitoringSummary: () =>
    request<{ items: MonitoringItem[]; server_time: string }>('/api/v1/admin/monitoring/summary'),
  monitoringHistory: (nodeId: string, rangeSeconds: number) =>
    request<{ items: Array<{ sampled_at: string; metrics: Record<string, unknown> }> }>(
      `/api/v1/admin/monitoring/nodes/${encodeURIComponent(nodeId)}/metrics?range_seconds=${rangeSeconds}`,
    ),
  audit: (limit = 200) =>
    request<{ items: AuditRecord[]; next_before_id: number | null }>(
      `/api/v1/admin/audit?limit=${limit}`,
    ),
  createLogSession: (body: Record<string, unknown>) =>
    request<Record<string, unknown>>('/api/v1/admin/log-sessions', {
      method: 'POST',
      body: jsonBody(body),
    }),
  stopLogSession: (sessionId: string) =>
    request<Record<string, unknown>>(
      `/api/v1/admin/log-sessions/${encodeURIComponent(sessionId)}`,
      { method: 'DELETE' },
    ),
  revokeNode: (nodeId: string) =>
    request<{ revoked: boolean; idempotent: boolean; node_id: string }>(
      `/api/v1/admin/nodes/${encodeURIComponent(nodeId)}/revoke`,
      { method: 'POST' },
    ),
  siteRevisions: (siteId: string) =>
    request<{ items: SiteRevision[] }>(
      `/api/v1/admin/sites/${encodeURIComponent(siteId)}/revisions?limit=100`,
    ),
  siteRevision: (siteId: string, version: number) =>
    request<SiteRevision>(
      `/api/v1/admin/sites/${encodeURIComponent(siteId)}/revisions/${version}`,
    ),
  siteAttachments: async (siteId: string) => {
    const response = await request<SiteAttachmentListPayload>(
      `/api/v1/admin/sites/${encodeURIComponent(siteId)}/attachments`,
    )
    const items = response.items.map(normalizedSiteAttachment)
    const maxItems = normalizedPositiveInteger(response.max_items, 8)
    return {
      items,
      maxItems,
      remaining: Math.min(
        maxItems,
        normalizedNonNegativeInteger(response.remaining, Math.max(0, maxItems - items.length)),
      ),
      maxBytes: normalizedPositiveInteger(response.max_bytes, 5 * 1024 * 1024),
    }
  },
  uploadSiteAttachment: async (siteId: string, file: File) => {
    const response = await request<{ attachment: SiteAttachmentPayload }>(
      `/api/v1/admin/sites/${encodeURIComponent(siteId)}/attachments`, {
      method: 'POST',
      headers: {
        'Content-Type': file.type,
        'X-Filename': encodeURIComponent(file.name || 'screenshot'),
      },
      body: file,
    })
    return { attachment: normalizedSiteAttachment(response.attachment) }
  },
  deleteSiteAttachment: (siteId: string, attachmentId: string) =>
    request<{ deleted: boolean }>(
      `/api/v1/admin/sites/${encodeURIComponent(siteId)}/attachments/${encodeURIComponent(attachmentId)}`,
      { method: 'DELETE' },
    ),
}
