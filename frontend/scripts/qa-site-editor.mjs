import { spawn } from 'node:child_process'
import { access, mkdir, rm, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

process.env.QA_PORT ||= '4181'
const port = Number(process.env.QA_PORT)
const debugPort = port + 5000
const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const artifactRoot = resolve(projectRoot, 'artifacts')
const profileRoot = resolve(artifactRoot, `.chrome-site-editor-${Date.now()}`)
await mkdir(artifactRoot, { recursive: true })

const candidates = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
]
let browserPath = ''
for (const candidate of candidates) {
  try {
    await access(candidate)
    browserPath = candidate
    break
  } catch {
    // Try the next browser.
  }
}
if (!browserPath) throw new Error('No supported headless browser was found')

const { qaServer } = await import('./qa-server.mjs')
const browser = spawn(
  browserPath,
  [
    '--headless=new',
    '--disable-gpu',
    '--hide-scrollbars',
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${profileRoot}`,
    '--window-size=1680,980',
    `http://127.0.0.1:${port}/#/sites`,
  ],
  { stdio: 'ignore', windowsHide: true },
)

const wait = (milliseconds) => new Promise((resolveWait) => setTimeout(resolveWait, milliseconds))

async function debuggingTarget() {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const targets = await fetch(`http://127.0.0.1:${debugPort}/json`)
      const pages = await targets.json()
      const target = pages.find((item) => item.type === 'page' && item.url.includes(`:${port}/`))
      if (target?.webSocketDebuggerUrl) return target
    } catch {
      // Chrome is still starting.
    }
    await wait(50)
  }
  throw new Error('Timed out waiting for the headless browser')
}

let socket
const pending = new Map()
let commandId = 0

function command(method, params = {}) {
  commandId += 1
  const id = commandId
  socket.send(JSON.stringify({ id, method, params }))
  return new Promise((resolveCommand, rejectCommand) => {
    pending.set(id, { resolveCommand, rejectCommand })
  })
}

async function evaluate(expression) {
  const result = await command('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true,
  })
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'Evaluation failed')
  return result.result?.value
}

function assert(value, message) {
  if (!value) throw new Error(message)
}

try {
  const target = await debuggingTarget()
  socket = new WebSocket(target.webSocketDebuggerUrl)
  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data)
    if (!message.id || !pending.has(message.id)) return
    const handlers = pending.get(message.id)
    pending.delete(message.id)
    if (message.error) handlers.rejectCommand(new Error(message.error.message))
    else handlers.resolveCommand(message.result)
  })
  await new Promise((resolveOpen, rejectOpen) => {
    socket.addEventListener('open', resolveOpen, { once: true })
    socket.addEventListener('error', rejectOpen, { once: true })
  })
  await command('Runtime.enable')
  await wait(700)

  const detailPresentation = await evaluate(`(() => {
    const data = document.querySelector('.data-panel')
    const detail = document.querySelector('.detail-panel')
    const style = detail ? getComputedStyle(detail) : null
    const dataRect = data?.getBoundingClientRect()
    const detailRect = detail?.getBoundingClientRect()
    return {
      found: Boolean(detail),
      subtitle: detail?.querySelector('.detail-head p')?.textContent.trim() || '',
      overflowX: style?.overflowX || '',
      overflowY: style?.overflowY || '',
      horizontallyScrollable: Boolean(detail && detail.scrollWidth > detail.clientWidth + 1),
      independentlyScrollable: Boolean(detail && detail.scrollHeight > detail.clientHeight + 1),
      bottomDelta:
        dataRect && detailRect
          ? Math.abs(dataRect.bottom - detailRect.bottom)
          : Number.POSITIVE_INFINITY,
    }
  })()`)
  assert(detailPresentation.found, 'The site detail panel was not found')
  assert(detailPresentation.subtitle.includes('配置 v'), 'The site detail subtitle was not found')
  assert(
    !['生产', '预发布', '测试'].some((label) => detailPresentation.subtitle.includes(label)),
    `The site detail still renders an environment label: ${detailPresentation.subtitle}`,
  )
  assert(
    !detailPresentation.horizontallyScrollable,
    `The site detail still overflows horizontally (${detailPresentation.overflowX})`,
  )
  assert(
    detailPresentation.overflowY === 'visible',
    `The site detail still creates nested vertical scrolling (${detailPresentation.overflowY})`,
  )
  assert(!detailPresentation.independentlyScrollable, 'The site detail still renders its own scrollbar')
  assert(
    detailPresentation.bottomDelta <= 1,
    `The data and detail panels are misaligned by ${detailPresentation.bottomDelta}px`,
  )

  assert(
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('.detail-panel button')]
        .find((item) => item.textContent.includes('编辑配置'))
      button?.click()
      return Boolean(button)
    })()`),
    'The existing-site editor button was not found',
  )
  await wait(180)
  const existingEditorLayout = await evaluate(`(() => {
    const modal = document.querySelector('.site-editor-modal')
    const footer = modal?.querySelector('.modal-footer')
    const content = modal?.querySelector('.n-card-content')
    const modalRect = modal?.getBoundingClientRect()
    const footerRect = footer?.getBoundingClientRect()
    return {
      viewportHeight: window.innerHeight,
      modalTop: modalRect?.top ?? -1,
      modalBottom: modalRect?.bottom ?? Number.POSITIVE_INFINITY,
      footerTop: footerRect?.top ?? Number.POSITIVE_INFINITY,
      footerBottom: footerRect?.bottom ?? Number.POSITIVE_INFINITY,
      footerHeight: footerRect?.height ?? 0,
      contentBottom: content?.getBoundingClientRect().bottom ?? Number.POSITIVE_INFINITY,
    }
  })()`)
  assert(existingEditorLayout.footerHeight > 0, 'The existing-site editor footer is not rendered')
  assert(
    existingEditorLayout.modalTop >= 8 &&
      existingEditorLayout.modalBottom <= existingEditorLayout.viewportHeight - 8,
    `The existing-site editor extends outside the viewport: ${JSON.stringify(existingEditorLayout)}`,
  )
  assert(
    existingEditorLayout.footerBottom <= existingEditorLayout.modalBottom + 1,
    `The existing-site editor footer extends outside its modal: ${JSON.stringify(existingEditorLayout)}`,
  )
  assert(
    existingEditorLayout.contentBottom <= existingEditorLayout.footerTop + 1,
    `The existing-site editor content overlaps its footer: ${JSON.stringify(existingEditorLayout)}`,
  )
  await command('Page.reload', { ignoreCache: true })
  await wait(700)

  assert(
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('button')]
        .find((item) => item.textContent.includes('新增站点'))
      button?.click()
      return Boolean(button)
    })()`),
    'The create-site button was not found',
  )
  await wait(250)

  const initial = await evaluate(`(() => {
    const modal = document.querySelector('.site-editor-modal')
    const templates = [...document.querySelectorAll('.template-card')]
    const node = document.querySelector('.choice-card')
    return {
      modal: Boolean(modal),
      tabs: modal?.querySelectorAll('.n-tabs').length || 0,
      templates: templates.map((item) => item.textContent.trim()),
      nodePressed: node?.getAttribute('aria-pressed'),
      labels: [...(modal?.querySelectorAll('label > span') || [])]
        .map((item) => item.textContent.trim()),
    }
  })()`)
  assert(initial.modal, 'The unified site editor did not open')
  assert(initial.tabs === 0, 'Legacy mode tabs are still rendered')
  assert(!initial.labels.includes('环境'), 'The site editor still renders the obsolete environment selector')
  assert(initial.templates.length === 8, 'Expected eight site templates')
  assert(initial.templates.some((item) => item.includes('负载均衡 HTTPS')), 'HTTPS load-balancer template is missing')
  assert(initial.templates.some((item) => item.includes('Nginx Stub Status')), 'Stub Status template is missing')

  const editorLayout = await evaluate(`(() => {
    const modal = document.querySelector('.site-editor-modal')
    const footer = modal?.querySelector('.modal-footer')
    const content = modal?.querySelector('.n-card-content')
    const modalRect = modal?.getBoundingClientRect()
    const footerRect = footer?.getBoundingClientRect()
    return {
      viewportHeight: window.innerHeight,
      modalTop: modalRect?.top ?? -1,
      modalBottom: modalRect?.bottom ?? Number.POSITIVE_INFINITY,
      footerTop: footerRect?.top ?? Number.POSITIVE_INFINITY,
      footerBottom: footerRect?.bottom ?? Number.POSITIVE_INFINITY,
      footerHeight: footerRect?.height ?? 0,
      contentBottom: content?.getBoundingClientRect().bottom ?? Number.POSITIVE_INFINITY,
    }
  })()`)
  assert(editorLayout.footerHeight > 0, 'The site editor footer is not rendered')
  assert(
    editorLayout.modalTop >= 8 && editorLayout.modalBottom <= editorLayout.viewportHeight - 8,
    `The site editor extends outside the viewport: ${JSON.stringify(editorLayout)}`,
  )
  assert(
    editorLayout.footerBottom <= editorLayout.modalBottom + 1,
    `The site editor footer extends outside its modal: ${JSON.stringify(editorLayout)}`,
  )
  assert(
    editorLayout.contentBottom <= editorLayout.footerTop + 1,
    `The site editor content overlaps its footer: ${JSON.stringify(editorLayout)}`,
  )

  assert(
    await evaluate(`(() => {
      const node = document.querySelector('.choice-card')
      node?.click()
      return Boolean(node)
    })()`),
    'The node card was not found',
  )
  await wait(80)
  const nodeSelection = await evaluate(
    `document.querySelector('.choice-card')?.getAttribute('aria-pressed')`,
  )
  assert(nodeSelection === 'true', 'Clicking the node card did not select the node')

  assert(
    await evaluate(`(() => {
      const modal = document.querySelector('.site-editor-modal')
      const domainInput = [...modal.querySelectorAll('label')]
        .find((item) => item.querySelector(':scope > span')?.textContent.trim() === '域名')
        ?.querySelector('input')
      if (!domainInput) return false
      domainInput.value = 'api.int.example.com'
      domainInput.dispatchEvent(new Event('input', { bubbles: true }))
      const template = [...document.querySelectorAll('.template-card')]
        .find((item) => item.textContent.includes('标准 HTTPS'))
      template?.click()
      return Boolean(template)
    })()`),
    'HTTPS template or domain field was not available',
  )
  await wait(100)
  assert(
    await evaluate(`(() => {
      const modal = document.querySelector('.site-editor-modal')
      const selection = modal.querySelector(
        '.certificate-field [aria-labelledby="site-certificate-label"] .n-base-selection, ' +
        '.certificate-field .n-base-selection',
      )
      selection?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      return Boolean(selection)
    })()`),
    'Certificate selector was not found',
  )
  await wait(100)
  assert(
    await evaluate(`(() => {
      const option = [...document.querySelectorAll('.n-base-select-option')]
        .find((item) => item.textContent.includes('*.int.example.com'))
      option?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      return Boolean(option)
    })()`),
    'The matching certificate option was not found',
  )
  await wait(100)
  const certificateState = await evaluate(`(() => ({
    config: document.querySelector('.conf-editor')?.value || '',
    pathRows: document.querySelectorAll('.certificate-path-row').length,
    summary: document.querySelector('.certificate-path-summary')?.textContent.trim() || '',
  }))()`)
  assert(
    certificateState.config.includes('/apps/nginx/cert/int.example.com.pem'),
    'Selecting a certificate did not rewrite ssl_certificate in the Conf preview',
  )
  assert(
    certificateState.config.includes('/apps/nginx/cert/int.example.com.key'),
    'Selecting a certificate did not rewrite ssl_certificate_key in the Conf preview',
  )
  assert(!certificateState.config.includes('/apps/nginx/cert/example.com.pem'), 'Template certificate path remained in the Conf preview')
  assert(certificateState.pathRows === 1, 'The selected node certificate path was not displayed')

  assert(
    await evaluate(`(() => {
      const nodes = [...document.querySelectorAll('.choice-card')]
      nodes[1]?.click()
      return nodes.length >= 2
    })()`),
    'The second node was not available',
  )
  await wait(100)
  const multiNodeState = await evaluate(`(() => ({
    pathRows: document.querySelectorAll('.certificate-path-row').length,
    summary: document.querySelector('.certificate-path-summary')?.textContent.trim() || '',
  }))()`)
  assert(multiNodeState.pathRows === 2, 'Certificate paths for both nodes were not displayed')
  assert(multiNodeState.summary.includes('逐节点替换'), 'Different certificate paths were not explained')

  assert(
    await evaluate(`(() => {
      const nodes = [...document.querySelectorAll('.choice-card')]
      nodes[0]?.click()
      return Boolean(nodes[0])
    })()`),
    'The first node could not be deselected',
  )
  await wait(100)
  const secondNodeConfig = await evaluate(`document.querySelector('.conf-editor')?.value || ''`)
  assert(
    secondNodeConfig.includes('/usr/local/nginx/certs/int.example.com.pem'),
    'Changing the representative node did not update the Conf preview path',
  )

  assert(
    await evaluate(`(() => {
      const editor = document.querySelector('.conf-editor')
      if (!editor) return false
      editor.value = editor.value
        .replace('/usr/local/nginx/certs/int.example.com.pem', '/manual/override.pem')
        .replace('/usr/local/nginx/certs/int.example.com.key', '/manual/override.key')
      editor.dispatchEvent(new Event('input', { bubbles: true }))
      return true
    })()`),
    'The Conf editor was not available for a manual certificate-path change',
  )
  await wait(80)
  const mismatchState = await evaluate(`(() => ({
    warning: document.querySelector('.certificate-path-preview')?.classList.contains('warning'),
    syncButton: [...document.querySelectorAll('button')]
      .some((item) => item.textContent.includes('同步右侧预览')),
  }))()`)
  assert(mismatchState.warning, 'A manually changed certificate path was not highlighted')
  assert(mismatchState.syncButton, 'The certificate preview did not offer an explicit sync action')

  assert(
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('.site-editor-modal button')]
        .find((item) => item.textContent.includes('保存草稿'))
      button?.click()
      return Boolean(button)
    })()`),
    'The save button was not found for certificate mismatch validation',
  )
  await wait(100)
  const mismatchBlocked = await evaluate(`(() => ({
    modal: Boolean(document.querySelector('.site-editor-modal')),
    message: [...document.querySelectorAll('.toast, .n-message, [role="alert"]')]
      .map((item) => item.textContent || '')
      .join(' '),
  }))()`)
  assert(mismatchBlocked.modal, 'An out-of-sync certificate path was saved instead of blocked')
  assert(
    mismatchBlocked.message.includes('同步右侧预览'),
    'The blocked save did not explain how to synchronize the certificate path',
  )

  assert(
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('button')]
        .find((item) => item.textContent.includes('同步右侧预览'))
      button?.click()
      return Boolean(button)
    })()`),
    'The certificate preview sync action was not clickable',
  )
  await wait(80)
  const synchronizedConfig = await evaluate(`document.querySelector('.conf-editor')?.value || ''`)
  assert(
    synchronizedConfig.includes('/usr/local/nginx/certs/int.example.com.pem') &&
      synchronizedConfig.includes('/usr/local/nginx/certs/int.example.com.key'),
    'Synchronizing did not restore the selected node certificate paths in the editor',
  )
  assert(!synchronizedConfig.includes('/manual/override.'), 'A manual certificate override survived sync')

  assert(
    await evaluate(`(() => {
      const modal = document.querySelector('.site-editor-modal')
      const selection = modal.querySelector(
        '.certificate-field [aria-labelledby="site-certificate-label"] .n-base-selection, ' +
        '.certificate-field .n-base-selection',
      )
      selection?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      return Boolean(selection)
    })()`),
    'Certificate selector could not be reopened',
  )
  await wait(80)
  assert(
    await evaluate(`(() => {
      const option = [...document.querySelectorAll('.n-base-select-option')]
        .find((item) => item.textContent.includes('暂不绑定'))
      option?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      return Boolean(option)
    })()`),
    'The unbind option was not found',
  )
  await wait(80)
  const unboundState = await evaluate(`(() => ({
    config: document.querySelector('.conf-editor')?.value || '',
    pathRows: document.querySelectorAll('.certificate-path-row').length,
  }))()`)
  assert(unboundState.pathRows === 0, 'Certificate path panel remained after unbinding')
  assert(
    unboundState.config.includes('/usr/local/nginx/certs/int.example.com.pem'),
    'Unbinding unexpectedly removed the existing Conf certificate path',
  )

  assert(
    await evaluate(`(() => {
      const template = [...document.querySelectorAll('.template-card')]
        .find((item) => item.textContent.includes('Stream TCP 代理'))
      template?.click()
      return Boolean(template)
    })()`),
    'Stream template was not clickable',
  )
  await wait(80)
  assert(
    await evaluate(`(() => {
      const confirm = [...document.querySelectorAll('.n-dialog .n-button')]
        .find((item) => item.textContent.includes('确认替换'))
      confirm?.click()
      return Boolean(confirm)
    })()`),
    'Dirty Conf template replacement did not require confirmation',
  )
  await wait(80)

  const streamState = await evaluate(`(() => ({
    context: document.querySelector('.editor-context-badge')?.textContent.trim(),
    config: document.querySelector('.conf-editor')?.value || '',
    selectedCount: [...document.querySelectorAll('.choice-card')]
      .filter((item) => item.getAttribute('aria-pressed') === 'true').length,
  }))()`)
  assert(streamState.context === 'STREAM', 'Stream context did not update')
  assert(streamState.config.includes('upstream tcp_backend'), 'Stream template content was not applied')
  assert(streamState.selectedCount === 1, 'Selected node was lost when switching context')

  assert(
    await evaluate(`(() => {
      const template = [...document.querySelectorAll('.template-card')]
        .find((item) => item.textContent.includes('Nginx Stub Status'))
      template?.click()
      return Boolean(template)
    })()`),
    'Stub Status template was not clickable',
  )
  await wait(80)
  const stubState = await evaluate(`(() => ({
    context: document.querySelector('.editor-context-badge')?.textContent.trim(),
    config: document.querySelector('.conf-editor')?.value || '',
    namedField: [...document.querySelectorAll('label > span')]
      .some((item) => item.textContent.trim() === '配置名称'),
  }))()`)
  assert(stubState.context === 'HTTP', 'Stub Status context did not update')
  assert(stubState.config.includes('stub_status;'), 'Stub Status template content was not applied')
  assert(stubState.namedField, 'Generic configuration fields were not shown')

  const toastFooterLayout = await evaluate(`(() => {
    const toast = document.querySelector('.toast')
    const footer = document.querySelector('.site-editor-modal .n-card__footer')
    const toastRect = toast?.getBoundingClientRect()
    const footerRect = footer?.getBoundingClientRect()
    const intersects = Boolean(
      toastRect && footerRect &&
      toastRect.left < footerRect.right && toastRect.right > footerRect.left &&
      toastRect.top < footerRect.bottom && toastRect.bottom > footerRect.top
    )
    return {
      hasFooter: Boolean(footerRect),
      intersects,
    }
  })()`)
  assert(toastFooterLayout.hasFooter, 'The site editor footer is missing before visual capture')
  assert(
    !toastFooterLayout.intersects,
    `A toast obscures the site editor footer: ${JSON.stringify(toastFooterLayout)}`,
  )

  const screenshot = await command('Page.captureScreenshot', { format: 'png' })
  await writeFile(resolve(artifactRoot, 'vue-site-editor.png'), Buffer.from(screenshot.data, 'base64'))

  assert(
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('.modal-footer button')]
        .find((item) => item.textContent.includes('取消'))
      button?.click()
      return Boolean(button)
    })()`),
    'The editor cancel button was not found',
  )
  await wait(100)
  const dirtyCloseState = await evaluate(`(() => ({
    editor: Boolean(document.querySelector('.site-editor-modal')),
    dialog: [...document.querySelectorAll('.n-dialog')]
      .some((item) => item.textContent.includes('放弃未保存的修改？')),
    continueButton: [...document.querySelectorAll('.n-dialog button')]
      .some((item) => item.textContent.includes('继续编辑')),
  }))()`)
  assert(dirtyCloseState.editor, 'Dirty editor closed without confirmation')
  assert(dirtyCloseState.dialog, 'Dirty editor did not show a discard confirmation')
  assert(dirtyCloseState.continueButton, 'Discard confirmation did not offer to continue editing')

  assert(
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('.n-dialog button')]
        .find((item) => item.textContent.includes('继续编辑'))
      button?.click()
      return Boolean(button)
    })()`),
    'The continue-editing action was not clickable',
  )
  await wait(350)
  assert(
    await evaluate(`Boolean(document.querySelector('.site-editor-modal')) &&
      ![...document.querySelectorAll('.n-dialog')]
        .some((item) => item.textContent.includes('放弃未保存的修改？') && item.getClientRects().length)`),
    'Continuing to edit did not dismiss only the confirmation dialog',
  )

  assert(
    await evaluate(`(() => {
      window.__qaOriginalFetch = window.fetch.bind(window)
      window.__qaCapturedSave = null
      window.__qaReleaseSave = null
      window.fetch = async (input, init = {}) => {
        const url = String(input?.url || input)
        const method = String(init.method || input?.method || 'GET').toUpperCase()
        if (url.includes('/api/v1/admin/ui-state') && method === 'PUT') {
          window.__qaCapturedSave = JSON.parse(String(init.body || '{}'))
          await new Promise((resolveSave) => { window.__qaReleaseSave = resolveSave })
        }
        return window.__qaOriginalFetch(input, init)
      }
      const button = [...document.querySelectorAll('.modal-footer button')]
        .find((item) => item.textContent.includes('保存草稿'))
      button?.click()
      return Boolean(button)
    })()`),
    'The save button was not found for the saving-lock test',
  )
  for (let attempt = 0; attempt < 30; attempt += 1) {
    if (await evaluate(`Boolean(window.__qaCapturedSave)`)) break
    await wait(20)
  }
  const savingState = await evaluate(`(() => {
    const grid = document.querySelector('.site-editor-grid')
    const cancel = [...document.querySelectorAll('.modal-footer button')]
      .find((item) => item.textContent.includes('取消'))
    const save = [...document.querySelectorAll('.modal-footer button')]
      .find((item) => item.textContent.includes('保存草稿'))
    const captured = window.__qaCapturedSave
    const savedSite = captured?.state?.sites?.find((item) => item.name === 'Nginx Stub Status')
    return {
      busy: grid?.getAttribute('aria-busy'),
      inert: grid?.hasAttribute('inert'),
      savingClass: grid?.classList.contains('is-saving'),
      cancelDisabled: Boolean(cancel?.disabled),
      saveDisabled: Boolean(save?.disabled),
      closeButton: Boolean(document.querySelector('.site-editor-modal .n-card-header__close')),
      payloadConfig: savedSite?.config || '',
    }
  })()`)
  assert(savingState.busy === 'true', 'The editor did not expose its saving state to assistive technology')
  assert(savingState.inert, 'Editor fields remained interactive while saving')
  assert(savingState.savingClass, 'The saving visual state was not applied')
  assert(savingState.cancelDisabled, 'Cancel remained enabled while saving')
  assert(savingState.saveDisabled, 'Save remained enabled while saving')
  assert(!savingState.closeButton, 'The modal close control remained available while saving')
  assert(
    savingState.payloadConfig === stubState.config,
    'The saved UI-state payload did not match the editor preview',
  )
  await evaluate(`window.__qaReleaseSave?.()`)
  await wait(450)
  assert(
    !(await evaluate(`Boolean(document.querySelector('.site-editor-modal')?.getClientRects().length)`)),
    'The editor did not close after the held save completed',
  )

  await evaluate(`window.fetch = window.__qaOriginalFetch || window.fetch`)
  await command('Page.enable')
  await command('Page.addScriptToEvaluateOnNewDocument', {
    source: `(() => {
      const originalFetch = window.fetch.bind(window)
      window.__qaNodeInterceptInstalled = true
      window.__qaNodeInterceptCount = 0
      window.fetch = async (input, init) => {
        const response = await originalFetch(input, init)
        const url = String(input?.url || input)
        if (!url.includes('/api/v1/admin/nodes')) return response
        window.__qaNodeInterceptCount += 1
        const body = await response.clone().json()
        if (body?.items?.[1]) {
          body.items[1].status = 'offline'
          body.items[1].reported_status = 'offline'
        }
        return new Response(JSON.stringify(body), {
          status: response.status,
          headers: { 'content-type': 'application/json; charset=utf-8' },
        })
      }
    })()`,
  })
  await command('Page.reload', { ignoreCache: true })
  await wait(900)
  await command('Accessibility.enable')
  const accessibility = await command('Accessibility.getFullAXTree')
  const accessibleNames = new Set(
    accessibility.nodes.map((node) => String(node.name?.value || '')).filter(Boolean),
  )
  assert(accessibleNames.has('按 Agent 筛选'), 'The Agent filter has no accessible name')
  assert(accessibleNames.has('按状态筛选'), 'The status filter has no accessible name')

  const offlineState = await evaluate(`(() => ({
    chip: [...document.querySelectorAll('.node-chip.offline')]
      .some((item) => item.textContent.includes('it-nginx-bj-01')),
    dot: Boolean(document.querySelector('.deployment-list .online-dot.offline')),
    interceptInstalled: window.__qaNodeInterceptInstalled || false,
    interceptCount: window.__qaNodeInterceptCount || 0,
    chips: [...document.querySelectorAll('.node-chip')].map((item) => ({
      text: item.textContent.trim(),
      className: item.className,
      title: item.getAttribute('title'),
    })),
  }))()`)
  assert(
    offlineState.chip,
    `Offline deployment was not visually distinguished in the site list: ${JSON.stringify(offlineState)}`,
  )
  assert(offlineState.dot, 'Offline deployment did not use the danger status indicator')

  await command('Emulation.setDeviceMetricsOverride', {
    width: 960,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  })
  await wait(120)
  const mainViewport = await evaluate(`(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }))()`)
  assert(mainViewport.clientWidth === 960, 'The 960px equivalent viewport was not applied')
  assert(
    mainViewport.scrollWidth <= mainViewport.clientWidth,
    `The main document overflows horizontally at 960px (${mainViewport.scrollWidth}px)`,
  )

  assert(
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('button')]
        .find((item) => item.textContent.includes('新增站点'))
      button?.click()
      return Boolean(button)
    })()`),
    'The create-site button was not available at the narrow viewport',
  )
  await wait(180)
  const narrowEditor = await evaluate(`(() => {
    const modal = document.querySelector('.site-editor-modal')
    const rect = modal?.getBoundingClientRect()
    const offline = [...document.querySelectorAll('.choice-card.offline')]
      .find((item) => item.textContent.includes('it-nginx-bj-01'))
    return {
      documentClientWidth: document.documentElement.clientWidth,
      documentScrollWidth: document.documentElement.scrollWidth,
      modalLeft: rect?.left ?? -1,
      modalRight: rect?.right ?? -1,
      offlineDisabled: Boolean(offline?.disabled),
      offlineOpacity: offline ? Number(getComputedStyle(offline).opacity) : 1,
    }
  })()`)
  assert(
    narrowEditor.documentScrollWidth <= narrowEditor.documentClientWidth,
    `The editor causes document overflow at 960px (${narrowEditor.documentScrollWidth}px)`,
  )
  assert(
    narrowEditor.modalLeft >= 0 && narrowEditor.modalRight <= narrowEditor.documentClientWidth,
    'The site editor extends outside the 960px viewport',
  )
  assert(narrowEditor.offlineDisabled, 'An offline node remained selectable in the editor')
  assert(narrowEditor.offlineOpacity < 1, 'The offline node card has no distinct visual treatment')

  console.log('PASS unified site editor interaction')
  console.log('PASS dirty close protection and saving lock')
  console.log('PASS editor preview and saved payload stay consistent')
  console.log('PASS 960px viewport, accessible filters, and offline-node treatment')
  console.log(`INFO templates=${initial.templates.length} node-card=clickable stream=ok stub-status=ok`)
} finally {
  socket?.close()
  if (browser.exitCode === null) {
    browser.kill()
    await Promise.race([
      new Promise((resolveExit) => browser.once('exit', resolveExit)),
      wait(1500),
    ])
  }
  await new Promise((resolveClose) => qaServer.close(resolveClose))
  await rm(profileRoot, { recursive: true, force: true }).catch(() => undefined)
}
