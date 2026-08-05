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
    capabilities: [
      'config_apply',
      'config_move',
      'config_delete',
      'metrics_v1',
      'stub_status_v1',
      'log_stream_v1',
      'keepalived_inspect',
      'keepalived_validate',
    ],
    facts: {
      nginx_config: '/apps/nginx/conf/nginx.conf',
      managed_config_root: '/apps/nginx/conf/conf.d',
      managed_certificate_root: '/apps/nginx/cert',
      main_config_editable: true,
      log_files: ['/apps/nginx/logs/access.log', '/apps/nginx/logs/error.log'],
      stub_status_url: 'http://127.0.0.1:18080/nginx_status',
      keepalived: {
        vip: '192.0.2.110',
        vip_owned: true,
        role: 'MASTER',
        local_addresses: ['192.0.2.108', '192.0.2.110'],
        service: { active: true, active_state: 'active', sub_state: 'running' },
        config_path: '/etc/keepalived/keepalived.conf',
        keepalived_config_hash: '11'.repeat(32),
        keepalived_version: '2.2.8',
        config_summary: {
          summary_complete: true,
          truncated: false,
          instances: [{
            name: 'VI_NGINX',
            virtual_router_id: 54,
            priority: 120,
            advert_int: 1,
            unicast_src_ip: '192.0.2.108',
            unicast_peers: ['192.0.2.111'],
            virtual_ips: ['192.0.2.110'],
          }],
        },
      },
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
    capabilities: [
      'config_apply',
      'config_move',
      'config_delete',
      'metrics_v1',
      'stub_status_v1',
      'keepalived_inspect',
      'keepalived_validate',
    ],
    facts: {
      nginx_config: '/apps/nginx/conf/nginx.conf',
      managed_config_root: '/apps/nginx/conf/conf.d',
      managed_certificate_root: '/apps/nginx/cert',
      keepalived: {
        vip: '192.0.2.110',
        vip_owned: false,
        role: 'BACKUP',
        local_addresses: ['192.0.2.111'],
        service: { active: true, active_state: 'active', sub_state: 'running' },
        config_path: '/etc/keepalived/keepalived.conf',
        keepalived_config_hash: '22'.repeat(32),
        keepalived_version: '2.2.8',
        config_summary: {
          summary_complete: true,
          truncated: false,
          instances: [{
            name: 'VI_NGINX',
            virtual_router_id: 54,
            priority: 100,
            advert_int: 1,
            unicast_src_ip: '192.0.2.111',
            unicast_peers: ['192.0.2.108'],
            virtual_ips: ['192.0.2.110'],
          }],
        },
      },
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
      node.id === 'node-bj-01'
        ? {
            certificatePath: '/usr/local/nginx/certs/int.example.com.pem',
            keyPath: '/usr/local/nginx/certs/int.example.com.key',
          }
        : {
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
      nodeConfigs: {
        'node-sh-01': 'server {\n  listen 8443;\n}\nupstream orders {\n  server 10.165.1.99:15432;\n}',
      },
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
    cpu: {
      percent: 22.4 + index * 8,
      count: 8,
      load1: 1.08,
      load5: 0.94,
      load15: 0.82,
      load_per_core: 0.135,
    },
    memory: {
      percent: 51.8 + index * 5,
      used_bytes: 8_589_934_592,
      swap_percent: 0,
    },
    network: {
      rx_bytes_per_second: 1_820_000,
      tx_bytes_per_second: 780_000,
      errors: 0,
    },
    disk_io: {
      read_bytes_per_second: 264_000,
      write_bytes_per_second: 430_000,
    },
    filesystems: [
      { mount: '/', used_bytes: 91_268_055_040, total_bytes: 214_748_364_800, percent: 42.5 },
      { mount: '/apps', used_bytes: 68_719_476_736, total_bytes: 161_061_273_600, percent: 42.7 },
    ],
    nginx: { running: true, processes: 3, workers: 2, rss_bytes: 49_283_072 },
    system: { uptime_seconds: 4_322_400, kernel: '6.1.0-18-amd64' },
    stub_status: {
      configured: true,
      available: true,
      active: 38 + index * 7,
      accepts: 1_923_002,
      handled: 1_923_002,
      requests: 8_230_104,
      reading: 1,
      writing: 3,
      waiting: 34 + index * 7,
      requests_per_second: 132.6 + index * 24,
      dropped_connections: 0,
    },
  },
}))

const monitoringHistory = Array.from({ length: 36 }, (_, index) => {
  const phase = index / 35
  const broadWave = Math.sin(phase * Math.PI * 4)
  const detailWave = Math.sin(phase * Math.PI * 10)
  const base = monitoring[0].metrics
  return {
    sampled_at: new Date(Date.now() - (35 - index) * 60_000).toISOString(),
    metrics: {
      ...base,
      cpu: {
        ...base.cpu,
        percent: Number((22.4 + broadWave * 5.4 + detailWave * 1.4).toFixed(1)),
        load1: Number((1.08 + broadWave * 0.26).toFixed(2)),
        load_per_core: Number((0.135 + broadWave * 0.033).toFixed(3)),
      },
      memory: {
        ...base.memory,
        percent: Number((51.8 + Math.sin(phase * Math.PI * 3) * 2.2).toFixed(1)),
      },
      network: {
        ...base.network,
        rx_bytes_per_second: Math.round(1_820_000 + broadWave * 410_000 + detailWave * 120_000),
      },
      stub_status: {
        ...base.stub_status,
        active: Math.round(38 + broadWave * 7 + detailWave * 2),
        requests_per_second: Number((132.6 + broadWave * 34 + detailWave * 11).toFixed(1)),
      },
    },
  }
})

const minutesFromNow = (minutes) => new Date(Date.now() + minutes * 60_000).toISOString()
const recordJobs = [
  {
    id: 'job-config-success',
    batch_id: 'batch-release-1846',
    operation_id: 'operation-config-success',
    node_id: 'node-sh-01',
    node_name: 'it-nginx-sh-01',
    action: 'config_apply',
    status: 'succeeded',
    created_at: minutesFromNow(-18),
    expires_at: minutesFromNow(12),
    claimed_at: minutesFromNow(-17),
    completed_at: minutesFromNow(-16),
    created_by: 'admin',
    result: { summary: 'nginx -t 通过，配置已原子替换并 reload' },
  },
  {
    id: 'job-test-failed',
    batch_id: 'batch-release-1847',
    operation_id: 'operation-config-failed',
    node_id: 'node-bj-01',
    node_name: 'it-nginx-bj-01',
    action: 'nginx_test',
    status: 'failed',
    created_at: minutesFromNow(-11),
    expires_at: minutesFromNow(19),
    claimed_at: minutesFromNow(-10),
    completed_at: minutesFromNow(-9),
    created_by: 'operator.li',
    result: {
      summary: 'nginx: [emerg] host not found in upstream "orders_backend"',
      failure_stage: 'nginx -t',
      reloaded: false,
    },
  },
  {
    id: 'job-certificate-running',
    batch_id: 'batch-certificate-1848',
    operation_id: 'operation-certificate-running',
    node_id: 'node-sh-01',
    node_name: 'it-nginx-sh-01',
    action: 'certificate_apply',
    status: 'running',
    created_at: minutesFromNow(-3),
    expires_at: minutesFromNow(27),
    claimed_at: minutesFromNow(-2),
    completed_at: null,
    created_by: 'admin',
    result: null,
  },
  {
    id: 'job-inventory-expired',
    batch_id: 'batch-inventory-1845',
    operation_id: null,
    node_id: 'node-bj-01',
    node_name: 'it-nginx-bj-01',
    action: 'inspect',
    status: 'expired',
    created_at: minutesFromNow(-48),
    expires_at: minutesFromNow(-18),
    claimed_at: null,
    completed_at: minutesFromNow(-18),
    created_by: 'admin',
    result: { summary: 'Agent 未在任务有效期内领取扫描任务' },
  },
]

const recordOperations = [
  {
    id: 'operation-config-success',
    site_id: 'api.int.example.com',
    kind: 'config_apply',
    status: 'succeeded',
    base_version: 3,
    candidate_revision_id: 'revision-4',
    created_by: 'admin',
    created_at: minutesFromNow(-19),
    updated_at: minutesFromNow(-16),
    completed_at: minutesFromNow(-16),
    metadata: {},
  },
  {
    id: 'operation-config-failed',
    site_id: 'orders.int.example.com',
    kind: 'config_move',
    status: 'failed',
    base_version: 8,
    candidate_revision_id: 'revision-9',
    created_by: 'operator.li',
    created_at: minutesFromNow(-12),
    updated_at: minutesFromNow(-9),
    completed_at: minutesFromNow(-9),
    metadata: { summary: '目标节点 nginx -t 未通过，迁移已回滚' },
  },
  {
    id: 'operation-certificate-running',
    site_id: 'console.int.example.com',
    kind: 'certificate_apply',
    status: 'running',
    base_version: 6,
    candidate_revision_id: null,
    created_by: 'admin',
    created_at: minutesFromNow(-3),
    updated_at: minutesFromNow(-1),
    completed_at: null,
    metadata: {},
  },
]

const recordAudit = [
  {
    id: 203,
    created_at: minutesFromNow(-2),
    actor_type: 'user',
    actor_id: 'admin',
    event: 'certificate.replace.requested',
    target_type: 'certificate',
    target_id: 'cert-wildcard',
    detail: {},
  },
  {
    id: 202,
    created_at: minutesFromNow(-9),
    actor_type: 'user',
    actor_id: 'operator.li',
    event: 'configuration.publish.failed',
    target_type: 'site',
    target_id: 'orders.int.example.com',
    detail: {},
  },
  {
    id: 201,
    created_at: minutesFromNow(-36),
    actor_type: 'user',
    actor_id: 'admin',
    event: 'agent.enrollment.approved',
    target_type: 'node',
    target_id: 'node-bj-01',
    detail: {},
  },
]

function send(response, body, status = 200, contentType = 'application/json; charset=utf-8') {
  response.writeHead(status, {
    'content-type': contentType,
    'cache-control': 'no-store',
  })
  response.end(contentType.startsWith('application/json') ? JSON.stringify(body) : body)
}

export const qaServer = createServer((request, response) => {
  const requestUrl = new URL(request.url || '/', `http://127.0.0.1:${port}`)
  const path = requestUrl.pathname
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
  if (path === '/api/v1/admin/jobs') return send(response, { items: recordJobs })
  if (path === '/api/v1/admin/operations') {
    return send(response, {
      items: requestUrl.searchParams.has('reconciliation_status') ? [] : recordOperations,
    })
  }
  if (path === '/api/v1/admin/enrollments') return send(response, { items: [] })
  if (path === '/api/v1/admin/monitoring/summary') {
    return send(response, { items: monitoring, server_time: now })
  }
  if (path.includes('/metrics')) {
    return send(response, {
      items: monitoringHistory,
    })
  }
  if (path === '/api/v1/admin/audit') {
    return send(response, { items: recordAudit, next_before_id: null })
  }
  return send(response, index, 200, 'text/html; charset=utf-8')
}).listen(port, '127.0.0.1', () => {
  // Intentionally quiet: this helper is used by automated visual checks.
})
