import { spawn } from 'node:child_process'
import { access } from 'node:fs/promises'

process.env.QA_PORT ||= '4192'
process.env.QA_HA_FAILURE = '1'
const port = Number(process.env.QA_PORT)
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error(`Invalid QA_PORT: ${process.env.QA_PORT}`)
}

const candidates = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
]
let browser = ''
for (const candidate of candidates) {
  try {
    await access(candidate)
    browser = candidate
    break
  } catch {
    // Try the next installed browser.
  }
}
if (!browser) throw new Error('No supported headless browser was found')

function renderedHtml() {
  return new Promise((resolve, reject) => {
    const child = spawn(
      browser,
      [
        '--headless=new',
        '--disable-gpu',
        '--virtual-time-budget=2200',
        '--dump-dom',
        `http://127.0.0.1:${port}/#/high-availability`,
      ],
      { windowsHide: true },
    )
    let output = ''
    let error = ''
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => { output += chunk })
    child.stderr.on('data', (chunk) => { error += chunk })
    child.once('error', reject)
    child.once('exit', (code) => {
      if (code === 0) resolve(output)
      else reject(new Error(error || `headless browser exited with ${code}`))
    })
  })
}

const { qaServer } = await import('./qa-server.mjs')
try {
  const html = await renderedHtml()
  const visibleHtml = html
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, '')
  if (visibleHtml.includes('it-nginx-sh-01 的配置校验未完成')) {
    throw new Error('HA page must clear an older failure after a newer successful validation')
  }
  if (!visibleHtml.includes('it-nginx-bj-01 的配置校验未完成')) {
    throw new Error('HA page must preserve a terminal historical job over a stale queued snapshot')
  }
  const required = [
    '高可用',
    '192.0.2.110',
    '192.0.2.108',
    '192.0.2.111',
    'MASTER',
    'BACKUP',
    'FAULT',
    'UNKNOWN',
    '刷新状态',
    '校验配置',
    '配置一致性',
    '观测边界',
    '已配置检查脚本，但未启用 Keepalived 脚本安全策略',
  ]
  for (const text of required) {
    if (!visibleHtml.includes(text)) throw new Error(`HA page is missing: ${text}`)
  }
  if (visibleHtml.includes('尚未匹配')) throw new Error('HA page must bind nodes from Agent facts')
  if (visibleHtml.includes('10.165.0.110')) throw new Error('HA page must not use a hard-coded VIP')
  if (visibleHtml.includes('强制切换')) throw new Error('HA page must not expose forced failover')
  console.log('high-availability page contract: ok')
} finally {
  await new Promise((resolve) => qaServer.close(resolve))
}
