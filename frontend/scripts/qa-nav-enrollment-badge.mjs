import { spawn } from 'node:child_process'
import { access, mkdir, rm } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

process.env.QA_PORT ||= '4195'
process.env.QA_NAV_ENROLLMENTS = '1'
const port = Number(process.env.QA_PORT)
const debugPort = port + 5000
const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const profileRoot = resolve(projectRoot, 'artifacts', `.chrome-nav-enrollment-${Date.now()}`)
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

  const fixture = await evaluate(`Promise.all([
    fetch('/api/v1/admin/nodes').then((response) => response.json()),
    fetch('/api/v1/admin/enrollments').then((response) => response.json()),
  ]).then(([nodes, enrollments]) => ({
    managed: nodes.items.length,
    pending: enrollments.items.length,
  }))`)
  assert(
    fixture.managed === 3 && fixture.pending === 2,
    `The focused fixture is invalid: ${JSON.stringify(fixture)}`,
  )

  const navigation = await evaluate(`(() => {
    const link = [...document.querySelectorAll('.nav-item')]
      .find((item) => item.textContent.includes('节点 Agent'))
    if (!link) return { found: false }
    const semanticParts = [
      link.textContent,
      link.getAttribute('aria-label'),
      link.getAttribute('title'),
      ...[...link.querySelectorAll('*')].flatMap((item) => [
        item.getAttribute('aria-label'),
        item.getAttribute('title'),
      ]),
    ].filter(Boolean)
    return {
      found: true,
      managedCount: link.querySelector('.nav-count')?.textContent.trim() || '',
      visibleText: link.textContent.replace(/\\s+/g, ' ').trim(),
      perceivableText: semanticParts.join(' ').replace(/\\s+/g, ' ').trim(),
      horizontallyOverflowing: link.scrollWidth > link.clientWidth + 1,
      height: link.getBoundingClientRect().height,
      normalHeight: [...document.querySelectorAll('.nav-item')]
        .find((item) => item !== link)?.getBoundingClientRect().height || 0,
      labelHeight: link.querySelector('.nav-label')?.getBoundingClientRect().height || 0,
      labelLineHeight: Number.parseFloat(
        getComputedStyle(link.querySelector('.nav-label')).lineHeight,
      ) || 0,
    }
  })()`)

  console.log(`INFO fixture=${JSON.stringify(fixture)}`)
  console.log(`INFO navigation=${JSON.stringify(navigation)}`)
  assert(navigation.found, 'The 节点 Agent navigation item was not rendered')
  assert(
    navigation.managedCount === '3',
    `The navigation lost its managed-node count semantics: ${navigation.managedCount}`,
  )
  assert(!navigation.horizontallyOverflowing, 'The managed and pending badges overflow the navigation item')
  assert(
    navigation.height <= navigation.normalHeight + 1,
    `The pending badges make the node navigation item taller than normal items: ${JSON.stringify(navigation)}`,
  )
  assert(
    navigation.labelHeight <= navigation.labelLineHeight + 1,
    `The 节点 Agent label wraps onto multiple lines: ${JSON.stringify(navigation)}`,
  )
  assert(
    /(?:2\s*(?:个)?\s*待审批|待审批\s*(?:[:：]?\s*)2)/.test(navigation.perceivableText),
    `The navigation does not expose the 2 pending approvals through text, aria-label, or title: ${navigation.perceivableText}`,
  )
  assert(
    await evaluate(`(() => {
      const link = [...document.querySelectorAll('.nav-item')]
        .find((item) => item.textContent.includes('节点 Agent'))
      link?.click()
      return Boolean(link)
    })()`),
    'The 节点 Agent navigation item was not clickable',
  )
  await wait(300)
  const enrollmentSection = await evaluate(`(() => ({
    cards: document.querySelectorAll('.enrollment-card').length,
    text: document.querySelector('.enrollment-section')?.textContent
      .replace(/\\s+/g, ' ').trim() || '',
  }))()`)
  assert(
    enrollmentSection.cards === 2 && enrollmentSection.text.includes('2 个待审批'),
    `The node page does not describe its two pending approvals consistently: ${JSON.stringify(enrollmentSection)}`,
  )
  process.env.QA_NAV_ENROLLMENTS = '0'
  await command('Page.reload', { ignoreCache: true })
  await wait(700)
  const emptyPendingState = await evaluate(`(() => {
    const link = [...document.querySelectorAll('.nav-item')]
      .find((item) => item.textContent.includes('节点 Agent'))
    return {
      managedCount: link?.querySelector('.nav-count')?.textContent.trim() || '',
      pendingBadge: Boolean(link?.querySelector('.nav-pending')),
      text: link?.textContent.replace(/\\s+/g, ' ').trim() || '',
    }
  })()`)
  assert(
    emptyPendingState.managedCount === '3' && !emptyPendingState.pendingBadge,
    `The pending badge did not disappear when no approvals remained: ${JSON.stringify(emptyPendingState)}`,
  )
  console.log('PASS 节点 Agent navigation preserves 3 managed nodes and exposes 2 pending approvals')
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
