<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArrowRight, CheckCircle2, LockKeyhole, ServerCog } from '@lucide/vue'
import { NButton, NInput } from 'naive-ui'
import { gsap } from 'gsap'
import * as THREE from 'three'
import WAVES from 'vanta/dist/vanta.waves.min'
import { useConsoleStore } from '../stores/console'

const store = useConsoleStore()
const background = ref<HTMLElement | null>(null)
const story = ref<HTMLElement | null>(null)
const card = ref<HTMLElement | null>(null)
const username = ref('admin')
const password = ref('')
const submitting = ref(false)
const error = ref('')
let effect: { destroy: () => void } | null = null
let entrance: gsap.core.Timeline | null = null

onMounted(async () => {
  await nextTick()
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
  entrance = gsap
    .timeline()
    .from(story.value, { x: -14, duration: 0.42, ease: 'power2.out' })
    .from(card.value, { y: 14, duration: 0.36, ease: 'power2.out' }, '-=.24')
  if (!background.value) return
  effect = WAVES({
    el: background.value,
    THREE,
    mouseControls: true,
    touchControls: false,
    gyroControls: false,
    minHeight: 200,
    minWidth: 200,
    scale: 1,
    scaleMobile: 1,
    color: 0xd5e6ff,
    backgroundColor: 0xf2f8ff,
    shininess: 22,
    waveHeight: 7,
    waveSpeed: 0.28,
    zoom: 0.9,
  })
})

onBeforeUnmount(() => {
  entrance?.kill()
  effect?.destroy()
})

async function submit() {
  if (!username.value.trim() || !password.value) {
    error.value = '请输入账号和密码'
    return
  }
  submitting.value = true
  error.value = ''
  try {
    await store.login(username.value.trim(), password.value)
  } catch (caught) {
    error.value = store.apiMessage(caught)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <div ref="background" class="login-vanta" aria-hidden="true"></div>
    <div class="login-overlay"></div>

    <section ref="story" class="login-story" aria-label="产品简介">
      <div class="login-brand">
        <span class="brand-mark large"><span>N</span></span>
        <div>
          <strong>NGINX MANAGER</strong>
          <small>LIGHTWEIGHT CONTROL PLANE</small>
        </div>
      </div>
      <div class="login-headline">
        <span>轻量 · 可审计 · Agent 主动连接</span>
        <h1>把分散的 Nginx<br />纳入一个可信工作台</h1>
        <p>配置、证书、节点状态和实时日志集中管理，操作前校验，失败自动恢复。</p>
      </div>
      <div class="login-features">
        <span><CheckCircle2 :size="17" />多配置目录与 Stream</span>
        <span><CheckCircle2 :size="17" />原子写入与版本记录</span>
        <span><CheckCircle2 :size="17" />LDAP 与分级权限</span>
      </div>
    </section>

    <section class="login-panel" aria-labelledby="login-title">
      <div ref="card" class="login-card">
        <span class="login-card-icon"><ServerCog :size="26" /></span>
        <h2 id="login-title">登录控制台</h2>
        <p>使用本地管理账号或 LDAP 账号继续。</p>
        <form @submit.prevent="submit">
          <label for="login-username">账号</label>
          <NInput
            id="login-username"
            v-model:value="username"
            size="large"
            autocomplete="username"
            placeholder="请输入账号"
          />
          <label for="login-password">密码</label>
          <NInput
            id="login-password"
            v-model:value="password"
            size="large"
            type="password"
            show-password-on="click"
            autocomplete="current-password"
            placeholder="请输入密码"
            @keyup.enter="submit"
          />
          <div v-if="error" class="form-error" role="alert">
            <LockKeyhole :size="16" />
            {{ error }}
          </div>
          <NButton
            attr-type="submit"
            type="primary"
            size="large"
            block
            :loading="submitting"
          >
            进入控制台
            <template #icon><ArrowRight :size="18" /></template>
          </NButton>
        </form>
        <small class="login-footnote">连接和认证策略由控制端统一管理</small>
      </div>
    </section>
  </main>
</template>
