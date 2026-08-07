import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const source = await readFile(resolve(projectRoot, 'src', 'views', 'MonitoringView.vue'), 'utf8')

function assert(value, message) {
  if (!value) throw new Error(message)
}

// User-facing contract: operators must know that the page refreshes and when it will refresh again.
assert(
  source.includes('每 5 秒自动刷新') || source.includes('每 5 秒自动更新'),
  '运行监控页未明确显示“每 5 秒自动刷新”',
)
assert(
  /下次刷新|秒后刷新/.test(source),
  '运行监控页未显示距下次自动刷新的可理解状态',
)
assert(
  source.includes('页面隐藏，已暂停') && source.includes('正在刷新'),
  '运行监控页未覆盖暂停和刷新中的可见状态',
)
assert(!/20 秒自动(?:刷新|更新)/.test(source), '运行监控页仍残留旧的 20 秒刷新文案')

// Timing/lifecycle contract: one five-second scheduler, paused while hidden and cleared on leave.
assert(
  /AUTO_REFRESH_MS\s*=\s*5_000/.test(source) && /setTimeout[\s\S]*AUTO_REFRESH_MS/.test(source),
  '运行监控自动刷新周期不是 5 秒',
)
assert(source.includes('document.hidden'), '页面隐藏时仍会执行监控刷新')
assert(
  source.includes("addEventListener('visibilitychange', handleVisibilityChange)") &&
    source.includes("removeEventListener('visibilitychange', handleVisibilityChange)"),
  '运行监控页未在显示状态变化时暂停或恢复刷新',
)
assert(
  source.includes('onBeforeUnmount') &&
    source.includes('window.clearTimeout(refreshTimer)') &&
    source.includes('window.clearInterval(countdownTimer)'),
  '离开运行监控页时未清理自动刷新计时器',
)
assert(
  /async function refreshNow\(\)[\s\S]*?clearRefreshTimers\(\)[\s\S]*?runRefreshCycle\(\)[\s\S]*?scheduleRefresh\(\)/.test(source),
  '手动刷新后没有重置自动刷新周期',
)
assert(
  /refreshCycleInFlight/.test(source) && /runRefreshCycle\(true\)/.test(source),
  '自动和手动刷新之间缺少防重入保护',
)

// Quiet refresh must keep the last successful snapshot on screen until replacement data arrives.
const autoRefreshBody = source.match(
  /async function refreshSummary\(quiet = false\)([\s\S]*?)\r?\n}\r?\n\r?\nasync function loadHistory/,
)?.[1] || ''
assert(autoRefreshBody, '未找到监控摘要刷新逻辑')
assert(
  !/store\.monitoring\s*=\s*\[\]/.test(autoRefreshBody),
  '自动刷新请求期间会清空当前监控数据',
)

console.log('PASS monitoring exposes a five-second refresh status and preserves the current snapshot')
console.log('PASS monitoring pauses while hidden, resumes when visible, and clears every lifecycle hook')
console.log('PASS manual refresh resets the schedule and refresh cycles cannot overlap')
