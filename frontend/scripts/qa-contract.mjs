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
const fontAssets = {
  'SmileySans-Oblique.woff2': '4895e7a5b72753b7d4bf090581fbc4375e0ec53484944f369a584588f1eeaf08',
  'SarasaUiSC-Regular.ttf': '9f6c9e41b3d9cc982c7d57653f956aa23c896f6833d58862447a9a8c584a36dd',
  'SarasaUiSC-SemiBold.ttf': '8e44ca2705bffe1ab341e413b381a1d065a9825a162eb7397010fed59a93c9dd',
  'SmileySans-LICENSE.txt': '9401f4050f1b66c26b6ccdc8b0e14a3c1cc37aac122eda84386f25854a9bec72',
  'SarasaGothic-LICENSE.txt': '32c932e0dbae4f6e6386964bbc2d04178707665a05ca65cf636241af13d50a53',
}
const fontAssetsUnmodified = (
  await Promise.all(
    Object.entries(fontAssets).map(async ([name, expected]) => {
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
    'Smiley Sans 与 Sarasa Gothic 原版字体及许可证已打包',
    html.includes('Smiley Sans') && html.includes('Sarasa UI SC') && fontAssetsUnmodified,
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
