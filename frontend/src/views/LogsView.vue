<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { CircleStop, Eraser, Pause, Play, Radio, Search } from '@lucide/vue'
import { NButton, NCheckbox, NInput, NInputNumber, NSelect } from 'naive-ui'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import { api } from '../api'
import { useConsoleStore } from '../stores/console'
import { logLineMatches, type LogPreset } from '../utils/logFilters'

const store = useConsoleStore()
const selectedNodeId = ref(
  store.nodes.find(
    (node) => node.status !== 'offline' && (node.capabilities || []).includes('log_stream_v1'),
  )?.id || '',
)
const selectedPath = ref('')
const preset = ref<LogPreset>('all')
const include = ref('')
const exclude = ref('')
const caseSensitive = ref(false)
const tailLines = ref(200)
const lines = ref<string[]>([])
const pausedLines = ref<string[]>([])
const paused = ref(false)
const wrapLines = ref(false)
const connecting = ref(false)
const restarting = ref(false)
const session = ref<Record<string, unknown> | null>(null)
const connectionState = ref<'idle' | 'connecting' | 'open' | 'retrying'>('idle')
const connectionProblem = ref('')
const stats = ref({ read: 0, sent: 0, dropped: 0 })
const output = ref<HTMLElement | null>(null)
let eventSource: EventSource | null = null
let retryTimer: number | undefined
let sessionGeneration = 0

const selectedNode = computed(() => store.nodes.find((node) => node.id === selectedNodeId.value))
const sourceLocked = computed(() => connecting.value || restarting.value || Boolean(session.value))
const nodeOptions = computed(() =>
  store.nodes.map((node) => ({
    label: `${node.node_name}${node.status === 'offline' ? ' · 离线' : ''}`,
    value: node.id,
    disabled: node.status === 'offline' || !(node.capabilities || []).includes('log_stream_v1'),
  })),
)
const pathOptions = computed(() =>
  (selectedNode.value?.facts.log_files || []).map((path) => ({ label: path, value: path })),
)
const sourceLines = computed(() => (paused.value ? pausedLines.value : lines.value))
const visibleLines = computed(() =>
  sourceLines.value.filter((line) =>
    logLineMatches(line, preset.value, include.value, exclude.value, caseSensitive.value),
  ),
)
const displayText = computed(() => {
  if (!lines.value.length) return '连接 Agent 后，日志会显示在这里。'
  if (!visibleLines.value.length) {
    return '当前窗口没有匹配的日志。\n\n请调整右侧快捷条件、包含内容或排除内容。'
  }
  return visibleLines.value.join('\n')
})
const hiddenCount = computed(() => Math.max(0, sourceLines.value.length - visibleLines.value.length))
const activeFilterCount = computed(
  () => {
    const hasTextFilter = Boolean(include.value.trim() || exclude.value.trim())
    return (
      Number(preset.value !== 'all') +
      Number(Boolean(include.value.trim())) +
      Number(Boolean(exclude.value.trim())) +
      Number(hasTextFilter && caseSensitive.value)
    )
  },
)
const presetItems = computed(() =>
  ([
    ['all', '全部'],
    ['error', 'Error'],
    ['warn', 'Warn'],
    ['http4xx', 'HTTP 4xx'],
    ['http5xx', 'HTTP 5xx'],
  ] as const).map(([value, label]) => ({
    value,
    label,
    count: sourceLines.value.filter((line) => logLineMatches(line, value, '', '', false)).length,
  })),
)
const connectionLabel = computed(() => {
  if (paused.value) return '显示已暂停，后台仍在接收'
  if (connectionState.value === 'open') return '实时接收中'
  if (connectionState.value === 'retrying') return '连接中断，正在重试'
  if (connectionState.value === 'connecting') return '正在建立日志会话'
  return '尚未连接'
})

watch(
  selectedNodeId,
  () => {
    if (session.value) stop()
    selectedPath.value = pathOptions.value[0]?.value || ''
  },
  { immediate: true },
)

watch([visibleLines, paused], async () => {
  if (!output.value || paused.value) return
  const followsTail =
    output.value.scrollHeight - output.value.scrollTop - output.value.clientHeight <= 64
  if (!followsTail) return
  await nextTick()
  if (output.value) output.value.scrollTop = output.value.scrollHeight
})

function consume(event: MessageEvent) {
  let payload: Record<string, unknown>
  try {
    payload = JSON.parse(event.data || '{}') as Record<string, unknown>
  } catch {
    return
  }
  if (payload.rotated) lines.value.push('── 日志文件发生轮转，已继续读取新文件 ──')
  if (payload.error) lines.value.push(`【Agent 读取失败】${payload.error}`)
  if (payload.content) {
    lines.value.push(...String(payload.content).replace(/\n$/, '').split('\n'))
  }
  if (lines.value.length > 5000) lines.value.splice(0, lines.value.length - 5000)
  stats.value = {
    read: Number(payload.read_lines ?? stats.value.read),
    sent: Number(payload.sent_lines ?? stats.value.sent),
    dropped: Number(payload.dropped_lines ?? stats.value.dropped),
  }
}

function openStream(id: string, generation: number) {
  eventSource?.close()
  const source = new EventSource(
    `/api/v1/admin/log-sessions/${encodeURIComponent(id)}/events`,
  )
  eventSource = source
  source.addEventListener('log', (event) => {
    if (eventSource !== source || generation !== sessionGeneration) return
    consume(event as MessageEvent)
  })
  source.addEventListener('end', () => {
    if (eventSource !== source || generation !== sessionGeneration) return
    source.close()
    eventSource = null
    session.value = null
    connectionState.value = 'idle'
    if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    retryTimer = undefined
    connectionProblem.value = '日志会话已经结束，可以重新连接。'
  })
  source.onopen = () => {
    if (eventSource !== source || generation !== sessionGeneration) return
    if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    retryTimer = undefined
    connectionProblem.value = ''
    connectionState.value = 'open'
  }
  source.onerror = () => {
    if (eventSource !== source || generation !== sessionGeneration) return
    if (!session.value) return
    connectionState.value = 'retrying'
    if (retryTimer === undefined) {
      retryTimer = window.setTimeout(() => {
        if (connectionState.value === 'retrying') {
          connectionProblem.value = '连接持续中断，请检查 Agent 状态或重新建立会话。'
        }
      }, 12_000)
    }
  }
}

async function start() {
  if (connecting.value) return
  if (!selectedNodeId.value || !selectedPath.value) {
    store.notify('请选择在线节点和日志文件', 'warning')
    return
  }
  connecting.value = true
  connectionState.value = 'connecting'
  connectionProblem.value = ''
  lines.value = []
  paused.value = false
  stats.value = { read: 0, sent: 0, dropped: 0 }
  const generation = ++sessionGeneration
  const nodeId = selectedNodeId.value
  const path = selectedPath.value
  try {
    const created = await api.createLogSession({
      node_id: nodeId,
      path,
      // Window filters stay in the browser so changing them never replaces the
      // Agent connection or permanently discards lines needed by another filter.
      include: '',
      exclude: '',
      case_sensitive: false,
      preset: 'all',
      tail_lines: tailLines.value,
    })
    if (generation !== sessionGeneration) {
      if (created.id) void api.stopLogSession(String(created.id)).catch(() => undefined)
      return
    }
    session.value = created
    openStream(String(created.id), generation)
  } catch (error) {
    if (generation !== sessionGeneration) return
    session.value = null
    connectionState.value = 'idle'
    store.notify('实时日志启动失败', 'danger', store.apiMessage(error))
  } finally {
    if (generation === sessionGeneration) connecting.value = false
  }
}

async function stop() {
  sessionGeneration += 1
  const current = session.value
  eventSource?.close()
  eventSource = null
  session.value = null
  connecting.value = false
  connectionState.value = 'idle'
  paused.value = false
  if (retryTimer !== undefined) window.clearTimeout(retryTimer)
  retryTimer = undefined
  if (current?.id) {
    try {
      await api.stopLogSession(String(current.id))
    } catch {
      // The server expires abandoned sessions; leaving the page must remain responsive.
    }
  }
}

async function restart() {
  if (restarting.value) return
  restarting.value = true
  try {
    await stop()
    await start()
  } finally {
    restarting.value = false
  }
}

function togglePause() {
  if (!paused.value) pausedLines.value = [...lines.value]
  paused.value = !paused.value
}

function clearWindow() {
  lines.value = []
  pausedLines.value = []
}

function clearFilters() {
  preset.value = 'all'
  include.value = ''
  exclude.value = ''
  caseSensitive.value = false
}

onBeforeUnmount(() => {
  if (retryTimer !== undefined) window.clearTimeout(retryTimer)
  void stop()
})
</script>

<template>
  <section class="page page-logs">
    <PageHeader title="实时日志" description="按需查看 Agent 白名单内的 Nginx 日志；控制端不长期保存日志内容。">
      <StatusTag
        :label="
          connectionState === 'open'
            ? '实时接收中'
            : connectionState === 'retrying'
              ? '连接重试中'
              : connectionState === 'connecting'
                ? '正在连接'
                : '尚未连接'
        "
        :tone="connectionState === 'open' ? 'success' : connectionState === 'retrying' ? 'warning' : 'neutral'"
        :pulse="connectionState === 'open'"
      />
    </PageHeader>

    <div class="log-toolbar">
      <label>
        <span>Agent 节点</span>
        <NSelect v-model:value="selectedNodeId" :options="nodeOptions" :disabled="sourceLocked" />
      </label>
      <label class="log-path-select">
        <span>日志文件</span>
        <NSelect
          v-model:value="selectedPath"
          :options="pathOptions"
          :disabled="sourceLocked"
          placeholder="选择 Agent 上报的日志路径"
        />
      </label>
      <label>
        <span>初始读取</span>
        <NInputNumber v-model:value="tailLines" :min="1" :max="1000" :disabled="sourceLocked" />
      </label>
      <NButton
        v-if="!session"
        type="primary"
        :loading="connecting"
        :disabled="!store.canOperate"
        @click="start"
      >
        <template #icon><Radio :size="17" /></template>
        开始查看
      </NButton>
      <NButton v-else type="error" secondary @click="stop">
        <template #icon><CircleStop :size="17" /></template>
        停止
      </NButton>
    </div>

    <div class="log-session-summary" aria-live="polite">
      <div class="log-summary-cell">
        <span>会话状态</span>
        <strong>{{ connectionLabel }}</strong>
        <small>{{ selectedNode?.node_name || '选择支持实时日志的 Agent' }}</small>
      </div>
      <div class="log-summary-cell">
        <span>Agent 已读取</span>
        <strong>{{ stats.read }}</strong>
        <small>当前会话累计行数</small>
      </div>
      <div class="log-summary-cell">
        <span>窗口显示</span>
        <strong>{{ visibleLines.length }}</strong>
        <small>{{ activeFilterCount ? `${activeFilterCount} 个过滤条件` : '未启用窗口过滤' }}</small>
      </div>
      <div class="log-summary-cell">
        <span>窗口隐藏</span>
        <strong>{{ hiddenCount }}</strong>
        <small>{{ stats.dropped ? `Agent 丢弃 ${stats.dropped} 行` : '浏览器保留 5,000 行' }}</small>
      </div>
    </div>

    <div v-if="connectionProblem" class="inline-error" role="alert">
      <strong>实时日志连接需要处理</strong>
      <span>{{ connectionProblem }}</span>
      <NButton size="small" :loading="connecting || restarting" @click="restart">重新连接</NButton>
    </div>

    <div class="log-workspace">
      <section class="terminal-card">
        <header>
          <div>
            <strong>{{ selectedNode?.node_name || '选择节点' }}</strong>
            <code>{{ selectedPath || '选择日志文件' }}</code>
          </div>
          <span class="terminal-state">
            <span class="status-dot" :class="{ active: connectionState === 'open' }"></span>
            {{ paused ? '显示已暂停' : connectionState === 'open' ? '实时接收' : '等待连接' }}
          </span>
        </header>
        <pre ref="output" class="log-output" :class="{ wrapped: wrapLines }" tabindex="0">{{ displayText }}</pre>
        <footer>
          <span>窗口 {{ sourceLines.length }} · 显示 {{ visibleLines.length }} · 隐藏 {{ hiddenCount }}</span>
          <span>会话累计读取 {{ stats.read }} · 浏览器最多保留 5,000 行<span v-if="stats.dropped"> · Agent 丢弃 {{ stats.dropped }}</span></span>
        </footer>
      </section>

      <aside class="log-filters">
        <div class="section-heading compact">
          <div>
            <span class="section-icon success"><Search :size="18" /></span>
            <div>
              <h2>窗口过滤</h2>
              <p>对已经接收的日志即时生效</p>
            </div>
          </div>
        </div>

        <div class="filter-result-summary" :data-active="activeFilterCount > 0">
          <div>
            <span>当前命中</span>
            <strong>{{ visibleLines.length }} / {{ sourceLines.length }}</strong>
          </div>
          <button v-if="activeFilterCount" type="button" @click="clearFilters">清除过滤</button>
          <small v-else>选择条件后立即过滤当前窗口</small>
        </div>

        <fieldset>
          <legend>快捷条件</legend>
          <div class="preset-grid">
            <button
              v-for="item in presetItems"
              :key="item.value"
              type="button"
              :data-preset="item.value"
              :class="{ active: preset === item.value }"
              :aria-pressed="preset === item.value"
              @click="preset = item.value"
            >
              <span>{{ item.label }}</span>
              <strong>{{ item.count }}</strong>
            </button>
          </div>
        </fieldset>

        <label>
          <span>必须包含</span>
          <NInput v-model:value="include" clearable placeholder="例如 upstream timed out" />
        </label>
        <label>
          <span>排除内容</span>
          <NInput v-model:value="exclude" clearable placeholder="例如 healthcheck" />
        </label>
        <div class="filter-toggle-row">
          <NCheckbox
            v-model:checked="caseSensitive"
            :disabled="!include.trim() && !exclude.trim()"
          >区分大小写</NCheckbox>
          <NCheckbox v-model:checked="wrapLines">自动换行</NCheckbox>
        </div>

        <div v-if="session && activeFilterCount" class="filter-change-note">
          过滤只改变当前浏览器窗口，不会重连 Agent，也不会丢掉其他日志。
        </div>

        <div class="filter-actions">
          <NButton :disabled="!session" @click="togglePause">
            <template #icon><component :is="paused ? Play : Pause" :size="17" /></template>
            {{ paused ? '继续显示' : '暂停显示' }}
          </NButton>
          <NButton @click="clearWindow">
            <template #icon><Eraser :size="17" /></template>
            清空当前窗口
          </NButton>
          <NButton
            v-if="connectionState === 'retrying'"
            :loading="connecting || restarting"
            @click="restart"
          >
            <template #icon><Radio :size="17" /></template>
            重新连接
          </NButton>
        </div>

        <p class="filter-note">
          文件路径只能来自 Agent 上报的白名单。页面不提供任意路径，也不提供日志下载。
        </p>
      </aside>
    </div>
  </section>
</template>
