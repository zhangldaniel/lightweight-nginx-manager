import { spawn } from 'node:child_process'
import { access, mkdir, rm } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

process.env.QA_PORT ||= '4184'
const port = Number(process.env.QA_PORT)
const debugPort = port + 5000
const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const artifactRoot = resolve(projectRoot, 'artifacts')
const profileRoot = resolve(artifactRoot, `.chrome-template-dirty-${Date.now()}`)
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
      const button = [...document.querySelectorAll('.detail-panel button')]
        .find((item) => item.textContent.includes('编辑配置'))
      button?.click()
      return Boolean(button)
    })()`),
    'The existing-site edit button was not found',
  )
  await wait(250)

  const initialState = await evaluate(`(() => ({
    editor: Boolean(document.querySelector('.site-editor-modal')),
    config: document.querySelector('.conf-editor')?.value || '',
    templates: [...document.querySelectorAll('.template-card')]
      .map((item) => item.textContent.trim()),
  }))()`)
  assert(initialState.editor, 'The existing-site editor did not open')
  assert(initialState.config.length > 0, 'The existing Conf was not loaded')

  assert(
    await evaluate(`(() => {
      const template = [...document.querySelectorAll('.template-card')]
        .find((item) => item.textContent.includes('静态站点'))
      template?.click()
      return Boolean(template)
    })()`),
    `The static-site template was not found: ${JSON.stringify(initialState.templates)}`,
  )
  await wait(120)

  const replacementState = await evaluate(`(() => ({
    dialog: [...document.querySelectorAll('.n-dialog')]
      .some((item) => item.getClientRects().length && item.textContent.includes('替换当前 Conf')),
    config: document.querySelector('.conf-editor')?.value || '',
  }))()`)
  assert(
    !replacementState.dialog,
    'Untouched existing Conf was incorrectly treated as manually edited when selecting a template',
  )
  assert(
    replacementState.config !== initialState.config,
    'The selected template was not applied to the untouched existing Conf',
  )

  assert(
    await evaluate(`(() => {
      const editor = document.querySelector('.conf-editor')
      if (!editor) return false
      editor.value += '\\n# manual change'
      editor.dispatchEvent(new Event('input', { bubbles: true }))
      const template = [...document.querySelectorAll('.template-card')]
        .find((item) => item.textContent.includes('HTTP 反向代理'))
      template?.click()
      return Boolean(template)
    })()`),
    'The Conf editor or HTTP template was not found for the manual-edit guard check',
  )
  await wait(120)
  assert(
    await evaluate(`[...document.querySelectorAll('.n-dialog')]
      .some((item) => item.getClientRects().length && item.textContent.includes('替换当前 Conf'))`),
    'A real manual Conf edit was not protected by template replacement confirmation',
  )

  console.log('PASS untouched existing Conf accepts an explicitly selected template without a false dirty warning')
  console.log('PASS real manual Conf edits still require template replacement confirmation')
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
