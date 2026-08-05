import assert from 'node:assert/strict'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const moduleServer = await createServer({
  root: projectRoot,
  logLevel: 'silent',
  appType: 'custom',
  server: { middlewareMode: true },
})

function node(id, name) {
  return {
    id,
    node_name: name,
    hostname: name,
    labels: {},
    status: 'online',
    reported_status: 'online',
    agent_version: 'qa',
    nginx_version: '1.26.3',
    config_hash: null,
    capabilities: [],
    facts: {
      managed_config_root: '/apps/nginx/conf/conf.d',
      managed_certificate_root: '/apps/nginx/cert',
    },
    enrolled_at: null,
    last_seen_at: null,
    revoked_at: null,
  }
}

try {
  const { certificateCoversDomain, domainPatternCovers } =
    await moduleServer.ssrLoadModule('/src/utils/certificateDomain.ts')
  const { configForCertificateNode, rewriteConfigCertificatePaths } =
    await moduleServer.ssrLoadModule('/src/utils/certificateConfig.ts')
  const { processInventoryJobs } =
    await moduleServer.ssrLoadModule('/src/utils/inventory.ts')
  const { renderSiteTemplate } =
    await moduleServer.ssrLoadModule('/src/utils/siteTemplates.ts')

  const nodeA = node('node-a', 'edge-a')
  const nodeB = node('node-b', 'edge-b')
  const certificate = {
    id: 'cert-san',
    domain: 'primary.example.com',
    domains: ['primary.example.com', 'api.example.com', '*.svc.example.com'],
    nodeIds: [nodeA.id, nodeB.id],
    nodePaths: {
      [nodeA.id]: {
        certificatePath: '/apps/nginx/cert/example.pem',
        keyPath: '/apps/nginx/cert/example.key',
      },
      [nodeB.id]: {
        certificatePath: '/usr/local/nginx/certs/example.pem',
        keyPath: '/usr/local/nginx/certs/example.key',
      },
    },
  }

  assert.equal(
    certificateCoversDomain(certificate, 'API.EXAMPLE.COM.'),
    true,
    'A certificate SAN must cover its exact DNS name',
  )
  assert.equal(
    certificateCoversDomain(certificate, 'one.svc.example.com'),
    true,
    'A wildcard SAN must cover exactly one DNS label',
  )
  assert.equal(
    certificateCoversDomain(certificate, 'two.one.svc.example.com'),
    false,
    'A wildcard SAN must not cover more than one DNS label',
  )
  assert.equal(
    domainPatternCovers('*.svc.example.com', 'svc.example.com'),
    false,
    'A wildcard SAN must not cover the bare suffix',
  )

  const inline = [
    'server { listen 443 ssl;',
    'ssl_certificate /old/example.pem;',
    'ssl_certificate_key /old/example.key; }',
    '# ssl_certificate /comment/must-stay.pem;',
  ].join(' ')
  const inlineResult = rewriteConfigCertificatePaths(inline, certificate, nodeA)
  assert.equal(inlineResult.replacements, 2, 'Both inline certificate directives must be rewritten')
  assert.match(inlineResult.content, /ssl_certificate \/apps\/nginx\/cert\/example\.pem;/)
  assert.match(inlineResult.content, /ssl_certificate_key \/apps\/nginx\/cert\/example\.key;/)
  assert.match(
    inlineResult.content,
    /# ssl_certificate \/comment\/must-stay\.pem;/,
    'Commented directives must not be rewritten',
  )

  const canonical = [
    'server {',
    '  listen 443 ssl;',
    '  server_name api.example.com;',
    '  ssl_certificate /apps/nginx/cert/example.pem;',
    '  ssl_certificate_key /apps/nginx/cert/example.key;',
    '}',
  ].join('\n')
  const nodeAConfig = configForCertificateNode(canonical, certificate, nodeA)
  const nodeBConfig = configForCertificateNode(canonical, certificate, nodeB)
  assert.notEqual(nodeAConfig, nodeBConfig, 'Different node certificate paths need different Conf payloads')
  assert.match(nodeAConfig, /\/apps\/nginx\/cert\/example\.pem/)
  assert.match(nodeBConfig, /\/usr\/local\/nginx\/certs\/example\.pem/)

  const configPath = '/apps/nginx/conf/conf.d/api.example.com.conf'
  const ui = {
    sites: [
      {
        id: 'site-api',
        resourceType: 'site',
        domain: 'api.example.com',
        type: 'proxy',
        context: 'http',
        configMode: 'conf',
        config: canonical,
        environment: '生产',
        nodeIds: [nodeA.id, nodeB.id],
        certificateId: certificate.id,
        version: 1,
        status: 'published',
        nodeHashes: { [nodeA.id]: 'hash-a', [nodeB.id]: 'hash-b' },
        nodeConfigPaths: { [nodeA.id]: configPath, [nodeB.id]: configPath },
        nodeConfigs: {},
        nodeConfigEntryIds: {},
      },
    ],
    certificates: [certificate],
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
  }
  const inventoryJob = {
    id: 'inventory-node-b',
    batch_id: 'batch-inventory',
    operation_id: null,
    node_id: nodeB.id,
    node_name: nodeB.node_name,
    action: 'config_inventory',
    status: 'succeeded',
    created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    claimed_at: new Date().toISOString(),
    completed_at: new Date().toISOString(),
    created_by: 'qa',
    result: {
      config_inventory: {
        files: [
          {
            path: configPath,
            content: nodeBConfig,
            sha256: 'hash-b',
            context: 'http',
            entry_id: 'http-primary',
          },
        ],
      },
    },
  }
  const inventoryResult = processInventoryJobs(ui, [nodeA, nodeB], [inventoryJob])
  assert.equal(inventoryResult.failures, 0, 'A valid inventory result must not fail import')
  assert.equal(
    ui.sites[0].status,
    'published',
    'A node-specific certificate path must not be reported as configuration drift',
  )
  assert.equal(
    Object.hasOwn(ui.sites[0].nodeConfigs, nodeB.id),
    false,
    'Expected node-specific Conf must not remain in the drift override map',
  )

  const staleCertificateUi = {
    sites: [],
    certificates: [
      {
        id: 'cert-deleted-path',
        domain: '*.int.example.com',
        domains: ['*.int.example.com'],
        source: '节点导入',
        nodeIds: [nodeA.id],
        nodePaths: {
          [nodeA.id]: {
            certificatePath: '/apps/nginx/cert/bak/int.example.com.pem',
            keyPath: '/apps/nginx/cert/bak/int.example.com.key',
          },
        },
        nodeHashes: {
          [nodeA.id]: { certificateHash: 'a'.repeat(64), keyHash: 'b'.repeat(64) },
        },
        linkedSiteIds: [],
      },
    ],
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
  }
  const rescanAfterDirectoryDeletion = {
    id: 'certificate-inventory-after-delete',
    batch_id: 'batch-certificate-inventory-after-delete',
    operation_id: null,
    node_id: nodeA.id,
    node_name: nodeA.node_name,
    action: 'certificate_inventory',
    status: 'succeeded',
    created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    claimed_at: new Date().toISOString(),
    completed_at: new Date().toISOString(),
    created_by: 'qa',
    result: {
      certificate_inventory: {
        certificates: [],
        observed_certificate_paths: [],
        scan_complete: true,
        skipped_count: 0,
        truncated: false,
      },
    },
  }

  const legacyEmptyScanUi = JSON.parse(JSON.stringify(staleCertificateUi))
  processInventoryJobs(legacyEmptyScanUi, [nodeA], [
    {
      ...rescanAfterDirectoryDeletion,
      id: 'certificate-inventory-legacy-empty-scan',
      result: {
        certificate_inventory: {
          certificates: [],
          skipped_count: 0,
          truncated: false,
        },
      },
    },
  ])
  assert.equal(
    legacyEmptyScanUi.certificates.length,
    1,
    'An old-format empty scan without explicit completeness metadata must not delete stored mappings',
  )
  assert.deepEqual(
    legacyEmptyScanUi.certificates[0].nodeIds,
    [nodeA.id],
    'An old-format empty scan must preserve the known node deployment',
  )

  processInventoryJobs(staleCertificateUi, [nodeA], [rescanAfterDirectoryDeletion])
  assert.equal(
    staleCertificateUi.certificates.length,
    0,
    'A complete rescan must remove an orphaned node-imported certificate whose directory was deleted',
  )

  const sharedCertificateUi = {
    sites: [],
    certificates: [
      {
        id: 'cert-shared',
        domain: '*.shared.example.com',
        source: '节点导入',
        nodeIds: [nodeA.id, nodeB.id],
        nodePaths: {
          [nodeA.id]: {
            certificatePath: '/apps/nginx/cert/bak/shared.pem',
            keyPath: '/apps/nginx/cert/bak/shared.key',
          },
          [nodeB.id]: {
            certificatePath: '/usr/local/nginx/certs/shared.pem',
            keyPath: '/usr/local/nginx/certs/shared.key',
          },
        },
        nodeHashes: {
          [nodeA.id]: { certificateHash: 'c'.repeat(64), keyHash: 'd'.repeat(64) },
          [nodeB.id]: { certificateHash: 'c'.repeat(64), keyHash: 'd'.repeat(64) },
        },
        linkedSiteIds: [],
      },
    ],
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
  }
  processInventoryJobs(sharedCertificateUi, [nodeA, nodeB], [
    { ...rescanAfterDirectoryDeletion, id: 'certificate-inventory-shared-after-delete' },
  ])
  assert.deepEqual(
    sharedCertificateUi.certificates[0].nodeIds,
    [nodeB.id],
    'Removing a deleted path from one node must retain the same certificate on other nodes',
  )
  assert.equal(
    Object.hasOwn(sharedCertificateUi.certificates[0].nodePaths, nodeA.id),
    false,
    'The deleted node path must be detached from a shared certificate',
  )

  const incompleteScanUi = {
    sites: [],
    certificates: JSON.parse(JSON.stringify(staleCertificateUi.certificates)),
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
  }
  incompleteScanUi.certificates = [
    {
      id: 'cert-incomplete-scan',
      domain: '*.incomplete.example.com',
      source: '节点导入',
      nodeIds: [nodeA.id],
      nodePaths: {
        [nodeA.id]: {
          certificatePath: '/apps/nginx/cert/incomplete.pem',
          keyPath: '/apps/nginx/cert/incomplete.key',
        },
      },
      nodeHashes: {},
      linkedSiteIds: [],
    },
  ]
  processInventoryJobs(incompleteScanUi, [nodeA], [
    {
      ...rescanAfterDirectoryDeletion,
      id: 'certificate-inventory-incomplete',
      result: {
        certificate_inventory: {
          certificates: [],
          observed_certificate_paths: [],
          scan_complete: false,
          skipped_count: 1,
          truncated: false,
        },
      },
    },
  ])
  assert.equal(
    incompleteScanUi.certificates.length,
    1,
    'An incomplete scan must not delete a certificate that the Agent could not inspect',
  )

  const skippedButCompleteUi = {
    sites: [],
    certificates: [
      {
        id: 'cert-deleted-beside-invalid-file',
        domain: '*.deleted.example.com',
        source: '节点导入',
        nodeIds: [nodeA.id],
        nodePaths: {
          [nodeA.id]: {
            certificatePath: '/apps/nginx/cert/bak/deleted.example.com.pem',
            keyPath: '/apps/nginx/cert/bak/deleted.example.com.key',
          },
        },
        nodeHashes: {},
        linkedSiteIds: [],
      },
    ],
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
  }
  processInventoryJobs(skippedButCompleteUi, [nodeA], [
    {
      ...rescanAfterDirectoryDeletion,
      id: 'certificate-inventory-complete-with-invalid-file',
      result: {
        certificate_inventory: {
          certificates: [],
          observed_certificate_paths: ['/apps/nginx/cert/unrelated-invalid.pem'],
          scan_complete: true,
          skipped_count: 1,
          truncated: false,
        },
      },
    },
  ])
  assert.equal(
    skippedButCompleteUi.certificates.length,
    0,
    'A complete observed-path scan must remove a deleted path even when another file is invalid',
  )

  const manualCertificateUi = {
    sites: [],
    certificates: [
      {
        id: 'cert-manual-missing-path',
        domain: 'manual.example.com',
        source: '手动上传',
        nodeIds: [nodeA.id],
        nodePaths: {
          [nodeA.id]: {
            certificatePath: '/apps/nginx/cert/manual.example.com.pem',
            keyPath: '/apps/nginx/cert/manual.example.com.key',
          },
        },
        nodeHashes: {
          [nodeA.id]: { certificateHash: 'e'.repeat(64), keyHash: 'f'.repeat(64) },
        },
        linkedSiteIds: [],
      },
    ],
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
  }
  processInventoryJobs(manualCertificateUi, [nodeA], [
    { ...rescanAfterDirectoryDeletion, id: 'certificate-inventory-manual-after-delete' },
  ])
  assert.equal(
    manualCertificateUi.certificates.length,
    1,
    'A manually-created certificate resource must survive when its node path disappears',
  )
  assert.deepEqual(
    manualCertificateUi.certificates[0].nodeIds,
    [],
    'A missing manual certificate path must no longer claim deployment on that node',
  )
  assert.equal(
    Object.hasOwn(manualCertificateUi.certificates[0].nodePaths, nodeA.id),
    false,
    'A missing manual certificate path must be detached from the node mapping',
  )

  const linkedCertificateUi = {
    sites: [{ id: 'site-linked-certificate', certificateId: 'cert-linked-path', nodeIds: [nodeA.id] }],
    certificates: [
      {
        id: 'cert-linked-path',
        domain: 'linked.example.com',
        source: '节点导入',
        nodeIds: [nodeA.id],
        nodePaths: {
          [nodeA.id]: {
            certificatePath: '/apps/nginx/cert/linked.example.com.pem',
            keyPath: '/apps/nginx/cert/linked.example.com.key',
          },
        },
        nodeHashes: {
          [nodeA.id]: { certificateHash: '1'.repeat(64), keyHash: '2'.repeat(64) },
        },
        linkedSiteIds: ['site-linked-certificate'],
      },
    ],
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
  }
  processInventoryJobs(linkedCertificateUi, [nodeA], [
    { ...rescanAfterDirectoryDeletion, id: 'certificate-inventory-linked-after-delete' },
  ])
  assert.equal(
    linkedCertificateUi.certificates.length,
    1,
    'A certificate referenced by a site must remain visible when its node path disappears',
  )
  assert.deepEqual(
    linkedCertificateUi.certificates[0].nodeIds,
    [],
    'A linked certificate must stop claiming deployment on a node after its path disappears',
  )
  assert.equal(
    linkedCertificateUi.sites[0].certificateId,
    'cert-linked-path',
    'Inventory reconciliation must not silently unbind a site from its certificate resource',
  )

  const pendingCertificateUi = {
    sites: [],
    certificates: [
      {
        id: 'cert-pending-replacement',
        domain: 'pending.example.com',
        source: '节点导入',
        status: 'replacing',
        nodeIds: [nodeA.id],
        nodePaths: {
          [nodeA.id]: {
            certificatePath: '/apps/nginx/cert/pending.example.com.pem',
            keyPath: '/apps/nginx/cert/pending.example.com.key',
          },
        },
        nodeHashes: {
          [nodeA.id]: { certificateHash: '3'.repeat(64), keyHash: '4'.repeat(64) },
        },
        linkedSiteIds: [],
        pendingRemote: { operationId: 'certificate-operation-pending', jobs: [] },
      },
    ],
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
  }
  processInventoryJobs(pendingCertificateUi, [nodeA], [
    { ...rescanAfterDirectoryDeletion, id: 'certificate-inventory-during-replacement' },
  ])
  assert.deepEqual(
    pendingCertificateUi.certificates[0].nodeIds,
    [nodeA.id],
    'A scan must not detach a node while certificate replacement is still being reconciled',
  )
  assert.equal(
    pendingCertificateUi.certificates[0].nodePaths[nodeA.id].certificatePath,
    '/apps/nginx/cert/pending.example.com.pem',
    'A scan must preserve the known path while certificate replacement is pending',
  )

  const orderedScanUi = {
    sites: [],
    certificates: [],
    importedInventoryJobs: [],
    importedCertificateInventoryJobs: [],
  }
  const oldInventoryJob = {
    ...rescanAfterDirectoryDeletion,
    id: 'certificate-inventory-old-snapshot',
    created_at: '2026-01-01T00:00:00.000Z',
    completed_at: '2026-01-01T00:00:01.000Z',
    result: {
      certificate_inventory: {
        certificates: [
          {
            certificate_path: '/apps/nginx/cert/deleted-after-old-scan.pem',
            private_key_path: '/apps/nginx/cert/deleted-after-old-scan.key',
            certificate_sha256: '5'.repeat(64),
            key_material_sha256: '6'.repeat(64),
            fingerprint: Array(32).fill('AB').join(':'),
            domains: ['ordered.example.com'],
            subject: 'ordered.example.com',
            issuer: 'QA CA',
            days_remaining: 90,
            not_after: '2027-01-01T00:00:00Z',
          },
        ],
        skipped_count: 0,
        truncated: false,
      },
    },
  }
  const newInventoryJob = {
    ...rescanAfterDirectoryDeletion,
    id: 'certificate-inventory-new-snapshot',
    created_at: '2026-01-02T00:00:00.000Z',
    completed_at: '2026-01-02T00:00:01.000Z',
  }
  processInventoryJobs(orderedScanUi, [nodeA], [newInventoryJob, oldInventoryJob])
  assert.equal(
    orderedScanUi.certificates.length,
    0,
    'When API jobs arrive newest-first, the newest complete inventory must remain authoritative',
  )

  const balanced = renderSiteTemplate(
    'balanced-https',
    'load-balanced.example.com',
    '10.0.0.31:8080, https://10.0.0.32:8443/health',
  )
  assert.match(balanced, /server 10\.0\.0\.31:8080 /)
  assert.match(balanced, /server 10\.0\.0\.32:8443 /)
  assert.doesNotMatch(balanced, /10\.0\.0\.21:8080/)
  console.log('PASS certificate SAN and one-label wildcard coverage')
  console.log('PASS inline certificate directive rewriting')
  console.log('PASS per-node certificate Conf generation')
  console.log('PASS inventory accepts expected node-specific certificate paths')
  console.log('PASS legacy certificate inventory cannot delete stored node mappings')
  console.log('PASS complete certificate rescan removes deleted node paths')
  console.log('PASS certificate reconciliation preserves manual, linked, and pending resources')
  console.log('PASS newest certificate inventory remains authoritative')
  console.log('PASS balanced HTTPS template uses every entered upstream')
} finally {
  await moduleServer.close()
}
