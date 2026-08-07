import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const source = await readFile(resolve(projectRoot, 'src/utils/ipvs.ts'), 'utf8')
const output = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText
const { buildLvsOverview } = await import(`data:text/javascript;base64,${Buffer.from(output).toString('base64')}`)

function node(id, vip, vrid, members, weight) {
  return {
    id,
    node_name: id,
    hostname: id,
    labels: {},
    status: 'online',
    reported_status: 'online',
    agent_version: 'test',
    nginx_version: null,
    config_hash: null,
    capabilities: ['ipvs_observer_v1'],
    facts: {
      keepalived: {
        vip,
        role: id.endsWith('1') ? 'MASTER' : 'BACKUP',
        config_summary: {
          summary_complete: true,
          truncated: false,
          instances: [{ virtual_router_id: vrid, virtual_ips: [vip, '192.0.2.60'] }],
        },
      },
      ipvs: {
        available: true,
        source: 'procfs',
        services: [{
          id: 'tcp-192-0-2-60-443',
          kind: 'address',
          protocol: 'TCP',
          address: '192.0.2.60',
          port: 443,
          scheduler: 'wrr',
          one_packet: false,
          active_connections: 0,
          inactive_connections: 0,
          destinations: members.map((address) => ({
            address,
            port: 443,
            forwarding: 'dr',
            weight,
            active_connections: 0,
            inactive_connections: 0,
          })),
        }],
      },
    },
    enrolled_at: null,
    last_seen_at: null,
    revoked_at: null,
  }
}

const overview = buildLvsOverview([
  node('director-a1', '192.0.2.40', 40, ['192.0.2.43'], 1),
  node('director-a2', '192.0.2.40', 40, ['192.0.2.43'], 1),
  node('director-b1', '192.0.2.50', 50, ['192.0.2.53'], 2),
])

if (overview.groups.length !== 2) {
  throw new Error(`unrelated VRRP groups were merged: expected 2, got ${overview.groups.length}`)
}
if (overview.groups.some((group) => group.drift || group.missingDirectorCount !== 0)) {
  throw new Error('an unrelated VRRP group was counted as a missing or drifting Director')
}

console.log('ipvs VRRP group isolation: ok')
