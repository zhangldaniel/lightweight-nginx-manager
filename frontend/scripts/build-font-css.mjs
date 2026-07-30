import { readdir, readFile, stat, writeFile } from 'node:fs/promises'
import { extname, join, relative, resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const sourceRoot = join(root, 'src')
const packageRoot = join(root, 'node_modules', '@fontsource-variable', 'noto-sans-sc')
const sourceCss = await readFile(join(packageRoot, 'index.css'), 'utf8')
const usedCodePoints = new Set()

async function collect(directory) {
  for (const name of await readdir(directory)) {
    const path = join(directory, name)
    const item = await stat(path)
    if (item.isDirectory()) {
      await collect(path)
      continue
    }
    if (!['.vue', '.ts', '.css'].includes(extname(path)) || name === 'noto-ui.css') continue
    for (const character of await readFile(path, 'utf8')) {
      if (character.codePointAt(0) > 127) usedCodePoints.add(character.codePointAt(0))
    }
  }
}

function inRanges(codePoint, source) {
  return source.split(',').some((raw) => {
    const value = raw.trim().replace(/^U\+/i, '')
    const [start, end = start] = value.split('-')
    return codePoint >= Number.parseInt(start, 16) && codePoint <= Number.parseInt(end, 16)
  })
}

await collect(sourceRoot)

const blocks = Array.from(
  sourceCss.matchAll(/\/\* ([\s\S]*?) \*\/\s*(@font-face \{[\s\S]*?\n\})/g),
)
const selected = []
for (const match of blocks) {
  const block = match[2]
  const ranges = /unicode-range:\s*([^;]+)/.exec(block)?.[1]
  if (!ranges || !Array.from(usedCodePoints).some((codePoint) => inRanges(codePoint, ranges))) continue
  selected.push(
    `/* ${match[1]} */\n${block.replace(
      /url\(\.\/files\//g,
      'url(../../node_modules/@fontsource-variable/noto-sans-sc/files/',
    )}`,
  )
}

const output = join(sourceRoot, 'styles', 'noto-ui.css')
await writeFile(
  output,
  `/* Generated from the public Noto Sans SC package. Do not edit manually. */\n${selected.join('\n\n')}\n`,
  'utf8',
)
console.log(`Generated ${relative(root, output)} with ${selected.length} Unicode subsets.`)
