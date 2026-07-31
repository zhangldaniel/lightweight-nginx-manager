<script setup lang="ts">
import type { Component } from 'vue'
import type { Tone } from '../types'

withDefaults(
  defineProps<{
    label: string
    value: string | number
    note: string
    icon: Component
    tone?: Tone
    featured?: boolean
  }>(),
  { tone: 'info', featured: false },
)
</script>

<template>
  <article class="metric-card" :class="{ featured }" :data-tone="tone">
    <span class="metric-icon"><component :is="icon" :size="18" stroke-width="2" /></span>
    <div class="metric-copy">
      <span>{{ label }}</span>
      <strong>{{ value }}</strong>
      <small>{{ note }}</small>
    </div>
  </article>
</template>
