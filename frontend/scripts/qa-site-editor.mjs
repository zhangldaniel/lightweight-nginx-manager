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
    }
  })()`)
  assert(initial.modal, 'The unified site editor did not open')
  assert(initial.tabs === 0, 'Legacy mode tabs are still rendered')
  assert(initial.templates.length === 8, 'Expected eight site templates')
  assert(initial.templates.some((item) => item.includes('负载均衡 HTTPS')), 'HTTPS load-balancer template is missing')
  assert(initial.templates.some((item) => item.includes('Nginx Stub Status')), 'Stub Status template is missing')

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
      const template = [...document.querySelectorAll('.template-card')]
        .find((item) => item.textContent.includes('Stream TCP 代理'))
      template?.click()
      return Boolean(template)
    })()`),
    'Stream template was not clickable',
  )
  await wait(80)
  const streamState = await evaluate(`(() => ({
    context: document.querySelector('.editor-context-badge')?.textContent.trim(),
    config: document.querySelector('.conf-editor')?.value || '',
    selected: document.querySelector('.choice-card')?.getAttribute('aria-pressed'),
  }))()`)
  assert(streamState.context === 'STREAM', 'Stream context did not update')
  assert(streamState.config.includes('upstream tcp_backend'), 'Stream template content was not applied')
  assert(streamState.selected === 'true', 'Selected node was lost when switching context')

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

  const screenshot = await command('Page.captureScreenshot', { format: 'png' })
  await writeFile(resolve(artifactRoot, 'vue-site-editor.png'), Buffer.from(screenshot.data, 'base64'))
  console.log('PASS unified site editor interaction')
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
