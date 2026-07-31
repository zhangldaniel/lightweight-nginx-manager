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
  console.log('PASS balanced HTTPS template uses every entered upstream')
} finally {
  await moduleServer.close()
}
