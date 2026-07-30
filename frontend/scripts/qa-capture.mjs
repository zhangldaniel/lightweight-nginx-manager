import { spawn } from 'node:child_process'
import { access, mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { qaServer } from './qa-server.mjs'

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const outputRoot = resolve(projectRoot, 'artifacts')
await mkdir(outputRoot, { recursive: true })

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

async function capture(route, filename) {
  const target = resolve(outputRoot, filename)
  const url = route.startsWith('/')
    ? `http://127.0.0.1:4179${route}`
    : `http://127.0.0.1:4179/#/${route}`
  await new Promise((resolveCapture, rejectCapture) => {
    const child = spawn(
      browser,
      [
        '--headless=new',
        '--disable-gpu',
        '--hide-scrollbars',
        '--force-device-scale-factor=1',
        '--window-size=1680,980',
        '--virtual-time-budget=1800',
        `--screenshot=${target}`,
        url,
      ],
      { stdio: 'ignore', windowsHide: true },
    )
    child.once('error', rejectCapture)
    child.once('exit', (code) => {
      if (code === 0) resolveCapture()
      else rejectCapture(new Error(`headless browser exited with ${code}`))
    })
  })
}

try {
  await capture('/logout-preview#/login', 'vue-login.png')
  await capture('sites', 'vue-sites.png')
  await capture('certificates', 'vue-certificates.png')
  await capture('monitoring', 'vue-monitoring.png')
} finally {
  await new Promise((resolveClose) => qaServer.close(resolveClose))
}
