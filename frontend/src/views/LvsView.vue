<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Boxes,
  CircleDot,
  Network,
  RefreshCw,
  Route,
  Server,
  ShieldCheck,
  Waypoints,
} from '@lucide/vue'
import { NButton, NInput, NSelect } from 'naive-ui'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import { useConsoleStore } from '../stores/console'
import type { LvsMember, Tone } from '../types'
import {
  buildLvsOverview,
  lvsMemberKey,
  lvsObservation,
  type LvsVirtualServiceGroup,
} from '../utils/ipvs'

type ObjectMode = 'services' | 'pools' | 'members'

const store = useConsoleStore()
const objectMode = ref<ObjectMode>('services')
const query = ref('')
const directorId = ref('all')
const selectedKey = ref('')
const detailDirectorId = ref('all')
const refreshing = ref(false)

const overview = computed(() => buildLvsOverview(store.nodes))
const directorOptions = computed(() => [
  { label: `全部 Director（${overview.value.availableNodes.length}）`, value: 'all' },
  ...overview.value.capableNodes.map((node) => ({
    label: `${node.node_name} · ${node.status === 'offline' ? '离线' : '在线'}`,
    value: node.id,
  })),
])

function snapshots(group: LvsVirtualServiceGroup) {
  return directorId.value === 'all'
    ? group.snapshots
    : group.snapshots.filter((snapshot) => snapshot.node.id === directorId.value)
}

const filteredGroups = computed(() => {
  const needle = query.value.trim().toLowerCase()
  return overview.value.groups.filter((group) => {
    const visible = snapshots(group)
    if (!visible.length) return false
    if (!needle) return true
    const haystack = [
      group.label,
      group.protocol,
      group.scheduler,
      String(group.port ?? ''),
      String(group.fwmark ?? ''),
      ...visible.map((snapshot) => snapshot.node.node_name),
      ...visible.flatMap((snapshot) => snapshot.service.destinations.flatMap((member) => [
        member.address,
        String(member.port),
        member.forwarding,
      ])),
    ].join(' ').toLowerCase()
    return haystack.includes(needle)
  })
})

watch(filteredGroups, (groups) => {
  if (!groups.some((group) => group.key === selectedKey.value)) {
    selectedKey.value = groups[0]?.key || ''
  }
}, { immediate: true })

const selectedGroup = computed(() =>
  overview.value.groups.find((group) => group.key === selectedKey.value) || null,
)

watch(selectedKey, () => {
  detailDirectorId.value = 'all'
})

interface MemberRow {
  key: string
  group: LvsVirtualServiceGroup
  member: LvsMember
  directors: string[]
  weights: number[]
  forwarding: string[]
  activeConnections: number
  inactiveConnections: number
}

function memberRows(group: LvsVirtualServiceGroup, onlyVisible = false): MemberRow[] {
  const rows = new Map<string, MemberRow>()
  const source = onlyVisible ? snapshots(group) : group.snapshots
  for (const snapshot of source) {
    for (const member of snapshot.service.destinations) {
      const key = lvsMemberKey(member)
      const row = rows.get(key) || {
        key,
        group,
        member,
        directors: [],
        weights: [],
        forwarding: [],
        activeConnections: 0,
        inactiveConnections: 0,
      }
      row.directors.push(snapshot.node.node_name)
      row.weights.push(member.weight)
      row.forwarding.push(member.forwarding)
      row.activeConnections = Math.max(row.activeConnections, member.active_connections)
      row.inactiveConnections = Math.max(row.inactiveConnections, member.inactive_connections)
      rows.set(key, row)
    }
  }
  return [...rows.values()].sort((left, right) => left.key.localeCompare(right.key, undefined, { numeric: true }))
}

const objectRows = computed(() => {
  if (objectMode.value === 'members') {
    return filteredGroups.value.flatMap((group) => memberRows(group, true).map((row) => ({
      id: `${group.key}|${row.key}`,
      group,
      title: `${row.member.address}:${row.member.port}`,
      subtitle: 'Pool Member · Real Server',
      relation: group.label,
      method: `${[...new Set(row.forwarding)].join(' / ')} · weight ${[...new Set(row.weights)].join(' / ')}`,
      directors: row.directors,
      active: row.activeConnections,
      inactive: row.inactiveConnections,
      state: row.weights.every((weight) => weight === 0) ? 'disabled' : group.drift ? 'drift' : group.partial ? 'partial' : 'observed',
    })))
  }
  return filteredGroups.value.map((group) => ({
    id: group.key,
    group,
    title: objectMode.value === 'pools' ? `Pool · ${group.label}` : group.label,
    subtitle: objectMode.value === 'pools' ? '派生 Backend Pool' : `${group.protocol} Virtual Service`,
    relation: objectMode.value === 'pools' ? group.label : `${group.memberCount} 个 Pool Member`,
    method: group.scheduler + (group.persistenceSeconds !== null ? ` · persistence ${group.persistenceSeconds}s` : ''),
    directors: snapshots(group).map((snapshot) => snapshot.node.node_name),
    active: group.activeConnections,
    inactive: group.inactiveConnections,
    state: group.drift ? 'drift' : group.partial ? 'partial' : 'observed',
  }))
})

const detailSnapshots = computed(() => {
  if (!selectedGroup.value) return []
  return detailDirectorId.value === 'all'
    ? selectedGroup.value.snapshots
    : selectedGroup.value.snapshots.filter((snapshot) => snapshot.node.id === detailDirectorId.value)
})
const detailMembers = computed(() => selectedGroup.value
  ? memberRows({ ...selectedGroup.value, snapshots: detailSnapshots.value })
  : [],
)

function stateMeta(state: string): { label: string; tone: Tone } {
  if (state === 'drift') return { label: '配置漂移', tone: 'danger' }
  if (state === 'disabled') return { label: '已停用', tone: 'warning' }
  if (state === 'partial') return { label: '部分数据', tone: 'warning' }
  return { label: '已观测', tone: 'success' }
}

function forwardingLabel(value: string) {
  return ({ nat: 'NAT', dr: 'DR', tunnel: 'TUN', local: 'LOCAL', bypass: 'BYPASS', unknown: '未知' } as Record<string, string>)[value] || value
}

function unavailableLabel(reason?: string) {
  return ({
    disabled: '观察器未启用',
    not_loaded: 'IPVS 模块未加载',
    permission_denied: 'procfs 无读取权限',
    unavailable: 'IPVS 状态不可用',
    invalid_format: 'IPVS 数据格式无法识别',
    helper_unavailable: '特权 Helper 不可用',
  } as Record<string, string>)[reason || ''] || '尚未收到有效观测'
}

function formatNumber(value: number) {
  return new Intl.NumberFormat('zh-CN').format(value || 0)
}

function formatEndpoint(member: LvsMember) {
  return member.address.includes(':') ? `[${member.address}]:${member.port}` : `${member.address}:${member.port}`
}

async function refresh() {
  refreshing.value = true
  try {
    await store.refresh(false, true)
  } catch (error) {
    store.notify('LVS 状态刷新失败', 'danger', store.apiMessage(error))
  } finally {
    refreshing.value = false
  }
}
</script>

<template>
  <section class="page page-lvs">
    <PageHeader
      eyebrow="LAYER 4 DELIVERY"
      title="LVS"
      description="按 Virtual Service、Backend Pool 和 Pool Member 查看宿主机 IPVS 的实时转发表。"
    >
      <StatusTag label="只读观测" tone="info" />
      <NButton secondary :loading="refreshing" @click="refresh">
        <template #icon><RefreshCw :size="16" /></template>
        刷新状态
      </NButton>
    </PageHeader>

    <div class="lvs-scope-note">
      <ShieldCheck :size="17" />
      <div>
        <strong>遵循 F5 的对象层级，不伪装成 F5 配置面。</strong>
        <span>Backend Pool 是由每个 IPVS Virtual Service 派生的视图；当前版本不会执行 ipvsadm，也不会修改权重或转发规则。</span>
      </div>
    </div>

    <section class="lvs-kpi-rail" aria-label="LVS 概览">
      <article>
        <span><Waypoints :size="16" /> Virtual Services</span>
        <strong>{{ overview.virtualServiceCount }}</strong>
        <small>{{ overview.availableNodes.length }} 个 Director 已观测</small>
      </article>
      <article>
        <span><Boxes :size="16" /> Backend Pools</span>
        <strong>{{ overview.poolCount }}</strong>
        <small>每个服务派生一个后端池</small>
      </article>
      <article>
        <span><Server :size="16" /> Pool Members</span>
        <strong>{{ overview.memberCount }}</strong>
        <small v-if="overview.disabledMemberCount">{{ overview.disabledMemberCount }} 个权重为 0</small>
        <small v-else>存在不等于健康</small>
      </article>
      <article :class="{ attention: overview.driftCount }">
        <span><AlertTriangle :size="16" /> Director 对账</span>
        <strong>{{ overview.driftCount }}</strong>
        <small>{{ overview.driftCount ? '存在配置漂移' : '已观测规则一致' }}</small>
      </article>
      <article>
        <span><Activity :size="16" /> 活跃连接</span>
        <strong>{{ formatNumber(overview.activeConnections) }}</strong>
        <small>{{ formatNumber(overview.connectionsPerSecond) }} CPS · Director 合计</small>
      </article>
    </section>

    <div v-if="!overview.capableNodes.length" class="lvs-empty-state">
      <div class="empty-icon"><Route :size="30" /></div>
      <h2>还没有启用 LVS 观察器</h2>
      <p>在需要观测的 Director 上重新运行 Agent 安装命令，并添加 <code>--enable-lvs-observer</code>。</p>
      <span>观察器只读取宿主机命名空间的 /proc/net/ip_vs，不会加载模块或改变规则。</span>
    </div>

    <template v-else>
      <div v-if="overview.unavailableNodes.length" class="lvs-warning-strip">
        <AlertTriangle :size="17" />
        <div>
          <strong>{{ overview.unavailableNodes.length }} 个 Director 暂无 IPVS 数据</strong>
          <span v-for="node in overview.unavailableNodes" :key="node.id">
            {{ node.node_name }}：{{ unavailableLabel(lvsObservation(node)?.reason) }}
          </span>
        </div>
      </div>

      <div v-if="overview.availableNodes.length && !overview.groups.length" class="lvs-empty-state compact">
        <div class="empty-icon"><Network :size="28" /></div>
        <h2>IPVS 已就绪，但没有 Virtual Service</h2>
        <p>Agent 已成功读取内核状态；当前转发表为空，这不是故障。</p>
      </div>

      <div v-else-if="overview.groups.length" class="lvs-workbench">
        <section class="lvs-browser" aria-label="LVS 对象浏览器">
          <div class="lvs-browser-tabs" role="tablist" aria-label="LVS 对象类型">
            <button :class="{ active: objectMode === 'services' }" @click="objectMode = 'services'">
              Virtual Services <span>{{ overview.virtualServiceCount }}</span>
            </button>
            <button :class="{ active: objectMode === 'pools' }" @click="objectMode = 'pools'">
              Backend Pools <span>{{ overview.poolCount }}</span>
            </button>
            <button :class="{ active: objectMode === 'members' }" @click="objectMode = 'members'">
              Pool Members <span>{{ overview.memberCount }}</span>
            </button>
          </div>
          <div class="lvs-toolbar">
            <NInput v-model:value="query" clearable placeholder="搜索 VIP、端口、协议、Member IP 或 Director">
              <template #prefix><Waypoints :size="16" /></template>
            </NInput>
            <NSelect v-model:value="directorId" :options="directorOptions" />
          </div>

          <div class="lvs-table" role="table" aria-label="LVS 对象列表">
            <div class="lvs-table-head" role="row">
              <span>对象</span><span>关联对象</span><span>调度 / 转发</span><span>Director</span><span>连接</span><span>状态</span>
            </div>
            <button
              v-for="row in objectRows"
              :key="row.id"
              class="lvs-table-row"
              :class="{ selected: row.group.key === selectedKey }"
              role="row"
              @click="selectedKey = row.group.key"
            >
              <span class="object-cell">
                <strong>{{ row.title }}</strong>
                <small>{{ row.subtitle }}</small>
              </span>
              <span><strong>{{ row.relation }}</strong></span>
              <span><code>{{ row.method }}</code></span>
              <span class="director-chips">
                <i v-for="name in row.directors.slice(0, 2)" :key="name"><CircleDot :size="10" /> {{ name }}</i>
                <i v-if="row.directors.length > 2">+{{ row.directors.length - 2 }}</i>
              </span>
              <span class="connection-cell"><strong>{{ formatNumber(row.active) }}</strong><small>active · {{ formatNumber(row.inactive) }} inactive</small></span>
              <span><StatusTag v-bind="stateMeta(row.state)" /></span>
            </button>
            <div v-if="!objectRows.length" class="lvs-no-results">没有匹配当前搜索和 Director 条件的对象。</div>
          </div>
        </section>

        <aside v-if="selectedGroup" class="lvs-detail" aria-label="Virtual Service 详情">
          <header class="lvs-detail-header">
            <div>
              <span class="detail-eyebrow">SELECTED VIRTUAL SERVICE</span>
              <h2>{{ selectedGroup.label }}</h2>
              <p>{{ selectedGroup.protocol }} · {{ selectedGroup.scheduler }} scheduler</p>
            </div>
            <StatusTag
              v-bind="stateMeta(selectedGroup.drift ? 'drift' : selectedGroup.partial ? 'partial' : 'observed')"
            />
          </header>

          <div class="lvs-detail-facts">
            <div><span>Virtual Service</span><strong>{{ selectedGroup.label }}</strong></div>
            <div><span>Scheduler</span><strong>{{ selectedGroup.scheduler }}</strong></div>
            <div><span>Persistence</span><strong>{{ selectedGroup.persistenceSeconds === null ? '未启用' : `${selectedGroup.persistenceSeconds}s` }}</strong></div>
            <div><span>同步状态</span><strong>{{ selectedGroup.drift ? '配置漂移' : '规则一致' }}</strong></div>
          </div>

          <section class="lvs-topology">
            <div class="section-title"><Network :size="16" /><strong>实时流量路径</strong><span>Observed State</span></div>
            <div class="topology-flow">
              <div class="topology-node client"><span>CLIENT</span><strong>客户端流量</strong></div>
              <ArrowRight class="flow-arrow" :size="20" />
              <div class="topology-node virtual"><span>VIRTUAL SERVICE</span><strong>{{ selectedGroup.label }}</strong><small>{{ selectedGroup.protocol }} · {{ selectedGroup.scheduler }}</small></div>
              <ArrowRight class="flow-arrow" :size="20" />
              <div class="topology-node pool"><span>DERIVED POOL</span><strong>{{ selectedGroup.memberCount }} Members</strong></div>
            </div>
            <div class="topology-members">
              <span v-for="member in detailMembers.slice(0, 8)" :key="member.key" :class="{ disabled: member.weights.every((weight) => weight === 0) }">
                <CircleDot :size="11" /> {{ formatEndpoint(member.member) }}
              </span>
              <span v-if="detailMembers.length > 8">+{{ detailMembers.length - 8 }} Members</span>
            </div>
          </section>

          <section class="lvs-detail-section">
            <div class="section-title"><Server :size="16" /><strong>Director 快照</strong><span>{{ selectedGroup.snapshots.length }} 份</span></div>
            <div class="snapshot-tabs">
              <button :class="{ active: detailDirectorId === 'all' }" @click="detailDirectorId = 'all'">对比全部</button>
              <button
                v-for="snapshot in selectedGroup.snapshots"
                :key="snapshot.node.id"
                :class="{ active: detailDirectorId === snapshot.node.id }"
                @click="detailDirectorId = snapshot.node.id"
              >{{ snapshot.node.node_name }}</button>
            </div>
            <div class="snapshot-grid">
              <article v-for="snapshot in detailSnapshots" :key="snapshot.node.id">
                <header><strong>{{ snapshot.node.node_name }}</strong><StatusTag :label="snapshot.node.status === 'offline' ? '数据已过期' : '在线'" :tone="snapshot.node.status === 'offline' ? 'warning' : 'success'" /></header>
                <dl>
                  <div><dt>Scheduler</dt><dd>{{ snapshot.service.scheduler }}</dd></div>
                  <div><dt>Members</dt><dd>{{ snapshot.service.destinations.length }}</dd></div>
                  <div><dt>Active</dt><dd>{{ formatNumber(snapshot.service.active_connections) }}</dd></div>
                  <div><dt>采样</dt><dd>{{ snapshot.node.last_seen_at || '—' }}</dd></div>
                </dl>
              </article>
            </div>
          </section>

          <section class="lvs-detail-section">
            <div class="section-title"><Boxes :size="16" /><strong>Pool Members</strong><span>健康状态需由外部 Monitor 证明</span></div>
            <div class="member-list">
              <article v-for="row in detailMembers" :key="row.key">
                <div class="member-identity"><CircleDot :size="13" /><strong>{{ formatEndpoint(row.member) }}</strong><small>Real Server</small></div>
                <div><span>Forward</span><strong>{{ [...new Set(row.forwarding)].map(forwardingLabel).join(' / ') }}</strong></div>
                <div><span>Weight</span><strong>{{ [...new Set(row.weights)].join(' / ') }}</strong></div>
                <div><span>Connections</span><strong>{{ formatNumber(row.activeConnections) }} / {{ formatNumber(row.inactiveConnections) }}</strong></div>
                <StatusTag :label="row.weights.every((weight) => weight === 0) ? '已停用' : '可调度'" :tone="row.weights.every((weight) => weight === 0) ? 'warning' : 'info'" />
              </article>
              <div v-if="!detailMembers.length" class="member-empty">该 Virtual Service 暂无 Pool Member。</div>
            </div>
          </section>
        </aside>
      </div>
    </template>
  </section>
</template>

<style scoped>
.page-lvs { --lvs-accent: var(--green); }
.lvs-scope-note,.lvs-warning-strip { display:flex; align-items:flex-start; gap:12px; margin-bottom:14px; padding:12px 14px; border:1px solid var(--line); background:var(--surface-soft); color:var(--text-2); }
.lvs-scope-note svg { color:var(--green); }
.lvs-scope-note div,.lvs-warning-strip div { display:flex; flex-wrap:wrap; gap:4px 12px; }
.lvs-scope-note strong,.lvs-warning-strip strong { color:var(--text); }
.lvs-scope-note span,.lvs-warning-strip span { font-size:12px; }
.lvs-warning-strip { border-color:#e2c98c; background:var(--amber-soft); }
.lvs-warning-strip svg { color:var(--amber); }
.lvs-kpi-rail { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); margin-bottom:16px; border:1px solid var(--line); }
.lvs-kpi-rail article { min-width:0; padding:15px 17px; border-right:1px solid var(--line); background:#fff; }
.lvs-kpi-rail article:last-child { border-right:0; }
.lvs-kpi-rail article.attention { box-shadow:inset 0 -3px 0 var(--red); }
.lvs-kpi-rail span { display:flex; align-items:center; gap:7px; color:var(--text-3); font-size:11px; font-weight:700; letter-spacing:.04em; text-transform:uppercase; }
.lvs-kpi-rail strong { display:block; margin:5px 0 0; color:var(--text); font-family:var(--font-accent); font-size:28px; line-height:1; }
.lvs-kpi-rail small { color:var(--text-3); font-size:11px; }
.lvs-empty-state { display:grid; min-height:360px; place-items:center; align-content:center; gap:8px; border:1px solid var(--line); background:var(--surface-soft); text-align:center; }
.lvs-empty-state.compact { min-height:240px; }
.lvs-empty-state .empty-icon { display:grid; width:60px; height:60px; place-items:center; border:1px solid var(--line); background:#fff; color:var(--green); }
.lvs-empty-state h2 { font-size:20px; }
.lvs-empty-state p,.lvs-empty-state span { max-width:640px; color:var(--text-2); font-size:13px; }
.lvs-empty-state code { padding:2px 5px; background:#eef2f1; color:var(--green); }
.lvs-workbench { display:grid; grid-template-columns:minmax(680px,1fr) minmax(390px,34%); align-items:start; gap:18px; }
.lvs-browser,.lvs-detail { min-width:0; border:1px solid var(--line); background:#fff; }
.lvs-browser-tabs { display:flex; border-bottom:1px solid var(--line); }
.lvs-browser-tabs button { position:relative; flex:1; min-height:48px; border:0; border-right:1px solid var(--line); background:#fff; color:var(--text-2); cursor:pointer; font-weight:650; }
.lvs-browser-tabs button:last-child { border-right:0; }
.lvs-browser-tabs button.active { color:var(--text); background:var(--surface-soft); }
.lvs-browser-tabs button.active::after { position:absolute; right:18px; bottom:-1px; left:18px; height:3px; background:var(--lime); content:''; box-shadow:0 -1px 0 var(--green); }
.lvs-browser-tabs button span { margin-left:6px; color:var(--text-3); font-size:11px; }
.lvs-toolbar { display:grid; grid-template-columns:minmax(0,1fr) 250px; gap:10px; padding:11px; border-bottom:1px solid var(--line); }
.lvs-table { width:100%; overflow:auto; }
.lvs-table-head,.lvs-table-row { display:grid; grid-template-columns:minmax(210px,1.25fr) minmax(150px,.85fr) minmax(145px,.8fr) minmax(145px,.8fr) 120px 92px; align-items:center; min-width:980px; }
.lvs-table-head { min-height:38px; padding:0 14px; border-bottom:1px solid var(--line); background:var(--surface-soft); color:var(--text-3); font-size:10px; font-weight:750; letter-spacing:.04em; text-transform:uppercase; }
.lvs-table-row { width:100%; min-height:73px; padding:0 14px; border:0; border-bottom:1px solid var(--line); background:#fff; text-align:left; cursor:pointer; transition:background 140ms ease, box-shadow 140ms ease; }
.lvs-table-row:hover { background:#f8faf9; }
.lvs-table-row.selected { background:#eef6f2; box-shadow:inset 4px 0 0 var(--green); }
.lvs-table-row > span { min-width:0; padding-right:12px; color:var(--text-2); font-size:12px; }
.lvs-table-row strong { display:block; overflow:hidden; color:var(--text); text-overflow:ellipsis; white-space:nowrap; }
.lvs-table-row small { display:block; overflow:hidden; color:var(--text-3); text-overflow:ellipsis; white-space:nowrap; }
.lvs-table-row code { color:var(--text-2); font-size:11px; }
.director-chips { display:flex; flex-wrap:wrap; gap:4px; }
.director-chips i { display:inline-flex; align-items:center; gap:3px; padding:2px 5px; border:1px solid var(--line); color:var(--text-2); font-style:normal; font-size:10px; }
.director-chips svg { color:var(--green); fill:var(--green); }
.connection-cell strong { font-family:var(--font-mono); font-size:13px; }
.lvs-no-results { padding:50px 20px; color:var(--text-3); text-align:center; }
.lvs-detail { position:sticky; top:12px; max-height:calc(100vh - var(--topbar) - 38px); overflow:auto; scrollbar-gutter:stable; }
.lvs-detail-header { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; padding:20px; border-top:5px solid var(--lime); border-bottom:1px solid var(--line); }
.detail-eyebrow { color:var(--green); font-size:9px; font-weight:800; letter-spacing:.14em; }
.lvs-detail-header h2 { margin-top:3px; overflow-wrap:anywhere; font-size:22px; }
.lvs-detail-header p { color:var(--text-3); font-size:12px; }
.lvs-detail-facts { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); border-bottom:1px solid var(--line); }
.lvs-detail-facts div { min-width:0; padding:12px 16px; border-right:1px solid var(--line); border-bottom:1px solid var(--line); }
.lvs-detail-facts div:nth-child(even) { border-right:0; }
.lvs-detail-facts div:nth-last-child(-n+2) { border-bottom:0; }
.lvs-detail-facts span,.member-list span { display:block; color:var(--text-3); font-size:9px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; }
.lvs-detail-facts strong { display:block; overflow:hidden; margin-top:3px; text-overflow:ellipsis; white-space:nowrap; }
.lvs-topology,.lvs-detail-section { padding:17px 16px; border-bottom:1px solid var(--line); }
.section-title { display:flex; align-items:center; gap:7px; margin-bottom:12px; color:var(--text-2); }
.section-title svg { color:var(--green); }
.section-title strong { color:var(--text); }
.section-title span { margin-left:auto; color:var(--text-3); font-size:10px; }
.topology-flow { display:grid; grid-template-columns:minmax(95px,.75fr) 22px minmax(140px,1.2fr) 22px minmax(100px,.8fr); align-items:center; gap:5px; }
.topology-node { display:grid; min-height:68px; align-content:center; padding:9px 10px; border:1px solid var(--line-strong); background:#fff; }
.topology-node.virtual { border-color:var(--green); background:#f0f8f4; }
.topology-node span { color:var(--text-3); font-size:8px; font-weight:800; letter-spacing:.08em; }
.topology-node strong { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.topology-node small { color:var(--text-3); font-size:9px; }
.flow-arrow { color:var(--green); }
.topology-members { display:flex; flex-wrap:wrap; gap:5px; margin-top:10px; padding-left:calc(44% + 24px); }
.topology-members span { display:inline-flex; align-items:center; gap:4px; padding:3px 6px; border:1px solid #bcd5ca; background:#f4faf7; color:#356153; font-size:9px; }
.topology-members span.disabled { border-color:#dfc688; background:var(--amber-soft); color:#8a641e; }
.snapshot-tabs { display:flex; flex-wrap:wrap; gap:5px; margin-bottom:10px; }
.snapshot-tabs button { min-height:30px; padding:0 9px; border:1px solid var(--line); background:#fff; color:var(--text-2); cursor:pointer; font-size:10px; }
.snapshot-tabs button.active { border-color:var(--green); background:#eef6f2; color:var(--green); font-weight:700; }
.snapshot-grid { display:grid; gap:8px; }
.snapshot-grid article { padding:10px; border:1px solid var(--line); }
.snapshot-grid header { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.snapshot-grid dl { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin-top:9px; }
.snapshot-grid dl div { min-width:0; }
.snapshot-grid dt { color:var(--text-3); font-size:9px; text-transform:uppercase; }
.snapshot-grid dd { overflow:hidden; color:var(--text-2); font-size:11px; text-overflow:ellipsis; white-space:nowrap; }
.member-list { display:grid; gap:7px; }
.member-list article { display:grid; grid-template-columns:minmax(160px,1.2fr) .65fr .55fr .8fr auto; align-items:center; gap:9px; padding:9px 10px; border:1px solid var(--line); }
.member-list article > div { min-width:0; }
.member-list strong { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:11px; }
.member-identity { display:grid; grid-template-columns:16px minmax(0,1fr); align-items:center; }
.member-identity svg { grid-row:1 / 3; color:var(--green); fill:var(--green); }
.member-identity small { color:var(--text-3); font-size:9px; }
.member-empty { padding:24px; border:1px dashed var(--line); color:var(--text-3); text-align:center; }
@media (max-width:1300px) { .lvs-kpi-rail { grid-template-columns:repeat(3,1fr); } .lvs-kpi-rail article:nth-child(3) { border-right:0; } .lvs-kpi-rail article:nth-child(-n+3) { border-bottom:1px solid var(--line); } .lvs-workbench { grid-template-columns:1fr; } .lvs-detail { position:relative; top:0; max-height:none; } }
@media (max-width:760px) { .lvs-kpi-rail { grid-template-columns:1fr 1fr; } .lvs-kpi-rail article { border-bottom:1px solid var(--line); } .lvs-kpi-rail article:nth-child(even) { border-right:0; } .lvs-toolbar { grid-template-columns:1fr; } .lvs-browser-tabs { overflow:auto; } .lvs-browser-tabs button { min-width:170px; } .topology-flow { grid-template-columns:1fr; } .flow-arrow { transform:rotate(90deg); justify-self:center; } .topology-members { padding-left:0; } .member-list article { grid-template-columns:1fr 1fr; } }
@media (prefers-reduced-motion:no-preference) { .topology-flow .flow-arrow { animation:flow-pulse 1.8s ease-in-out infinite; } @keyframes flow-pulse { 0%,100% { opacity:.35; transform:translateX(-2px); } 50% { opacity:1; transform:translateX(2px); } } }
</style>
