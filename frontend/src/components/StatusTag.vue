<script setup lang="ts">
import type { Tone } from '../types'

withDefaults(defineProps<{ label: string; tone?: Tone; pulse?: boolean }>(), {
  tone: 'neutral',
  pulse: false,
})
</script>

<template>
  <span class="status-tag" :data-tone="tone">
    <span v-if="pulse" class="status-dot" aria-hidden="true"></span>
    {{ label }}
  </span>
</template>
