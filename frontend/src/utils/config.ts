import type { NodeRecord, SiteRecord } from '../types'

export function safeName(value: string) {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9._-]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 120) || 'site'
  )
}

export function normalizeProxyTarget(value: string) {
  const target = value.trim()
  if (!target || /^[a-z][a-z0-9+.-]*:/i.test(target) || target.startsWith('$')) return target
  return `http://${target}`
}

export function defaultSiteConfig(domain: string, target: string) {
  const normalized = normalizeProxyTarget(target || '127.0.0.1:8080')
  return [
    'server {',
    '  listen 80;',
    `  server_name ${domain || 'api.example.com'};`,
    '',
    '  location / {',
    `    proxy_pass ${normalized};`,
    '    proxy_http_version 1.1;',
    '    proxy_set_header Host $host;',
    '    proxy_set_header X-Real-IP $remote_addr;',
    '    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
    '    proxy_set_header X-Forwarded-Proto $scheme;',
    '  }',
    '}',
  ].join('\n')
}

export function nodeEntries(node: NodeRecord, context: 'http' | 'stream') {
  const entries = Array.isArray(node.facts.config_entries) ? node.facts.config_entries : []
  return entries.filter((entry) => entry.context === context)
}

export function selectedEntry(site: SiteRecord, node: NodeRecord) {
  const context = site.context === 'stream' ? 'stream' : 'http'
  const entries = nodeEntries(node, context)
  const selectedId = site.nodeConfigEntryIds?.[node.id]
  return entries.find((entry) => entry.id === selectedId) || entries.find((entry) => entry.default) || entries[0]
}

export function managedConfigFilename(site: SiteRecord) {
  if (site.filename) return site.filename
  const suffix = site.context === 'stream' ? '.stream' : '.conf'
  return `${safeName(site.domain || site.name || 'site')}${suffix}`
}

export function managedConfigPath(site: SiteRecord, node: NodeRecord) {
  if (site.nodeConfigPaths?.[node.id]) return site.nodeConfigPaths[node.id]
  if (site.context === 'main') return String(node.facts.nginx_config || '/etc/nginx/nginx.conf')
  const entry = selectedEntry(site, node)
  const root =
    entry?.directory ||
    String(node.facts.managed_config_root || '/etc/nginx/nginx-manager.d').replace(/\/+$/, '')
  return `${root}/${managedConfigFilename(site)}`
}

export function configPathForEntry(site: SiteRecord, node: NodeRecord, entryId: string) {
  const context = site.context === 'stream' ? 'stream' : 'http'
  const entry = nodeEntries(node, context).find((item) => item.id === entryId)
  if (!entry) throw new Error(`${node.node_name} 没有选中的 ${context.toUpperCase()} 配置入口`)
  return `${entry.directory.replace(/\/+$/, '')}/${managedConfigFilename(site)}`
}

export async function sha256(value: string) {
  const data = new TextEncoder().encode(value)
  const digest = await crypto.subtle.digest('SHA-256', data)
  return Array.from(new Uint8Array(digest))
    .map((item) => item.toString(16).padStart(2, '0'))
    .join('')
}

export function uid(prefix: string) {
  return `${prefix}-${crypto.randomUUID()}`
}
