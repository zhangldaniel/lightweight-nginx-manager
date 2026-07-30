import { copyFile, mkdir, stat } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'

const frontendRoot = resolve(import.meta.dirname, '..')
const source = resolve(frontendRoot, 'dist/index.html')
const target = resolve(frontendRoot, 'release/index.html')

await mkdir(dirname(target), { recursive: true })
await copyFile(source, target)

const size = (await stat(target)).size
console.log(`Packaged Server UI: ${target} (${(size / 1024 / 1024).toFixed(2)} MiB)`)
