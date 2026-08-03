import { copyFile, cp, mkdir, rm, stat } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'

const frontendRoot = resolve(import.meta.dirname, '..')
const source = resolve(frontendRoot, 'dist/index.html')
const target = resolve(frontendRoot, 'release/index.html')
const sourceAssets = resolve(frontendRoot, 'dist/ui-assets')
const targetAssets = resolve(frontendRoot, 'release/ui-assets')

await mkdir(dirname(target), { recursive: true })
await copyFile(source, target)
await rm(targetAssets, { recursive: true, force: true })

let assetsCopied = false
try {
  if ((await stat(sourceAssets)).isDirectory()) {
    await cp(sourceAssets, targetAssets, { recursive: true })
    assetsCopied = true
  }
} catch (error) {
  if (error?.code !== 'ENOENT') throw error
}

const size = (await stat(target)).size
console.log(
  `Packaged Server UI: ${target} (${(size / 1024 / 1024).toFixed(2)} MiB)`
  + (assetsCopied ? ' + ui-assets' : ''),
)
