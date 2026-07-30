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
  const subtle = globalThis.crypto?.subtle
  if (subtle) {
    try {
      const digest = await subtle.digest('SHA-256', data)
      return Array.from(new Uint8Array(digest))
        .map((item) => item.toString(16).padStart(2, '0'))
        .join('')
    } catch {
      // SubtleCrypto can be unavailable on an HTTP management network.
    }
  }
  return sha256Portable(data)
}

export function uid(prefix: string) {
  const browserCrypto = globalThis.crypto
  if (typeof browserCrypto?.randomUUID === 'function') {
    return `${prefix}-${browserCrypto.randomUUID()}`
  }

  const bytes = new Uint8Array(16)
  if (typeof browserCrypto?.getRandomValues === 'function') {
    browserCrypto.getRandomValues(bytes)
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256)
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (item) => item.toString(16).padStart(2, '0'))
  const id = `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-${hex
    .slice(6, 8)
    .join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10).join('')}`
  return `${prefix}-${id}`
}

function sha256Portable(data: Uint8Array) {
  const constants = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ])
  const paddedLength = Math.ceil((data.length + 9) / 64) * 64
  const padded = new Uint8Array(paddedLength)
  padded.set(data)
  padded[data.length] = 0x80
  const bitLength = data.length * 8
  const view = new DataView(padded.buffer)
  view.setUint32(paddedLength - 8, Math.floor(bitLength / 0x100000000))
  view.setUint32(paddedLength - 4, bitLength >>> 0)

  const state = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ])
  const words = new Uint32Array(64)
  const rotate = (item: number, amount: number) => (item >>> amount) | (item << (32 - amount))

  for (let offset = 0; offset < paddedLength; offset += 64) {
    for (let index = 0; index < 16; index += 1) {
      words[index] = view.getUint32(offset + index * 4)
    }
    for (let index = 16; index < 64; index += 1) {
      const first = words[index - 15]
      const second = words[index - 2]
      const sigma0 = rotate(first, 7) ^ rotate(first, 18) ^ (first >>> 3)
      const sigma1 = rotate(second, 17) ^ rotate(second, 19) ^ (second >>> 10)
      words[index] = (words[index - 16] + sigma0 + words[index - 7] + sigma1) >>> 0
    }

    let [a, b, c, d, e, f, g, h] = state
    for (let index = 0; index < 64; index += 1) {
      const sum1 = rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25)
      const choose = (e & f) ^ (~e & g)
      const temporary1 = (h + sum1 + choose + constants[index] + words[index]) >>> 0
      const sum0 = rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22)
      const majority = (a & b) ^ (a & c) ^ (b & c)
      const temporary2 = (sum0 + majority) >>> 0
      h = g
      g = f
      f = e
      e = (d + temporary1) >>> 0
      d = c
      c = b
      b = a
      a = (temporary1 + temporary2) >>> 0
    }
    state[0] = (state[0] + a) >>> 0
    state[1] = (state[1] + b) >>> 0
    state[2] = (state[2] + c) >>> 0
    state[3] = (state[3] + d) >>> 0
    state[4] = (state[4] + e) >>> 0
    state[5] = (state[5] + f) >>> 0
    state[6] = (state[6] + g) >>> 0
    state[7] = (state[7] + h) >>> 0
  }

  return Array.from(state, (item) => item.toString(16).padStart(8, '0')).join('')
}
