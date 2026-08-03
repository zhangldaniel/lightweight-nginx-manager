import { createHash } from 'node:crypto'
import { readFile, stat } from 'node:fs/promises'
import { resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const output = resolve(root, 'dist/index.html')
const html = await readFile(output, 'utf8')
const size = (await stat(output)).size
const appSource = await readFile(resolve(root, 'src/App.vue'), 'utf8')
const storeSource = await readFile(resolve(root, 'src/stores/console.ts'), 'utf8')
const certificateSource = await readFile(resolve(root, 'src/views/CertificatesView.vue'), 'utf8')
const harmonyAssets = {
  'HarmonyOS_Sans_SC_Regular.ttf': '297b088424be212207df2ce8b98e335468b782aa6b96832af0b8b773d711e2b1',
  'HarmonyOS_Sans_SC_Medium.ttf': '6ed1553edccddc48eb27ff25d134a4a715cf54211238d4840b3038576cba1944',
  'HarmonyOS_Sans_SC_Bold.ttf': '43a424b85e47fb53a17b3b32026a71801f86f8e022ca6798d186b47d39fa5f01',
  'HarmonyOS-Sans-LICENSE.txt': 'b2ffec0e6269ee41c3b5fc0345ab37600b46d66ebea6c9c58ff37f517bdfa164',
}
const harmonyAssetsUnmodified = (
  await Promise.all(
    Object.entries(harmonyAssets).map(async ([name, expected]) => {
      try {
        const payload = await readFile(resolve(root, 'dist/ui-assets', name))
        return createHash('sha256').update(payload).digest('hex') === expected
      } catch {
        return false
      }
    }),
  )
).every(Boolean)

const checks = [
  ['单文件输出', !/<script[^>]+src=|<link[^>]+rel=["']stylesheet/i.test(html)],
  ['无公网字体或静态资源', !/(?:src|href)=["']https?:\/\//i.test(html)],
  [
    'HarmonyOS Sans SC 原版字体与授权文件已打包',
    html.includes('HarmonyOS Sans SC') && harmonyAssetsUnmodified,
  ],
  ['Vue 应用入口存在', html.includes('id="app"')],
  ['站点管理能力已打包', html.includes('站点与配置')],
  ['证书新增与替换能力已打包', html.includes('添加证书') && html.includes('原路径替换')],
  [
    '节点、日志与监控页面已打包',
    html.includes('节点 Agent') && html.includes('实时日志') && html.includes('宿主机资源'),
  ],
  ['多配置入口语义已保留', html.includes('config_entries')],
  [
    '扫描任务自动轮询并防止重复提交',
    appSource.includes('setInterval(() => void refreshInBackground(), 2500)') &&
      appSource.includes("document.addEventListener('visibilitychange'") &&
      appSource.includes('store.refresh(false, true)') &&
      storeSource.includes('background = false') &&
      certificateSource.includes('activeCertificateScan'),
  ],
  ['构建体积低于 4 MiB', size < 4 * 1024 * 1024],
]

const failed = checks.filter(([, passed]) => !passed)
for (const [label, passed] of checks) {
  console.log(`${passed ? 'PASS' : 'FAIL'}  ${label}`)
}
console.log(`INFO  单文件大小 ${(size / 1024 / 1024).toFixed(2)} MiB`)

if (failed.length) {
  process.exitCode = 1
}
