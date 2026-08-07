import { spawn } from 'node:child_process'
import { access, readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

process.env.QA_PORT ||= '4194'
const port = Number(process.env.QA_PORT)
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error(`Invalid QA_PORT: ${process.env.QA_PORT}`)
}

const candidates = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
]
let browser = ''
for (const candidate of candidates) {
  try {
    await access(candidate)
    browser = candidate
    break
  } catch {
    // Try the next installed browser.
  }
}
if (!browser) throw new Error('No supported headless browser was found')
const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')

function renderedHtml() {
  return new Promise((resolve, reject) => {
    const child = spawn(
      browser,
      [
        '--headless=new',
        '--disable-gpu',
        '--virtual-time-budget=2600',
        '--dump-dom',
        `http://127.0.0.1:${port}/#/lvs`,
      ],
      { windowsHide: true },
    )
    let output = ''
    let error = ''
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => { output += chunk })
    child.stderr.on('data', (chunk) => { error += chunk })
    child.once('error', reject)
    child.once('exit', (code) => {
      if (code === 0) resolve(output)
      else reject(new Error(error || `headless browser exited with ${code}`))
    })
  })
}

const { qaServer } = await import('./qa-server.mjs')
try {
  const html = await renderedHtml()
  const visibleHtml = html
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, '')
  const required = [
    'LVS',
    '配置管理与运行健康是两条独立证据链',
    'Virtual Services',
    'Backend Pools',
    'Pool Members',
    '新增 Virtual Service',
    '接管暂不可用',
    '外部配置',
    '接管会先展示权威差异',
    '管理 / 运行',
    '192.0.2.110:443',
    '192.0.2.108:53',
    '配置漂移',
    '已停用',
    '健康状态需由外部 Monitor 证明',
    'IPVS 规则已观测',
  ]
  for (const text of required) {
    if (!visibleHtml.includes(text)) throw new Error(`LVS page is missing: ${text}`)
  }

  const renderedStatusTags = [...visibleHtml.matchAll(/<span\b([^>]*)>([\s\S]*?)<\/span>/gi)]
    .filter(([, attributes]) => /\bstatus-tag\b/.test(attributes))
    .map(([, attributes, body]) => ({
      label: body.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim(),
      tone: attributes.match(/\bdata-tone="([^"]+)"/)?.[1] || '',
    }))
  const tonesFor = (label) => renderedStatusTags
    .filter((tag) => tag.label === label)
    .map((tag) => tag.tone)
  const assertInformationalTone = (label) => {
    const tones = tonesFor(label)
    if (!tones.length) throw new Error(`LVS status semantic fixture is missing: ${label}`)
    if (tones.some((tone) => !['neutral', 'info'].includes(tone))) {
      throw new Error(`${label} is informational, but rendered with attention tone(s): ${tones.join(', ')}`)
    }
  }
  assertInformationalTone('外部配置')
  assertInformationalTone('已观测')

  const [apiSource, viewSource, utilitySource] = await Promise.all([
    readFile(resolve(projectRoot, 'src/api.ts'), 'utf8'),
    readFile(resolve(projectRoot, 'src/views/LvsView.vue'), 'utf8'),
    readFile(resolve(projectRoot, 'src/utils/ipvs.ts'), 'utf8'),
  ])
  const informationalStateContract = [
    "if (state === 'partial') return { label: '观测不完整', tone: 'info' }",
    "return { label: '已观测', tone: 'info' }",
  ]
  for (const token of informationalStateContract) {
    if (!viewSource.includes(token)) throw new Error(`LVS UI is missing informational observation state: ${token}`)
  }
  for (const obsoleteLabel of ['现有配置·待接管', '部分数据']) {
    if (viewSource.includes(obsoleteLabel) || visibleHtml.includes(obsoleteLabel)) {
      throw new Error(`LVS UI still exposes ambiguous warning copy: ${obsoleteLabel}`)
    }
  }
  const safePlanContract = [
    '/api/v1/admin/lvs/plans',
    'jsonBody({ node_ids: nodeIds, intent, adopt_existing: adoptExisting })',
    '/apply',
    'plan_digest: planDigest',
    'request_id: requestId',
  ]
  for (const token of safePlanContract) {
    if (!apiSource.includes(token)) throw new Error(`LVS API is missing safe plan/apply token: ${token}`)
  }
  const safeInteractionContract = [
    "type DraftMode = 'create' | 'edit' | 'takeover' | 'delete'",
    'previewDraft',
    'planConfirmed',
    "const planRequestId = ref('')",
    'planRequestId.value = requestId()',
    'planRequestId.value)',
    "node.capabilities.includes('lvs_manage_v1')",
    'lvsServiceEditable',
    'canonicalLvsService',
    'lvsPlanSemanticDiff(',
    "nodeIds: []",
    'group.drift || group.partial',
    "service.origin !== 'managed'",
    "snapshot.node.capabilities.includes('lvs_adopt_v1')",
    "const rollbackStatus = String(result.rollback_status || '')",
    '二次确认并发布',
    '新增成员',
    '接管暂不可用',
    '回滚状态未知',
    '外部配置，平台保持只读',
    '含不支持指令·只读',
    '前序 Director 发布失败',
    'toggleMemberMonitor',
    'member.monitor.connect_timeout',
    'plannedHasChanges',
    'lvsTopologyForNode',
    '单 Director 发布没有 VRRP 主备接管能力',
  ]
  for (const token of safeInteractionContract) {
    if (!viewSource.includes(token)) throw new Error(`LVS UI is missing safe workflow token: ${token}`)
  }
  if (!/state === 'partial'[\s\S]{0,100}tone:\s*'(?:neutral|info)'/.test(viewSource)) {
    throw new Error('Partial LVS observation must remain visible without using the warning attention tone')
  }
  for (const token of ['buildLvsSemanticDiff', 'lvsPlanSemanticDiff', 'lvsServiceEditable']) {
    if (!utilitySource.includes(token)) throw new Error(`LVS safety utility is missing: ${token}`)
  }
  const forbiddenInputs = ['raw config', 'shell command', 'ipvsadm command', '执行 ipvsadm']
  for (const token of forbiddenInputs) {
    if (viewSource.toLowerCase().includes(token.toLowerCase())) {
      throw new Error(`LVS UI exposes unsafe free-form input: ${token}`)
    }
  }
  console.log('lvs safe plan/apply management contract: ok')
} finally {
  await new Promise((resolve) => qaServer.close(resolve))
}
