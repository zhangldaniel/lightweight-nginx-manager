<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    values: Array<number | null>
    tone?: 'blue' | 'green' | 'amber' | 'red'
    label: string
    suffix?: string
    ceiling?: number
    warning?: number
  }>(),
  { tone: 'blue', suffix: '', ceiling: undefined, warning: undefined },
)

const numericValues = computed(() =>
  props.values.filter((value): value is number => typeof value === 'number' && Number.isFinite(value)),
)
const bounds = computed(() => {
  const values = numericValues.value
  if (!values.length) return { min: 0, max: props.ceiling || 1 }
  const min = props.ceiling ? 0 : Math.min(...values, 0)
  const max = props.ceiling || Math.max(...values, 1)
  return { min, max: Math.max(min + 1, max) }
})
const segments = computed(() => {
  const result: Array<{ line: string; area: string }> = []
  let current: Array<{ x: number; y: number }> = []
  const { min, max } = bounds.value
  const range = Math.max(1, max - min)
  const flush = () => {
    if (!current.length) return
    const line = current.map((point) => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ')
    const first = current[0]
    const last = current[current.length - 1]
    result.push({
      line,
      area: `${first.x.toFixed(2)},39.5 ${line} ${last.x.toFixed(2)},39.5`,
    })
    current = []
  }
  props.values.forEach((value, index) => {
    if (typeof value !== 'number' || !Number.isFinite(value)) {
      flush()
      return
    }
    const x = props.values.length <= 1 ? 100 : (index / (props.values.length - 1)) * 100
    const y = 37 - ((value - min) / range) * 31
    current.push({ x, y: Math.max(4, Math.min(37, y)) })
  })
  flush()
  return result
})
const latest = computed(() => {
  const value = props.values.at(-1)
  return typeof value === 'number' && Number.isFinite(value) ? value : null
})
const peak = computed(() => (numericValues.value.length ? Math.max(...numericValues.value) : null))
const warningY = computed(() => {
  if (props.warning === undefined) return null
  const { min, max } = bounds.value
  return 37 - ((props.warning - min) / Math.max(1, max - min)) * 31
})

function format(value: number | null) {
  if (value === null) return '—'
  return `${value.toFixed(value >= 100 ? 0 : 1)}${props.suffix}`
}
</script>

<template>
  <article class="chart-card" :data-tone="tone" :data-empty="numericValues.length ? undefined : 'true'">
    <header>
      <span>{{ label }}</span>
      <strong>{{ format(latest) }}</strong>
    </header>
    <div class="chart-plot">
      <svg viewBox="0 0 100 40" preserveAspectRatio="none" role="img" :aria-label="`${label}趋势`">
        <line x1="0" y1="13.5" x2="100" y2="13.5" class="chart-grid" />
        <line x1="0" y1="26.5" x2="100" y2="26.5" class="chart-grid" />
        <line x1="0" y1="39.5" x2="100" y2="39.5" class="chart-axis" />
        <line
          v-if="warningY !== null"
          x1="0"
          :y1="warningY"
          x2="100"
          :y2="warningY"
          class="chart-threshold"
        />
        <polygon
          v-for="(segment, index) in segments"
          :key="`area-${index}`"
          :points="segment.area"
          class="chart-area"
        />
        <polyline
          v-for="(segment, index) in segments"
          :key="`line-${index}`"
          :points="segment.line"
          class="chart-line"
        />
      </svg>
      <div v-if="!numericValues.length" class="chart-empty" aria-hidden="true">
        <strong>暂无趋势</strong>
        <span>等待 Agent 上报</span>
      </div>
    </div>
    <footer>
      <span>{{ numericValues.length ? `${numericValues.length} 个采样点` : '等待 Agent 上报历史数据' }}</span>
      <span v-if="peak !== null">峰值 {{ format(peak) }}</span>
    </footer>
  </article>
</template>
