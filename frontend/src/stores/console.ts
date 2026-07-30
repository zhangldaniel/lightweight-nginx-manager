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
import { processInventoryJobs } from '../utils/inventory'
import type {
  AuditRecord,
  CertificateRecord,
  Density,
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
    processedOperationIds: Array.isArray(value?.processedOperationIds)
      ? value.processedOperationIds
      : [],
  }
}

export const useConsoleStore = defineStore('console', () => {
  const session = ref<Session | null>(null)
  const booting = ref(true)
  const loading = ref(false)
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
  const density = ref<Density>(
    localStorage.getItem('nginx-manager-density') === 'compact' ? 'compact' : 'comfortable',
  )
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

  async function refresh(includeSlow = false) {
    if (!session.value) return
    loading.value = true
    try {
      const requests = [
        api.uiState(),
        api.nodes(),
        api.jobs(),
        api.operations(),
        api.enrollments(),
      ] as const
      const [stateDoc, nodeDoc, jobDoc, operationDoc, enrollmentDoc] = await Promise.all(requests)
      stateRevision.value = stateDoc.revision
      ui.value = normalizedState(stateDoc.state)
      nodes.value = nodeDoc.items
      jobs.value = jobDoc.items
      operations.value = operationDoc.items
      enrollments.value = enrollmentDoc.items
      const imported = processInventoryJobs(ui.value, nodes.value, jobs.value)
      if (imported.changed && canOperate.value) {
        try {
          const saved = await api.saveUiState(stateRevision.value, ui.value)
          stateRevision.value = saved.revision
          ui.value = normalizedState(saved.state)
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
      loading.value = false
    }
  }

  async function reconcilePendingOperations() {
    let changed = false
    for (const site of sites.value) {
      const pending = site.pendingRemote as
        | {
            operationId?: string
            operation?: string
            publish?: boolean
            baseStatus?: string
            targetNodeIds?: string[]
            jobs?: Array<{
              id: string
              nodeId: string
              candidateHash?: string
              path?: string
              entryId?: string
              migration?: boolean
            }>
          }
        | undefined
      if (!pending?.operationId) continue
      const operation = operations.value.find((item) => item.id === pending.operationId)
      if (!operation || ['queued', 'running'].includes(operation.status)) continue
      const operationJobs = jobs.value.filter((item) => item.operation_id === operation.id)
      const allSucceeded = operation.status === 'succeeded'
      if (allSucceeded && pending.operation === 'delete') {
        const removed = new Set(pending.targetNodeIds || [])
        site.nodeIds = site.nodeIds.filter((nodeId) => !removed.has(nodeId))
        for (const nodeId of removed) {
          delete site.nodeHashes?.[nodeId]
          delete site.nodeConfigPaths?.[nodeId]
          delete site.nodeConfigs?.[nodeId]
          delete site.nodeConfigEntryIds?.[nodeId]
        }
        site.status = site.nodeIds.length ? 'published' : 'unassigned'
        site.updatedAt = new Date().toISOString()
        delete site.lastFailure
      } else if (allSucceeded && pending.operation === 'transfer') {
        site.nodeHashes ||= {}
        site.nodeConfigPaths ||= {}
        site.nodeConfigEntryIds ||= {}
        site.nodeConfigs ||= {}
        for (const item of pending.jobs || []) {
          const job = operationJobs.find((candidate) => candidate.id === item.id)
          const hash = String(job?.result?.config_hash || item.candidateHash || '')
          if (hash) site.nodeHashes[item.nodeId] = hash
          if (item.path) site.nodeConfigPaths[item.nodeId] = item.path
          if (item.entryId) site.nodeConfigEntryIds[item.nodeId] = item.entryId
          delete site.nodeConfigs[item.nodeId]
          if (!site.nodeIds.includes(item.nodeId)) site.nodeIds.push(item.nodeId)
        }
        site.status = 'published'
        site.updatedAt = new Date().toISOString()
        delete site.lastFailure
      } else if (allSucceeded && pending.publish) {
        site.version = Number(site.version || 0) + 1
        site.status = 'published'
        site.updatedAt = new Date().toISOString()
        site.nodeHashes = site.nodeHashes || {}
        for (const item of pending.jobs || []) {
          if (item.candidateHash) site.nodeHashes[item.nodeId] = item.candidateHash
        }
        delete site.lastFailure
      } else if (allSucceeded) {
        site.status = pending.baseStatus || site.status || 'published'
        delete site.lastFailure
      } else {
        const failed = operationJobs.find((item) => item.status !== 'succeeded')
        const result = failed?.result || {}
        site.status = pending.publish ? 'failed' : pending.baseStatus || site.status || 'published'
        site.lastFailure = {
          summary: String(result.error || result.output || 'Agent 未完成本次操作'),
          stage: String(result.failure_stage || operation.kind || 'operation'),
          node: failed?.node_name || failed?.node_id || '',
          operationId: operation.id,
        }
      }
      delete site.pendingRemote
      changed = true
    }
    for (const certificate of certificates.value) {
      const pending = certificate.pendingRemote as
        | {
            operationId?: string
            jobs?: Array<{ id: string; nodeId: string }>
          }
        | undefined
      if (!pending?.operationId) continue
      const operation = operations.value.find((item) => item.id === pending.operationId)
      if (!operation || ['queued', 'running'].includes(operation.status)) continue
      const operationJobs = jobs.value.filter((item) => item.operation_id === operation.id)
      if (operation.status === 'succeeded') {
        certificate.nodeHashes ||= {}
        certificate.nodePaths ||= {}
        for (const item of pending.jobs || []) {
          const job = operationJobs.find((candidate) => candidate.id === item.id)
          if (!job || job.status !== 'succeeded') continue
          const result = job.result || {}
          certificate.nodeHashes[item.nodeId] = {
            certificateHash: String(result.certificate_sha256 || ''),
            keyHash: String(result.key_material_sha256 || result.private_key_sha256 || ''),
          }
          const certificatePath = String(result.certificate_path || '')
          const keyPath = String(result.private_key_path || '')
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
      delete certificate.pendingRemote
      changed = true
    }
    if (changed && canOperate.value) {
      try {
        const response = await api.saveUiState(stateRevision.value, ui.value)
        stateRevision.value = response.revision
        ui.value = normalizedState(response.state)
      } catch {
        // A concurrent UI save wins. The next refresh reloads the authoritative state.
      }
    }
  }

  async function saveUiState(successMessage = '已保存') {
    if (!canOperate.value) throw new Error('当前账号只有查看权限')
    try {
      const response = await api.saveUiState(stateRevision.value, ui.value)
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
    certificate.pendingRemote = {
      operationId: response.operation.id,
      jobs: response.jobs.map((job) => ({ id: job.id, nodeId: job.node_id })),
    }
    certificate.status = 'replacing'
    await saveUiState('证书替换任务已提交')
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

    const jobsToCreate: Array<Record<string, unknown>> = []
    const pendingJobs: Array<{ id: string; nodeId: string; candidateHash: string }> = []
    let unchanged = 0
    for (const node of targets) {
      const knownHash = site.nodeHashes?.[node.id] || ''
      if (!knownHash && site.version > 0) {
        throw new Error(`${node.node_name} 缺少当前配置 Hash，请先重新扫描节点配置`)
      }
      const content = String(site.config || '')
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
      await api.createJobs(
        targets.map((node) => node.id),
        'nginx_reload',
      )
      notify('配置未变化，已提交 nginx -t 和 reload', 'success', `版本保持 v${site.version}`)
      return
    }
    if (!jobsToCreate.length) throw new Error('没有需要提交的目标任务')

    const candidate = structuredClone(site)
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
    site.pendingRemote = {
      operationId: response.operation.id,
      operation: publish ? 'publish' : 'validate',
      publish,
      baseStatus: site.status,
      jobs: pendingJobs,
    }
    if (publish) site.status = 'publishing'
    await saveUiState(publish ? '发布任务已提交' : '逐节点校验已提交')
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
    site.pendingRemote = {
      operationId: response.operation.id,
      operation: 'delete',
      publish: false,
      baseStatus: site.status,
      targetNodeIds: nodeIds,
      jobs: response.jobs.map((job) => ({ id: job.id, nodeId: job.node_id })),
    }
    site.status = 'publishing'
    await saveUiState('安全移除任务已提交')
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

    const candidateHash = await sha256(site.config)
    const jobs: Array<Record<string, unknown>> = []
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
            content: site.nodeConfigs?.[node.id] || site.config,
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
            content: site.config,
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
    site.pendingRemote = {
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
          candidateHash,
          path: target.path,
          entryId: target.entry_id,
          migration: target.migration,
        }
      }),
    }
    site.status = 'publishing'
    await saveUiState('配置复制 / 迁移任务已提交')
  }

  async function revokeNode(nodeId: string) {
    if (!isAdmin.value) throw new Error('只有管理员可以吊销 Agent 身份')
    await api.revokeNode(nodeId)
    notify('Agent 身份已吊销', 'success')
    await refresh()
  }

  function setDensity(value: Density) {
    density.value = value
    localStorage.setItem('nginx-manager-density', value)
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
    density,
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
    setDensity,
  }
})
