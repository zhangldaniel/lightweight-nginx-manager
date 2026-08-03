import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import { ApiError, api, setCsrfToken } from '../api'
import {
  configPathForEntry,
  managedConfigPath,
  safeName,
  sha256,
  uid,
} from '../utils/config'
import { configForCertificateNode } from '../utils/certificateConfig'
import { processInventoryJobs } from '../utils/inventory'
import type {
  AuditRecord,
  CertificateRecord,
  EnrollmentRecord,
  JobRecord,
  MonitoringItem,
  NodeRecord,
  OperationRecord,
  Session,
  SiteRecord,
  ToastItem,
  Tone,
  UiState,
} from '../types'

const MAX_RECONCILE_BATCH = 500

const emptyState = (): UiState => ({
  sites: [],
  certificates: [],
  importedInventoryJobs: [],
  importedCertificateInventoryJobs: [],
  processedOperationIds: [],
})

function normalizedState(value?: Partial<UiState>): UiState {
  return {
    ...emptyState(),
    ...(value || {}),
    sites: Array.isArray(value?.sites) ? value.sites : [],
    certificates: Array.isArray(value?.certificates) ? value.certificates : [],
    importedInventoryJobs: Array.isArray(value?.importedInventoryJobs)
      ? value.importedInventoryJobs
      : [],
    importedCertificateInventoryJobs: Array.isArray(value?.importedCertificateInventoryJobs)
      ? value.importedCertificateInventoryJobs
      : [],
    // Legacy persisted shape only; recovery is driven exclusively by Server pending reconciliation.
    processedOperationIds: Array.isArray(value?.processedOperationIds)
      ? value.processedOperationIds
      : [],
  }
}

function clonePlain<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function isRetryableApiError(error: unknown) {
  if (!(error instanceof ApiError)) return true
  return error.status >= 500 || [408, 409, 425, 429].includes(error.status)
}

function isPermanentApiError(error: unknown) {
  return error instanceof ApiError && error.status >= 400 && !isRetryableApiError(error)
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(',')}]`
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => left.localeCompare(right))
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`).join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}

function sameUiState(left: UiState, right: UiState) {
  return canonicalJson(normalizedState(left)) === canonicalJson(normalizedState(right))
}
export const useConsoleStore = defineStore('console', () => {
  const session = ref<Session | null>(null)
  const booting = ref(true)
  const loading = ref(false)
  const refreshing = ref(false)
  const lastRefreshAt = ref<Date | null>(null)
  const stateRevision = ref(0)
  const ui = ref<UiState>(emptyState())
  const nodes = ref<NodeRecord[]>([])
  const jobs = ref<JobRecord[]>([])
  const operations = ref<OperationRecord[]>([])
  const enrollments = ref<EnrollmentRecord[]>([])
  const monitoring = ref<MonitoringItem[]>([])
  const audit = ref<AuditRecord[]>([])
  const selectedSiteId = ref('')
  const selectedCertificateId = ref('')
  const selectedNodeId = ref('')
  const toasts = reactive<ToastItem[]>([])

  const sites = computed(() => ui.value.sites)
  const certificates = computed(() => ui.value.certificates)
  const selectedSite = computed(
    () => sites.value.find((item) => item.id === selectedSiteId.value) || sites.value[0] || null,
  )
  const selectedCertificate = computed(
    () =>
      certificates.value.find((item) => item.id === selectedCertificateId.value) ||
      certificates.value[0] ||
      null,
  )
  const selectedNode = computed(
    () => nodes.value.find((item) => item.id === selectedNodeId.value) || nodes.value[0] || null,
  )
  const canOperate = computed(() => ['admin', 'operator'].includes(session.value?.role || ''))
  const isAdmin = computed(() => session.value?.role === 'admin')
  const onlineCount = computed(() => nodes.value.filter((item) => item.status !== 'offline').length)
  const riskyCertificateCount = computed(
    () =>
      certificates.value.filter((item) => {
        const days = Number(item.daysLeft ?? 9999)
        return days <= 30 || item.status === 'expired'
      }).length,
  )
  const unhealthyCount = computed(
    () => monitoring.value.filter((item) => !['healthy', 'no_data'].includes(item.health.status)).length,
  )

  function notify(title: string, type: Tone = 'info', message = '') {
    const toast = { id: Date.now() + Math.random(), title, type, message }
    toasts.push(toast)
    window.setTimeout(() => {
      const index = toasts.findIndex((item) => item.id === toast.id)
      if (index >= 0) toasts.splice(index, 1)
    }, type === 'danger' ? 6000 : 3200)
  }

  function apiMessage(error: unknown) {
    return error instanceof Error ? error.message : '发生未知错误'
  }

  async function checkSession() {
    try {
      session.value = await api.session()
      setCsrfToken(session.value.csrf_token)
      await refresh(true)
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        notify('控制端连接失败', 'danger', apiMessage(error))
      }
      session.value = null
    } finally {
      booting.value = false
    }
  }

  async function login(username: string, password: string) {
    session.value = await api.login(username, password)
    setCsrfToken(session.value.csrf_token)
    await refresh(true)
  }

  async function logout() {
    try {
      await api.logout()
    } finally {
      session.value = null
      setCsrfToken('')
    }
  }

  async function refresh(includeSlow = false, background = false) {
    if (!session.value) return
    if (refreshing.value) {
      if (background) return
      while (refreshing.value) {
        await new Promise<void>((resolve) => setTimeout(resolve, 25))
      }
      if (!session.value) return
    }
    refreshing.value = true
    if (!background) loading.value = true
    try {
      const requests = [
        api.uiState(),
        api.nodes(),
        api.jobs(),
        api.operations(500),
        api.enrollments(),
        api.reconciliationOperations(),
      ] as const
      const [stateDoc, nodeDoc, jobDoc, operationDoc, enrollmentDoc, reconciliationDoc] =
        await Promise.all(requests)
      if (!lastRefreshAt.value || stateDoc.revision > stateRevision.value) {
        stateRevision.value = stateDoc.revision
        ui.value = normalizedState(stateDoc.state)
      }
      nodes.value = nodeDoc.items
      jobs.value = jobDoc.items
      operations.value = operationDoc.items
      const listedOperationIds = new Set(operations.value.map((item) => item.id))
      for (const operation of reconciliationDoc.items) {
        if (!listedOperationIds.has(operation.id)) operations.value.push(operation)
      }
      enrollments.value = enrollmentDoc.items
      const recovery = recoverOrphanOperations(reconciliationDoc.items)
      const imported = processInventoryJobs(ui.value, nodes.value, jobs.value)
      if (
        (imported.changed || recovery.recovered > 0 || recovery.orphanOperationIds.length > 0) &&
        canOperate.value
      ) {
        try {
          const saved = await api.saveUiState(
            stateRevision.value,
            ui.value,
            recovery.orphanOperationIds,
          )
          stateRevision.value = saved.revision
          ui.value = normalizedState(saved.state)
          if (recovery.orphanOperationIds.length) {
            notify(
              '已释放无法关联的历史任务',
              'warning',
              `${recovery.orphanOperationIds.length} 条终态任务找不到对应资源；已记录审计，请核对节点实际状态。`,
            )
          }
          if (imported.failures) {
            notify(
              '部分节点扫描失败',
              'warning',
              imported.failureMessages.slice(0, 2).join('；') ||
                '已导入成功结果；失败原因可在“执行记录”中查看。',
            )
          } else if (imported.configurations || imported.certificates) {
            notify(
              '节点清单已同步',
              'success',
              [
                imported.configurations ? `${imported.configurations} 份配置` : '',
                imported.certificates ? `${imported.certificates} 张证书` : '',
                imported.skipped ? `跳过 ${imported.skipped} 项` : '',
                imported.truncated ? '结果达到安全上限' : '',
              ]
                .filter(Boolean)
                .join(' · '),
            )
          }
        } catch (error) {
          if (error instanceof ApiError && error.status === 409) {
            const fresh = await api.uiState()
            stateRevision.value = fresh.revision
            ui.value = normalizedState(fresh.state)
          } else {
            throw error
          }
        }
      }
      await reconcilePendingOperations()
      if (!selectedSiteId.value && sites.value.length) selectedSiteId.value = sites.value[0].id
      if (!selectedCertificateId.value && certificates.value.length) {
        selectedCertificateId.value = certificates.value[0].id
      }
      if (!selectedNodeId.value && nodes.value.length) selectedNodeId.value = nodes.value[0].id
      if (includeSlow) {
        const [monitorDoc, auditDoc] = await Promise.all([api.monitoringSummary(), api.audit()])
        monitoring.value = monitorDoc.items
        audit.value = auditDoc.items
      }
      lastRefreshAt.value = new Date()
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) session.value = null
      throw error
    } finally {
      refreshing.value = false
      if (!background) loading.value = false
    }
  }

  function recoverOrphanOperations(pendingOperations: OperationRecord[]) {
    let recovered = 0
    const orphanOperationIds: string[] = []
    for (const operation of pendingOperations) {
      const terminal = !['queued', 'running'].includes(operation.status)
      const metadata = operation.metadata || {}
      const rawJobs = Array.isArray(metadata.reconcile_jobs) ? metadata.reconcile_jobs : []
      if (Number(metadata.reconcile_version || 0) !== 1 || !rawJobs.length) {
        if (terminal) orphanOperationIds.push(operation.id)
        continue
      }
      const recoveryJobs = rawJobs
        .filter((item) => item && typeof item === 'object')
        .map((item) => item as Record<string, unknown>)
      if (!recoveryJobs.length) {
        if (terminal) orphanOperationIds.push(operation.id)
        continue
      }

      if (operation.site_id.startsWith('certificate:')) {
        const certificateId = operation.site_id.slice('certificate:'.length)
        const certificate = certificates.value.find((item) => item.id === certificateId)
        if (!certificate) {
          if (terminal) orphanOperationIds.push(operation.id)
          continue
        }
        if (certificate.pendingRemote) continue
        certificate.pendingRemote = {
          operationId: operation.id,
          recovered: true,
          jobs: recoveryJobs.map((job) => ({
            id: String(job.id || ''),
            nodeId: String(job.node_id || ''),
            certificatePath: String(job.certificate_path || ''),
            keyPath: String(job.private_key_path || ''),
          })),
        }
        if (!terminal) certificate.status = 'replacing'
        recovered += 1
        continue
      }

      const site = sites.value.find((item) => item.id === operation.site_id)
      if (!site) {
        if (terminal) orphanOperationIds.push(operation.id)
        continue
      }
      if (site.pendingRemote) continue
      const transferTargets = Array.isArray(metadata.transfer_targets)
        ? (metadata.transfer_targets as Array<Record<string, unknown>>)
        : []
      const action = String(recoveryJobs[0]?.action || '')
      const operationKind =
        operation.kind === 'validate' && action === 'nginx_reload' ? 'reload' : operation.kind
      const baseStatus =
        operationKind === 'reload'
          ? 'published'
          : site.status === 'publishing'
            ? 'published'
            : site.status
      site.pendingRemote = {
        operationId: operation.id,
        operation: operationKind,
        publish: operation.kind === 'publish',
        baseStatus,
        recovered: true,
        targetNodeIds: recoveryJobs.map((job) => String(job.node_id || '')).filter(Boolean),
        jobs: recoveryJobs.map((job) => {
          const nodeId = String(job.node_id || '')
          const target = transferTargets.find((item) => String(item.node_id || '') === nodeId)
          return {
            id: String(job.id || ''),
            nodeId,
            candidateHash: String(job.new_sha256 || ''),
            path: String(target?.path || job.target_path || job.path || ''),
            entryId: String(target?.entry_id || ''),
            migration: Boolean(target?.migration),
          }
        }),
      }
      if (!terminal) site.status = 'publishing'
      recovered += 1
    }
    return { recovered, orphanOperationIds: [...new Set(orphanOperationIds)] }
  }
  async function reconcilePendingOperations() {
    if (!canOperate.value) return
    const stateBeforeReconcile = clonePlain(ui.value)
    const reconciledOperationIds: string[] = []
    const missingOperationIds: string[] = []
    let changed = false
    let processedResourceCount = 0
    for (const site of sites.value) {
      if (processedResourceCount >= MAX_RECONCILE_BATCH) break
      const pending = site.pendingRemote as
        | {
            operationId?: string
            operation?: string
            publish?: boolean
            baseStatus?: string
            recovered?: boolean
            targetNodeIds?: string[]
            jobs?: Array<{
              id: string
              nodeId: string
              candidateHash?: string
              path?: string
              entryId?: string
              migration?: boolean
              content?: string
            }>
          }
        | undefined
      if (!pending?.operationId) continue
      let detail: Awaited<ReturnType<typeof api.operation>>
      try {
        detail = await api.operation(pending.operationId)
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          site.updatedAt = new Date().toISOString()
          site.status =
            pending.operation === 'validate'
              ? pending.baseStatus || site.status || 'published'
              : 'failed'
          site.lastFailure = {
            summary: '操作记录已不存在，已释放待处理状态；请核对节点上的实际配置。',
            stage: 'operation_missing',
            node: '',
            operationId: pending.operationId,
          }
          missingOperationIds.push(pending.operationId)
          delete site.pendingRemote
          processedResourceCount += 1
          changed = true
          continue
        }
        if (isRetryableApiError(error)) continue
        ui.value = stateBeforeReconcile
        throw error
      }
      const operation = detail.operation
      if (['queued', 'running'].includes(operation.status)) continue

      const operationJobs = detail.jobs
      const jobById = new Map(operationJobs.map((item) => [item.id, item]))
      const pendingJobs = pending.jobs || []
      if (
        pendingJobs.some((item) => {
          const job = jobById.get(item.id)
          return !job || ['queued', 'running'].includes(job.status)
        })
      ) continue
      const successfulItems = pendingJobs.filter(
        (item) => jobById.get(item.id)?.status === 'succeeded',
      )
      const allSucceeded =
        operation.status === 'succeeded' && successfulItems.length === pendingJobs.length
      site.nodeHashes ||= {}
      site.nodeConfigPaths ||= {}
      site.nodeConfigEntryIds ||= {}
      site.nodeConfigs ||= {}

      if (pending.operation === 'delete') {
        const removed = new Set(successfulItems.map((item) => item.nodeId))
        site.nodeIds = site.nodeIds.filter((nodeId) => !removed.has(nodeId))
        for (const nodeId of removed) {
          delete site.nodeHashes[nodeId]
          delete site.nodeConfigPaths[nodeId]
          delete site.nodeConfigs[nodeId]
          delete site.nodeConfigEntryIds[nodeId]
        }
      } else if (pending.operation === 'transfer') {
        const certificate = site.certificateId
          ? certificates.value.find((item) => item.id === site.certificateId)
          : undefined
        for (const item of successfulItems) {
          const job = jobById.get(item.id)
          const hash = String(job?.result?.config_hash || item.candidateHash || '')
          if (hash) site.nodeHashes[item.nodeId] = hash
          if (item.path) site.nodeConfigPaths[item.nodeId] = item.path
          if (item.entryId) site.nodeConfigEntryIds[item.nodeId] = item.entryId
          const node = nodes.value.find((candidate) => candidate.id === item.nodeId)
          const expected = node ? configForCertificateNode(site.config, certificate, node) : site.config
          if (item.content && item.content !== expected) site.nodeConfigs[item.nodeId] = item.content
          else delete site.nodeConfigs[item.nodeId]
          if (!site.nodeIds.includes(item.nodeId)) site.nodeIds.push(item.nodeId)
        }
      } else if (pending.publish) {
        for (const item of successfulItems) {
          if (item.candidateHash) site.nodeHashes[item.nodeId] = item.candidateHash
          delete site.nodeConfigs[item.nodeId]
        }
        if (allSucceeded) site.version = Number(site.version || 0) + 1
      }

      site.updatedAt = new Date().toISOString()
      if (allSucceeded) {
        site.status =
          pending.operation === 'delete' && !site.nodeIds.length
            ? 'unassigned'
            : pending.publish || ['delete', 'transfer'].includes(pending.operation || '')
              ? 'published'
              : pending.baseStatus || site.status || 'published'
        if (pending.operation === 'transfer' && pending.recovered) {
          site.status = 'drift'
          site.changeNote = '已恢复配置迁移任务；请扫描节点配置确认实际正文'
        }
        delete site.lastFailure
      } else {
        const failed = operationJobs.find((item) => item.status !== 'succeeded')
        const result = failed?.result || {}
        site.status =
          pending.operation === 'validate'
            ? pending.baseStatus || site.status || 'published'
            : 'failed'
        site.lastFailure = {
          summary: String(result.error || result.output || 'Agent 未完成本次操作'),
          stage: String(result.failure_stage || operation.kind || 'operation'),
          node: failed?.node_name || failed?.node_id || '',
          operationId: operation.id,
        }
      }
      reconciledOperationIds.push(operation.id)
      delete site.pendingRemote
      processedResourceCount += 1
      changed = true
    }

    for (const certificate of certificates.value) {
      if (processedResourceCount >= MAX_RECONCILE_BATCH) break
      const pending = certificate.pendingRemote as
        | {
            operationId?: string
            jobs?: Array<{
              id: string
              nodeId: string
              certificatePath?: string
              keyPath?: string
            }>
          }
        | undefined
      if (!pending?.operationId) continue
      let detail: Awaited<ReturnType<typeof api.operation>>
      try {
        detail = await api.operation(pending.operationId)
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          certificate.status = 'failed'
          certificate.lastFailure = {
            summary: '操作记录已不存在，已释放待处理状态；请核对节点上的实际证书。',
            node: '',
            operationId: pending.operationId,
          }
          missingOperationIds.push(pending.operationId)
          delete certificate.pendingRemote
          processedResourceCount += 1
          changed = true
          continue
        }
        if (isRetryableApiError(error)) continue
        ui.value = stateBeforeReconcile
        throw error
      }
      const operation = detail.operation
      if (['queued', 'running'].includes(operation.status)) continue

      const operationJobs = detail.jobs
      const jobById = new Map(operationJobs.map((item) => [item.id, item]))
      const pendingJobs = pending.jobs || []
      if (
        pendingJobs.some((item) => {
          const job = jobById.get(item.id)
          return !job || ['queued', 'running'].includes(job.status)
        })
      ) continue
      certificate.nodeHashes ||= {}
      certificate.nodePaths ||= {}
      for (const item of pending.jobs || []) {
        const job = jobById.get(item.id)
        if (!job || job.status !== 'succeeded') continue
        const result = job.result || {}
        const certificateHash = String(result.certificate_sha256 || '')
        const keyHash = String(result.key_material_sha256 || result.private_key_sha256 || '')
        const knownHashes = certificate.nodeHashes[item.nodeId] || {}
        certificate.nodeHashes[item.nodeId] = {
          certificateHash: certificateHash || knownHashes.certificateHash,
          keyHash: keyHash || knownHashes.keyHash,
        }
        const certificatePath = String(result.certificate_path || item.certificatePath || '')
        const keyPath = String(result.private_key_path || item.keyPath || '')
        if (certificatePath && keyPath) {
          certificate.nodePaths[item.nodeId] = { certificatePath, keyPath }
        }
        if (!certificate.nodeIds.includes(item.nodeId)) certificate.nodeIds.push(item.nodeId)
        certificate.fingerprint =
          String(result.certificate_fingerprint || certificate.fingerprint || '') || undefined
        certificate.issuer =
          String(result.certificate_issuer || certificate.issuer || '') || undefined
        certificate.expiresAt =
          String(result.certificate_not_after || certificate.expiresAt || '') || undefined
      }

      if (operation.status === 'succeeded') {
        certificate.status = 'normal'
        delete certificate.lastFailure
      } else {
        const failed = operationJobs.find((item) => item.status !== 'succeeded')
        certificate.status = 'failed'
        certificate.lastFailure = {
          summary: String(
            failed?.result?.error || failed?.result?.output || 'Agent 未完成证书替换',
          ),
          node: failed?.node_name || failed?.node_id || '',
          operationId: operation.id,
        }
      }
      reconciledOperationIds.push(operation.id)
      delete certificate.pendingRemote
      processedResourceCount += 1
      changed = true
    }

    if (changed) {
      const attemptedRevision = stateRevision.value
      const desiredState = clonePlain(ui.value)
      try {
        const response = await api.saveUiState(
          attemptedRevision,
          desiredState,
          reconciledOperationIds,
        )
        stateRevision.value = response.revision
        ui.value = normalizedState(response.state)
      } catch (error) {
        if (isPermanentApiError(error)) {
          ui.value = stateBeforeReconcile
          notify('对账结果无法保存', 'danger', apiMessage(error))
          throw error
        }
        try {
          const fresh = await api.uiState()
          const committed =
            fresh.revision > attemptedRevision && sameUiState(fresh.state, desiredState)
          stateRevision.value = fresh.revision
          ui.value = normalizedState(fresh.state)
          if (!committed) return
        } catch {
          ui.value = stateBeforeReconcile
          throw error
        }
      }
      if (missingOperationIds.length) {
        notify(
          '已释放缺失的操作记录',
          'warning',
          `${missingOperationIds.length} 条记录无法读取；已保留失败说明，请核对节点实际状态。`,
        )
      }
    }
  }
  async function persistRemoteState(apply: () => void, successMessage: string) {
    const stateBeforeApply = clonePlain(ui.value)
    let lastError: unknown = new Error('远端任务状态保存失败')
    for (let attempt = 0; attempt < 3; attempt += 1) {
      apply()
      const attemptedRevision = stateRevision.value
      const desiredState = clonePlain(ui.value)
      try {
        const response = await api.saveUiState(attemptedRevision, desiredState)
        stateRevision.value = response.revision
        ui.value = normalizedState(response.state)
        notify(successMessage, 'success')
        return
      } catch (error) {
        lastError = error
        if (isPermanentApiError(error)) {
          try {
            const fresh = await api.uiState()
            stateRevision.value = fresh.revision
            ui.value = normalizedState(fresh.state)
          } catch {
            ui.value = stateBeforeApply
            lastRefreshAt.value = null
          }
          notify(
            '远端任务已创建，但状态保存被拒绝',
            'danger',
            `${apiMessage(error)}；任务可由后续刷新从服务端恢复。`,
          )
          throw error
        }
        try {
          const fresh = await api.uiState()
          const committed =
            fresh.revision > attemptedRevision && sameUiState(fresh.state, desiredState)
          stateRevision.value = fresh.revision
          ui.value = normalizedState(fresh.state)
          if (committed) {
            notify(successMessage, 'success')
            return
          }
        } catch {
          if (attempt === 2) {
            ui.value = stateBeforeApply
            lastRefreshAt.value = null
            throw error
          }
        }
      }
    }
    ui.value = stateBeforeApply
    lastRefreshAt.value = null
    try {
      await refresh(false, true)
    } catch {
      // The server-side operation metadata remains the recovery source.
    }
    throw lastError
  }
  async function saveUiState(successMessage = '已保存') {
    if (!canOperate.value) throw new Error('当前账号只有查看权限')
    const attemptedRevision = stateRevision.value
    const desiredState = clonePlain(ui.value)
    try {
      const response = await api.saveUiState(attemptedRevision, desiredState)
      stateRevision.value = response.revision
      ui.value = normalizedState(response.state)
      if (successMessage) notify(successMessage, 'success')
      return true
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        const body = error.body as {
          detail?: { revision?: number; state?: UiState }
          revision?: number
          state?: UiState
        }
        const authoritative =
          body.detail && typeof body.detail === 'object' ? body.detail : body
        if (typeof authoritative.revision === 'number' && authoritative.state) {
          stateRevision.value = authoritative.revision
          ui.value = normalizedState(authoritative.state)
        } else {
          const fresh = await api.uiState()
          stateRevision.value = fresh.revision
          ui.value = normalizedState(fresh.state)
        }
        notify('保存冲突，已加载服务器最新内容', 'warning', '请核对后重新提交。')
        return false
      }
      try {
        const fresh = await api.uiState()
        const committed =
          fresh.revision > attemptedRevision && sameUiState(fresh.state, desiredState)
        stateRevision.value = fresh.revision
        ui.value = normalizedState(fresh.state)
        if (committed) {
          if (successMessage) notify(successMessage, 'success')
          return true
        }
      } catch {
        lastRefreshAt.value = null
      }
      throw error
    }
  }
  async function upsertSite(site: SiteRecord) {
    const index = sites.value.findIndex((item) => item.id === site.id)
    if (index >= 0) sites.value[index] = site
    else sites.value.unshift(site)
    selectedSiteId.value = site.id
    return saveUiState(index >= 0 ? '站点草稿已保存' : '站点草稿已创建')
  }

  async function removeSiteRecord(siteId: string) {
    const site = sites.value.find((item) => item.id === siteId)
    if (!site) return false
    if (site.nodeIds.length) throw new Error('请先从全部节点移除配置，再删除平台记录')
    ui.value.sites = sites.value.filter((item) => item.id !== siteId)
    selectedSiteId.value = ui.value.sites[0]?.id || ''
    return saveUiState('平台站点记录已删除')
  }

  async function upsertCertificate(certificate: CertificateRecord) {
    const index = certificates.value.findIndex((item) => item.id === certificate.id)
    if (index >= 0) certificates.value[index] = certificate
    else certificates.value.unshift(certificate)
    selectedCertificateId.value = certificate.id
    return saveUiState(index >= 0 ? '证书信息已更新' : '证书记录已创建')
  }

  async function decideEnrollment(id: string, decision: 'approve' | 'reject') {
    await api.decideEnrollment(id, decision)
    notify(decision === 'approve' ? '节点已批准接入' : '接入申请已拒绝', 'success')
    await refresh()
  }

  async function quickNodeAction(nodeId: string, action: 'inspect' | 'nginx_test' | 'nginx_reload' | 'config_inventory' | 'certificate_inventory') {
    await api.createJobs([nodeId], action)
    const labels: Record<string, string> = {
      inspect: '节点探测',
      nginx_test: 'Nginx 校验',
      nginx_reload: 'Nginx reload',
      config_inventory: '配置扫描',
      certificate_inventory: '证书扫描',
    }
    notify(`${labels[action]}任务已提交`, 'success')
    await refresh()
  }

  async function scanInventory(action: 'config_inventory' | 'certificate_inventory') {
    const targetIds = nodes.value.filter((node) => node.status !== 'offline').map((node) => node.id)
    if (!targetIds.length) throw new Error('没有在线 Agent')
    await api.createJobs(targetIds, action)
    notify(action === 'config_inventory' ? '配置扫描任务已提交' : '证书扫描任务已提交', 'success')
    await refresh()
  }

  async function applyCertificate(
    certificate: CertificateRecord,
    nodeIds: string[],
    certificatePem: string,
    privateKeyPem: string,
  ) {
    if (!canOperate.value) throw new Error('当前账号只有查看权限')
    if (!certificatePem.includes('-----BEGIN CERTIFICATE-----')) throw new Error('证书内容不是 PEM 格式')
    if (!privateKeyPem.includes('PRIVATE KEY-----')) throw new Error('私钥内容不是 PEM 格式')
    const targetNodes = nodeIds
      .map((id) => nodes.value.find((item) => item.id === id))
      .filter(Boolean) as NodeRecord[]
    if (!targetNodes.length) throw new Error('请选择至少一个在线节点')
    const jobSpecs: Array<Record<string, unknown>> = []
    for (const node of targetNodes) {
      if (node.status === 'offline') throw new Error(`${node.node_name} 当前离线`)
      const stored = certificate.nodePaths?.[node.id]
      const knownHashes = certificate.nodeHashes?.[node.id]
      if (
        stored &&
        (!knownHashes?.certificateHash || !knownHashes?.keyHash)
      ) {
        throw new Error(`${node.node_name} 缺少旧证书校验值，请先扫描节点证书`)
      }
      const root = String(node.facts.managed_certificate_root || '/etc/nginx/ssl/nginx-manager').replace(/\/+$/, '')
      const name = safeName(certificate.domain)
      const certificatePath = stored?.certificatePath || `${root}/${name}.crt`
      const keyPath = stored?.keyPath || `${root}/${name}.key`
      jobSpecs.push({
        node_id: node.id,
        action: 'certificate_apply',
        payload: {
          certificate: {
            path: certificatePath,
            pem: certificatePem,
            expected_sha256: knownHashes?.certificateHash || 'missing',
          },
          private_key: {
            path: keyPath,
            pem: privateKeyPem,
            expected_sha256: knownHashes?.keyHash || 'missing',
          },
          expected_domain: certificate.domain,
          reload: true,
        },
      })
    }
    const response = await api.createOperation({
      request_id: uid('operation'),
      site_id: `certificate:${certificate.id}`,
      kind: 'certificate',
      base_version: 0,
      candidate: {
        id: certificate.id,
        domain: certificate.domain,
        nodeIds,
        note: certificate.note || '',
      },
      jobs: jobSpecs,
      ttl_seconds: 300,
    })
    const pendingRemote = {
      operationId: response.operation.id,
      jobs: response.jobs.map((job) => {
        const spec = jobSpecs.find((item) => item.node_id === job.node_id)
        const payload = spec?.payload as Record<string, unknown> | undefined
        const certificateTarget = payload?.certificate as Record<string, unknown> | undefined
        const keyTarget = payload?.private_key as Record<string, unknown> | undefined
        return {
          id: job.id,
          nodeId: job.node_id,
          certificatePath: String(certificateTarget?.path || ''),
          keyPath: String(keyTarget?.path || ''),
        }
      }),
    }
    await persistRemoteState(() => {
      const current = certificates.value.find((item) => item.id === certificate.id)
      if (!current) throw new Error('证书记录不存在')
      current.pendingRemote = pendingRemote
      current.status = 'replacing'
    }, '证书替换任务已提交')
  }

  async function runSite(siteId: string, publish: boolean) {
    if (!canOperate.value) throw new Error('当前账号只有查看权限')
    let site = sites.value.find((item) => item.id === siteId)
    if (!site) throw new Error('站点不存在')
    if (site.pendingRemote) throw new Error('该站点已有任务执行中')
    const targets = site.nodeIds.map((id) => nodes.value.find((item) => item.id === id)).filter(Boolean) as NodeRecord[]
    if (!targets.length) throw new Error('请先选择至少一个部署节点')
    if (targets.some((node) => node.status === 'offline')) throw new Error('目标节点中存在离线 Agent')

    const saved = await saveUiState('')
    if (!saved) return
    site = sites.value.find((item) => item.id === siteId)
    if (!site) return
    const certificate = site.certificateId
      ? certificates.value.find((item) => item.id === site?.certificateId)
      : undefined
    if (site.certificateId && !certificate) throw new Error('所选证书已不存在，请重新绑定')

    const jobsToCreate: Array<Record<string, unknown>> = []
    const pendingJobs: Array<{ id: string; nodeId: string; candidateHash: string }> = []
    let unchanged = 0
    for (const node of targets) {
      const knownHash = site.nodeHashes?.[node.id] || ''
      if (!knownHash && site.version > 0) {
        throw new Error(`${node.node_name} 缺少当前配置 Hash，请先重新扫描节点配置`)
      }
      const content = configForCertificateNode(String(site.config || ''), certificate, node)
      const candidateHash = await sha256(content)
      if (publish && knownHash && knownHash.toLowerCase() === candidateHash) {
        unchanged += 1
        continue
      }
      jobsToCreate.push({
        node_id: node.id,
        action: 'config_apply',
        payload: {
          path: managedConfigPath(site, node),
          content,
          expected_sha256: knownHash || 'missing',
          new_sha256: candidateHash,
          validate_only: !publish,
          reload: publish,
        },
      })
    }

    if (publish && unchanged === targets.length) {
      const candidate = clonePlain(site)
      delete candidate.pendingRemote
      delete candidate.lastFailure
      const response = await api.createOperation({
        request_id: uid('operation'),
        site_id: site.id,
        kind: 'validate',
        base_version: Number(site.version || 0),
        candidate,
        jobs: targets.map((node) => ({ node_id: node.id, action: 'nginx_reload', payload: {} })),
        ttl_seconds: 300,
      })
      const pendingRemote = {
        operationId: response.operation.id,
        operation: 'reload',
        publish: false,
        baseStatus: 'published',
        jobs: response.jobs.map((job) => ({
          id: job.id,
          nodeId: job.node_id,
          candidateHash: site?.nodeHashes?.[job.node_id] || '',
        })),
      }
      await persistRemoteState(() => {
        const current = sites.value.find((item) => item.id === siteId)
        if (!current) throw new Error('站点不存在')
        current.pendingRemote = pendingRemote
        current.status = 'publishing'
      }, `配置未变化，已提交 nginx -t 和 reload；版本保持 v${site.version}`)
      return
    }
    if (!jobsToCreate.length) throw new Error('没有需要提交的目标任务')

    const candidate = clonePlain(site)
    delete candidate.pendingRemote
    delete candidate.lastFailure
    const response = await api.createOperation({
      request_id: uid('operation'),
      site_id: site.id,
      kind: publish ? 'publish' : 'validate',
      base_version: Number(site.version || 0),
      candidate,
      jobs: jobsToCreate,
      ttl_seconds: 300,
    })
    for (const job of response.jobs) {
      const spec = jobsToCreate.find((item) => item.node_id === job.node_id)
      const payload = spec?.payload as Record<string, unknown> | undefined
      pendingJobs.push({
        id: job.id,
        nodeId: job.node_id,
        candidateHash: String(payload?.new_sha256 || ''),
      })
    }
    const pendingRemote = {
      operationId: response.operation.id,
      operation: publish ? 'publish' : 'validate',
      publish,
      baseStatus: site.status,
      jobs: pendingJobs,
    }
    await persistRemoteState(() => {
      const current = sites.value.find((item) => item.id === siteId)
      if (!current) throw new Error('站点不存在')
      current.pendingRemote = pendingRemote
      if (publish) current.status = 'publishing'
    }, publish ? '发布任务已提交' : '逐节点校验已提交')
  }

  async function removeSiteFromNodes(siteId: string, nodeIds: string[]) {
    if (!canOperate.value) throw new Error('当前账号只有查看权限')
    let site = sites.value.find((item) => item.id === siteId)
    if (!site) throw new Error('站点不存在')
    if (site.pendingRemote) throw new Error('该站点已有任务执行中')
    const targets = nodeIds
      .map((id) => nodes.value.find((item) => item.id === id))
      .filter(Boolean) as NodeRecord[]
    if (!targets.length) throw new Error('没有可移除的节点')
    const saved = await saveUiState('')
    if (!saved) return
    site = sites.value.find((item) => item.id === siteId)
    if (!site) return
    const jobSpecs = targets.map((node) => {
      const knownHash = site?.nodeHashes?.[node.id]
      if (!knownHash) throw new Error(`${node.node_name} 缺少当前配置 Hash，无法安全删除`)
      return {
        node_id: node.id,
        action: 'config_delete',
        payload: {
          path: managedConfigPath(site as SiteRecord, node),
          expected_sha256: knownHash,
          reload: true,
        },
      }
    })
    const response = await api.createOperation({
      request_id: uid('operation'),
      site_id: site.id,
      kind: 'delete',
      base_version: Number(site.version || 0),
      candidate: { targetNodeIds: nodeIds },
      jobs: jobSpecs,
      ttl_seconds: 300,
    })
    const pendingRemote = {
      operationId: response.operation.id,
      operation: 'delete',
      publish: false,
      baseStatus: site.status,
      targetNodeIds: nodeIds,
      jobs: response.jobs.map((job) => ({ id: job.id, nodeId: job.node_id })),
    }
    await persistRemoteState(() => {
      const current = sites.value.find((item) => item.id === siteId)
      if (!current) throw new Error('站点不存在')
      current.pendingRemote = pendingRemote
      current.status = 'publishing'
    }, '安全移除任务已提交')
  }

  async function transferSite(
    siteId: string,
    targets: Array<{ nodeId: string; entryId: string }>,
    mode: 'create' | 'replace',
  ) {
    if (!canOperate.value) throw new Error('当前账号只有查看权限')
    let site = sites.value.find((item) => item.id === siteId)
    if (!site) throw new Error('配置不存在')
    if (site.context === 'main') throw new Error('nginx.conf 不支持复制或迁移')
    if (site.pendingRemote) throw new Error('该配置已有任务执行中')
    if (!targets.length) throw new Error('请选择至少一个目标节点')
    const saved = await saveUiState('')
    if (!saved) return
    site = sites.value.find((item) => item.id === siteId)
    if (!site) return

    const certificate = site.certificateId
      ? certificates.value.find((item) => item.id === site?.certificateId)
      : undefined
    if (site.certificateId && !certificate) throw new Error('所选证书已不存在，请重新绑定')
    const jobs: Array<Record<string, unknown>> = []
    const candidateHashes = new Map<string, string>()
    const candidateContents = new Map<string, string>()
    const targetMetadata: Array<{
      node_id: string
      path: string
      entry_id: string
      migration: boolean
    }> = []
    for (const target of targets) {
      const node = nodes.value.find((item) => item.id === target.nodeId)
      if (!node) throw new Error('目标节点不存在')
      if (node.status === 'offline') throw new Error(`${node.node_name} 当前离线`)
      const path = configPathForEntry(site, node, target.entryId)
      const migration = site.nodeIds.includes(node.id)
      const sourceContent = migration ? site.nodeConfigs?.[node.id] || site.config : site.config
      const content = configForCertificateNode(sourceContent, certificate, node)
      const candidateHash = await sha256(content)
      candidateHashes.set(node.id, candidateHash)
      candidateContents.set(node.id, content)
      if (migration) {
        const sourcePath = managedConfigPath(site, node)
        const sourceHash = site.nodeHashes?.[node.id]
        if (!sourceHash) throw new Error(`${node.node_name} 缺少源配置 Hash，请先扫描`)
        if (!node.capabilities.includes('config_move')) {
          throw new Error(`${node.node_name} 的 Agent 尚不支持原子迁移`)
        }
        if (sourcePath === path) throw new Error(`${node.node_name} 的目标入口与当前入口相同`)
        jobs.push({
          node_id: node.id,
          action: 'config_move',
          payload: {
            source_path: sourcePath,
            target_path: path,
            content,
            expected_sha256: sourceHash,
            target_expected_sha256: mode === 'replace' ? 'present' : 'missing',
            reload: true,
          },
        })
      } else {
        jobs.push({
          node_id: node.id,
          action: 'config_apply',
          payload: {
            path,
            content,
            expected_sha256: mode === 'replace' ? 'present' : 'missing',
            new_sha256: candidateHash,
            validate_only: false,
            reload: true,
          },
        })
      }
      targetMetadata.push({
        node_id: node.id,
        path,
        entry_id: target.entryId,
        migration,
      })
    }
    const response = await api.createOperation({
      request_id: uid('operation'),
      site_id: site.id,
      kind: 'transfer',
      base_version: Number(site.version || 0),
      candidate: { mode, targets: targetMetadata },
      jobs,
      ttl_seconds: 300,
    })
    const pendingRemote = {
      operationId: response.operation.id,
      operation: 'transfer',
      publish: false,
      baseStatus: site.status,
      targetNodeIds: targets.map((item) => item.nodeId),
      jobs: response.jobs.map((job) => {
        const target = targetMetadata.find((item) => item.node_id === job.node_id)!
        return {
          id: job.id,
          nodeId: job.node_id,
          candidateHash: candidateHashes.get(job.node_id) || '',
          content: candidateContents.get(job.node_id) || '',
          path: target.path,
          entryId: target.entry_id,
          migration: target.migration,
        }
      }),
    }
    await persistRemoteState(() => {
      const current = sites.value.find((item) => item.id === siteId)
      if (!current) throw new Error('配置不存在')
      current.pendingRemote = pendingRemote
      current.status = 'publishing'
    }, '配置复制 / 迁移任务已提交')
  }

  async function revokeNode(nodeId: string) {
    if (!isAdmin.value) throw new Error('只有管理员可以吊销 Agent 身份')
    await api.revokeNode(nodeId)
    notify('Agent 身份已吊销', 'success')
    await refresh()
  }

  return {
    session,
    booting,
    loading,
    lastRefreshAt,
    stateRevision,
    ui,
    nodes,
    jobs,
    operations,
    enrollments,
    monitoring,
    audit,
    sites,
    certificates,
    selectedSiteId,
    selectedCertificateId,
    selectedNodeId,
    selectedSite,
    selectedCertificate,
    selectedNode,
    toasts,
    canOperate,
    isAdmin,
    onlineCount,
    riskyCertificateCount,
    unhealthyCount,
    notify,
    apiMessage,
    checkSession,
    login,
    logout,
    refresh,
    saveUiState,
    upsertSite,
    removeSiteRecord,
    upsertCertificate,
    decideEnrollment,
    quickNodeAction,
    scanInventory,
    applyCertificate,
    runSite,
    removeSiteFromNodes,
    transferSite,
    revokeNode,
  }
})
