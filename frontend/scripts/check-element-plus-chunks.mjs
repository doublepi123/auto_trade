import { readdir } from 'node:fs/promises'
import path from 'node:path'

const assetsDir = path.resolve('dist/assets')
const entries = await readdir(assetsDir)
const elementChunks = entries.filter((name) => /^el-.*\.js$/.test(name))
const maxElementChunks = 20

if (elementChunks.length > maxElementChunks) {
  throw new Error(`Element Plus chunk count too high: ${elementChunks.length}\n${elementChunks.join('\n')}`)
}

console.log(`Element Plus chunk count OK (${elementChunks.length})`)
