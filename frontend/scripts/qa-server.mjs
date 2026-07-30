import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const port = Number(process.env.QA_PORT || 4179)
const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const index = await readFile(resolve(projectRoot, 'dist/index.html'))
const now = new Date().toISOString()
const nodes = [
  {
    id: 'node-sh-01',
    node_name: 'it-nginx-sh-01',
    hostname: 'nginx-sh-01',
    labels: { region: '上海', environment: '生产' },
    status: 'online',
    reported_status: 'online',
    agent_version: '0.4.0',
    nginx_version: '1.26.3',
    config_hash: 'aa'.repeat(32),
    capabilities: ['config_move', 'metrics_v1', 'stub_status_v1', 'log_stream_v1'],
    facts: {
      nginx_config: '/apps/nginx/conf/nginx.conf',
      managed_config_root: '/apps/nginx/conf/conf.d',
      managed_certificate_root: '/apps/nginx/cert',
      main_config_editable: true,
      log_files: ['/apps/nginx/logs/access.log', '/apps/nginx/logs/error.log'],
      stub_status_url: 'http://127.0.0.1:18080/nginx_status',
      config_entries: [
        {
          id: 'http-primary',
          context: 'http',
          directory: '/apps/nginx/conf/conf.d',
          suffix: '.conf',
          default: true,
          label: 'HTTP 主入口',
        },
        {
          id: 'http-extra',
          context: 'http',
          directory: '/apps/nginx/conf/sites.d',
          suffix: '.conf',
          label: 'HTTP 扩展入口',
        },
        {
          id: 'stream-primary',
          context: 'stream',
          directory: '/apps/nginx/conf/conf.d',
          suffix: '.stream',
          default: true,
          label: 'Stream 主入口',
        },
      ],
    },
    enrolled_at: now,
    last_seen_at: now,
    revoked_at: null,
  },
  {
    id: 'node-bj-01',
    node_name: 'it-nginx-bj-01',
    hostname: 'nginx-bj-01',
    labels: { region: '北京', environment: '生产' },
    status: 'online',
    reported_status: 'online',
    agent_version: '0.4.0',
    nginx_version: '1.26.3',
    config_hash: 'bb'.repeat(32),
    capabilities: ['config_move', 'metrics_v1', 'stub_status_v1'],
    facts: {
      nginx_config: '/apps/nginx/conf/nginx.conf',
      managed_config_root: '/apps/nginx/conf/conf.d',
      managed_certificate_root: '/apps/nginx/cert',
      config_entries: [
        {
          id: 'http-primary',
          context: 'http',
          directory: '/apps/nginx/conf/conf.d',
          suffix: '.conf',
          default: true,
          label: 'HTTP 主入口',
        },
      ],
    },
    enrolled_at: now,
    last_seen_at: now,
    revoked_at: null,
  },
]
const certificate = {
  id: 'cert-wildcard',
  domain: '*.int.example.com',
  name: '*.int.example.com',
  issuer: 'RapidSSL TLS RSA CA G1',
  source: '节点导入',
  daysLeft: 74,
  status: 'normal',
  nodeIds: nodes.map((node) => node.id),
  linkedSiteIds: ['site-api'],
  fingerprint: '7A:41:4C:1A:C3:26:95:E3:71:1E:CB:1B:B3:FB:A1:BF',
  nodePaths: Object.fromEntries(
    nodes.map((node) => [
      node.id,
      {
        certificatePath: '/apps/nginx/cert/int.example.com.pem',
        keyPath: '/apps/nginx/cert/int.example.com.key',
      },
    ]),
  ),
}
const config = `server {
  listen 443 ssl;
  server_name api.int.example.com;

  ssl_certificate     /apps/nginx/cert/int.example.com.pem;
  ssl_certificate_key /apps/nginx/cert/int.example.com.key;

  location / {
    proxy_pass http://10.165.0.29:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
  }
}`
const state = {
  sites: [
    {
      id: 'site-api',
      resourceType: 'site',
      domain: 'api.int.example.com',
      type: 'proxy',
      target: 'http://10.165.0.29:8080',
      context: 'http',
      configMode: 'conf',
      config,
      environment: '生产',
      nodeIds: nodes.map((node) => node.id),
      certificateId: certificate.id,
      version: 3,
      status: 'published',
      note: '技术中心 API 网关 · 负责人：基础架构组',
      changeNote: '调整上游健康检查',
      updatedAt: now,
      nodeHashes: Object.fromEntries(nodes.map((node) => [node.id, 'cc'.repeat(32)])),
      nodeConfigPaths: Object.fromEntries(
        nodes.map((node) => [node.id, '/apps/nginx/conf/conf.d/api.int.example.com.conf']),
      ),
      nodeConfigEntryIds: Object.fromEntries(nodes.map((node) => [node.id, 'http-primary'])),
    },
    {
      id: 'generic-upstream',
      resourceType: 'generic',
      name: '订单服务 upstream',
      filename: 'orders-upstream.conf',
      type: 'custom',
      context: 'http',
      configMode: 'generic',
      config: 'upstream orders {\\n  server 10.165.1.23:32116;\\n  server 10.165.1.24:32116;\\n}',
      environment: '生产',
      nodeIds: ['node-sh-01'],
      version: 1,
      status: 'draft',
      note: '订单服务后端池',
      nodeHashes: { 'node-sh-01': 'dd'.repeat(32) },
      nodeConfigPaths: {
        'node-sh-01': '/apps/nginx/conf/conf.d/orders-upstream.conf',
      },
      nodeConfigEntryIds: { 'node-sh-01': 'http-primary' },
    },
  ],
  certificates: [certificate],
  importedInventoryJobs: [],
  importedCertificateInventoryJobs: [],
  processedOperationIds: [],
}
const monitoring = nodes.map((node, index) => ({
  node,
  sampled_at: now,
  health: { status: 'healthy', reasons: [] },
  metrics: {
    cpu: { percent: 22.4 + index * 8 },
    memory: { percent: 51.8 + index * 5, used_bytes: 8_589_934_592 },
    network: { rx_bytes_per_second: 1_820_000, tx_bytes_per_second: 780_000 },
    disk_io: { write_bytes_per_second: 430_000 },
    filesystems: [{ mount: '/', percent: 42.5 }],
    stub_status: {
      available: true,
      active: 38 + index * 7,
      accepts: 1_923_002,
      handled: 1_923_002,
      requests: 8_230_104,
      requests_per_second: 132.6 + index * 24,
    },
  },
}))

function send(response, body, status = 200, contentType = 'application/json; charset=utf-8') {
  response.writeHead(status, {
    'content-type': contentType,
    'cache-control': 'no-store',
  })
  response.end(contentType.startsWith('application/json') ? JSON.stringify(body) : body)
}

export const qaServer = createServer((request, response) => {
  const path = new URL(request.url || '/', `http://127.0.0.1:${port}`).pathname
  if (path === '/logout-preview') {
    response.writeHead(302, {
      location: '/#/login',
      'set-cookie': 'qa_logged_out=1; Path=/; SameSite=Strict',
      'cache-control': 'no-store',
    })
    return response.end()
  }
  if (path === '/api/v1/auth/session') {
    if (request.headers.cookie?.includes('qa_logged_out=1')) {
      return send(response, { detail: 'authentication required' }, 401)
    }
    return send(response, {
      authenticated: true,
      username: 'admin',
      role: 'admin',
      auth_source: 'local',
      csrf_token: 'qa-token',
      expires_at: null,
    })
  }
  if (path === '/api/v1/admin/ui-state') return send(response, { revision: 1, state })
  if (path === '/api/v1/admin/nodes') return send(response, { items: nodes })
  if (path === '/api/v1/admin/jobs' || path === '/api/v1/admin/operations') {
    return send(response, { items: [] })
  }
  if (path === '/api/v1/admin/enrollments') return send(response, { items: [] })
  if (path === '/api/v1/admin/monitoring/summary') {
    return send(response, { items: monitoring, server_time: now })
  }
  if (path.includes('/metrics')) {
    return send(response, {
      items: Array.from({ length: 36 }, (_, index) => ({
        sampled_at: new Date(Date.now() - (35 - index) * 60_000).toISOString(),
        metrics: monitoring[0].metrics,
      })),
    })
  }
  if (path === '/api/v1/admin/audit') {
    return send(response, { items: [], next_before_id: null })
  }
  return send(response, index, 200, 'text/html; charset=utf-8')
}).listen(port, '127.0.0.1', () => {
  // Intentionally quiet: this helper is used by automated visual checks.
})
