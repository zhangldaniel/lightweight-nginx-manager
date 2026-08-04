import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { access, mkdir, rm } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

process.env.QA_PORT ||= '4187'
const port = Number(process.env.QA_PORT)
const debugPort = port + 5000
const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const artifactRoot = resolve(projectRoot, 'artifacts')
const profileRoot = resolve(artifactRoot, `.chrome-site-detail-${Date.now()}`)
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
    '--window-size=1440,620',
    `http://127.0.0.1:${port}/#/sites`,
  ],
  { stdio: 'ignore', windowsHide: true },
)

const wait = (milliseconds) => new Promise((resolveWait) => setTimeout(resolveWait, milliseconds))

async function removeBrowserProfile() {
  // Chrome can recreate its downloaded spelling dictionary just after the main
  // process exits, so sweep the dedicated QA profile more than once.
  for (let attempt = 0; attempt < 6; attempt += 1) {
    await rm(profileRoot, { recursive: true, force: true }).catch(() => undefined)
    if (attempt < 5) await wait(200)
  }
}

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

async function waitFor(expression, message) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (await evaluate(expression)) return
    await wait(50)
  }
  throw new Error(message)
}

async function selectDeploymentAction(label) {
  assert(
    await evaluate(`(() => {
      const modal = [...document.querySelectorAll('.action-modal')]
        .find((item) => item.getClientRects().length && item.textContent.includes('调整部署范围'))
      const selection = modal?.querySelector('.n-base-selection')
      selection?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      return Boolean(selection)
    })()`),
    'The deployment action selector was not available',
  )
  await waitFor(
    `[...document.querySelectorAll('.n-base-select-option')].some((item) => item.textContent.includes(${JSON.stringify(label)}))`,
    `The deployment action option ${label} was not available`,
  )
  assert(
    await evaluate(`(() => {
      const option = [...document.querySelectorAll('.n-base-select-option')]
        .find((item) => item.textContent.includes(${JSON.stringify(label)}))
      option?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      return Boolean(option)
    })()`),
    `The deployment action option ${label} was not clickable`,
  )
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
  await command('Emulation.setDeviceMetricsOverride', {
    width: 1440,
    height: 620,
    deviceScaleFactor: 1,
    mobile: false,
  })
  await command('Page.reload', { ignoreCache: true })
  await wait(700)
  await waitFor(
    `document.querySelectorAll('.site-row').length === 2 && Boolean(document.querySelector('.sites-detail-scroll'))`,
    'The sites page did not expose its detail scrolling contract',
  )
  await evaluate(`document.querySelector('.master-detail')?.scrollIntoView({ block: 'start' })`)
  await wait(100)

  const desktopBeforeSelection = await evaluate(`(() => {
    const panel = document.querySelector('.sites-detail-panel')
    const scroller = document.querySelector('.sites-detail-scroll')
    const rect = panel?.getBoundingClientRect()
    if (scroller) scroller.scrollTop = scroller.scrollHeight
    return {
      panel: Boolean(panel),
      position: panel ? getComputedStyle(panel).position : '',
      panelBottom: rect?.bottom ?? Number.POSITIVE_INFINITY,
      viewportHeight: window.innerHeight,
      scrollTop: scroller?.scrollTop ?? 0,
      scrollHeight: scroller?.scrollHeight ?? 0,
      clientHeight: scroller?.clientHeight ?? 0,
    }
  })()`)
  assert(desktopBeforeSelection.panel, 'The desktop detail panel is missing')
  assert.equal(desktopBeforeSelection.position, 'sticky', 'The desktop detail is not viewport-sticky')
  assert(
    desktopBeforeSelection.panelBottom <= desktopBeforeSelection.viewportHeight + 1,
    `The desktop detail escapes the viewport: ${JSON.stringify(desktopBeforeSelection)}`,
  )
  assert(
    desktopBeforeSelection.scrollHeight > desktopBeforeSelection.clientHeight &&
      desktopBeforeSelection.scrollTop > 0,
    `The desktop detail has no independent scrolling body: ${JSON.stringify(desktopBeforeSelection)}`,
  )

  assert(
    await evaluate(`(() => {
      const row = document.querySelectorAll('.site-row')[1]
      row?.focus()
      row?.click()
      return Boolean(row)
    })()`),
    'The second configuration row was not clickable on desktop',
  )
  await waitFor(
    `document.querySelector('.sites-detail-panel h2')?.textContent.includes('订单服务 upstream')`,
    'Selecting a different configuration did not update the desktop detail',
  )
  await wait(80)
  assert.equal(
    await evaluate(`document.querySelector('.sites-detail-scroll')?.scrollTop ?? -1`),
    0,
    'Selecting a configuration did not reset the detail scroll position',
  )

  const deploymentActions = await evaluate(`(() =>
    [...document.querySelectorAll('.sites-detail-panel .detail-actions button')]
      .map((button) => button.textContent.replace(/\\s+/g, ' ').trim())
  )()`)
  assert(
    deploymentActions.some((label) => label.includes('调整部署范围')),
    `The detail has no unified deployment adjustment entry: ${JSON.stringify(deploymentActions)}`,
  )
  assert(
    !deploymentActions.some(
      (label) => label.includes('复制 / 迁移') || label.includes('从节点移除'),
    ),
    `Legacy topology actions remain in the detail: ${JSON.stringify(deploymentActions)}`,
  )

  assert(
    await evaluate(`(() => {
      const edit = [...document.querySelectorAll('.sites-detail-panel button')]
        .find((button) => button.textContent.includes('编辑配置'))
      edit?.click()
      return Boolean(edit)
    })()`),
    'The existing configuration editor was not reachable',
  )
  await waitFor(
    `Boolean(document.querySelector('.site-editor-modal')?.getClientRects().length)`,
    'The existing configuration editor did not open',
  )
  const readonlyDeployment = await evaluate(`(() => {
    const modal = document.querySelector('.site-editor-modal')
    const fieldset = [...(modal?.querySelectorAll('fieldset') || [])]
      .find((item) => item.querySelector('legend')?.textContent.includes('当前部署范围'))
    return {
      found: Boolean(fieldset),
      legend: fieldset?.querySelector('legend')?.textContent.trim() || '',
      text: fieldset?.textContent.replace(/\\s+/g, ' ').trim() || '',
      enabledNodeSelectors: [...(fieldset?.querySelectorAll('.choice-card') || [])]
        .filter((item) => !item.disabled && item.getAttribute('aria-disabled') !== 'true').length,
      editableDirectoryControls:
        modal?.querySelectorAll('.entry-targets input, .entry-targets [role="combobox"]').length || 0,
    }
  })()`)
  assert(readonlyDeployment.found, 'The editor does not expose its current deployment range')
  assert(readonlyDeployment.legend.includes('只读'), 'The editor does not label deployment topology as read-only')
  assert(
    readonlyDeployment.text.includes('it-nginx-sh-01') &&
      readonlyDeployment.text.includes('调整部署范围'),
    `The read-only deployment range is incomplete: ${JSON.stringify(readonlyDeployment)}`,
  )
  assert.equal(readonlyDeployment.enabledNodeSelectors, 0, 'An existing-site node selector remains clickable')
  assert.equal(readonlyDeployment.editableDirectoryControls, 0, 'An existing-site directory selector remains editable')
  await evaluate(`(() => {
    const cancel = [...document.querySelectorAll('.site-editor-modal button')]
      .find((button) => button.textContent.trim() === '取消')
    cancel?.click()
  })()`)
  await waitFor(
    `!document.querySelector('.site-editor-modal')?.getClientRects().length`,
    'The unchanged editor did not close',
  )

  assert(
    await evaluate(`(() => {
      const adjust = [...document.querySelectorAll('.sites-detail-panel button')]
        .find((button) => button.textContent.includes('调整部署范围'))
      adjust?.click()
      return Boolean(adjust)
    })()`),
    'The unified deployment adjustment entry was not clickable',
  )
  await waitFor(
    `[...document.querySelectorAll('.action-modal')].some((item) => item.getClientRects().length && item.textContent.includes('调整部署范围'))`,
    'The unified deployment adjustment did not open',
  )
  const addMode = await evaluate(`(() => {
    const modal = [...document.querySelectorAll('.action-modal')]
      .find((item) => item.getClientRects().length && item.textContent.includes('调整部署范围'))
    return {
      text: modal?.textContent.replace(/\\s+/g, ' ').trim() || '',
      targets: [...(modal?.querySelectorAll('.transfer-target strong') || [])]
        .map((item) => item.textContent.trim()),
    }
  })()`)
  assert(addMode.text.includes('发布到新的节点'), 'The deployment modal did not open in add-node mode')
  assert.deepEqual(addMode.targets, ['it-nginx-bj-01'], 'Add-node mode exposes the wrong target set')

  await selectDeploymentAction('迁移配置目录')
  await waitFor(
    `[...document.querySelectorAll('.action-modal')].some((item) => item.getClientRects().length && item.textContent.includes('原子迁移配置文件'))`,
    'The unified deployment modal did not switch to directory migration',
  )
  const migrateMode = await evaluate(`(() => {
    const modal = [...document.querySelectorAll('.action-modal')]
      .find((item) => item.getClientRects().length && item.textContent.includes('调整部署范围'))
    return [...(modal?.querySelectorAll('.transfer-target strong') || [])]
      .map((item) => item.textContent.trim())
  })()`)
  assert.deepEqual(migrateMode, ['it-nginx-sh-01'], 'Migration mode exposes the wrong deployed nodes')

  await selectDeploymentAction('移除节点')
  await waitFor(
    `[...document.querySelectorAll('.action-modal')].some((item) => item.getClientRects().length && item.textContent.includes('安全删除受托管配置'))`,
    'The unified deployment modal did not switch to node removal',
  )
  assert(
    await evaluate(`(() => {
      const modal = [...document.querySelectorAll('.action-modal')]
        .find((item) => item.getClientRects().length && item.textContent.includes('调整部署范围'))
      const checkbox = modal?.querySelector('.transfer-target .n-checkbox')
      checkbox?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      return Boolean(checkbox)
    })()`),
    'The final deployed node was not selectable for removal',
  )
  await waitFor(
    `[...document.querySelectorAll('.action-modal')].some((item) => item.getClientRects().length && Boolean(item.querySelector('.security-banner')))`,
    'Removing the final node did not expose the platform-record choice',
  )
  const finalNodeChoice = await evaluate(`(() => {
    const modal = [...document.querySelectorAll('.action-modal')]
      .find((item) => item.getClientRects().length && item.textContent.includes('调整部署范围'))
    const banner = modal?.querySelector('.security-banner')
    return {
      text: banner?.textContent.replace(/\\s+/g, ' ').trim() || '',
      selectedTargets: modal?.querySelectorAll('.transfer-target.selected').length || 0,
    }
  })()`)
  assert.equal(finalNodeChoice.selectedTargets, 1, 'The final deployed node was not selected')
  assert(
    finalNodeChoice.text.includes('默认只将配置保留为“未部署”') &&
      finalNodeChoice.text.includes('同时删除平台记录'),
    `The final-node choice is not explicit: ${JSON.stringify(finalNodeChoice)}`,
  )
  await evaluate(`(() => {
    const modal = [...document.querySelectorAll('.action-modal')]
      .find((item) => item.getClientRects().length && item.textContent.includes('调整部署范围'))
    modal?.querySelector('.security-banner .n-checkbox')
      ?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  })()`)
  await waitFor(
    `[...document.querySelectorAll('.action-modal')].some((item) => item.getClientRects().length && Boolean(item.querySelector('.security-banner input[placeholder="订单服务 upstream"]')))`,
    'Deleting the platform record did not require the configuration name',
  )
  const destructiveConfirmation = await evaluate(`(() => {
    const modal = [...document.querySelectorAll('.action-modal')]
      .find((item) => item.getClientRects().length && item.textContent.includes('调整部署范围'))
    const input = modal?.querySelector('.security-banner input[placeholder="订单服务 upstream"]')
    const submit = [...(modal?.querySelectorAll('.modal-footer button') || [])]
      .find((button) => button.textContent.includes('移除并删除平台记录'))
    return { input: Boolean(input), submitDisabled: Boolean(submit?.disabled) }
  })()`)
  assert(destructiveConfirmation.input, 'The destructive confirmation input is missing')
  assert(destructiveConfirmation.submitDisabled, 'Destructive removal is enabled before name confirmation')
  await evaluate(`(() => {
    const modal = [...document.querySelectorAll('.action-modal')]
      .find((item) => item.getClientRects().length && item.textContent.includes('调整部署范围'))
    const cancel = [...(modal?.querySelectorAll('.modal-footer button') || [])]
      .find((button) => button.textContent.trim() === '取消')
    cancel?.click()
  })()`)
  await waitFor(
    `![...document.querySelectorAll('.action-modal')].some((item) => item.getClientRects().length && item.textContent.includes('调整部署范围'))`,
    'The deployment adjustment modal did not close',
  )

  await command('Emulation.setDeviceMetricsOverride', {
    width: 960,
    height: 560,
    deviceScaleFactor: 1,
    mobile: false,
  })
  await command('Page.reload', { ignoreCache: true })
  await wait(700)
  await waitFor(
    `document.querySelectorAll('.site-row').length === 2 && Boolean(document.querySelector('.sites-detail-panel'))`,
    'The sites page did not reload at the narrow viewport',
  )
  await wait(250)

  const narrowInitial = await evaluate(`(() => {
    const list = document.querySelector('.site-list')
    const panel = document.querySelector('.sites-detail-panel')
    if (list) {
      list.style.height = '76px'
      list.style.overflowY = 'auto'
      list.scrollTop = list.scrollHeight
    }
    return {
      listScrollTop: list?.scrollTop ?? 0,
      drawerOpen: panel?.classList.contains('is-drawer-open') || false,
      panelPosition: panel ? getComputedStyle(panel).position : '',
    }
  })()`)
  assert(narrowInitial.listScrollTop > 0, 'The QA list fixture could not establish a scroll position')
  assert(!narrowInitial.drawerOpen, 'The narrow detail drawer opened before the user selected a row')
  assert.equal(narrowInitial.panelPosition, 'fixed', 'The narrow detail is not an overlay drawer')

  assert(
    await evaluate(`(() => {
      const row = document.querySelectorAll('.site-row')[1]
      row?.focus()
      row?.click()
      return Boolean(row)
    })()`),
    'The second configuration row was not clickable at the narrow viewport',
  )
  await wait(150)
  const narrowAfterClick = await evaluate(`(() => ({
    innerWidth: window.innerWidth,
    mediaMatches: window.matchMedia('(max-width: 1220px)').matches,
    drawerOpen:
      document.querySelector('.sites-detail-panel')?.classList.contains('is-drawer-open') || false,
    selectedTitle:
      document.querySelector('.site-row[aria-current="true"] .site-primary strong')?.textContent.trim() || '',
  }))()`)
  assert(
    narrowAfterClick.drawerOpen,
    `Clicking a configuration did not open the narrow detail drawer: ${JSON.stringify(narrowAfterClick)}`,
  )
  await wait(350)

  const openDrawer = await evaluate(`(() => {
    const panel = document.querySelector('.sites-detail-panel')
    const close = document.querySelector('.sites-detail-close')
    const rect = panel?.getBoundingClientRect()
    const style = panel ? getComputedStyle(panel) : null
    return {
      visible: style?.visibility === 'visible' && style?.pointerEvents !== 'none',
      rightDelta: rect ? Math.abs(window.innerWidth - rect.right) : Number.POSITIVE_INFINITY,
      title: panel?.querySelector('h2')?.textContent.trim() || '',
      closeName: close?.getAttribute('aria-label') || close?.textContent.trim() || '',
      role: panel?.getAttribute('role') || '',
      ariaModal: panel?.getAttribute('aria-modal') || '',
      focusedClose: document.activeElement === close,
      currentRows: document.querySelectorAll('.site-row[aria-current="true"]').length,
    }
  })()`)
  assert(openDrawer.visible && openDrawer.rightDelta <= 1, `The drawer is not visibly right-anchored: ${JSON.stringify(openDrawer)}`)
  assert(openDrawer.title.includes('订单服务 upstream'), 'The drawer does not show the selected configuration')
  assert.equal(openDrawer.closeName, '关闭详情', 'The drawer close action has no stable accessible name')
  assert.equal(openDrawer.role, 'dialog', 'The narrow detail is not exposed as a dialog')
  assert.equal(openDrawer.ariaModal, 'true', 'The narrow detail is not exposed as modal')
  assert(openDrawer.focusedClose, 'Opening the detail drawer did not move focus into it')
  assert.equal(openDrawer.currentRows, 1, 'The list does not expose exactly one selected configuration')

  await evaluate(`document.querySelectorAll('.site-row')[0]?.click()`)
  await waitFor(
    `document.querySelector('.sites-detail-panel h2')?.textContent.includes('api.int.example.com')`,
    'Changing selection while the drawer is open did not update its content',
  )
  const drawerScroll = await evaluate(`(() => {
    const scroller = document.querySelector('.sites-detail-scroll')
    if (scroller) scroller.scrollTop = scroller.scrollHeight
    return {
      scrollTop: scroller?.scrollTop ?? 0,
      scrollHeight: scroller?.scrollHeight ?? 0,
      clientHeight: scroller?.clientHeight ?? 0,
    }
  })()`)
  assert(
    drawerScroll.scrollTop > 0,
    `The drawer body could not be scrolled independently: ${JSON.stringify(drawerScroll)}`,
  )
  await evaluate(`document.querySelectorAll('.site-row')[1]?.click()`)
  await waitFor(
    `document.querySelector('.sites-detail-panel h2')?.textContent.includes('订单服务 upstream')`,
    'Changing selection while the drawer is open did not update its content',
  )
  await wait(80)
  assert.equal(
    await evaluate(`document.querySelector('.sites-detail-scroll')?.scrollTop ?? -1`),
    0,
    'Changing selection while the drawer is open did not reset its body scroll',
  )

  const beforeClose = await evaluate(`(() => ({
    listScrollTop: document.querySelector('.site-list')?.scrollTop ?? 0,
    selectedTitle: document.querySelector('.site-row[aria-current="true"] .site-primary strong')?.textContent.trim() || '',
  }))()`)
  assert(
    await evaluate(`(() => {
      const close = document.querySelector('.sites-detail-close')
      close?.click()
      return Boolean(close)
    })()`),
    'The drawer close action was not clickable',
  )
  await waitFor(
    `!document.querySelector('.sites-detail-panel')?.classList.contains('is-drawer-open')`,
    'The detail drawer did not close',
  )
  await wait(350)
  const afterClose = await evaluate(`(() => ({
    listScrollTop: document.querySelector('.site-list')?.scrollTop ?? 0,
    selectedTitle: document.querySelector('.site-row[aria-current="true"] .site-primary strong')?.textContent.trim() || '',
    hidden: getComputedStyle(document.querySelector('.sites-detail-panel')).visibility === 'hidden',
    inert: document.querySelector('.sites-detail-panel')?.inert || false,
    focusedRow:
      document.activeElement?.closest?.('.site-row')?.querySelector('.site-primary strong')?.textContent.trim() || '',
  }))()`)
  assert.equal(afterClose.selectedTitle, beforeClose.selectedTitle, 'Closing the drawer lost the selected configuration')
  assert.equal(afterClose.listScrollTop, beforeClose.listScrollTop, 'Closing the drawer lost the list scroll position')
  assert(afterClose.hidden, 'The closed drawer remains visible or interactive')
  assert(afterClose.inert, 'The closed drawer remains keyboard-interactive')
  assert(
    afterClose.focusedRow.includes('订单服务 upstream'),
    `Closing the drawer did not restore focus to its source row: ${JSON.stringify(afterClose)}`,
  )

  assert(
    await evaluate(`(() => {
      const selectedRow = document.querySelector('.site-row[aria-current="true"]')
      selectedRow?.click()
      return Boolean(selectedRow)
    })()`),
    'The selected configuration row was not clickable after closing the drawer',
  )
  await waitFor(
    `document.querySelector('.sites-detail-panel')?.classList.contains('is-drawer-open')`,
    'Clicking the already-selected configuration did not reopen its drawer',
  )

  await command('Page.enable')
  await command('Page.addScriptToEvaluateOnNewDocument', {
    source: `(() => {
      if (!location.search.includes('qa-deleted-selection=1')) return
      const originalFetch = window.fetch.bind(window)
      window.__qaDeletedSelectionUiStateCount = 0
      window.fetch = async (input, init) => {
        const response = await originalFetch(input, init)
        const url = String(input?.url || input)
        const method = String(init?.method || input?.method || 'GET').toUpperCase()
        if (method === 'GET' && url.includes('/api/v1/admin/ui-state')) {
          window.__qaDeletedSelectionUiStateCount += 1
          const body = await response.clone().json()
          if (window.__qaDeletedSelectionUiStateCount > 1) {
            body.revision = Number(body.revision || 0) + window.__qaDeletedSelectionUiStateCount
            body.state.sites = body.state.sites.filter((site) => site.id !== 'generic-upstream')
          }
          return new Response(JSON.stringify(body), {
            status: response.status,
            headers: { 'content-type': 'application/json; charset=utf-8' },
          })
        }
        return response
      }
    })()`,
  })
  await command('Page.navigate', {
    url: `http://127.0.0.1:${port}/?qa-deleted-selection=1#/sites`,
  })
  await wait(700)
  await waitFor(
    `document.querySelectorAll('.site-row').length === 2`,
    'The delete-lifecycle fixture did not load both configurations',
  )
  await evaluate(`document.querySelectorAll('.site-row')[1]?.click()`)
  await waitFor(
    `document.querySelector('.sites-detail-panel')?.classList.contains('is-drawer-open')`,
    'The delete-lifecycle fixture could not open the selected drawer',
  )
  await wait(2800)
  await waitFor(
    `window.__qaDeletedSelectionUiStateCount > 1 && document.querySelectorAll('.site-row').length === 1`,
    'The selected configuration was not removed by the background refresh fixture',
  )
  const deletedSelectionLifecycle = await evaluate(`(() => ({
    drawerOpen:
      document.querySelector('.sites-detail-panel')?.classList.contains('is-drawer-open') || false,
    selectedTitle:
      document.querySelector('.site-row[aria-current="true"] .site-primary strong')?.textContent.trim() || '',
  }))()`)
  assert(
    !deletedSelectionLifecycle.drawerOpen,
    `Deleting the selected configuration silently switched the open drawer: ${JSON.stringify(deletedSelectionLifecycle)}`,
  )
  assert(
    deletedSelectionLifecycle.selectedTitle.includes('api.int.example.com'),
    `Deleting the selected configuration did not leave a stable list selection: ${JSON.stringify(deletedSelectionLifecycle)}`,
  )

  await command('Page.addScriptToEvaluateOnNewDocument', {
    source: `(() => {
      if (!location.search.includes('qa-eligibility=1')) return
      const originalFetch = window.fetch.bind(window)
      window.fetch = async (input, init) => {
        const response = await originalFetch(input, init)
        const url = String(input?.url || input)
        const method = String(init?.method || input?.method || 'GET').toUpperCase()
        if (method === 'GET' && url.includes('/api/v1/admin/ui-state')) {
          const body = await response.clone().json()
          const site = body.state.sites.find((item) => item.id === 'site-api')
          if (site) site.nodeReadOnly = { 'node-bj-01': true }
          return new Response(JSON.stringify(body), {
            status: response.status,
            headers: { 'content-type': 'application/json; charset=utf-8' },
          })
        }
        if (method === 'GET' && url.includes('/api/v1/admin/nodes')) {
          const body = await response.clone().json()
          const sh = body.items.find((item) => item.id === 'node-sh-01')
          const bj = body.items.find((item) => item.id === 'node-bj-01')
          if (sh) sh.capabilities = sh.capabilities.filter((item) => item !== 'config_delete')
          if (bj) bj.capabilities = bj.capabilities.filter((item) => item !== 'config_apply')
          return new Response(JSON.stringify(body), {
            status: response.status,
            headers: { 'content-type': 'application/json; charset=utf-8' },
          })
        }
        return response
      }
    })()`,
  })
  await command('Emulation.setDeviceMetricsOverride', {
    width: 1440,
    height: 620,
    deviceScaleFactor: 1,
    mobile: false,
  })
  await command('Page.navigate', {
    url: `http://127.0.0.1:${port}/?qa-eligibility=1#/sites`,
  })
  await wait(700)
  await waitFor(
    `document.querySelectorAll('.site-row').length === 2`,
    'The deployment eligibility fixture did not load',
  )
  await evaluate(`document.querySelectorAll('.site-row')[1]?.click()`)
  await evaluate(`(() => {
    const adjust = [...document.querySelectorAll('.sites-detail-panel button')]
      .find((button) => button.textContent.includes('调整部署范围'))
    adjust?.click()
  })()`)
  await waitFor(
    `[...document.querySelectorAll('.action-modal')].some((item) => item.getClientRects().length && item.textContent.includes('调整部署范围'))`,
    'The deployment eligibility modal did not open',
  )
  const unsupportedAdd = await evaluate(`(() => {
    const target = document.querySelector('.action-modal .transfer-target')
    return {
      disabled: target?.classList.contains('disabled') || false,
      checkboxDisabled: Boolean(target?.querySelector('.n-checkbox--disabled')),
      hint: target?.querySelector('small')?.textContent.trim() || '',
    }
  })()`)
  assert(
    unsupportedAdd.disabled && unsupportedAdd.checkboxDisabled && unsupportedAdd.hint.includes('配置写入'),
    `Add mode accepted a node without config_apply: ${JSON.stringify(unsupportedAdd)}`,
  )

  await selectDeploymentAction('迁移配置目录')
  const unsupportedMigration = await evaluate(`(() => {
    const target = document.querySelector('.action-modal .transfer-target')
    return {
      disabled: target?.classList.contains('disabled') || false,
      hint: target?.querySelector('small')?.textContent.trim() || '',
    }
  })()`)
  assert(
    unsupportedMigration.disabled && unsupportedMigration.hint.includes('配置删除'),
    `Migration mode accepted a node without config_delete: ${JSON.stringify(unsupportedMigration)}`,
  )

  await selectDeploymentAction('移除节点')
  const unsupportedRemoval = await evaluate(`(() => {
    const target = document.querySelector('.action-modal .transfer-target')
    return {
      disabled: target?.classList.contains('disabled') || false,
      hint: target?.querySelector('small')?.textContent.trim() || '',
    }
  })()`)
  assert(
    unsupportedRemoval.disabled && unsupportedRemoval.hint.includes('配置删除'),
    `Remove mode accepted a node without config_delete: ${JSON.stringify(unsupportedRemoval)}`,
  )
  await evaluate(`(() => {
    const cancel = [...document.querySelectorAll('.action-modal .modal-footer button')]
      .find((button) => button.textContent.trim() === '取消')
    cancel?.click()
    document.querySelectorAll('.site-row')[0]?.click()
  })()`)
  await waitFor(
    `document.querySelector('.sites-detail-panel h2')?.textContent.includes('api.int.example.com')`,
    'The read-only eligibility fixture could not select the site configuration',
  )
  await evaluate(`(() => {
    const adjust = [...document.querySelectorAll('.sites-detail-panel button')]
      .find((button) => button.textContent.includes('调整部署范围'))
    adjust?.click()
  })()`)
  await waitFor(
    `[...document.querySelectorAll('.action-modal')].some((item) => item.getClientRects().length && item.textContent.includes('调整部署范围'))`,
    'The read-only eligibility modal did not reopen',
  )
  await selectDeploymentAction('移除节点')
  const readOnlyTarget = await evaluate(`(() => {
    const target = [...document.querySelectorAll('.action-modal .transfer-target')]
      .find((item) => item.textContent.includes('it-nginx-bj-01'))
    return {
      disabled: target?.classList.contains('disabled') || false,
      hint: target?.querySelector('small')?.textContent.trim() || '',
    }
  })()`)
  assert(
    readOnlyTarget.disabled && readOnlyTarget.hint.includes('只读'),
    `A read-only managed configuration remained selectable: ${JSON.stringify(readOnlyTarget)}`,
  )
  await evaluate(`(() => {
    const cancel = [...document.querySelectorAll('.action-modal .modal-footer button')]
      .find((button) => button.textContent.trim() === '取消')
    cancel?.click()
  })()`)

  await command('Page.addScriptToEvaluateOnNewDocument', {
    source: `(() => {
      const originalFetch = window.fetch.bind(window)
      window.__qaPendingInterceptInstalled = true
      window.__qaPendingUiStateCount = 0
      window.fetch = async (input, init) => {
        const response = await originalFetch(input, init)
        const url = String(input?.url || input)
        const method = String(init?.method || input?.method || 'GET').toUpperCase()
        if (method === 'GET' && url.includes('/api/v1/admin/ui-state')) {
          window.__qaPendingUiStateCount += 1
          const body = await response.clone().json()
          const site = body?.state?.sites?.[0]
          if (site) {
            site.status = 'publishing'
            site.pendingRemote = {
              operationId: 'operation-ui-pending',
              operation: 'publish',
              publish: true,
              baseStatus: 'published',
              candidateVersion: Number(site.version || 0) + 1,
              alreadyCompleted: 0,
              totalTargets: 1,
              jobs: [{
                id: 'job-ui-pending',
                nodeId: site.nodeIds[0],
                candidateHash: 'pending-hash',
              }],
            }
          }
          const unassigned = body?.state?.sites?.[1]
          if (unassigned) {
            unassigned.nodeIds = []
            unassigned.nodeHashes = {}
            unassigned.nodeConfigPaths = {}
            unassigned.nodeConfigEntryIds = {}
            unassigned.status = 'publishing'
            unassigned.pendingRemote = {
              operationId: 'operation-ui-pending-add',
              operation: 'transfer',
              publish: false,
              baseStatus: 'unassigned',
              targetNodeIds: ['node-bj-01'],
              jobs: [{ id: 'job-ui-pending-add', nodeId: 'node-bj-01' }],
            }
          }
          return new Response(JSON.stringify(body), {
            status: response.status,
            headers: { 'content-type': 'application/json; charset=utf-8' },
          })
        }
        if (url.endsWith('/api/v1/admin/operations/operation-ui-pending')) {
          const now = new Date().toISOString()
          return new Response(JSON.stringify({
            operation: {
              id: 'operation-ui-pending',
              site_id: 'site-api',
              kind: 'publish',
              status: 'running',
              base_version: 3,
              candidate_revision_id: null,
              created_by: 'qa',
              created_at: now,
              updated_at: now,
              completed_at: null,
              metadata: {},
            },
            jobs: [{
              id: 'job-ui-pending',
              batch_id: 'operation-ui-pending',
              operation_id: 'operation-ui-pending',
              node_id: 'node-sh-01',
              node_name: 'it-nginx-sh-01',
              action: 'config_apply',
              status: 'running',
              created_at: now,
              expires_at: now,
              claimed_at: now,
              completed_at: null,
              created_by: 'qa',
              result: {},
            }],
          }), {
            status: 200,
            headers: { 'content-type': 'application/json; charset=utf-8' },
          })
        }
        if (url.endsWith('/api/v1/admin/operations/operation-ui-pending-add')) {
          const now = new Date().toISOString()
          return new Response(JSON.stringify({
            operation: {
              id: 'operation-ui-pending-add',
              site_id: 'generic-upstream',
              kind: 'transfer',
              status: 'running',
              base_version: 1,
              candidate_revision_id: null,
              created_by: 'qa',
              created_at: now,
              updated_at: now,
              completed_at: null,
              metadata: {},
            },
            jobs: [{
              id: 'job-ui-pending-add',
              batch_id: 'operation-ui-pending-add',
              operation_id: 'operation-ui-pending-add',
              node_id: 'node-bj-01',
              node_name: 'it-nginx-bj-01',
              action: 'config_apply',
              status: 'running',
              created_at: now,
              expires_at: now,
              claimed_at: now,
              completed_at: null,
              created_by: 'qa',
              result: {},
            }],
          }), {
            status: 200,
            headers: { 'content-type': 'application/json; charset=utf-8' },
          })
        }
        return response
      }
    })()`,
  })
  await command('Emulation.setDeviceMetricsOverride', {
    width: 1440,
    height: 620,
    deviceScaleFactor: 1,
    mobile: false,
  })
  await command('Page.navigate', {
    url: `http://127.0.0.1:${port}/?qa-pending=1#/sites`,
  })
  await wait(700)
  await waitFor(
    `document.querySelectorAll('.site-row').length === 2`,
    'The sites page did not load with the pending-operation fixture',
  )
  await evaluate(`document.querySelectorAll('.site-row')[0]?.click()`)
  await waitFor(
    `document.querySelector('.sites-detail-panel h2')?.textContent.includes('api.int.example.com')`,
    'The pending configuration could not be selected',
  )
  const pendingFixture = await evaluate(`(() => ({
    title: document.querySelector('.sites-detail-panel h2')?.textContent.trim() || '',
  }))()`)
  assert(
    pendingFixture.title.includes('api.int.example.com'),
    `The pending-operation fixture selected the wrong configuration: ${JSON.stringify(pendingFixture)}`,
  )
  const pendingUi = await evaluate(`(() => {
    const buttons = [...document.querySelectorAll('.sites-detail-panel .detail-actions button')]
    const edit = buttons.find((button) => button.textContent.includes('编辑配置'))
    const adjust = buttons.find((button) => button.textContent.includes('调整部署范围'))
    const validate = buttons.find((button) => button.textContent.includes('逐节点校验'))
    const publish = buttons.find((button) => button.textContent.includes('校验并发布'))
    return {
      editDisabled: Boolean(edit?.disabled),
      editTitle: edit?.getAttribute('title') || '',
      adjustDisabled: Boolean(adjust?.disabled),
      validateDisabled: Boolean(validate?.disabled),
      publishDisabled: Boolean(publish?.disabled),
      subtitle: document.querySelector('.sites-detail-panel .detail-head p')?.textContent.trim() || '',
      status: document.querySelector('.sites-detail-panel .status-tag')?.textContent.trim() || '',
      interceptInstalled: window.__qaPendingInterceptInstalled || false,
      interceptedUiStates: window.__qaPendingUiStateCount || 0,
    }
  })()`)
  assert(
    pendingUi.editDisabled,
    `Editing remained enabled while a remote operation was in flight: ${JSON.stringify(pendingUi)}`,
  )
  assert(
    pendingUi.editTitle.includes('操作完成后才能编辑'),
    `The disabled editor does not explain the pending operation: ${JSON.stringify(pendingUi)}`,
  )
  assert(pendingUi.adjustDisabled, 'Deployment adjustment remained enabled during a remote operation')
  assert(pendingUi.validateDisabled, 'Validation remained enabled during a remote operation')
  assert(pendingUi.publishDisabled, 'Publishing remained enabled during a remote operation')

  await evaluate(`document.querySelectorAll('.site-row')[1]?.click()`)
  await waitFor(
    `document.querySelector('.sites-detail-panel h2')?.textContent.includes('订单服务 upstream')`,
    'The pending add fixture could not select its unassigned configuration',
  )
  const pendingAddUi = await evaluate(`(() => {
    const buttons = [...document.querySelectorAll('.sites-detail-panel .detail-actions button')]
    const edit = buttons.find((button) => button.textContent.includes('编辑配置'))
    const adjust = buttons.find((button) => button.textContent.includes('调整部署范围'))
    const validate = buttons.find((button) => button.textContent.includes('逐节点校验'))
    const publish = buttons.find((button) => button.textContent.includes('校验并发布'))
    const remove = buttons.find((button) => button.textContent.includes('删除平台记录'))
    return {
      editDisabled: Boolean(edit?.disabled),
      adjustDisabled: Boolean(adjust?.disabled),
      validateDisabled: Boolean(validate?.disabled),
      publishDisabled: Boolean(publish?.disabled),
      removeDisabled: Boolean(remove?.disabled),
      removeTitle: remove?.getAttribute('title') || '',
    }
  })()`)
  assert(pendingAddUi.editDisabled, 'Editing remained enabled during a pending add operation')
  assert(pendingAddUi.adjustDisabled, 'Deployment adjustment remained enabled during a pending add operation')
  assert(pendingAddUi.validateDisabled, 'Validation remained enabled during a pending add operation')
  assert(pendingAddUi.publishDisabled, 'Publishing remained enabled during a pending add operation')
  assert(
    pendingAddUi.removeDisabled && pendingAddUi.removeTitle.includes('任务'),
    `Platform-record deletion remained available during a pending add: ${JSON.stringify(pendingAddUi)}`,
  )

  console.log('PASS existing-site editor exposes deployment topology as read-only')
  console.log('PASS unified deployment adjustment covers add, migrate, remove, and final-node choice')
  console.log('PASS in-flight configurations disable edit and deployment adjustment')
  console.log('PASS desktop detail remains viewport-sticky and resets its scroll on selection')
  console.log('PASS narrow selection opens a right drawer with an accessible close action')
  console.log('PASS closing and reopening the drawer preserves list position and selection')
  console.log('PASS deleting the selected narrow-screen record closes its detail drawer')
  console.log('PASS deployment eligibility blocks read-only and capability-incompatible nodes')
  console.log('PASS every site mutation action is disabled while that site is busy')
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
  await removeBrowserProfile()
}
