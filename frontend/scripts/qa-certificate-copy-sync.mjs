import { spawn } from 'node:child_process'
import { access, mkdir, rm } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

process.env.QA_PORT ||= '4193'
process.env.QA_CERTIFICATE_COPY_SYNC = '1'
const port = Number(process.env.QA_PORT)
const debugPort = port + 5000
const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const profileRoot = resolve(projectRoot, 'artifacts', `.chrome-certificate-copy-${Date.now()}`)
await mkdir(profileRoot, { recursive: true })

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
    '--window-size=1440,900',
    `http://127.0.0.1:${port}/#/sites`,
  ],
  { stdio: 'ignore', windowsHide: true },
)

const wait = (milliseconds) => new Promise((resolveWait) => setTimeout(resolveWait, milliseconds))
const pastedConfig = `server {
  listen 443 ssl;
  server_name pasted-copy.int.example.com;
  ssl_certificate /apps/nginx/cert/int.example.com.pem;
  ssl_certificate_key /apps/nginx/cert/int.example.com.key;
  location / {
    proxy_pass http://192.0.2.88:8080;
  }
}`

async function debuggingTarget() {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json`)
      const pages = await response.json()
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

function certificatePreviewStateExpression() {
  return `(() => {
    const preview = document.querySelector('.certificate-path-preview')
    return {
      found: Boolean(preview),
      warning: preview?.classList.contains('warning') || false,
      summary: preview?.querySelector('.certificate-path-summary')?.textContent
        .replace(/\\s+/g, ' ').trim() || '',
      config: document.querySelector('.conf-editor')?.value || '',
    }
  })()`
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
      const selected = document.querySelector('.site-row.selected .site-primary strong')
      const button = [...document.querySelectorAll('.detail-panel button')]
        .find((item) => item.textContent.includes('编辑配置'))
      if (!selected?.textContent.includes('api-copy.int.example.com') || !button) return false
      button.click()
      return true
    })()`),
    'The copied HTTPS fixture was not selected or its editor could not be opened',
  )
  await wait(220)

  assert(
    await evaluate(`(() => {
      const editor = document.querySelector('.conf-editor')
      if (!editor) return false
      editor.value = ${JSON.stringify(pastedConfig)}
      editor.dispatchEvent(new Event('input', { bubbles: true }))
      return true
    })()`),
    'The copied HTTPS Conf could not be pasted into the bound-certificate editor',
  )
  await wait(80)

  const initialState = await evaluate(certificatePreviewStateExpression())
  assert(initialState.found, 'The copied HTTPS editor did not render its certificate preview')
  assert(
    initialState.config.includes('server_name pasted-copy.int.example.com') &&
      initialState.config.includes('/apps/nginx/cert/int.example.com.pem') &&
      initialState.config.includes('/apps/nginx/cert/int.example.com.key'),
    'The pasted Conf does not contain the fixture certificate asset paths',
  )

  assert(
    await evaluate(`(() => {
      const selection = document.querySelector(
        '.site-editor-modal .certificate-field .n-base-selection',
      )
      selection?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      return Boolean(selection)
    })()`),
    'The copied HTTPS certificate selector was not found',
  )
  await wait(100)
  assert(
    await evaluate(`(() => {
      const option = [...document.querySelectorAll('.n-base-select-option')]
        .find((item) => item.textContent.includes('*.int.example.com'))
      option?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      return Boolean(option)
    })()`),
    'The already-bound certificate could not be selected again',
  )
  await wait(120)

  const reselectedState = await evaluate(certificatePreviewStateExpression())
  console.log(`INFO initial=${JSON.stringify(initialState)}`)
  console.log(`INFO reselected=${JSON.stringify(reselectedState)}`)
  assert(
    !reselectedState.warning && reselectedState.summary.includes('已同步'),
    'Re-selecting the same certificate did not restore a synchronized preview',
  )
  assert(
    !initialState.warning && !initialState.summary.includes('不同步'),
    `Pasted HTTPS Conf was falsely marked out of sync before re-selecting the same certificate: ${initialState.summary}`,
  )
  console.log('PASS pasted HTTPS Conf is synchronized before and after re-selecting its certificate')
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
