<script setup lang="ts">
import { computed, ref } from 'vue'
import { Copy, FileKey2, Plus, RefreshCw, ShieldCheck } from '@lucide/vue'
import { NButton, NCheckbox, NInput, NModal, NProgress } from 'naive-ui'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import { useConsoleStore } from '../stores/console'
import type { CertificateRecord } from '../types'
import { uid } from '../utils/config'
import { certificateDays, certificateStatus } from '../utils/format'

const store = useConsoleStore()
const scanning = ref(false)
const modalOpen = ref(false)
const replacing = ref(false)
const modalMode = ref<'add' | 'replace'>('replace')
const activeCertificate = ref<CertificateRecord | null>(null)
const certificateDomain = ref('')
const selectedNodes = ref<string[]>([])
const certificatePem = ref('')
const privateKeyPem = ref('')

const sortedCertificates = computed(() =>
  [...store.certificates].sort(
    (left, right) => (certificateDays(left) ?? 99999) - (certificateDays(right) ?? 99999),
  ),
)

const modalNodes = computed(() => {
  const allowedIds =
    modalMode.value === 'replace' ? new Set(activeCertificate.value?.nodeIds || []) : null
  return store.nodes.filter(
    (node) => node.status !== 'offline' && (!allowedIds || allowedIds.has(node.id)),
  )
})

function resetPemFields() {
  certificatePem.value = ''
  privateKeyPem.value = ''
}

function openAdd() {
  modalMode.value = 'add'
  activeCertificate.value = null
  certificateDomain.value = ''
  selectedNodes.value = store.nodes
    .filter((node) => node.status !== 'offline')
    .map((node) => node.id)
  resetPemFields()
  modalOpen.value = true
}

function openReplace(certificate: CertificateRecord) {
  modalMode.value = 'replace'
  activeCertificate.value = certificate
  certificateDomain.value = certificate.domain
  selectedNodes.value = certificate.nodeIds.filter(
    (id) => store.nodes.find((node) => node.id === id)?.status !== 'offline',
  )
  resetPemFields()
  modalOpen.value = true
}

function toggleNode(id: string, checked: boolean) {
  if (checked && !selectedNodes.value.includes(id)) selectedNodes.value.push(id)
  if (!checked) selectedNodes.value = selectedNodes.value.filter((item) => item !== id)
}

async function scan() {
  scanning.value = true
  try {
    await store.scanInventory('certificate_inventory')
  } catch (error) {
    store.notify('证书扫描未提交', 'danger', store.apiMessage(error))
  } finally {
    scanning.value = false
  }
}

async function submitCertificate() {
  const domain = certificateDomain.value.trim()
  if (!domain || /\s/.test(domain)) {
    store.notify('请填写有效的证书域名', 'warning', '支持普通域名和 *.example.com 通配符域名')
    return
  }
  if (modalMode.value === 'add' && store.certificates.some((item) => item.domain === domain)) {
    store.notify('该域名证书已经存在', 'warning', '请在已有证书卡片中执行“替换证书”')
    return
  }
  replacing.value = true
  try {
    let certificate = activeCertificate.value
    if (modalMode.value === 'add') {
      certificate = {
        id: uid('certificate'),
        domain,
        name: domain,
        domains: [domain],
        source: '手动上传',
        status: 'draft',
        nodeIds: [],
        nodePaths: {},
        nodeHashes: {},
      }
      await store.upsertCertificate(certificate)
    }
    if (!certificate) return
    await store.applyCertificate(
      certificate,
      selectedNodes.value,
      certificatePem.value,
      privateKeyPem.value,
    )
    resetPemFields()
    modalOpen.value = false
  } catch (error) {
    store.notify(
      modalMode.value === 'add' ? '证书部署未提交' : '证书替换未提交',
      'danger',
      store.apiMessage(error),
    )
  } finally {
    replacing.value = false
  }
}

async function copy(value: string, label: string) {
  await navigator.clipboard.writeText(value)
  store.notify(`${label}已复制`, 'success')
}
</script>

<template>
  <section class="page page-certificates">
    <PageHeader title="证书" description="查看到期风险、部署节点和每台机器上的原始证书路径。">
      <NButton :loading="scanning" :disabled="!store.canOperate" @click="scan">
        <template #icon><RefreshCw :size="18" /></template>
        扫描节点证书
      </NButton>
      <NButton type="primary" :disabled="!store.canOperate" @click="openAdd">
        <template #icon><Plus :size="18" /></template>
        添加证书
      </NButton>
    </PageHeader>

    <div v-if="sortedCertificates.length" class="certificate-grid">
      <article
        v-for="certificate in sortedCertificates"
        :key="certificate.id"
        class="certificate-card spotlight-card"
        :data-tone="certificateStatus(certificate).tone"
      >
        <div class="certificate-card-head">
          <span class="certificate-icon"><ShieldCheck :size="22" /></span>
          <div>
            <h2>{{ certificate.domain }}</h2>
            <p>{{ certificate.issuer || certificate.source || '节点导入证书' }}</p>
          </div>
          <StatusTag v-bind="certificateStatus(certificate)" />
        </div>

        <div class="certificate-stats">
          <div>
            <span>剩余时间</span>
            <strong>{{ certificateDays(certificate) ?? '—' }}<small> 天</small></strong>
          </div>
          <div>
            <span>部署节点</span>
            <strong>{{ certificate.nodeIds.length }}<small> 个</small></strong>
          </div>
          <div>
            <span>关联站点</span>
            <strong>{{
              store.sites.filter((site) => site.certificateId === certificate.id).length
            }}<small> 个</small></strong>
          </div>
        </div>

        <div class="certificate-lifetime">
          <NProgress
            type="line"
            :show-indicator="false"
            :height="5"
            :percentage="Math.max(2, Math.min(100, ((certificateDays(certificate) ?? 0) / 120) * 100))"
            :status="
              certificateStatus(certificate).tone === 'danger'
                ? 'error'
                : certificateStatus(certificate).tone === 'warning'
                  ? 'warning'
                  : 'success'
            "
          />
          <span>{{ certificateStatus(certificate).label }}</span>
        </div>

        <div class="certificate-locations">
          <div class="section-title">
            <strong>节点与原路径</strong>
            <span>{{ certificate.nodeIds.length }} 份</span>
          </div>
          <article v-for="nodeId in certificate.nodeIds" :key="nodeId" class="certificate-location">
            <div class="location-node">
              <span
                class="online-dot"
                :class="{ offline: store.nodes.find((item) => item.id === nodeId)?.status === 'offline' }"
              ></span>
              <strong>{{ store.nodes.find((item) => item.id === nodeId)?.node_name || nodeId }}</strong>
            </div>
            <div class="path-row">
              <span>证书</span>
              <code>{{ certificate.nodePaths?.[nodeId]?.certificatePath || '等待扫描' }}</code>
              <button
                type="button"
                aria-label="复制证书路径"
                @click="copy(certificate.nodePaths?.[nodeId]?.certificatePath || '', '证书路径')"
              >
                <Copy :size="15" />
              </button>
            </div>
            <div class="path-row">
              <span>私钥</span>
              <code>{{ certificate.nodePaths?.[nodeId]?.keyPath || '等待扫描' }}</code>
              <button
                type="button"
                aria-label="复制私钥路径"
                @click="copy(certificate.nodePaths?.[nodeId]?.keyPath || '', '私钥路径')"
              >
                <Copy :size="15" />
              </button>
            </div>
          </article>
        </div>

        <footer class="certificate-card-foot">
          <code>{{ certificate.fingerprint || '证书指纹将在扫描后显示' }}</code>
          <NButton :disabled="!store.canOperate" @click="openReplace(certificate)">替换证书</NButton>
        </footer>
      </article>
    </div>

    <div v-else class="empty-state large">
      <FileKey2 :size="34" />
      <strong>尚未发现证书</strong>
      <span>让在线 Agent 扫描已配置的证书目录和 Nginx 引用路径。</span>
      <NButton type="primary" :loading="scanning" :disabled="!store.canOperate" @click="scan">
        扫描节点证书
      </NButton>
    </div>

    <p class="page-footnote">
      替换会写回原证书和私钥路径。私钥不会写入平台状态；Agent 校验失败时会恢复原文件。
    </p>

    <NModal
      v-model:show="modalOpen"
      preset="card"
      class="certificate-modal"
      :title="modalMode === 'add' ? '添加证书' : `替换 ${activeCertificate?.domain || ''}`"
      :bordered="false"
      :mask-closable="false"
    >
      <div class="security-banner">
        <ShieldCheck :size="20" />
        <div>
          <strong>私钥仅进入一次性 Agent 任务</strong>
          <p>
            {{
              modalMode === 'add'
                ? '证书会写入所选节点的托管证书目录；PEM 内容不会写入 UI State、操作记录或浏览器存储。'
                : '证书会原路径替换；PEM 内容不会写入 UI State、操作记录或浏览器存储。'
            }}
          </p>
        </div>
      </div>

      <label v-if="modalMode === 'add'" class="certificate-domain-field">
        <span>证书域名</span>
        <NInput
          v-model:value="certificateDomain"
          placeholder="例如 *.example.com"
          autocomplete="off"
        />
        <small>Agent 会校验证书域名、证书链和私钥是否匹配。</small>
      </label>

      <fieldset>
        <legend>{{ modalMode === 'add' ? '部署节点' : '替换节点' }}</legend>
        <div class="choice-grid">
          <label
            v-for="node in modalNodes"
            :key="node.id"
            class="choice-card"
            :class="{ selected: selectedNodes.includes(node.id) }"
          >
            <NCheckbox
              :checked="selectedNodes.includes(node.id)"
              @update:checked="(checked) => toggleNode(node.id, checked)"
            />
            <span>
              <strong>{{ node.node_name }}</strong>
              <small>
                {{
                  modalMode === 'add'
                    ? node.facts.managed_certificate_root || '使用 Agent 托管证书目录'
                    : activeCertificate?.nodePaths?.[node.id]?.certificatePath
                }}
              </small>
            </span>
          </label>
        </div>
        <p v-if="!modalNodes.length" class="field-empty-hint">当前没有可操作的在线 Agent。</p>
      </fieldset>

      <div class="certificate-form-grid">
        <label>
          <span>证书链 PEM</span>
          <NInput
            v-model:value="certificatePem"
            type="textarea"
            :autosize="{ minRows: 9, maxRows: 16 }"
            placeholder="-----BEGIN CERTIFICATE-----"
          />
        </label>
        <label>
          <span>私钥 PEM</span>
          <NInput
            v-model:value="privateKeyPem"
            type="textarea"
            :autosize="{ minRows: 9, maxRows: 16 }"
            placeholder="-----BEGIN PRIVATE KEY-----"
          />
        </label>
      </div>

      <template #footer>
        <div class="modal-footer">
          <NButton @click="modalOpen = false">取消</NButton>
          <NButton
            type="primary"
            :loading="replacing"
            :disabled="
              !certificateDomain.trim() ||
              !selectedNodes.length ||
              !certificatePem ||
              !privateKeyPem
            "
            @click="submitCertificate"
          >
            {{ modalMode === 'add' ? '校验并部署' : '校验并原路径替换' }}
          </NButton>
        </div>
      </template>
    </NModal>
  </section>
</template>
