import type {
  AuditRecord,
  EnrollmentRecord,
  JobRecord,
  MonitoringItem,
  NodeRecord,
  OperationRecord,
  Session,
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

export const api = {
  session: () => request<Session>('/api/v1/auth/session'),
  login: (username: string, password: string) =>
    request<Session>('/api/v1/auth/login', {
      method: 'POST',
      body: jsonBody({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>('/api/v1/auth/logout', { method: 'POST' }),
  uiState: () => request<{ revision: number; state: UiState }>('/api/v1/admin/ui-state'),
  saveUiState: (revision: number, state: UiState) =>
    request<{ revision: number; state: UiState }>('/api/v1/admin/ui-state', {
      method: 'PUT',
      body: jsonBody({ revision, state }),
    }),
  nodes: () => request<{ items: NodeRecord[] }>('/api/v1/admin/nodes'),
  jobs: (limit = 200) => request<{ items: JobRecord[] }>(`/api/v1/admin/jobs?limit=${limit}`),
  operations: (limit = 200) =>
    request<{ items: OperationRecord[] }>(`/api/v1/admin/operations?limit=${limit}`),
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
  createOperation: (body: Record<string, unknown>) =>
    request<{
      operation: OperationRecord
      jobs: JobRecord[]
      idempotent: boolean
    }>('/api/v1/admin/operations', {
      method: 'POST',
      body: jsonBody(body),
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
}
