import { spawn } from 'node:child_process'
import { access } from 'node:fs/promises'

process.env.QA_PORT ||= '4194'
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
        '--virtual-time-budget=2600',
        '--dump-dom',
        `http://127.0.0.1:${port}/#/lvs`,
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
  const required = [
    'LVS',
    '只读观测',
    'Virtual Services',
    'Backend Pools',
    'Pool Members',
    '192.0.2.110:443',
    '192.0.2.108:53',
    '配置漂移',
    '已停用',
    '健康状态需由外部 Monitor 证明',
  ]
  for (const text of required) {
    if (!visibleHtml.includes(text)) throw new Error(`LVS page is missing: ${text}`)
  }
  const forbiddenButtons = ['修改权重', '删除成员', '新增虚拟服务', '执行 ipvsadm']
  const buttonText = [...visibleHtml.matchAll(/<button\b[^>]*>([\s\S]*?)<\/button>/gi)]
    .map((match) => match[1].replace(/<[^>]+>/g, '').trim())
  for (const text of forbiddenButtons) {
    if (buttonText.some((label) => label.includes(text))) {
      throw new Error(`LVS page exposes a write action: ${text}`)
    }
  }
  console.log('lvs read-only page contract: ok')
} finally {
  await new Promise((resolve) => qaServer.close(resolve))
}
