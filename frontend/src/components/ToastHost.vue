<script setup lang="ts">
import { AlertTriangle, CheckCircle2, Info, XCircle } from '@lucide/vue'
import { useConsoleStore } from '../stores/console'

const store = useConsoleStore()
const icons = { success: CheckCircle2, warning: AlertTriangle, danger: XCircle, info: Info, neutral: Info }
</script>

<template>
  <div class="toast-host" role="status" aria-live="polite" aria-atomic="false">
    <TransitionGroup name="toast">
      <article v-for="toast in store.toasts" :key="toast.id" class="toast" :data-tone="toast.type">
        <component :is="icons[toast.type]" :size="19" aria-hidden="true" />
        <div>
          <strong>{{ toast.title }}</strong>
          <p v-if="toast.message">{{ toast.message }}</p>
        </div>
      </article>
    </TransitionGroup>
  </div>
</template>
