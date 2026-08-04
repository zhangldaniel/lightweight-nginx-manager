import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
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
const hash = (value) => createHash('sha256').update(value).digest('hex')
const now = new Date().toISOString()

const configEntry = (directory) => ({
  id: 'http-primary',
  context: 'http',
  directory,
  suffix: '.conf',
  default: true,
  label: 'HTTP primary',
})

const node = (id, directory) => ({
  id,
  node_name: id,
  hostname: id,
  labels: {},
  status: 'online',
  reported_status: 'online',
  agent_version: 'qa',
  nginx_version: '1.26.3',
  config_hash: null,
  capabilities: ['config_apply', 'config_delete', 'config_move'],
  facts: {
    managed_config_root: directory,
    config_entries: [configEntry(directory)],
  },
  enrolled_at: now,
  last_seen_at: now,
  revoked_at: null,
})

const nodes = [
  node('node-a', '/etc/nginx/conf.d'),
  node('node-b', '/srv/nginx/conf.d'),
  node('node-c', '/opt/nginx/conf.d'),
]

const partialConfig = 'server { listen 8443; server_name partial.example.com; }'
const partialHash = hash(partialConfig)
const validateConfig = 'server { listen 8080; server_name validate.example.com; }'
const recoveryConfig = 'server { listen 9443; server_name recovery.example.com; }'

const site = (overrides) => ({
  id: 'site',
  resourceType: 'site',
  domain: 'example.com',
  filename: 'example.com.conf',
  type: 'proxy',
  target: 'http://127.0.0.1:8080',
  context: 'http',
  configMode: 'conf',
  config: 'server { listen 80; server_name example.com; }',
  nodeIds: [],
  version: 0,
  status: 'draft',
  note: '',
  changeNote: '',
  updatedAt: now,
  nodeHashes: {},
  nodeConfigPaths: {},
  nodeConfigEntryIds: {},
  nodeConfigs: {},
  history: [],
  ...overrides,
})

const recoveryOperation = {
  id: 'operation-recovery',
  site_id: 'site-recovery',
  kind: 'publish',
  status: 'partial',
  base_version: 4,
  candidate_revision_id: null,
  created_by: 'qa',
  created_at: now,
  updated_at: now,
  completed_at: now,
  metadata: {},
}

const recoveryJobs = [
  {
    id: 'recovery-job-b',
    batch_id: recoveryOperation.id,
    operation_id: recoveryOperation.id,
    node_id: 'node-b',
    node_name: 'node-b',
    action: 'config_apply',
    status: 'succeeded',
    created_at: now,
    expires_at: now,
    claimed_at: now,
    completed_at: now,
    created_by: 'qa',
    result: { config_hash: hash(recoveryConfig) },
  },
  {
    id: 'recovery-job-c',
    batch_id: recoveryOperation.id,
    operation_id: recoveryOperation.id,
    node_id: 'node-c',
    node_name: 'node-c',
    action: 'config_apply',
    status: 'failed',
    created_at: now,
    expires_at: now,
    claimed_at: now,
    completed_at: now,
    created_by: 'qa',
    result: { error: 'synthetic recovery failure', failure_stage: 'nginx_test' },
  },
]

let revision = 1
let state = {
  sites: [
    site({
      id: 'site-lock',
      domain: 'lock.example.com',
      filename: 'lock.example.com.conf',
      nodeIds: ['node-b'],
      version: 1,
      status: 'published',
      nodeHashes: { 'node-b': hash('server { listen 80; server_name example.com; }') },
      nodeConfigPaths: { 'node-b': '/srv/nginx/conf.d/lock.example.com.conf' },
      nodeConfigEntryIds: { 'node-b': 'http-primary' },
    }),
    site({
      id: 'site-partial',
      domain: 'partial.example.com',
      filename: 'partial.example.com.conf',
      config: partialConfig,
      nodeIds: ['node-a', 'node-b'],
      version: 3,
      status: 'failed',
      nodeHashes: { 'node-a': partialHash, 'node-b': 'old-node-b' },
      nodeConfigPaths: {
        'node-a': '/etc/nginx/conf.d/partial.example.com.conf',
        'node-b': '/srv/nginx/conf.d/partial.example.com.conf',
      },
      nodeConfigEntryIds: { 'node-a': 'http-primary', 'node-b': 'http-primary' },
      lastFailure: {
        summary: 'node-b publish failed',
        operation: 'publish',
        completedNodes: 1,
        totalNodes: 2,
        failedNodeIds: ['node-b'],
        candidateVersion: 4,
      },
    }),
    site({
      id: 'site-validate',
      domain: 'validate.example.com',
      filename: 'validate.example.com.conf',
      config: validateConfig,
      nodeIds: ['node-a'],
      version: 2,
      status: 'failed',
      nodeHashes: { 'node-a': 'validate-old' },
      nodeConfigPaths: { 'node-a': '/etc/nginx/conf.d/validate.example.com.conf' },
      nodeConfigEntryIds: { 'node-a': 'http-primary' },
      lastFailure: {
        summary: 'old publish failed',
        operation: 'publish',
        completedNodes: 0,
        totalNodes: 1,
        failedNodeIds: ['node-a'],
        candidateVersion: 3,
      },
    }),
    site({
      id: 'site-recovery',
      domain: 'recovery.example.com',
      filename: 'recovery.example.com.conf',
      config: recoveryConfig,
      nodeIds: ['node-a', 'node-b', 'node-c'],
      version: 4,
      status: 'publishing',
      nodeHashes: {
        'node-a': hash(recoveryConfig),
        'node-b': 'recovery-old-b',
        'node-c': 'recovery-old-c',
      },
      nodeConfigPaths: {
        'node-a': '/etc/nginx/conf.d/recovery.example.com.conf',
        'node-b': '/srv/nginx/conf.d/recovery.example.com.conf',
        'node-c': '/opt/nginx/conf.d/recovery.example.com.conf',
      },
      nodeConfigEntryIds: {
        'node-a': 'http-primary',
        'node-b': 'http-primary',
        'node-c': 'http-primary',
      },
      pendingRemote: {
        operationId: recoveryOperation.id,
        operation: 'publish',
        publish: true,
        baseStatus: 'failed',
        alreadyCompleted: 1,
        totalTargets: 3,
        candidateVersion: 5,
        jobs: [
          { id: 'recovery-job-b', nodeId: 'node-b', candidateHash: hash(recoveryConfig) },
          { id: 'recovery-job-c', nodeId: 'node-c', candidateHash: hash(recoveryConfig) },
        ],
      },
    }),
  ],
  certificates: [],
  importedInventoryJobs: [],
  importedCertificateInventoryJobs: [],
  processedOperationIds: [],
}

try {
  const { api } = await moduleServer.ssrLoadModule('/src/api.ts')
  const { useConsoleStore } = await moduleServer.ssrLoadModule('/src/stores/console.ts')

  const terminalDetails = new Map([
    [
      recoveryOperation.id,
      { operation: clone(recoveryOperation), jobs: clone(recoveryJobs) },
    ],
  ])
  const submitted = []
  let operationSequence = 0
  let lockBarrier = null

  api.uiState = async () => ({ revision, state: clone(state) })
  api.nodes = async () => ({ items: clone(nodes) })
  api.jobs = async () => ({ items: [] })
  api.operations = async () => ({ items: [] })
  api.enrollments = async () => ({ items: [] })
  api.reconciliationOperations = async () => ({ items: [] })
  api.operation = async (operationId) => clone(terminalDetails.get(operationId))
  api.saveUiState = async (expectedRevision, nextState) => {
    assert.equal(expectedRevision, revision)
    revision += 1
    state = clone(nextState)
    return { revision, state: clone(state), reconciled_operation_ids: [] }
  }
  api.createOperation = async (body) => {
    if (body.site_id === 'site-lock' && lockBarrier) {
      lockBarrier.entered()
      await lockBarrier.release
    }
    assert.equal(
      body.ui_revision,
      revision,
      'The operation was not tied to the UI state revision used for its candidate',
    )
    operationSequence += 1
    submitted.push(clone(body))
    const operationId = `operation-${operationSequence}`
    const operation = {
      id: operationId,
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
    }
    const jobs = body.jobs.map((job, index) => ({
      id: `${operationId}-job-${index + 1}`,
      batch_id: operationId,
      operation_id: operationId,
      node_id: job.node_id,
      node_name: job.node_id,
      action: job.action,
      status: 'queued',
      created_at: now,
      expires_at: now,
      claimed_at: null,
      completed_at: null,
      created_by: 'qa',
      result: {},
    }))
    terminalDetails.set(operationId, {
      operation: { ...operation, status: 'succeeded', completed_at: now },
      jobs: jobs.map((job, index) => ({
        ...job,
        status: 'succeeded',
        claimed_at: now,
        completed_at: now,
        result: {
          config_hash:
            body.jobs[index]?.payload?.new_sha256 ||
            body.jobs[index]?.payload?.content_sha256 ||
            `result-${operationId}-${job.node_id}`,
        },
      })),
    })
    return { operation, jobs, idempotent: false }
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

  const recovered = store.sites.find((item) => item.id === 'site-recovery')
  assert.equal(recovered.status, 'failed')
  assert.equal(recovered.version, 4)
  assert.equal(recovered.lastFailure?.completedNodes, 2)
  assert.equal(recovered.lastFailure?.totalNodes, 3)
  assert.equal(recovered.lastFailure?.candidateVersion, 5)
  assert.deepEqual(recovered.lastFailure?.failedNodeIds, ['node-c'])

  let markBarrierEntered
  let releaseBarrier
  const barrierEntered = new Promise((resolve) => {
    markBarrierEntered = resolve
  })
  const barrierRelease = new Promise((resolve) => {
    releaseBarrier = resolve
  })
  lockBarrier = { entered: markBarrierEntered, release: barrierRelease }

  const lockedTransfer = store.transferSite(
    'site-lock',
    [{ nodeId: 'node-a', entryId: 'http-primary' }],
    'create',
  )
  await barrierEntered

  const lockedEdit = clone(store.sites.find((item) => item.id === 'site-lock'))
  lockedEdit.note = 'must not race operation creation'
  const editOutcome = await store.upsertSite(lockedEdit).then(
    () => ({ rejected: false }),
    (error) => ({ rejected: true, error }),
  )
  const deleteOutcome = await store.removeSiteRecord('site-lock').then(
    () => ({ rejected: false }),
    (error) => ({ rejected: true, error }),
  )

  releaseBarrier()
  await lockedTransfer
  lockBarrier = null

  assert.equal(editOutcome.rejected, true, 'The local operation lock allowed a racing edit')
  assert.equal(deleteOutcome.rejected, true, 'The local operation lock allowed a racing delete')
  await store.refresh()
  assert(store.sites.some((item) => item.id === 'site-lock'))

  let partial = store.sites.find((item) => item.id === 'site-partial')
  const blockedTopology = await store
    .transferSite(
      'site-partial',
      [{ nodeId: 'node-c', entryId: 'http-primary' }],
      'create',
    )
    .then(
      () => ({ rejected: false }),
      (error) => ({ rejected: true, error }),
    )
  assert.equal(blockedTopology.rejected, true, 'A partial publish leaked into a topology add')
  assert.equal(partial.status, 'failed')
  assert.equal(partial.version, 3)
  assert.equal(partial.lastFailure?.operation, 'publish')
  assert.equal(partial.lastFailure?.candidateVersion, 4)

  await store.removeSiteFromNodes('site-partial', ['node-b'])
  await store.refresh()
  partial = store.sites.find((item) => item.id === 'site-partial')
  assert.deepEqual([...partial.nodeIds].sort(), ['node-a'])
  assert.equal(partial.status, 'failed')
  assert.equal(partial.lastFailure?.operation, 'publish')
  assert.equal(partial.lastFailure?.candidateVersion, 4)
  assert.equal(partial.candidateVersion, 4)
  assert.equal(partial.nodeHashes['node-a'], partialHash)

  await store.runSite('site-partial', false)
  await store.refresh()
  partial = store.sites.find((item) => item.id === 'site-partial')
  assert.equal(partial.status, 'draft')
  assert.equal(partial.version, 3)
  assert.equal(partial.candidateVersion, 4)
  assert.equal(partial.lastFailure, undefined)

  await assert.rejects(
    () => store.removeSiteFromNodes('site-partial', ['node-a']),
    /未发布候选配置/,
    'Removing the last node left an unpublished candidate with no deployment path',
  )

  const stillBlockedTopology = await store
    .transferSite(
      'site-partial',
      [{ nodeId: 'node-c', entryId: 'http-primary' }],
      'create',
    )
    .then(
      () => ({ rejected: false }),
      (error) => ({ rejected: true, error }),
    )
  assert.equal(
    stillBlockedTopology.rejected,
    true,
    'A validated candidate leaked into a topology add',
  )

  const submissionsBeforePublish = submitted.length
  await store.runSite('site-partial', true)
  const resumedPublish = submitted.at(-1)
  assert.equal(submitted.length, submissionsBeforePublish + 1)
  assert.equal(
    resumedPublish.kind,
    'publish',
    'An all-candidate retry was downgraded to reload-only validation',
  )
  await store.refresh()
  partial = store.sites.find((item) => item.id === 'site-partial')
  assert.equal(partial.status, 'published')
  assert.equal(partial.version, 4)
  assert.equal(partial.candidateVersion, undefined)
  assert.equal(partial.lastFailure, undefined)

  await store.runSite('site-validate', false)
  await store.refresh()
  const validated = store.sites.find((item) => item.id === 'site-validate')
  assert.equal(validated.status, 'draft')
  assert.equal(validated.version, 2)
  assert.equal(validated.candidateVersion, 3)
  assert.equal(validated.lastFailure, undefined)

  console.log('PASS local operation lock rejects racing edits and record deletion')
  console.log('PASS unresolved and validated candidates cannot leak through topology changes')
  console.log('PASS partial publish -> remove -> validate -> publish increments exactly one version')
  console.log('PASS recovery retains cumulative completion and candidate version')
  console.log('PASS successful validation moves a failed candidate back to draft')
} finally {
  await moduleServer.close()
}
