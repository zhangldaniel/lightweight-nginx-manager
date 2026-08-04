import assert from 'node:assert/strict'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

const memoryStorage = new Map()
globalThis.localStorage = {
  getItem: (key) => memoryStorage.get(key) ?? null,
  setItem: (key, value) => memoryStorage.set(key, String(value)),
  removeItem: (key) => memoryStorage.delete(key),
}
globalThis.window = { setTimeout: () => 0 }

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const moduleServer = await createServer({
  root: projectRoot,
  logLevel: 'silent',
  appType: 'custom',
  server: { middlewareMode: true },
})

const clone = (value) => JSON.parse(JSON.stringify(value))

const now = new Date().toISOString()
const nodes = ['node-a', 'node-b'].map((id) => ({
  id,
  node_name: id,
  hostname: id,
  labels: {},
  status: 'online',
  reported_status: 'online',
  agent_version: 'qa',
  nginx_version: '1.26.3',
  config_hash: null,
  capabilities: ['config_apply'],
  facts: {},
  enrolled_at: now,
  last_seen_at: now,
  revoked_at: null,
}))
const partialPublish = {
  id: 'operation-partial-publish',
  site_id: 'site-a',
  kind: 'publish',
  status: 'partial',
  base_version: 1,
  candidate_revision_id: null,
  created_by: 'qa',
  created_at: now,
  updated_at: now,
  completed_at: now,
  metadata: {
    reconcile_version: 1,
    reconcile_already_completed: 1,
    reconcile_total_targets: 3,
    reconcile_jobs: [
      { id: 'publish-a', node_id: 'node-a', action: 'config_apply', new_sha256: 'new-a' },
      { id: 'publish-b', node_id: 'node-b', action: 'config_apply', new_sha256: 'new-b' },
    ],
  },
}
const partialCertificate = {
  id: 'operation-partial-certificate',
  site_id: 'certificate:cert-a',
  kind: 'certificate',
  status: 'partial',
  base_version: 0,
  candidate_revision_id: null,
  created_by: 'qa',
  created_at: now,
  updated_at: now,
  completed_at: now,
  metadata: {
    reconcile_version: 1,
    reconcile_jobs: [
      {
        id: 'certificate-a',
        node_id: 'node-a',
        action: 'certificate_apply',
        certificate_path: '/apps/nginx/cert/a.pem',
        private_key_path: '/apps/nginx/cert/a.key',
      },
      {
        id: 'certificate-b',
        node_id: 'node-b',
        action: 'certificate_apply',
        certificate_path: '/apps/nginx/cert/b.pem',
        private_key_path: '/apps/nginx/cert/b.key',
      },
    ],
  },
}
const job = (id, nodeId, status, result = {}) => ({
  id,
  batch_id: id,
  operation_id: id.startsWith('publish') ? partialPublish.id : partialCertificate.id,
  node_id: nodeId,
  node_name: nodeId,
  action: id.startsWith('publish') ? 'config_apply' : 'certificate_apply',
  status,
  created_at: now,
  expires_at: now,
  claimed_at: now,
  completed_at: now,
  created_by: 'qa',
  result,
})
const details = {
  [partialPublish.id]: {
    operation: partialPublish,
    jobs: [
      job('publish-a', 'node-a', 'succeeded', { config_hash: 'new-a' }),
      job('publish-b', 'node-b', 'failed', { error: 'nginx test failed' }),
    ],
  },
  [partialCertificate.id]: {
    operation: partialCertificate,
    jobs: [
      job('certificate-a', 'node-a', 'succeeded', {
        certificate_sha256: 'cert-new-a',
        key_material_sha256: 'key-new-a',
        certificate_path: '/apps/nginx/cert/a.pem',
        private_key_path: '/apps/nginx/cert/a.key',
      }),
      job('certificate-b', 'node-b', 'failed', { error: 'permission denied' }),
    ],
  },
}
let revision = 1
let state = {
  sites: [{
    id: 'site-a',
    domain: 'api.example.com',
    config: 'server { listen 80; }',
    nodeIds: ['node-a', 'node-b'],
    version: 1,
    status: 'published',
    nodeHashes: { 'node-a': 'old-a', 'node-b': 'old-b' },
    nodeConfigPaths: {},
    nodeConfigs: {},
    nodeConfigEntryIds: {},
  }],
  certificates: [{
    id: 'cert-a',
    domain: '*.example.com',
    nodeIds: ['node-b'],
    nodePaths: {
      'node-b': { certificatePath: '/old/b.pem', keyPath: '/old/b.key' },
    },
    nodeHashes: {
      'node-b': { certificateHash: 'old-cert-b', keyHash: 'old-key-b' },
    },
    status: 'normal',
  }],
  importedInventoryJobs: [],
  importedCertificateInventoryJobs: [],
  processedOperationIds: [],
}

try {
  const { api, ApiError } = await moduleServer.ssrLoadModule('/src/api.ts')
  const { useConsoleStore } = await moduleServer.ssrLoadModule('/src/stores/console.ts')
  api.uiState = async () => ({ revision, state: clone(state) })
  api.nodes = async () => ({ items: nodes })
  const acknowledgedOperationIds = new Set()
  const operationDetailCalls = []
  const saveCalls = []
  api.jobs = async () => ({ items: [] })
  api.operations = async () => ({ items: [partialPublish, partialCertificate] })
  api.reconciliationOperations = async () => ({
    items: [partialPublish, partialCertificate].filter(
      (operation) => !acknowledgedOperationIds.has(operation.id),
    ),
  })
  api.operation = async (id) => {
    operationDetailCalls.push(id)
    return clone(details[id])
  }
  api.enrollments = async () => ({ items: [] })
  api.saveUiState = async (expectedRevision, nextState, reconciledOperationIds = []) => {
    assert.equal(expectedRevision, revision)
    saveCalls.push([...reconciledOperationIds])
    for (const operationId of reconciledOperationIds) acknowledgedOperationIds.add(operationId)
    revision += 1
    state = clone(nextState)
    return { revision, state: clone(state), reconciled_operation_ids: [...reconciledOperationIds] }
  }

  setActivePinia(createPinia())
  const store = useConsoleStore()
  store.session = {
    authenticated: true,
    username: 'qa',
    role: 'admin',
    auth_source: 'local',
    csrf_token: 'qa',
    expires_at: null,
  }
  await store.refresh()

  const site = store.sites.find((item) => item.id === 'site-a')
  assert.equal(site.status, 'failed')
  assert.equal(site.version, 1, 'A partial publish must not consume the next version')
  assert.equal(site.nodeHashes['node-a'], 'new-a')
  assert.equal(site.nodeHashes['node-b'], 'old-b')
  assert.equal(site.pendingRemote, undefined)
  assert.equal(site.lastFailure?.completedNodes, 2)
  assert.equal(site.lastFailure?.totalNodes, 3)

  const certificate = store.certificates.find((item) => item.id === 'cert-a')
  assert.equal(certificate.status, 'failed')
  assert.equal(certificate.nodeHashes['node-a'].certificateHash, 'cert-new-a')
  assert.equal(certificate.nodePaths['node-a'].certificatePath, '/apps/nginx/cert/a.pem')
  assert.equal(certificate.nodeHashes['node-b'].certificateHash, 'old-cert-b')
  assert.equal(certificate.pendingRemote, undefined)

  assert.deepEqual(
    acknowledgedOperationIds,
    new Set([partialPublish.id, partialCertificate.id]),
    'Terminal reconciliation must atomically acknowledge the merged operations',
  )
  assert(
    saveCalls.some(
      (operationIds) =>
        operationIds.includes(partialPublish.id) && operationIds.includes(partialCertificate.id),
    ),
    'The terminal UI-state save did not include operation acknowledgements',
  )
  assert.equal(revision, 3, 'Recovery marker and terminal reconciliation should both persist')

  const saveCallCount = saveCalls.length
  const operationDetailCallCount = operationDetailCalls.length
  await store.refresh()
  assert.equal(
    operationDetailCalls.length,
    operationDetailCallCount,
    'An acknowledged operation was replayed during the next refresh',
  )
  assert.equal(
    saveCalls.length,
    saveCallCount,
    'A refresh without pending reconciliation unexpectedly rewrote UI state',
  )
  revision = 10
  const missingOperation = {
    ...partialPublish,
    id: 'operation-missing-detail',
    site_id: 'site-missing',
    status: 'succeeded',
    metadata: {
      reconcile_version: 1,
      reconcile_jobs: [
        { id: 'missing-job', node_id: 'node-a', action: 'config_apply', new_sha256: 'missing-new' },
      ],
    },
  }
  const succeedingOperation = {
    ...partialPublish,
    id: 'operation-after-missing',
    site_id: 'site-good',
    status: 'succeeded',
    metadata: {
      reconcile_version: 1,
      reconcile_jobs: [
        { id: 'good-job', node_id: 'node-b', action: 'config_apply', new_sha256: 'good-new' },
      ],
    },
  }
  const networkOperation = {
    ...partialPublish,
    id: 'operation-network-detail',
    site_id: 'site-network',
    status: 'succeeded',
    metadata: {
      reconcile_version: 1,
      reconcile_jobs: [
        { id: 'network-job', node_id: 'node-a', action: 'config_apply', new_sha256: 'network-new' },
      ],
    },
  }
  state = {
    sites: [
      {
        id: 'site-missing',
        domain: 'missing.example.com',
        config: 'server { listen 80; }',
        nodeIds: ['node-a'],
        version: 1,
        status: 'published',
        nodeHashes: { 'node-a': 'missing-old' },
        nodeConfigPaths: {},
        nodeConfigs: {},
        nodeConfigEntryIds: {},
      },
      {
        id: 'site-good',
        domain: 'good.example.com',
        config: 'server { listen 80; }',
        nodeIds: ['node-b'],
        version: 1,
        status: 'published',
        nodeHashes: { 'node-b': 'good-old' },
        nodeConfigPaths: {},
        nodeConfigs: {},
        nodeConfigEntryIds: {},
      },
      {
        id: 'site-network',
        domain: 'network.example.com',
        config: 'server { listen 80; }',
        nodeIds: ['node-a'],
        version: 1,
        status: 'published',
        nodeHashes: { 'node-a': 'network-old' },
        nodeConfigPaths: {},
        nodeConfigs: {},
        nodeConfigEntryIds: {},
      },
    ],
    certificates: [],
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
    processedOperationIds: [],
  }
  acknowledgedOperationIds.clear()
  let reconciliationQueue = [missingOperation, networkOperation, succeedingOperation]
  api.operations = async () => ({ items: clone(reconciliationQueue) })
  api.reconciliationOperations = async () => ({
    items: clone(
      reconciliationQueue.filter((operation) => !acknowledgedOperationIds.has(operation.id)),
    ),
  })
  const isolatedDetailCalls = []
  api.operation = async (id) => {
    isolatedDetailCalls.push(id)
    if (id === missingOperation.id) {
      reconciliationQueue = reconciliationQueue.filter((operation) => operation.id !== id)
      throw new ApiError('operation not found', 404, { detail: 'operation not found' })
    }
    if (id === networkOperation.id) throw new TypeError('network unavailable')
    return {
      operation: clone(succeedingOperation),
      jobs: [
        job('good-job', 'node-b', 'succeeded', { config_hash: 'good-new' }),
      ],
    }
  }

  setActivePinia(createPinia())
  const isolatedStore = useConsoleStore()
  isolatedStore.session = store.session
  await isolatedStore.refresh()
  const missingSite = isolatedStore.sites.find((item) => item.id === 'site-missing')
  const networkSite = isolatedStore.sites.find((item) => item.id === 'site-network')
  const goodSite = isolatedStore.sites.find((item) => item.id === 'site-good')
  assert.equal(missingSite.pendingRemote, undefined, 'A missing operation detail left a stuck pending marker')
  assert.equal(missingSite.status, 'failed', 'A missing operation detail was not made auditable')
  assert.equal(missingSite.lastFailure?.stage, 'operation_missing')
  assert.equal(missingSite.lastFailure?.operationId, missingOperation.id)
  assert.equal(goodSite.status, 'published', 'A 404 on one resource blocked a later reconciliation')
  assert.equal(goodSite.version, 2, 'The operation after a 404 was not reconciled')
  assert.equal(goodSite.nodeHashes['node-b'], 'good-new')
  assert.equal(networkSite.status, 'published')
  assert.equal(networkSite.version, 1)
  assert.equal(networkSite.pendingRemote.operationId, networkOperation.id)
  assert.deepEqual(
    isolatedDetailCalls,
    [missingOperation.id, succeedingOperation.id, networkOperation.id],
    'Operation detail failures were not isolated per resource',
  )
  assert(
    acknowledgedOperationIds.has(succeedingOperation.id),
    'The successful operation after a 404 was not acknowledged',
  )

  revision = 30
  const permanentOperation = {
    ...partialPublish,
    id: 'operation-permanent-save-error',
    site_id: 'site-permanent-save-error',
    status: 'succeeded',
    metadata: {
      reconcile_version: 1,
      reconcile_jobs: [
        { id: 'permanent-job', node_id: 'node-a', action: 'config_apply', new_sha256: 'permanent-new' },
      ],
    },
  }
  state = {
    sites: [{
      id: 'site-permanent-save-error',
      domain: 'permanent.example.com',
      config: 'server { listen 80; }',
      nodeIds: ['node-a'],
      version: 1,
      status: 'publishing',
      nodeHashes: { 'node-a': 'permanent-old' },
      nodeConfigPaths: {},
      nodeConfigs: {},
      nodeConfigEntryIds: {},
      pendingRemote: {
        operationId: permanentOperation.id,
        operation: 'publish',
        publish: true,
        baseStatus: 'published',
        jobs: [{ id: 'permanent-job', nodeId: 'node-a', candidateHash: 'permanent-new' }],
      },
    }],
    certificates: [],
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
    processedOperationIds: [],
  }
  api.operations = async () => ({ items: [clone(permanentOperation)] })
  api.reconciliationOperations = async () => ({ items: [clone(permanentOperation)] })
  api.operation = async () => ({
    operation: clone(permanentOperation),
    jobs: [job('permanent-job', 'node-a', 'succeeded', { config_hash: 'permanent-new' })],
  })
  let permanentSaveAttempts = 0
  api.saveUiState = async () => {
    permanentSaveAttempts += 1
    throw new ApiError('state rejected', 422, { detail: 'state rejected' })
  }
  setActivePinia(createPinia())
  const permanentStore = useConsoleStore()
  permanentStore.session = store.session
  await assert.rejects(permanentStore.refresh(), /state rejected/)
  const permanentSite = permanentStore.sites.find((item) => item.id === 'site-permanent-save-error')
  assert.equal(permanentSaveAttempts, 1, 'A permanent 4xx save error was retried')
  assert.equal(permanentSite.version, 1, 'A rejected reconciliation remained visible as saved')
  assert.equal(permanentSite.pendingRemote.operationId, permanentOperation.id)
  assert(
    permanentStore.toasts.some((toast) => toast.title === '对账结果无法保存'),
    'A permanent reconciliation save error was not surfaced to the operator',
  )

  revision = 40
  const responseLostOperation = {
    ...partialPublish,
    id: 'operation-response-lost',
    site_id: 'site-response-lost',
    status: 'succeeded',
    metadata: {
      reconcile_version: 1,
      reconcile_jobs: [
        { id: 'response-lost-job', node_id: 'node-a', action: 'config_apply', new_sha256: 'response-new' },
      ],
    },
  }
  state = {
    sites: [{
      id: 'site-response-lost',
      domain: 'response-lost.example.com',
      config: 'server { listen 80; }',
      nodeIds: ['node-a'],
      version: 1,
      status: 'publishing',
      nodeHashes: { 'node-a': 'response-old' },
      nodeConfigPaths: {},
      nodeConfigs: {},
      nodeConfigEntryIds: {},
      pendingRemote: {
        operationId: responseLostOperation.id,
        operation: 'publish',
        publish: true,
        baseStatus: 'published',
        jobs: [{ id: 'response-lost-job', nodeId: 'node-a', candidateHash: 'response-new' }],
      },
    }],
    certificates: [],
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
    processedOperationIds: [],
  }
  acknowledgedOperationIds.clear()
  api.operations = async () => ({ items: [clone(responseLostOperation)] })
  api.reconciliationOperations = async () => ({
    items: acknowledgedOperationIds.has(responseLostOperation.id) ? [] : [clone(responseLostOperation)],
  })
  api.operation = async () => ({
    operation: clone(responseLostOperation),
    jobs: [job('response-lost-job', 'node-a', 'succeeded', { config_hash: 'response-new' })],
  })
  let responseLostSaveAttempts = 0
  api.saveUiState = async (expectedRevision, nextState, reconciledOperationIds = []) => {
    assert.equal(expectedRevision, revision)
    responseLostSaveAttempts += 1
    revision += 1
    state = clone(nextState)
    for (const operationId of reconciledOperationIds) acknowledgedOperationIds.add(operationId)
    throw new TypeError('response lost after commit')
  }
  setActivePinia(createPinia())
  const responseLostStore = useConsoleStore()
  responseLostStore.session = store.session
  await responseLostStore.refresh()
  const responseLostSite = responseLostStore.sites.find((item) => item.id === 'site-response-lost')
  assert.equal(responseLostSaveAttempts, 1)
  assert.equal(responseLostSite.version, 2)
  assert.equal(responseLostSite.nodeHashes['node-a'], 'response-new')
  assert.equal(responseLostSite.pendingRemote, undefined)
  assert(acknowledgedOperationIds.has(responseLostOperation.id))

  revision = 50
  state = {
    sites: [{
      id: 'site-ordinary-save',
      domain: 'ordinary.example.com',
      config: 'server { listen 80; }',
      nodeIds: [],
      version: 0,
      status: 'draft',
    }],
    certificates: [],
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
    processedOperationIds: [],
  }
  api.operations = async () => ({ items: [] })
  api.reconciliationOperations = async () => ({ items: [] })
  let ordinarySaveAttempts = 0
  api.saveUiState = async () => {
    ordinarySaveAttempts += 1
    throw new ApiError('state rejected', 422, { detail: 'state rejected' })
  }
  setActivePinia(createPinia())
  const ordinaryStore = useConsoleStore()
  ordinaryStore.session = store.session
  await ordinaryStore.refresh()
  const editedSite = clone(ordinaryStore.sites[0])
  editedSite.config = 'server { listen 81; }'
  await assert.rejects(ordinaryStore.upsertSite(editedSite), /state rejected/)
  assert.equal(ordinarySaveAttempts, 1)
  assert.equal(
    ordinaryStore.sites[0].config,
    'server { listen 80; }',
    'A rejected ordinary save left unsaved state visible in the store',
  )

  api.saveUiState = async (expectedRevision, nextState, reconciledOperationIds = []) => {
    assert.equal(expectedRevision, revision)
    for (const operationId of reconciledOperationIds) acknowledgedOperationIds.add(operationId)
    revision += 1
    state = clone(nextState)
    return { revision, state: clone(state), reconciled_operation_ids: [...reconciledOperationIds] }
  }
  revision = 20
  const previewConfig = [
    'server {',
    '  listen 443 ssl;',
    '  server_name payload.example.com;',
    '  ssl_certificate /apps/nginx/cert/payload.pem;',
    '  ssl_certificate_key /apps/nginx/cert/payload.key;',
    '}',
  ].join('\n')
  state = {
    sites: [{
      id: 'site-payload',
      domain: 'payload.example.com',
      config: previewConfig,
      context: 'http',
      nodeIds: ['node-a', 'node-b'],
      certificateId: 'cert-payload',
      version: 0,
      status: 'draft',
      nodeHashes: {},
      nodeConfigPaths: {},
      nodeConfigs: {},
      nodeConfigEntryIds: {},
    }],
    certificates: [{
      id: 'cert-payload',
      domain: 'payload.example.com',
      nodeIds: ['node-a', 'node-b'],
      nodePaths: {
        'node-a': {
          certificatePath: '/apps/nginx/cert/payload.pem',
          keyPath: '/apps/nginx/cert/payload.key',
        },
        'node-b': {
          certificatePath: '/usr/local/nginx/certs/payload.pem',
          keyPath: '/usr/local/nginx/certs/payload.key',
        },
      },
      nodeHashes: {},
      status: 'normal',
    }],
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
    processedOperationIds: [],
  }
  api.operations = async () => ({ items: [] })
  api.reconciliationOperations = async () => ({ items: [] })
  let publishedOperation = null
  api.createOperation = async (body) => {
    publishedOperation = clone(body)
    return {
      operation: {
        id: 'operation-payload-publish',
        site_id: body.site_id,
        kind: body.kind,
        status: 'queued',
        base_version: body.base_version,
        candidate_revision_id: null,
        created_by: 'qa',
        created_at: now,
        updated_at: now,
        completed_at: null,
        metadata: {},
      },
      jobs: body.jobs.map((item, index) => ({
        ...job(`payload-job-${index}`, item.node_id, 'queued', {}, 'operation-payload-publish'),
        action: item.action,
      })),
      idempotent: false,
    }
  }

  setActivePinia(createPinia())
  const payloadStore = useConsoleStore()
  payloadStore.session = store.session
  await payloadStore.refresh()
  await payloadStore.runSite('site-payload', true)

  assert(publishedOperation, 'Publishing did not create an operation payload')
  assert.equal(
    publishedOperation.candidate.config,
    previewConfig,
    'The platform candidate no longer matches the editor preview',
  )
  const nodeAPayload = publishedOperation.jobs.find((item) => item.node_id === 'node-a').payload
  const nodeBPayload = publishedOperation.jobs.find((item) => item.node_id === 'node-b').payload
  assert.equal(
    nodeAPayload.content,
    previewConfig,
    'The representative node payload differs from the editor preview',
  )
  assert.match(nodeBPayload.content, /\/usr\/local\/nginx\/certs\/payload\.pem/)
  assert.match(nodeBPayload.content, /\/usr\/local\/nginx\/certs\/payload\.key/)
  assert.doesNotMatch(nodeBPayload.content, /\/apps\/nginx\/cert\/payload\.(?:pem|key)/)

  revision = 70
  const batchOperations = Array.from({ length: 501 }, (_, index) => {
    const suffix = String(index).padStart(4, '0')
    return {
      ...partialPublish,
      id: `operation-batch-${suffix}`,
      site_id: `site-batch-${suffix}`,
      status: 'succeeded',
      metadata: {
        reconcile_version: 1,
        reconcile_jobs: [{
          id: `batch-job-${suffix}`,
          node_id: 'node-a',
          action: 'config_apply',
          new_sha256: `batch-hash-${suffix}`,
        }],
      },
    }
  })
  const batchDetails = Object.fromEntries(
    batchOperations.map((operation, index) => {
      const suffix = String(index).padStart(4, '0')
      return [operation.id, {
        operation,
        jobs: [{
          id: `batch-job-${suffix}`,
          batch_id: `batch-job-${suffix}`,
          operation_id: operation.id,
          node_id: 'node-a',
          node_name: 'node-a',
          action: 'config_apply',
          status: 'succeeded',
          created_at: now,
          expires_at: now,
          claimed_at: now,
          completed_at: now,
          created_by: 'qa',
          result: { config_hash: `batch-hash-${suffix}` },
        }],
      }]
    }),
  )
  state = {
    sites: batchOperations.map((operation, index) => {
      const suffix = String(index).padStart(4, '0')
      return {
        id: operation.site_id,
        domain: `batch-${suffix}.example.com`,
        config: 'server { listen 80; }',
        nodeIds: ['node-a'],
        version: 1,
        status: 'publishing',
        nodeHashes: { 'node-a': `old-${suffix}` },
        nodeConfigPaths: {},
        nodeConfigs: {},
        nodeConfigEntryIds: {},
        pendingRemote: {
          operationId: operation.id,
          operation: 'publish',
          publish: true,
          baseStatus: 'published',
          jobs: [{
            id: `batch-job-${suffix}`,
            nodeId: 'node-a',
            candidateHash: `batch-hash-${suffix}`,
          }],
        },
      }
    }),
    certificates: [],
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
    processedOperationIds: [],
  }
  acknowledgedOperationIds.clear()
  const batchSaveCalls = []
  api.operations = async () => ({ items: clone(batchOperations.slice(0, 500)) })
  api.reconciliationOperations = async () => ({
    items: clone(
      batchOperations
        .filter((operation) => !acknowledgedOperationIds.has(operation.id))
        .slice(0, 500),
    ),
  })
  api.operation = async (id) => clone(batchDetails[id])
  api.saveUiState = async (expectedRevision, nextState, reconciledOperationIds = []) => {
    assert.equal(expectedRevision, revision)
    batchSaveCalls.push([...reconciledOperationIds])
    assert(
      reconciledOperationIds.length <= 500,
      'A reconciliation save exceeded the Server 500-operation contract',
    )
    for (const operationId of reconciledOperationIds) acknowledgedOperationIds.add(operationId)
    revision += 1
    state = clone(nextState)
    return { revision, state: clone(state), reconciled_operation_ids: [...reconciledOperationIds] }
  }

  setActivePinia(createPinia())
  const batchStore = useConsoleStore()
  batchStore.session = store.session
  await batchStore.refresh()
  assert.equal(batchSaveCalls.length, 1, 'The first refresh must persist exactly one resource batch')
  assert.equal(batchSaveCalls[0].length, 500, 'The first resource batch did not acknowledge 500 operations')
  assert.equal(acknowledgedOperationIds.size, 500)
  assert.equal(batchStore.sites.filter((item) => item.version === 2).length, 500)
  assert.equal(batchStore.sites.filter((item) => item.pendingRemote).length, 1)
  assert.equal(batchStore.sites.find((item) => item.pendingRemote)?.version, 1)

  await batchStore.refresh()
  assert.equal(batchSaveCalls.length, 2, 'The second refresh did not persist the remaining resource batch')
  assert.equal(batchSaveCalls[1].length, 1, 'The second resource batch did not acknowledge the final operation')
  assert.equal(acknowledgedOperationIds.size, 501)
  assert.equal(batchStore.sites.filter((item) => item.version === 2).length, 501)
  assert.equal(batchStore.sites.filter((item) => item.pendingRemote).length, 0)
  console.log('PASS reconciliation persists at most 500 resources per batch')

  console.log('PASS orphan operation recovery and partial-success reconciliation')
  console.log('PASS acknowledged operations are not replayed')
  console.log('PASS operation detail 404/network failures are isolated per resource')
  console.log('PASS permanent save errors fail once and response-loss recovery is authoritative')
  console.log('PASS rejected ordinary saves restore authoritative state')
  console.log('PASS certificate preview and final per-node publish payload stay consistent')
} finally {
  await moduleServer.close()
}
