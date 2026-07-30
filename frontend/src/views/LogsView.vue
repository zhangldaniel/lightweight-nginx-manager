<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { CircleStop, Eraser, Pause, Play, Radio, Search } from '@lucide/vue'
import { NButton, NCheckbox, NInput, NInputNumber, NSelect } from 'naive-ui'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import { api } from '../api'
import { useConsoleStore } from '../stores/console'

type LogPreset = 'all' | 'error' | 'warn' | 'http4xx' | 'http5xx'

const store = useConsoleStore()
const selectedNodeId = ref(store.nodes.find((node) => node.status !== 'offline')?.id || '')
const selectedPath = ref('')
const preset = ref<LogPreset>('all')
const include = ref('')
const exclude = ref('')
const caseSensitive = ref(false)
const tailLines = ref(200)
const lines = ref<string[]>([])
const pausedLines = ref<string[]>([])
const paused = ref(false)
const connecting = ref(false)
const session = ref<Record<string, unknown> | null>(null)
const connectionState = ref<'idle' | 'connecting' | 'open' | 'retrying'>('idle')
const stats = ref({ read: 0, sent: 0, dropped: 0 })
const output = ref<HTMLElement | null>(null)
let eventSource: EventSource | null = null

const selectedNode = computed(() => store.nodes.find((node) => node.id === selectedNodeId.value))
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

function logLineHasHttpStatus(line: string, prefix: '4' | '5') {
  return new RegExp(`(?:^|[^0-9])${prefix}[0-9]{2}(?:[^0-9]|$)`).test(line)
}

function logLineMatches(
  line: string,
  currentPreset: LogPreset,
  mustInclude: string,
  mustExclude: string,
  sensitive: boolean,
) {
  const haystack = sensitive ? line : line.toLowerCase()
  const includeNeedle = sensitive ? mustInclude : mustInclude.toLowerCase()
  const excludeNeedle = sensitive ? mustExclude : mustExclude.toLowerCase()
  if (includeNeedle && !haystack.includes(includeNeedle)) return false
  if (excludeNeedle && haystack.includes(excludeNeedle)) return false
  if (currentPreset === 'error' && !/\berror\b|\[error\]/i.test(line)) return false
  if (currentPreset === 'warn' && !/\bwarn(?:ing)?\b|\[warn\]/i.test(line)) return false
  if (currentPreset === 'http4xx' && !logLineHasHttpStatus(line, '4')) return false
  if (currentPreset === 'http5xx' && !logLineHasHttpStatus(line, '5')) return false
  return true
}

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
    read: Number(payload.read_lines || stats.value.read),
    sent: Number(payload.sent_lines || stats.value.sent),
    dropped: Number(payload.dropped_lines || stats.value.dropped),
  }
}

function openStream(id: string) {
  eventSource?.close()
  eventSource = new EventSource(
    `/api/v1/admin/log-sessions/${encodeURIComponent(id)}/events`,
  )
  eventSource.addEventListener('log', consume as EventListener)
  eventSource.addEventListener('end', () => {
    eventSource?.close()
    eventSource = null
    session.value = null
    connectionState.value = 'idle'
  })
  eventSource.onopen = () => {
    connectionState.value = 'open'
  }
  eventSource.onerror = () => {
    if (session.value) connectionState.value = 'retrying'
  }
}

async function start() {
  if (!selectedNodeId.value || !selectedPath.value) {
    store.notify('请选择在线节点和日志文件', 'warning')
    return
  }
  connecting.value = true
  connectionState.value = 'connecting'
  lines.value = []
  paused.value = false
  stats.value = { read: 0, sent: 0, dropped: 0 }
  try {
    session.value = await api.createLogSession({
      node_id: selectedNodeId.value,
      path: selectedPath.value,
      include: include.value,
      exclude: exclude.value,
      case_sensitive: caseSensitive.value,
      preset: preset.value,
      tail_lines: tailLines.value,
    })
    openStream(String(session.value.id))
  } catch (error) {
    session.value = null
    connectionState.value = 'idle'
    store.notify('实时日志启动失败', 'danger', store.apiMessage(error))
  } finally {
    connecting.value = false
  }
}

async function stop() {
  const current = session.value
  eventSource?.close()
  eventSource = null
  session.value = null
  connectionState.value = 'idle'
  paused.value = false
  if (current?.id) {
    try {
      await api.stopLogSession(String(current.id))
    } catch {
      // The server expires abandoned sessions; leaving the page must remain responsive.
    }
  }
}

function togglePause() {
  if (!paused.value) pausedLines.value = [...lines.value]
  paused.value = !paused.value
}

function clearWindow() {
  lines.value = []
  pausedLines.value = []
  stats.value = { read: 0, sent: 0, dropped: 0 }
}

onBeforeUnmount(() => {
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
        <NSelect v-model:value="selectedNodeId" :options="nodeOptions" />
      </label>
      <label class="log-path-select">
        <span>日志文件</span>
        <NSelect
          v-model:value="selectedPath"
          :options="pathOptions"
          placeholder="选择 Agent 上报的日志路径"
        />
      </label>
      <label>
        <span>初始读取</span>
        <NInputNumber v-model:value="tailLines" :min="1" :max="1000" />
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
        <pre ref="output" class="log-output" tabindex="0">{{ displayText }}</pre>
        <footer>
          <span>读取 {{ stats.read }} · 显示 {{ visibleLines.length }} · 过滤 {{ Math.max(0, lines.length - visibleLines.length) }}</span>
          <span>浏览器最多保留 5,000 行<span v-if="stats.dropped"> · Agent 丢弃 {{ stats.dropped }}</span></span>
        </footer>
      </section>

      <aside class="log-filters">
        <div class="section-heading compact">
          <div>
            <span class="section-icon success"><Search :size="18" /></span>
            <div>
              <h2>过滤与显示</h2>
              <p>当前窗口即时过滤</p>
            </div>
          </div>
        </div>

        <fieldset>
          <legend>快捷条件</legend>
          <div class="preset-grid">
            <button
              v-for="item in [
                ['all', '全部'],
                ['error', 'Error'],
                ['warn', 'Warn'],
                ['http4xx', 'HTTP 4xx'],
                ['http5xx', 'HTTP 5xx'],
              ]"
              :key="item[0]"
              type="button"
              :class="{ active: preset === item[0] }"
              @click="preset = item[0] as LogPreset"
            >
              {{ item[1] }}
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
        <NCheckbox v-model:checked="caseSensitive">区分大小写</NCheckbox>

        <div class="filter-actions">
          <NButton :disabled="!session" @click="togglePause">
            <template #icon><component :is="paused ? Play : Pause" :size="17" /></template>
            {{ paused ? '继续显示' : '暂停显示' }}
          </NButton>
          <NButton @click="clearWindow">
            <template #icon><Eraser :size="17" /></template>
            清空当前窗口
          </NButton>
        </div>

        <p class="filter-note">
          文件路径只能来自 Agent 上报的白名单。页面不提供任意路径，也不提供日志下载。
        </p>
      </aside>
    </div>
  </section>
</template>
