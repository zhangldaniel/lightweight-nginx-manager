import type {
  CertificateRecord,
  JobRecord,
  NodeRecord,
  SiteRecord,
  UiState,
} from '../types'
import { uid } from './config'

type InventoryFile = {
  path?: string
  content?: string
  sha256?: string
  context?: 'http' | 'stream' | 'main'
  entry_id?: string
  read_only?: boolean
}

type CertificateInventoryItem = {
  certificate_path?: string
  private_key_path?: string
  certificate_sha256?: string
  key_material_sha256?: string
  fingerprint?: string
  domains?: string[]
  subject?: string
  issuer?: string
  days_remaining?: number
  not_after?: string
}

export interface InventoryImportSummary {
  changed: boolean
  configurations: number
  certificates: number
  failures: number
  failureMessages: string[]
  skipped: number
  truncated: boolean
}

function directiveValues(content: string, directive: string) {
  const pattern = new RegExp(`(?:^|[;{}\\s])${directive}\\s+([^;{}]+);`, 'gm')
  return Array.from(content.matchAll(pattern), (match) => String(match[1] || '').trim()).filter(
    Boolean,
  )
}

function metadata(file: InventoryFile) {
  const path = String(file.path || '')
  const context =
    file.context === 'main'
      ? 'main'
      : file.context === 'stream' || /\.stream$/i.test(path)
        ? 'stream'
        : 'http'
  const filename =
    path.split('/').pop() || (context === 'stream' ? 'imported.stream' : 'imported.conf')
  const content = String(file.content || '')
  if (context === 'main') {
    return {
      resourceType: 'generic' as const,
      context,
      name: 'Nginx 主配置',
      domain: 'Nginx 主配置',
      aliases: [] as string[],
      type: 'custom',
      target: path,
      filename,
    }
  }
  if (context === 'stream') {
    const name = filename.replace(/\.stream$/i, '')
    return {
      resourceType: 'generic' as const,
      context,
      name,
      domain: name,
      aliases: [] as string[],
      type: 'custom',
      target: 'Stream 配置',
      filename,
    }
  }

  const serverNames = directiveValues(content, 'server_name')
    .join(' ')
    .split(/\s+/)
    .map((name) => name.replace(/^["']|["']$/g, ''))
    .filter(Boolean)
  const businessNames = serverNames.filter(
    (name) =>
      !['_', 'localhost', '127.0.0.1', '[::1]'].includes(name) &&
      !name.startsWith('~') &&
      !name.startsWith('$'),
  )
  const listens = directiveValues(content, 'listen')
  const hasStubStatus = /(?:^|[;{}\s])stub_status\s*;/m.test(content)
  const loopbackOnly =
    listens.length > 0 &&
    listens.every((value) => /^(?:127\.0\.0\.1|\[::1\])(?::\d+)?(?:\s|$)/.test(value))
  const resourceType = businessNames.length && !(hasStubStatus && loopbackOnly) ? 'site' : 'generic'
  const baseName = filename.replace(/\.conf$/i, '')
  const proxyTargets = ['proxy_pass', 'grpc_pass', 'fastcgi_pass', 'uwsgi_pass']
    .map((directive) => directiveValues(content, directive))
    .find((values) => values.length)
  const roots = directiveValues(content, 'root')
  return {
    resourceType: resourceType as 'site' | 'generic',
    context,
    name:
      resourceType === 'generic' ? (hasStubStatus ? 'Nginx Status' : baseName) : businessNames[0],
    domain: businessNames[0] || baseName,
    aliases: businessNames.slice(1),
    type: proxyTargets?.length ? 'proxy' : roots.length ? 'static' : 'proxy',
    target: proxyTargets?.[0] || roots[0] || '现有 Nginx 配置',
    filename,
  }
}

function importConfiguration(ui: UiState, file: InventoryFile, node: NodeRecord) {
  const path = String(file.path || '')
  const content = typeof file.content === 'string' ? file.content : ''
  const hash = String(file.sha256 || '')
  if (!path || !content || !hash) return false
  const info = metadata(file)
  let site = ui.sites.find((candidate) => candidate.nodeConfigPaths?.[node.id] === path)
  if (!site && info.context !== 'main') {
    site = ui.sites.find(
      (candidate) =>
        candidate.filename === info.filename &&
        candidate.resourceType === info.resourceType &&
        (candidate.context || 'http') === info.context &&
        (candidate.name || candidate.domain) === info.name,
    )
  }
  if (!site) {
    site = {
      id: uid('site'),
      resourceType: info.resourceType,
      context: info.context as SiteRecord['context'],
      name: info.name,
      filename: info.filename,
      domain: info.resourceType === 'site' ? info.domain : undefined,
      type: info.type,
      target: info.target,
      environment: '生产',
      nodeIds: [],
      certificateId: '',
      configMode: info.resourceType === 'generic' ? 'generic' : 'conf',
      version: 1,
      status: 'published',
      note: `从节点现有配置导入：${path}${
        info.aliases.length ? `；其他域名：${info.aliases.join('、')}` : ''
      }`,
      changeNote: '从节点导入现有配置',
      updatedAt: new Date().toISOString(),
      config: content,
      nodeHashes: {},
      nodeConfigPaths: {},
      nodeConfigEntryIds: {},
      nodeConfigs: {},
      nodeReadOnly: {},
      history: [
        {
          version: 1,
          note: '从节点导入现有配置（未修改节点文件）',
          time: new Date().toISOString(),
          user: '控制端',
        },
      ],
    }
    ui.sites.unshift(site)
  }

  const resolved = site as SiteRecord
  resolved.nodeHashes ||= {}
  resolved.nodeConfigPaths ||= {}
  resolved.nodeConfigEntryIds ||= {}
  resolved.nodeConfigs ||= {}
  const nodeReadOnly = ((resolved.nodeReadOnly ||= {}) as Record<string, boolean>)
  const changed =
    resolved.nodeHashes[node.id] !== hash ||
    resolved.nodeConfigPaths[node.id] !== path ||
    (content !== resolved.config && resolved.nodeConfigs[node.id] !== content) ||
    (content === resolved.config && Object.hasOwn(resolved.nodeConfigs, node.id)) ||
    !resolved.nodeIds.includes(node.id)

  resolved.nodeHashes[node.id] = hash
  resolved.nodeConfigPaths[node.id] = path
  if (file.entry_id) resolved.nodeConfigEntryIds[node.id] = file.entry_id
  nodeReadOnly[node.id] = Boolean(file.read_only)
  if (content === resolved.config) delete resolved.nodeConfigs[node.id]
  else resolved.nodeConfigs[node.id] = content
  if (!resolved.nodeIds.includes(node.id)) resolved.nodeIds.push(node.id)
  if (Object.keys(resolved.nodeConfigs).length) {
    resolved.status = 'drift'
    resolved.changeNote = '扫描发现节点实际配置与平台配置不一致'
  } else if (['drift', 'failed', 'unassigned'].includes(resolved.status)) {
    resolved.status = 'published'
    resolved.changeNote = '重新扫描确认各节点配置一致'
  }
  return changed
}

function importCertificate(
  ui: UiState,
  item: CertificateInventoryItem,
  node: NodeRecord,
) {
  const certificatePath = String(item.certificate_path || '')
  const keyPath = String(item.private_key_path || '')
  if (!certificatePath || !keyPath) return false
  let certificate = ui.certificates.find(
    (candidate) => candidate.nodePaths?.[node.id]?.certificatePath === certificatePath,
  )
  if (!certificate && item.fingerprint) {
    certificate = ui.certificates.find(
      (candidate) =>
        candidate.fingerprint === item.fingerprint && !candidate.nodeIds.includes(node.id),
    )
  }
  const domains = Array.isArray(item.domains) ? item.domains.filter(Boolean) : []
  const domain =
    domains[0] ||
    String(item.subject || '') ||
    certificatePath.split('/').pop()?.replace(/\.(?:pem|crt)$/i, '') ||
    '未命名证书'
  if (!certificate) {
    certificate = {
      id: uid('cert'),
      name: domain,
      domain,
      domains: domains.length ? domains : [domain],
      issuer: item.issuer || '未知签发者',
      daysLeft: Number(item.days_remaining || 0),
      source: '节点导入',
      status: 'normal',
      nodeIds: [],
      nodeHashes: {},
      nodePaths: {},
      linkedSiteIds: [],
      note: '从节点现有证书导入；平台只保存证书信息、文件路径和校验值',
      fingerprint: item.fingerprint,
      expiresAt: item.not_after,
      notAfter: item.not_after,
    }
    ui.certificates.unshift(certificate)
  }

  certificate.name ||= domain
  certificate.domain = domain
  certificate.domains = domains.length ? domains : certificate.domains || [domain]
  certificate.issuer = item.issuer || certificate.issuer || '未知签发者'
  certificate.daysLeft = Number(item.days_remaining || 0)
  certificate.source ||= '节点导入'
  certificate.fingerprint = item.fingerprint || certificate.fingerprint
  certificate.expiresAt = item.not_after || certificate.expiresAt
  certificate.notAfter = item.not_after || certificate.notAfter
  certificate.nodeIds ||= []
  certificate.nodePaths ||= {}
  certificate.nodeHashes ||= {}
  certificate.linkedSiteIds ||= []
  certificate.nodePaths[node.id] = { certificatePath, keyPath }
  certificate.nodeHashes[node.id] = {
    certificateHash: item.certificate_sha256,
    keyHash: item.key_material_sha256,
  }
  if (!certificate.nodeIds.includes(node.id)) certificate.nodeIds.push(node.id)

  for (const site of ui.sites) {
    if (
      site.resourceType !== 'generic' &&
      site.nodeIds.includes(node.id) &&
      String(site.config || '').includes(certificatePath)
    ) {
      site.certificateId = certificate.id
      if (!certificate.linkedSiteIds.includes(site.id)) certificate.linkedSiteIds.push(site.id)
    }
  }
  return true
}

export function processInventoryJobs(
  ui: UiState,
  nodes: NodeRecord[],
  jobs: JobRecord[],
): InventoryImportSummary {
  const result: InventoryImportSummary = {
    changed: false,
    configurations: 0,
    certificates: 0,
    failures: 0,
    failureMessages: [],
    skipped: 0,
    truncated: false,
  }
  const nodeById = new Map(nodes.map((node) => [node.id, node]))
  const configProcessed = new Set(ui.importedInventoryJobs || [])
  const certificateProcessed = new Set(ui.importedCertificateInventoryJobs || [])
  const terminal = new Set(['succeeded', 'failed', 'expired'])

  for (const job of jobs) {
    if (!terminal.has(job.status)) continue
    if (job.action === 'config_inventory' && !configProcessed.has(job.id)) {
      configProcessed.add(job.id)
      result.changed = true
      const inventory = job.result?.config_inventory as
        | {
            files?: InventoryFile[]
            main_config?: InventoryFile
            skipped_count?: number
            truncated?: boolean
          }
        | undefined
      const node = nodeById.get(job.node_id)
      if (job.status !== 'succeeded' || !inventory || !node) {
        result.failures += 1
        const failure = String(job.result?.error || job.result?.failure_code || job.status)
        result.failureMessages.push(`${node?.node_name || job.node_name || job.node_id}: ${failure}`)
      } else {
        for (const file of inventory.files || []) {
          if (importConfiguration(ui, file, node)) result.configurations += 1
        }
        if (inventory.main_config) {
          const main = { ...inventory.main_config, context: 'main' as const, entry_id: 'main-config' }
          if (importConfiguration(ui, main, node)) result.configurations += 1
        }
        result.skipped += Number(inventory.skipped_count || 0)
        result.truncated ||= Boolean(inventory.truncated)
      }
    }
    if (job.action === 'certificate_inventory' && !certificateProcessed.has(job.id)) {
      certificateProcessed.add(job.id)
      result.changed = true
      const inventory = job.result?.certificate_inventory as
        | {
            certificates?: CertificateInventoryItem[]
            skipped_count?: number
            truncated?: boolean
          }
        | undefined
      const node = nodeById.get(job.node_id)
      if (job.status !== 'succeeded' || !inventory || !node) {
        result.failures += 1
        const failure = String(job.result?.error || job.result?.failure_code || job.status)
        result.failureMessages.push(`${node?.node_name || job.node_name || job.node_id}: ${failure}`)
      } else {
        for (const item of inventory.certificates || []) {
          if (importCertificate(ui, item, node)) result.certificates += 1
        }
        result.skipped += Number(inventory.skipped_count || 0)
        result.truncated ||= Boolean(inventory.truncated)
      }
    }
  }
  ui.importedInventoryJobs = Array.from(configProcessed).slice(-200)
  ui.importedCertificateInventoryJobs = Array.from(certificateProcessed).slice(-200)
  return result
}
