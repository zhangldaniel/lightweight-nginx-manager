<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    values: number[]
    tone?: 'blue' | 'green' | 'amber' | 'red'
    label: string
    suffix?: string
  }>(),
  { tone: 'blue', suffix: '' },
)

const points = computed(() => {
  if (!props.values.length) return ''
  const max = Math.max(...props.values, 1)
  const min = Math.min(...props.values, 0)
  const range = Math.max(1, max - min)
  return props.values
    .map((value, index) => {
      const x = props.values.length === 1 ? 100 : (index / (props.values.length - 1)) * 100
      const y = 37 - ((value - min) / range) * 31
      return `${x.toFixed(2)},${y.toFixed(2)}`
    })
    .join(' ')
})
const area = computed(() => (points.value ? `0,40 ${points.value} 100,40` : ''))
const latest = computed(() => props.values.at(-1) ?? 0)
</script>

<template>
  <article class="chart-card" :data-tone="tone">
    <header>
      <span>{{ label }}</span>
      <strong>{{ latest.toFixed(latest >= 100 ? 0 : 1) }}{{ suffix }}</strong>
    </header>
    <svg viewBox="0 0 100 40" preserveAspectRatio="none" role="img" :aria-label="`${label}趋势`">
      <defs>
        <linearGradient :id="`area-${label}`" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="currentColor" stop-opacity=".22" />
          <stop offset="100%" stop-color="currentColor" stop-opacity="0" />
        </linearGradient>
      </defs>
      <line x1="0" y1="39.5" x2="100" y2="39.5" class="chart-axis" />
      <polygon v-if="area" :points="area" :fill="`url(#area-${label})`" />
      <polyline v-if="points" :points="points" />
    </svg>
    <small v-if="!values.length">等待 Agent 上报历史数据</small>
  </article>
</template>
