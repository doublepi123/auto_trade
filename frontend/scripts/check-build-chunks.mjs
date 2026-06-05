import { readdir, stat } from 'node:fs/promises'
import path from 'node:path'

const assetsDir = path.resolve('dist/assets')
const maxChunkBytes = 500 * 1024

const entries = await readdir(assetsDir)
const jsFiles = entries.filter((name) => name.endsWith('.js'))

if (jsFiles.length === 0) {
  throw new Error('No built JS chunks found in dist/assets')
}

const oversized = []
for (const file of jsFiles) {
  const filePath = path.join(assetsDir, file)
  const { size } = await stat(filePath)
  if (size > maxChunkBytes) {
    oversized.push({ file, size })
  }
}

if (oversized.length > 0) {
  const details = oversized.map(({ file, size }) => `${file}: ${size} bytes`).join('\n')
  throw new Error(`Chunk budget exceeded:\n${details}`)
}

console.log(`Chunk budget OK (${jsFiles.length} JS chunks, max ${maxChunkBytes} bytes)`)
