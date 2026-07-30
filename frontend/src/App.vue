<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { RouterView } from 'vue-router'
import {
  NConfigProvider,
  NDialogProvider,
  NLoadingBarProvider,
  darkTheme,
  dateZhCN,
  zhCN,
  type GlobalThemeOverrides,
} from 'naive-ui'
import AppShell from './components/AppShell.vue'
import LoginView from './views/LoginView.vue'
import ToastHost from './components/ToastHost.vue'
import { useConsoleStore } from './stores/console'

const store = useConsoleStore()
const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#1677ff',
    primaryColorHover: '#4096ff',
    primaryColorPressed: '#0958d9',
    primaryColorSuppl: '#1677ff',
    successColor: '#22a06b',
    warningColor: '#d97706',
    errorColor: '#dc3c4d',
    borderRadius: '10px',
    borderRadiusSmall: '8px',
    fontFamily:
      '"Noto Sans SC Variable", "Geist Variable", "PingFang SC", "Microsoft YaHei", sans-serif',
    fontSize: '15px',
  },
  Button: {
    heightMedium: '40px',
    borderRadiusMedium: '10px',
    fontWeight: '650',
  },
  Input: {
    heightMedium: '40px',
    borderRadius: '10px',
  },
  Select: {
    peers: {
      InternalSelection: {
        heightMedium: '40px',
        borderRadius: '10px',
      },
    },
  },
  Dialog: {
    borderRadius: '16px',
  },
}

const currentTheme = computed(() => null)

let refreshTimer: number | undefined

async function refreshInBackground() {
  if (document.hidden || !store.session) return
  try {
    await store.refresh(false, true)
  } catch {
    // Keep background refresh quiet. Manual refresh still reports connection errors.
  }
}

function handleVisibilityChange() {
  if (!document.hidden) void refreshInBackground()
}

onMounted(async () => {
  await store.checkSession()
  refreshTimer = window.setInterval(() => void refreshInBackground(), 2500)
  document.addEventListener('visibilitychange', handleVisibilityChange)
})

onUnmounted(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
})
</script>

<template>
  <NConfigProvider
    :theme="currentTheme === 'dark' ? darkTheme : null"
    :theme-overrides="themeOverrides"
    :locale="zhCN"
    :date-locale="dateZhCN"
  >
    <NLoadingBarProvider>
      <NDialogProvider>
        <div
          class="application"
          :class="`density-${store.density}`"
          :aria-busy="store.loading"
        >
          <div v-if="store.booting" class="boot-screen">
            <span class="brand-orbit" aria-hidden="true"></span>
            <strong>NGINX MANAGER</strong>
            <span>正在连接控制端…</span>
          </div>

          <LoginView v-else-if="!store.session" />

          <AppShell v-else>
            <RouterView v-slot="{ Component, route }">
              <Transition name="route-fade" mode="out-in">
                <component :is="Component" :key="route.name" />
              </Transition>
            </RouterView>
          </AppShell>

          <ToastHost />
        </div>
      </NDialogProvider>
    </NLoadingBarProvider>
  </NConfigProvider>
</template>
