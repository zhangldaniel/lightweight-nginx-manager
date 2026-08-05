import { spawn } from 'node:child_process'
import { access } from 'node:fs/promises'

process.env.QA_PORT ||= '4192'
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
  const required = [
    '高可用',
    '10.165.0.110',
    '10.165.0.108',
    '10.165.0.111',
    'MASTER',
    'BACKUP',
    'FAULT',
    'UNKNOWN',
    '刷新状态',
    '校验配置',
    '当前为部分数据',
    '配置一致性',
    '观测边界',
  ]
  for (const text of required) {
    if (!html.includes(text)) throw new Error(`HA page is missing: ${text}`)
  }
  if (html.includes('强制切换')) throw new Error('HA page must not expose forced failover')
  console.log('high-availability page contract: ok')
} finally {
  await new Promise((resolve) => qaServer.close(resolve))
}
