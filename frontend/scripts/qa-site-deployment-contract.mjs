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
const now = new Date().toISOString()
const node = (id, root) => ({
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
    managed_config_root: root,
    config_entries: [
      {
        id: 'http-primary',
        context: 'http',
        directory: root,
        suffix: '.conf',
        default: true,
        label: 'HTTP 主入口',
      },
      {
        id: 'http-extra',
        context: 'http',
        directory: `${root}/extra`,
        suffix: '.conf',
        label: 'HTTP 扩展入口',
      },
    ],
  },
  enrolled_at: now,
  last_seen_at: now,
  revoked_at: null,
})
const nodes = [node('node-a', '/etc/nginx/conf.d'), node('node-b', '/srv/nginx/conf.d')]
const partialCandidateConfig = 'server { listen 8443; server_name partial.example.com; }'
const partialCandidateHash = createHash('sha256').update(partialCandidateConfig).digest('hex')

let revision = 1
let state = {
  sites: [
    {
      id: 'site-a',
      resourceType: 'site',
      domain: 'contract.example.com',
      filename: 'contract.example.com.conf',
      type: 'proxy',
      target: 'http://127.0.0.1:8080',
      context: 'http',
      configMode: 'conf',
      config: 'server {\n  listen 80;\n  server_name contract.example.com;\n}',
      nodeIds: ['node-a'],
      version: 4,
      status: 'published',
      note: 'original note',
      changeNote: '',
      updatedAt: now,
      nodeHashes: { 'node-a': 'old-node-a' },
      nodeConfigPaths: {
        'node-a': '/etc/nginx/conf.d/contract.example.com.conf',
      },
      nodeConfigEntryIds: { 'node-a': 'http-primary' },
      nodeConfigs: {},
      history: [],
      lastFailure: {
        summary: 'old publish failure',
        operation: 'publish',
        completedNodes: 0,
        totalNodes: 1,
        candidateVersion: 5,
      },
    },
    {
      id: 'site-failed-validate',
      resourceType: 'site',
      domain: 'failed-validate.example.com',
      type: 'proxy',
      target: 'http://127.0.0.1:8080',
      context: 'http',
      configMode: 'conf',
      config: 'server { listen 80; server_name failed-validate.example.com; }',
      nodeIds: ['node-a'],
      version: 2,
      status: 'failed',
      nodeHashes: { 'node-a': 'failed-old' },
      nodeConfigPaths: { 'node-a': '/etc/nginx/conf.d/failed-validate.example.com.conf' },
      nodeConfigEntryIds: { 'node-a': 'http-primary' },
      nodeConfigs: {},
      history: [],
      lastFailure: {
        summary: 'publish failed',
        operation: 'publish',
        completedNodes: 0,
        totalNodes: 1,
        candidateVersion: 3,
      },
    },
    {
      id: 'site-partial-delete',
      resourceType: 'site',
      domain: 'partial.example.com',
      type: 'proxy',
      target: 'http://127.0.0.1:8080',
      context: 'http',
      configMode: 'conf',
      config: partialCandidateConfig,
      nodeIds: ['node-a', 'node-b'],
      version: 3,
      status: 'failed',
      nodeHashes: { 'node-a': partialCandidateHash, 'node-b': 'old-node-b' },
      nodeConfigPaths: {
        'node-a': '/etc/nginx/conf.d/partial.example.com.conf',
        'node-b': '/srv/nginx/conf.d/partial.example.com.conf',
      },
      nodeConfigEntryIds: { 'node-a': 'http-primary', 'node-b': 'http-primary' },
      nodeConfigs: {},
      history: [],
      lastFailure: {
        summary: 'node-b publish failed',
        operation: 'publish',
        completedNodes: 1,
        totalNodes: 2,
        failedNodeIds: ['node-b'],
        candidateVersion: 4,
      },
    },
  ],
  certificates: [],
  importedInventoryJobs: [],
  importedCertificateInventoryJobs: [],
  processedOperationIds: [],
}

try {
  const { api } = await moduleServer.ssrLoadModule('/src/api.ts')
  const { useConsoleStore } = await moduleServer.ssrLoadModule('/src/stores/console.ts')

  const createdOperations = []
  const terminalDetails = new Map()
  let createOperationBarrier = null
  api.uiState = async () => ({ revision, state: clone(state) })
  api.nodes = async () => ({ items: clone(nodes) })
  api.jobs = async () => ({ items: [] })
  api.operations = async () => ({ items: [] })
  api.reconciliationOperations = async () => ({ items: [] })
  api.enrollments = async () => ({ items: [] })
  api.operation = async (id) => clone(terminalDetails.get(id))
  api.saveUiState = async (expectedRevision, nextState, reconciledOperationIds = []) => {
    assert.equal(expectedRevision, revision)
    revision += 1
    state = clone(nextState)
    return {
      revision,
      state: clone(state),
      reconciled_operation_ids: [...reconciledOperationIds],
    }
  }
  api.createOperation = async (body) => {
    if (createOperationBarrier) await createOperationBarrier(body)
    assert.equal(
      body.ui_revision,
      revision,
      'The operation was not tied to the UI state revision used for its candidate',
    )
    const operationId = `operation-${createdOperations.length + 1}`
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
    const jobs = body.jobs.map((spec, index) => ({
      id: `${operationId}-job-${index + 1}`,
      batch_id: operationId,
      operation_id: operationId,
      node_id: spec.node_id,
      node_name: spec.node_id,
      action: spec.action,
      status: 'queued',
      created_at: now,
      expires_at: now,
      claimed_at: null,
      completed_at: null,
      created_by: 'qa',
      result: {},
    }))
    createdOperations.push(clone(body))
    terminalDetails.set(operationId, {
      operation: {
        ...operation,
        status: 'succeeded',
        completed_at: now,
      },
      jobs: jobs.map((job, index) => ({
        ...job,
        status: 'succeeded',
        claimed_at: now,
        completed_at: now,
        result: {
          config_hash:
            body.jobs[index]?.payload?.new_sha256 || `result-${operationId}-${job.node_id}`,
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

  const nodeA = store.nodes.find((item) => item.id === 'node-a')
  const originalCapabilities = [...nodeA.capabilities]
  nodeA.capabilities = nodeA.capabilities.filter((item) => item !== 'config_apply')
  await assert.rejects(
    () => store.runSite('site-a', false),
    /不支持配置写入/,
    'Validation ignored a node without config_apply capability',
  )
  nodeA.capabilities = originalCapabilities
  const readOnlySite = store.sites.find((item) => item.id === 'site-a')
  readOnlySite.nodeReadOnly = { 'node-a': true }
  await assert.rejects(
    () => store.runSite('site-a', true),
    /只读/,
    'Publish ignored a read-only node configuration',
  )
  delete readOnlySite.nodeReadOnly

  const edited = clone(store.sites.find((site) => site.id === 'site-a'))
  edited.note = 'content edit is allowed'
  edited.config = edited.config.replace('listen 80', 'listen 8081')
  edited.context = 'stream'
  edited.filename = 'rogue.stream'
  edited.nodeIds = ['node-b']
  edited.nodeConfigEntryIds = { 'node-b': 'http-extra' }
  await store.upsertSite(edited)

  const savedEdit = store.sites.find((site) => site.id === 'site-a')
  assert.equal(savedEdit.note, 'content edit is allowed')
  assert.match(savedEdit.config, /listen 8081/)
  assert.equal(savedEdit.context, 'http', 'Editing an existing configuration changed its context')
  assert.equal(
    savedEdit.filename,
    'contract.example.com.conf',
    'Editing an existing configuration changed its managed filename',
  )
  assert.deepEqual(
    savedEdit.nodeIds,
    ['node-a'],
    'Editing an existing configuration changed its deployment nodes',
  )
  assert.deepEqual(
    savedEdit.nodeConfigEntryIds,
    { 'node-a': 'http-primary' },
    'Editing an existing configuration changed its deployment directory',
  )
  assert.equal(
    savedEdit.lastFailure,
    undefined,
    'Editing the candidate content retained progress from an older failed candidate',
  )
  assert.equal(savedEdit.status, 'draft')
  assert.equal(savedEdit.candidateVersion, 5)
  await assert.rejects(
    () =>
      store.transferSite(
        'site-a',
        [{ nodeId: 'node-b', entryId: 'http-primary' }],
        'create',
      ),
    /未发布候选配置/,
    'A topology operation deployed an unpublished candidate',
  )

  await store.upsertSite({
    id: 'site-new',
    resourceType: 'site',
    domain: 'new.example.com',
    type: 'proxy',
    target: 'http://127.0.0.1:9000',
    context: 'http',
    configMode: 'conf',
    config: 'server { listen 9000; }',
    nodeIds: ['node-b'],
    version: 0,
    status: 'draft',
    note: '',
    changeNote: '',
    updatedAt: now,
    nodeHashes: {},
    nodeConfigPaths: {},
    nodeConfigEntryIds: { 'node-b': 'http-primary' },
    nodeConfigs: {},
    history: [],
  })
  assert.deepEqual(
    store.sites.find((site) => site.id === 'site-new').nodeIds,
    ['node-b'],
    'Creating a configuration lost its required first deployment target',
  )

  let releaseSubmission
  let markSubmissionEntered
  const submissionEntered = new Promise((resolve) => {
    markSubmissionEntered = resolve
  })
  const submissionGate = new Promise((resolve) => {
    releaseSubmission = resolve
  })
  createOperationBarrier = async () => {
    markSubmissionEntered()
    await submissionGate
  }
  const firstSubmission = store.runSite('site-new', false)
  await submissionEntered
  assert.equal(store.isSiteOperationBusy('site-new'), true)
  const racingEdit = clone(store.sites.find((site) => site.id === 'site-new'))
  racingEdit.config = 'server { listen 9999; }'
  await assert.rejects(
    () => store.upsertSite(racingEdit),
    /任务|操作|鎿嶄綔|浠诲姟/,
    'A candidate edit raced the operation before pendingRemote was persisted',
  )
  releaseSubmission()
  await firstSubmission
  createOperationBarrier = null
  const pendingEdit = clone(store.sites.find((site) => site.id === 'site-new'))
  const pendingNote = pendingEdit.note
  pendingEdit.note = 'must not race the submitted candidate'
  await assert.rejects(
    () => store.upsertSite(pendingEdit),
    /执行中|任务|操作/,
    'An in-flight configuration accepted a competing draft edit',
  )
  assert.equal(
    store.sites.find((site) => site.id === 'site-new').note,
    pendingNote,
    'A rejected in-flight edit remained visible in the store',
  )

  const reconcileLatestOperation = async () => {
    const before = createdOperations.length
    await store.refresh()
    assert.equal(
      createdOperations.length,
      before,
      'Reconciling an operation unexpectedly submitted another operation',
    )
  }

  await reconcileLatestOperation()
  assert.equal(store.isSiteOperationBusy('site-new'), false)

  await store.upsertSite({
    id: 'site-pending-empty',
    resourceType: 'site',
    domain: 'pending-empty.example.com',
    type: 'proxy',
    target: 'http://127.0.0.1:8080',
    context: 'http',
    configMode: 'conf',
    config: 'server { listen 80; }',
    nodeIds: [],
    version: 0,
    status: 'draft',
  })
  const pendingEmpty = store.sites.find((site) => site.id === 'site-pending-empty')
  pendingEmpty.pendingRemote = { operationId: 'operation-pending-empty' }
  await assert.rejects(
    () => store.removeSiteRecord('site-pending-empty'),
    /任务|操作|鎿嶄綔|浠诲姟/,
    'A node-less record with a pending operation was deleted',
  )
  assert(store.sites.some((site) => site.id === 'site-pending-empty'))
  delete pendingEmpty.pendingRemote
  await store.removeSiteRecord('site-pending-empty')

  await store.runSite('site-failed-validate', false)
  await reconcileLatestOperation()
  const validatedFailure = store.sites.find((site) => site.id === 'site-failed-validate')
  assert.equal(validatedFailure.status, 'draft')
  assert.equal(validatedFailure.lastFailure, undefined)
  assert.equal(validatedFailure.version, 2)
  assert.equal(validatedFailure.candidateVersion, 3)

  await store.runSite('site-a', true)
  await reconcileLatestOperation()
  let topologySite = store.sites.find((site) => site.id === 'site-a')
  assert.equal(topologySite.status, 'published')
  assert.equal(topologySite.version, 5)
  assert.equal(topologySite.candidateVersion, undefined)

  await store.transferSite(
    'site-a',
    [{ nodeId: 'node-b', entryId: 'http-primary' }],
    'create',
  )
  await reconcileLatestOperation()
  topologySite = store.sites.find((site) => site.id === 'site-a')
  assert.deepEqual([...topologySite.nodeIds].sort(), ['node-a', 'node-b'])
  assert.equal(topologySite.version, 5, 'Adding a deployment node increased the config version')
  assert.equal(topologySite.nodeConfigEntryIds['node-b'], 'http-primary')

  await store.transferSite(
    'site-a',
    [{ nodeId: 'node-a', entryId: 'http-extra' }],
    'create',
  )
  await reconcileLatestOperation()
  topologySite = store.sites.find((site) => site.id === 'site-a')
  assert.deepEqual([...topologySite.nodeIds].sort(), ['node-a', 'node-b'])
  assert.equal(topologySite.version, 5, 'Migrating a config directory increased the config version')
  assert.equal(topologySite.nodeConfigEntryIds['node-a'], 'http-extra')

  await store.runSite('site-a', false)
  await reconcileLatestOperation()
  topologySite = store.sites.find((site) => site.id === 'site-a')
  assert.equal(topologySite.version, 5, 'Node validation increased the config version')
  assert.deepEqual([...topologySite.nodeIds].sort(), ['node-a', 'node-b'])

  await store.removeSiteFromNodes('site-a', ['node-a'])
  await reconcileLatestOperation()
  topologySite = store.sites.find((site) => site.id === 'site-a')
  assert.deepEqual(topologySite.nodeIds, ['node-b'])
  assert.equal(topologySite.version, 5, 'Removing a deployment node increased the config version')
  assert.equal(topologySite.nodeConfigEntryIds['node-a'], undefined)

  await store.removeSiteFromNodes('site-a', ['node-b'])
  await reconcileLatestOperation()
  topologySite = store.sites.find((site) => site.id === 'site-a')
  assert(topologySite, 'Removing the last node also deleted the platform record')
  assert.deepEqual(topologySite.nodeIds, [])
  assert.equal(topologySite.status, 'unassigned')
  assert.equal(topologySite.version, 5, 'Removing the final deployment node increased the config version')

  await store.removeSiteFromNodes('site-partial-delete', ['node-b'])
  await reconcileLatestOperation()
  let partialDeleteSite = store.sites.find((site) => site.id === 'site-partial-delete')
  assert.equal(partialDeleteSite.status, 'failed')
  assert.equal(partialDeleteSite.version, 3)
  assert.equal(partialDeleteSite.lastFailure?.operation, 'publish')
  assert.deepEqual(partialDeleteSite.nodeIds, ['node-a'])

  await store.runSite('site-partial-delete', true)
  assert.equal(
    createdOperations.at(-1).kind,
    'publish',
    'An all-candidate partial publish was downgraded to reload-only validation',
  )
  await reconcileLatestOperation()
  partialDeleteSite = store.sites.find((site) => site.id === 'site-partial-delete')
  assert.equal(partialDeleteSite.status, 'published')
  assert.equal(partialDeleteSite.version, 4)
  assert.equal(partialDeleteSite.lastFailure, undefined)

  assert.deepEqual(
    createdOperations.map((operation) => operation.kind),
    ['validate', 'validate', 'publish', 'transfer', 'transfer', 'validate', 'delete', 'delete', 'delete', 'publish'],
    'Deployment topology or validation did not use the dedicated operation paths',
  )

  console.log('PASS existing-site edits preserve nodes, context, filename, and config directories')
  console.log('PASS new configurations retain their required first deployment target')
  console.log('PASS validation and publish reject read-only or incapable nodes before submission')
  console.log('PASS in-flight configurations reject competing draft edits')
  console.log('PASS transfer, migration, validation, and removal preserve config version')
} finally {
  await moduleServer.close()
}
