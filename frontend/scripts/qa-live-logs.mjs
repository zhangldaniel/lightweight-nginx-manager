import { spawn } from 'node:child_process'
import { access, mkdir, rm } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

process.env.QA_PORT ||= '4182'
const port = Number(process.env.QA_PORT)
const debugPort = port + 5000
const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const artifactRoot = resolve(projectRoot, 'artifacts')
const profileRoot = resolve(artifactRoot, `.chrome-live-logs-${Date.now()}`)
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
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${profileRoot}`,
    '--window-size=1680,980',
    'about:blank',
  ],
  { stdio: 'ignore', windowsHide: true },
)

const wait = (milliseconds) => new Promise((resolveWait) => setTimeout(resolveWait, milliseconds))

async function debuggingTarget() {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const targets = await fetch(`http://127.0.0.1:${debugPort}/json`)
      const pages = await targets.json()
      const target = pages.find((item) => item.type === 'page')
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

const fixtureLines = [
  'ordinary request completed',
  '[error] upstream prematurely closed connection',
  '[warn] upstream response is buffered',
  '{"status":"404","request":"GET /missing","marker":"json-404"}',
  '{"status":"502","request":"GET /api","marker":"json-502"}',
  '192.0.2.1 - - "GET /legacy HTTP/1.1" 500 146 marker-access-500',
  'healthcheck ok marker-health',
]

const injectedSource = `(() => {
  const originalFetch = window.fetch.bind(window)
  window.__qaLogSessionBodies = []
  window.fetch = async (input, init = {}) => {
    const url = typeof input === 'string' ? input : input.url
    const method = String(init.method || (typeof input === 'string' ? 'GET' : input.method) || 'GET').toUpperCase()
    if (url.endsWith('/api/v1/admin/log-sessions') && method === 'POST') {
      const body = JSON.parse(String(init.body || '{}'))
      window.__qaLogSessionBodies.push(body)
      return new Response(JSON.stringify({ id: 'qa-log-session' }), {
        status: 201,
        headers: { 'content-type': 'application/json' },
      })
    }
    if (url.includes('/api/v1/admin/log-sessions/') && method === 'DELETE') {
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    }
    return originalFetch(input, init)
  }

  class QaEventSource extends EventTarget {
    constructor(url) {
      super()
      this.url = url
      this.readyState = 0
      window.setTimeout(() => {
        if (this.readyState === 2) return
        this.readyState = 1
        this.onopen?.(new Event('open'))
        const payload = {
          content: ${JSON.stringify(fixtureLines.join('\n') + '\n')},
          read_lines: ${fixtureLines.length},
          sent_lines: ${fixtureLines.length},
          dropped_lines: 0,
        }
        this.dispatchEvent(new MessageEvent('log', { data: JSON.stringify(payload) }))
      }, 30)
    }
    close() { this.readyState = 2 }
  }
  window.EventSource = QaEventSource
})()`

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
  await command('Page.enable')
  await command('Page.addScriptToEvaluateOnNewDocument', { source: injectedSource })
  await command('Page.navigate', { url: `http://127.0.0.1:${port}/#/logs` })
  await wait(800)

  assert(
    await evaluate(`(() => {
      const button = document.querySelector('[data-preset="http4xx"]')
      button?.click()
      return Boolean(button)
    })()`),
    'HTTP 4xx preset was not found',
  )
  assert(
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('button')]
        .find((item) => item.textContent.includes('开始查看'))
      button?.click()
      return Boolean(button)
    })()`),
    'Start live-log button was not found',
  )
  await wait(200)

  const initial = await evaluate(`(() => ({
    visible: document.querySelector('.log-summary-cell:nth-child(3) strong')?.textContent.trim(),
    text: document.querySelector('.log-output')?.textContent || '',
    body: window.__qaLogSessionBodies[0],
  }))()`)
  assert(initial.visible === '1', `HTTP 4xx should show one line, got ${initial.visible}`)
  assert(initial.text.includes('json-404'), 'HTTP 4xx did not show the JSON access-log 404')
  assert(!initial.text.includes('json-502'), 'HTTP 4xx leaked a JSON access-log 502')
  assert(
    initial.body?.preset === 'all' && !initial.body?.include && !initial.body?.exclude,
    `Agent collection was incorrectly narrowed by a window filter: ${JSON.stringify(initial.body)}`,
  )

  await evaluate(`(() => {
    const button = document.querySelector('[data-preset="http5xx"]')
    button?.click()
  })()`)
  await wait(50)
  const switched = await evaluate(`(() => ({
    visible: document.querySelector('.log-summary-cell:nth-child(3) strong')?.textContent.trim(),
    text: document.querySelector('.log-output')?.textContent || '',
    requests: window.__qaLogSessionBodies.length,
  }))()`)
  assert(switched.visible === '2', `HTTP 5xx should show two lines, got ${switched.visible}`)
  assert(switched.text.includes('json-502') && switched.text.includes('marker-access-500'), 'HTTP 5xx missed JSON or common access logs')
  assert(switched.requests === 1, 'Switching window filters restarted or replaced the live connection')

  await evaluate(`(() => {
    const button = document.querySelector('[data-preset="error"]')
    button?.click()
  })()`)
  await wait(30)
  const errorPreset = await evaluate(`(() => ({
    visible: document.querySelector('.log-summary-cell:nth-child(3) strong')?.textContent.trim(),
    text: document.querySelector('.log-output')?.textContent || '',
  }))()`)
  assert(errorPreset.visible === '1', `Error should show one line, got ${errorPreset.visible}`)
  assert(errorPreset.text.includes('prematurely closed'), 'Error preset missed the Nginx error line')

  await evaluate(`(() => {
    const button = document.querySelector('[data-preset="warn"]')
    button?.click()
  })()`)
  await wait(30)
  const warnPreset = await evaluate(`(() => ({
    visible: document.querySelector('.log-summary-cell:nth-child(3) strong')?.textContent.trim(),
    text: document.querySelector('.log-output')?.textContent || '',
  }))()`)
  assert(warnPreset.visible === '1', `Warn should show one line, got ${warnPreset.visible}`)
  assert(warnPreset.text.includes('buffered'), 'Warn preset missed the Nginx warning line')

  await evaluate(`(() => {
    const all = document.querySelector('[data-preset="all"]')
    all?.click()
    const inputs = [...document.querySelectorAll('.log-filters input')]
    const include = inputs.find((item) => item.placeholder.includes('upstream timed out'))
    const exclude = inputs.find((item) => item.placeholder.includes('healthcheck'))
    const set = (element, value) => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
      setter.call(element, value)
      element.dispatchEvent(new Event('input', { bubbles: true }))
    }
    set(include, 'UPSTREAM')
    set(exclude, 'buffered')
  })()`)
  await wait(50)
  const textFilter = await evaluate(`(() => ({
    visible: document.querySelector('.log-summary-cell:nth-child(3) strong')?.textContent.trim(),
    hidden: document.querySelector('.log-summary-cell:nth-child(4) strong')?.textContent.trim(),
    text: document.querySelector('.log-output')?.textContent || '',
  }))()`)
  assert(textFilter.visible === '1', `Include/exclude should show one line, got ${textFilter.visible}`)
  assert(textFilter.hidden === '6', `Hidden count should be six, got ${textFilter.hidden}`)
  assert(textFilter.text.includes('prematurely closed'), 'Case-insensitive include missed the error line')
  assert(!textFilter.text.includes('buffered'), 'Exclude did not remove the matching warn line')

  await evaluate(`(() => {
    const checkbox = [...document.querySelectorAll('.n-checkbox')]
      .find((item) => item.textContent.includes('区分大小写'))
    checkbox?.click()
  })()`)
  await wait(30)
  const sensitive = await evaluate(`(() => ({
    visible: document.querySelector('.log-summary-cell:nth-child(3) strong')?.textContent.trim(),
    hidden: document.querySelector('.log-summary-cell:nth-child(4) strong')?.textContent.trim(),
  }))()`)
  assert(sensitive.visible === '0', `Case-sensitive include should show no lines, got ${sensitive.visible}`)
  assert(sensitive.hidden === '7', `Case-sensitive hidden count should be seven, got ${sensitive.hidden}`)

  console.log('PASS live-log presets filter JSON and common access logs immediately')
  console.log('PASS include/exclude counts are trustworthy and case-insensitive')
  console.log('PASS window filters preserve the existing Agent connection and full browser window')
} finally {
  try { socket?.close() } catch {}
  browser.kill()
  await Promise.race([
    new Promise((resolveExit) => browser.once('exit', resolveExit)),
    wait(1000),
  ])
  await new Promise((resolveClose) => qaServer.close(resolveClose))
  await rm(profileRoot, { recursive: true, force: true }).catch(() => undefined)
}
