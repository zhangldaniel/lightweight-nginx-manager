import { spawn } from 'node:child_process'
import { access, mkdir, rm } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

process.env.QA_PORT ||= '4196'
process.env.QA_MONITORING_LVS_PROFILE = '1'
const port = Number(process.env.QA_PORT)
const debugPort = port + 5000
const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const profileRoot = resolve(projectRoot, 'artifacts', `.chrome-monitoring-lvs-${Date.now()}`)
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
    '--window-size=1680,980',
    `http://127.0.0.1:${port}/#/monitoring`,
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
  if (result.exceptionDetails) {
    throw new Error(
      result.exceptionDetails.exception?.description ||
      result.exceptionDetails.text ||
      'Evaluation failed',
    )
  }
  return result.result?.value
}

function assert(value, message) {
  if (!value) throw new Error(message)
}

async function monitoringSnapshot() {
  return evaluate(`(() => {
    const page = document.querySelector('.page-monitoring')
    const text = page?.textContent.replace(/\\s+/g, ' ').trim() || ''
    const falseStubWarnings = [
      'Agent 未配置 Stub Status URL',
      'Stub Status 暂不可用',
      'Stub 未配置',
      'Stub 异常',
    ].filter((token) => text.includes(token))
    return {
      text,
      node: document.querySelector('.monitor-node-copy h2')?.textContent.trim() || '',
      health: document.querySelector('.monitor-signal')?.getAttribute('data-state') || '',
      falseStubWarnings,
      cpuVisible: text.includes('18.5%'),
      memoryVisible: text.includes('32.5%'),
      ipvsEvidence:
        (text.includes('IPVS') || text.includes('LVS')) &&
        ['18/s', '18.0/s', '1 个服务', '暂无', '无可用'].some((token) => text.includes(token)),
      ipvsObservationNote: text.includes('运行表是观测事实，不代表成员健康'),
      ipvsNeutralSummary:
        text.includes('宿主机指标正常；IPVS 运行表已观测') &&
        !text.includes('IPVS 指标处于正常范围'),
      ipvsStatusTone: document.querySelector('.monitor-details > .monitor-section-head .status-tag')?.getAttribute('data-tone') || '',
      ipvsStatusLabel: document.querySelector('.monitor-details > .monitor-section-head .status-tag')?.textContent.trim() || '',
      chartLabels: [...document.querySelectorAll('.monitor-charts .chart-card header span:first-child')]
        .map((item) => item.textContent.trim()),
    }
  })()`)
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
  await wait(900)

  const fixture = await evaluate(`fetch('/api/v1/admin/monitoring/summary')
    .then((response) => response.json())
    .then(({ items }) => items.map((item) => ({
      id: item.node.id,
      capabilities: item.node.capabilities,
      hasNginx: Boolean(item.metrics.nginx),
      hasStub: Boolean(item.metrics.stub_status),
      hasIpvs: item.node.facts?.ipvs?.available === true,
    })))`)
  assert(
    fixture.length === 2 &&
      fixture[0].id === 'node-lvs-standalone' &&
      !fixture[0].hasNginx &&
      !fixture[0].hasStub &&
      fixture[0].hasIpvs &&
      fixture[1].id === 'node-nginx-no-stub' &&
      fixture[1].hasNginx &&
      fixture[1].hasStub,
    `The focused monitoring fixture is invalid: ${JSON.stringify(fixture)}`,
  )

  const lvs = await monitoringSnapshot()

  const opened = await evaluate(`(() => {
    const select = document.querySelector('.monitor-node-select .n-base-selection')
    select?.click()
    return Boolean(select)
  })()`)
  assert(opened, 'The monitoring node selector was not found')
  await wait(100)
  const selected = await evaluate(`(() => {
    const option = [...document.querySelectorAll('.n-base-select-option')]
      .find((item) => item.textContent.includes('it-nginx-no-stub'))
    option?.click()
    return Boolean(option)
  })()`)
  assert(selected, 'The Nginx-without-Stub fixture could not be selected')
  await wait(250)
  const nginx = await monitoringSnapshot()

  console.log(`INFO fixture=${JSON.stringify(fixture)}`)
  console.log(`INFO lvs=${JSON.stringify(lvs)}`)
  console.log(`INFO nginx=${JSON.stringify(nginx)}`)
  assert(lvs.node === 'it-lvs-standalone', `The standalone LVS node was not selected: ${lvs.node}`)
  assert(lvs.health === 'healthy', `The standalone LVS node was promoted to ${lvs.health}`)
  assert(
    lvs.falseStubWarnings.length === 0,
    `Standalone LVS must not require Nginx Stub Status: ${lvs.falseStubWarnings.join(', ')}`,
  )
  assert(lvs.cpuVisible && lvs.memoryVisible, 'Standalone LVS lost its host CPU or memory metrics')
  assert(lvs.ipvsEvidence, 'Standalone LVS must show IPVS/LVS metrics or an explicit neutral no-data state')
  assert(lvs.ipvsObservationNote, 'Standalone LVS must explain that the IPVS runtime table is not member health')
  assert(lvs.ipvsNeutralSummary, 'Standalone LVS must describe IPVS as observed facts, not healthy members')
  assert(
    lvs.ipvsStatusTone === 'neutral' && lvs.ipvsStatusLabel === 'IPVS 已观测',
    `Standalone LVS IPVS state must remain neutral: ${lvs.ipvsStatusLabel}/${lvs.ipvsStatusTone}`,
  )
  assert(
    !lvs.chartLabels.includes('请求速率') && !lvs.chartLabels.includes('活跃连接'),
    `Standalone LVS history must contain host metrics only: ${JSON.stringify(lvs.chartLabels)}`,
  )
  assert(
    lvs.chartLabels.length === 6 &&
      lvs.chartLabels.includes('网络接收') &&
      lvs.chartLabels.includes('网络发送') &&
      lvs.chartLabels.includes('磁盘写入'),
    `Standalone LVS history must keep a complete six-card host grid: ${JSON.stringify(lvs.chartLabels)}`,
  )
  assert(
    nginx.node === 'it-nginx-no-stub' &&
      (nginx.health === 'warning' || /Stub Status 不可用|接口返回 HTTP 503/.test(nginx.text)),
    'An Nginx node with a broken Stub endpoint must still expose its warning',
  )
  assert(
    nginx.chartLabels.includes('请求速率') && nginx.chartLabels.includes('活跃连接'),
    `Nginx history lost its Stub charts: ${JSON.stringify(nginx.chartLabels)}`,
  )
  console.log('PASS standalone LVS monitoring does not require Nginx Stub Status')
  console.log('PASS Nginx monitoring still warns when its configured Stub endpoint is unavailable')
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
