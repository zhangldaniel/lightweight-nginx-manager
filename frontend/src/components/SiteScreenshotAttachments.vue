<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import { Eye, ImagePlus, LoaderCircle, RefreshCw, Trash2, Upload } from '@lucide/vue'
import { NButton, NModal, NPopconfirm } from 'naive-ui'
import { api } from '../api'
import type { SiteAttachment } from '../types'

const props = withDefaults(
  defineProps<{
    siteId?: string
    editable?: boolean
    compact?: boolean
    refreshToken?: number
  }>(),
  {
    siteId: '',
    editable: false,
    compact: false,
    refreshToken: 0,
  },
)

const emit = defineEmits<{
  changed: [siteId: string]
}>()

const FALLBACK_MAX_FILES = 8
const FALLBACK_MAX_FILE_BYTES = 5 * 1024 * 1024
const ACCEPTED_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp'])

const fileInput = ref<HTMLInputElement | null>(null)
const attachments = ref<SiteAttachment[]>([])
const loading = ref(false)
const uploading = ref(false)
const deletingId = ref('')
const loadError = ref('')
const uploadError = ref('')
const statusMessage = ref('')
const activeUploadName = ref('')
const uploadPosition = ref('')
const previewAttachment = ref<SiteAttachment | null>(null)
const dragActive = ref(false)
const titleId = `site-screenshots-${useId()}`
const maxFiles = ref(FALLBACK_MAX_FILES)
const maxFileBytes = ref(FALLBACK_MAX_FILE_BYTES)
const serverRemaining = ref(FALLBACK_MAX_FILES)
const loadedSiteId = ref('')
const activeUploadSiteId = ref('')
const activeUploadContext = ref(-1)
let loadSequence = 0
let siteContext = 0
let statusTimer: ReturnType<typeof setTimeout> | undefined

const remainingSlots = computed(() =>
  Math.max(0, Math.min(serverRemaining.value, maxFiles.value - attachments.value.length)),
)
const showingUploadProgress = computed(
  () =>
    uploading.value &&
    activeUploadSiteId.value === props.siteId &&
    activeUploadContext.value === siteContext,
)
const canAdd = computed(
  () => Boolean(props.siteId && props.editable && remainingSlots.value && !uploading.value),
)

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '请求失败，请稍后重试'
}

function announce(message: string) {
  statusMessage.value = message
  if (statusTimer) clearTimeout(statusTimer)
  statusTimer = setTimeout(() => {
    statusMessage.value = ''
  }, 3200)
}

async function loadAttachments() {
  const sequence = ++loadSequence
  const requestedSiteId = props.siteId
  loadError.value = ''
  if (requestedSiteId !== loadedSiteId.value) {
    attachments.value = []
    previewAttachment.value = null
    maxFiles.value = FALLBACK_MAX_FILES
    maxFileBytes.value = FALLBACK_MAX_FILE_BYTES
    serverRemaining.value = FALLBACK_MAX_FILES
  }
  if (!requestedSiteId) {
    attachments.value = []
    loadedSiteId.value = ''
    loading.value = false
    return
  }
  loading.value = true
  try {
    const response = await api.siteAttachments(requestedSiteId)
    if (sequence !== loadSequence || props.siteId !== requestedSiteId) return
    attachments.value = response.items
    maxFiles.value = response.maxItems
    maxFileBytes.value = response.maxBytes
    serverRemaining.value = response.remaining
    loadedSiteId.value = requestedSiteId
  } catch (error) {
    if (sequence !== loadSequence || props.siteId !== requestedSiteId) return
    attachments.value = []
    loadError.value = errorMessage(error)
  } finally {
    if (sequence === loadSequence) loading.value = false
  }
}

watch(
  () => [props.siteId, props.refreshToken],
  ([siteId], previous) => {
    if (!previous || siteId !== previous[0]) siteContext += 1
    void loadAttachments()
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  if (statusTimer) clearTimeout(statusTimer)
})

function openPicker() {
  if (canAdd.value) fileInput.value?.click()
}

function validateFiles(files: File[]) {
  const accepted: File[] = []
  const errors: string[] = []
  const slots = remainingSlots.value
  for (const file of files) {
    if (!ACCEPTED_TYPES.has(file.type)) {
      errors.push(`${file.name} 不是 PNG、JPEG 或 WebP`)
      continue
    }
    if (file.size > maxFileBytes.value) {
      errors.push(`${file.name} 超过 ${formatBytes(maxFileBytes.value)}`)
      continue
    }
    if (accepted.length >= slots) {
      errors.push(`每个配置最多保存 ${maxFiles.value} 张截图`)
      break
    }
    accepted.push(file)
  }
  return { accepted, errors }
}

async function uploadFiles(files: File[]) {
  uploadError.value = ''
  const uploadSiteId = props.siteId
  const uploadContext = siteContext
  if (!uploadSiteId) {
    uploadError.value = '请先保存配置草稿，再上传截图'
    return
  }
  if (!props.editable) {
    uploadError.value = '当前账号没有上传权限'
    return
  }
  if (uploading.value) return
  const { accepted, errors } = validateFiles(files)
  if (errors.length) uploadError.value = errors.join('；')
  if (!accepted.length) return

  uploading.value = true
  activeUploadSiteId.value = uploadSiteId
  activeUploadContext.value = uploadContext
  let completed = 0
  try {
    for (let index = 0; index < accepted.length; index += 1) {
      if (siteContext !== uploadContext || props.siteId !== uploadSiteId) break
      const file = accepted[index]
      activeUploadName.value = file.name
      uploadPosition.value = `${index + 1} / ${accepted.length}`
      try {
        const response = await api.uploadSiteAttachment(uploadSiteId, file)
        completed += 1
        if (siteContext !== uploadContext || props.siteId !== uploadSiteId) break
        attachments.value = [...attachments.value, response.attachment]
        serverRemaining.value = Math.max(0, serverRemaining.value - 1)
      } catch (error) {
        if (siteContext === uploadContext && props.siteId === uploadSiteId) {
          uploadError.value = `${file.name}：${errorMessage(error)}`
        }
        break
      }
    }
  } finally {
    uploading.value = false
    activeUploadName.value = ''
    uploadPosition.value = ''
    activeUploadSiteId.value = ''
    activeUploadContext.value = -1
  }
  if (completed) {
    if (siteContext === uploadContext && props.siteId === uploadSiteId) {
      announce(`已上传 ${completed} 张截图`)
    }
    emit('changed', uploadSiteId)
  }
}

function handleFileInput(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  void uploadFiles(files)
}

function handlePaste(event: ClipboardEvent) {
  const files = Array.from(event.clipboardData?.files || []).filter((file) =>
    ACCEPTED_TYPES.has(file.type),
  )
  if (!files.length) return
  event.preventDefault()
  void uploadFiles(files)
}

function handleDrop(event: DragEvent) {
  dragActive.value = false
  const files = Array.from(event.dataTransfer?.files || [])
  if (files.length) void uploadFiles(files)
}

async function removeAttachment(attachment: SiteAttachment) {
  const deleteSiteId = props.siteId
  const deleteContext = siteContext
  if (!deleteSiteId || deletingId.value) return
  deletingId.value = attachment.id
  uploadError.value = ''
  try {
    await api.deleteSiteAttachment(deleteSiteId, attachment.id)
    if (siteContext !== deleteContext || props.siteId !== deleteSiteId) {
      emit('changed', deleteSiteId)
      return
    }
    attachments.value = attachments.value.filter((item) => item.id !== attachment.id)
    serverRemaining.value = Math.min(maxFiles.value, serverRemaining.value + 1)
    if (previewAttachment.value?.id === attachment.id) previewAttachment.value = null
    announce('截图已删除')
    emit('changed', deleteSiteId)
  } catch (error) {
    if (siteContext === deleteContext && props.siteId === deleteSiteId) {
      uploadError.value = errorMessage(error)
    }
  } finally {
    deletingId.value = ''
  }
}

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value <= 0) return '0 KB'
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function formattedDate(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}
</script>

<template>
  <section class="site-screenshots" :class="{ compact }" :aria-labelledby="titleId">
    <div class="site-screenshots-head">
      <div>
        <strong :id="titleId">截图附件</strong>
        <small>{{ attachments.length }} / {{ maxFiles }} 张 · 上传后立即保存 · 随控制端数据备份</small>
      </div>
      <NButton
        v-if="editable"
        size="small"
        secondary
        :disabled="!canAdd"
        :loading="showingUploadProgress"
        :title="!siteId ? '请先保存配置草稿' : remainingSlots ? '选择截图' : '已达到截图数量上限'"
        @click="openPicker"
      >
        <template #icon><ImagePlus :size="15" /></template>
        添加截图
      </NButton>
    </div>

    <input
      ref="fileInput"
      class="visually-hidden-input"
      type="file"
      accept="image/png,image/jpeg,image/webp"
      multiple
      tabindex="-1"
      @change="handleFileInput"
    />

    <div v-if="loading" class="screenshot-state" aria-live="polite">
      <LoaderCircle class="spin" :size="17" /> 正在读取截图…
    </div>
    <div v-else-if="loadError" class="screenshot-state error" role="alert">
      <span>{{ loadError }}</span>
      <NButton size="tiny" secondary @click="loadAttachments">
        <template #icon><RefreshCw :size="13" /></template>
        重试
      </NButton>
    </div>

    <div v-else-if="attachments.length" class="screenshot-grid">
      <article v-for="attachment in attachments" :key="attachment.id" class="screenshot-card">
        <button
          type="button"
          class="screenshot-preview-trigger"
          :aria-label="`预览截图 ${attachment.filename}`"
          @click="previewAttachment = attachment"
        >
          <img :src="attachment.url" :alt="attachment.filename" loading="lazy" />
          <span class="screenshot-preview-cue"><Eye :size="14" /> 预览</span>
        </button>
        <div class="screenshot-meta">
          <div>
            <strong :title="attachment.filename">{{ attachment.filename }}</strong>
            <small>{{ formatBytes(attachment.size) }} · {{ formattedDate(attachment.created_at) }}</small>
          </div>
          <NPopconfirm
            v-if="editable"
            positive-text="删除"
            negative-text="取消"
            @positive-click="removeAttachment(attachment)"
          >
            <template #trigger>
              <NButton
                quaternary
                circle
                size="tiny"
                type="error"
                :loading="deletingId === attachment.id"
                :aria-label="`删除截图 ${attachment.filename}`"
              >
                <template #icon><Trash2 :size="14" /></template>
              </NButton>
            </template>
            删除这张截图？此操作不可恢复。
          </NPopconfirm>
        </div>
      </article>
    </div>

    <div
      v-if="(editable || !siteId) && (!attachments.length || remainingSlots) && !loading && !loadError"
      class="screenshot-dropzone"
      :class="{ active: dragActive, disabled: !canAdd }"
      role="button"
      :tabindex="canAdd ? 0 : -1"
      :aria-disabled="!canAdd"
      :aria-label="siteId ? '选择、粘贴或拖入截图' : '请先保存配置草稿，再上传截图'"
      @click="openPicker"
      @keydown.enter.prevent="openPicker"
      @keydown.space.prevent="openPicker"
      @paste="handlePaste"
      @dragenter.prevent="dragActive = canAdd"
      @dragover.prevent
      @dragleave.prevent="dragActive = false"
      @drop.prevent="handleDrop"
    >
      <Upload :size="17" />
      <span v-if="!siteId"><strong>保存草稿后可上传</strong><small>新配置需要先获得配置 ID</small></span>
      <span v-else-if="remainingSlots"><strong>选择、粘贴或拖入截图</strong><small>PNG / JPEG / WebP，单张不超过 {{ formatBytes(maxFileBytes) }}</small></span>
      <span v-else><strong>已达到 {{ maxFiles }} 张上限</strong><small>删除旧截图后可继续添加</small></span>
    </div>

    <div
      v-else-if="!attachments.length && !editable && !loading && !loadError"
      class="screenshot-state empty"
    >
      暂无截图
    </div>

    <div v-if="showingUploadProgress" class="upload-progress" role="status" aria-live="polite">
      <LoaderCircle class="spin" :size="15" />
      <span><strong>正在上传 {{ uploadPosition }}</strong>{{ activeUploadName }}</span>
    </div>
    <p v-if="uploadError" class="attachment-error" role="alert">{{ uploadError }}</p>
    <p class="visually-hidden" aria-live="polite">{{ statusMessage }}</p>

    <NModal
      :show="Boolean(previewAttachment)"
      preset="card"
      class="screenshot-preview-modal"
      :title="previewAttachment?.filename || '截图预览'"
      :bordered="false"
      @update:show="(show) => { if (!show) previewAttachment = null }"
    >
      <img
        v-if="previewAttachment"
        class="screenshot-full-image"
        :src="previewAttachment.url"
        :alt="previewAttachment.filename"
      />
      <template #footer>
        <div class="screenshot-modal-footer">
          <span v-if="previewAttachment">
            {{ formatBytes(previewAttachment.size) }} · {{ formattedDate(previewAttachment.created_at) }}
          </span>
          <NButton @click="previewAttachment = null">关闭</NButton>
        </div>
      </template>
    </NModal>
  </section>
</template>

<style scoped>
.site-screenshots {
  display: grid;
  gap: 10px;
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: #f8faf9;
}

.site-screenshots-head,
.screenshot-modal-footer {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
}

.site-screenshots-head > div {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.site-screenshots-head strong {
  color: var(--text);
  font-size: 12px;
}

.site-screenshots-head small,
.screenshot-meta small,
.screenshot-dropzone small,
.screenshot-modal-footer span {
  color: var(--text-3);
  font-size: 10px;
}

.visually-hidden-input,
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  white-space: nowrap;
}

.screenshot-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.screenshot-card {
  min-width: 0;
  overflow: hidden;
  border: 1px solid #d8e0de;
  border-radius: 4px;
  background: #fff;
}

.screenshot-preview-trigger {
  position: relative;
  display: block;
  width: 100%;
  height: 86px;
  padding: 0;
  overflow: hidden;
  border: 0;
  border-bottom: 1px solid #e1e7e5;
  background: #edf2f0;
  cursor: zoom-in;
}

.screenshot-preview-trigger img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  transition: transform 160ms ease;
}

.screenshot-preview-cue {
  position: absolute;
  right: 7px;
  bottom: 7px;
  display: flex;
  gap: 4px;
  align-items: center;
  padding: 3px 6px;
  border-radius: 2px;
  background: rgba(17, 24, 32, .82);
  color: #fff;
  font-size: 10px;
  opacity: 0;
  transform: translateY(3px);
  transition: opacity 160ms ease, transform 160ms ease;
}

.screenshot-preview-trigger:hover img { transform: scale(1.025); }
.screenshot-preview-trigger:hover .screenshot-preview-cue,
.screenshot-preview-trigger:focus-visible .screenshot-preview-cue {
  opacity: 1;
  transform: translateY(0);
}

.screenshot-preview-trigger:focus-visible,
.screenshot-dropzone:focus-visible {
  outline: 2px solid var(--green);
  outline-offset: 2px;
}

.screenshot-meta {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 6px;
  align-items: center;
  padding: 7px 8px;
}

.screenshot-meta > div {
  display: grid;
  gap: 1px;
  min-width: 0;
}

.screenshot-meta strong {
  overflow: hidden;
  color: var(--text-2);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.screenshot-dropzone {
  display: flex;
  gap: 8px;
  align-items: center;
  min-height: 48px;
  padding: 8px 10px;
  border: 1px dashed #aebeba;
  border-radius: 4px;
  background: #fff;
  color: var(--green);
  cursor: pointer;
  transition: border-color 160ms ease, background 160ms ease;
}

.screenshot-dropzone:hover,
.screenshot-dropzone.active {
  border-color: var(--green);
  background: #edf5f1;
}

.screenshot-dropzone.disabled {
  color: var(--text-3);
  cursor: not-allowed;
  opacity: .72;
}

.screenshot-dropzone span,
.upload-progress span {
  display: grid;
  gap: 1px;
  min-width: 0;
}

.screenshot-dropzone strong,
.upload-progress strong {
  color: var(--text-2);
  font-size: 11px;
}

.screenshot-state,
.upload-progress {
  display: flex;
  gap: 8px;
  align-items: center;
  min-height: 44px;
  padding: 8px 10px;
  border: 1px solid #dce5e2;
  border-radius: 4px;
  background: #fff;
  color: var(--text-2);
  font-size: 11px;
}

.screenshot-state.error,
.attachment-error {
  color: var(--danger);
}

.screenshot-state.empty {
  min-height: 0;
  justify-content: center;
  border-style: dashed;
  color: var(--text-3);
}

.screenshot-state.error { justify-content: space-between; }

.upload-progress {
  color: var(--green);
}

.attachment-error {
  margin: 0;
  font-size: 11px;
  line-height: 1.45;
}

.spin { animation: attachment-spin .8s linear infinite; }

.site-screenshots.compact {
  padding: 14px 20px 16px;
  border: 0;
  border-bottom: 1px solid var(--line);
  border-radius: 0;
  background: #fff;
}

.compact .screenshot-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.compact .screenshot-preview-trigger { height: 72px; }

:global(.n-card.screenshot-preview-modal) {
  width: min(1080px, calc(100vw - 36px));
  max-height: calc(100vh - 36px);
  border-radius: 6px;
}

:global(.screenshot-preview-modal > .n-card__content) {
  min-height: 0;
  overflow: auto;
  background: #111820;
}

.screenshot-full-image {
  display: block;
  width: auto;
  max-width: 100%;
  max-height: calc(100vh - 180px);
  margin: 0 auto;
  object-fit: contain;
}

@keyframes attachment-spin { to { transform: rotate(360deg); } }

@media (max-width: 720px) {
  .screenshot-grid,
  .compact .screenshot-grid { grid-template-columns: minmax(0, 1fr); }
  .screenshot-preview-trigger,
  .compact .screenshot-preview-trigger { height: 128px; }
}

@media (prefers-reduced-motion: reduce) {
  .screenshot-preview-trigger img,
  .screenshot-preview-cue,
  .screenshot-dropzone { transition: none; }
}
</style>
