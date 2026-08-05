import { defineConfig, loadEnv, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

function manualChunks(id: string): string | undefined {
  if (!id.includes('node_modules')) return undefined

  if (id.includes('node_modules/@element-plus/icons-vue/')) {
    return 'el-icons'
  }

  if (id.includes('node_modules/element-plus/')) {
    // Element Plus internals have circular imports; Rollup must own their chunk boundaries.
    return undefined
  }
  if (id.includes('node_modules/vue-router/')) return 'vue-router'
  if (id.includes('node_modules/vue/') || id.includes('node_modules/@vue/')) return 'vue-core'
  if (id.includes('node_modules/axios/')) return 'network'
  if (id.includes('node_modules/@vueuse/')) return 'vueuse'

  return undefined
}

function createBuildId(): string {
  const timestamp = new Date().toISOString().replace(/[-:.TZ]/g, '')
  const nonce = Math.random().toString(36).slice(2, 10)
  return `${timestamp}-${nonce}`
}

function appVersionManifest(buildId: string): Plugin {
  return {
    name: 'auto-trade-app-version-manifest',
    apply: 'build',
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: 'version.json',
        source: `${JSON.stringify({ build_id: buildId })}\n`,
      })
    },
  }
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const buildId = createBuildId()
  const proxyHeaders = env.AUTO_TRADE_API_KEY
    ? { 'X-API-Key': env.AUTO_TRADE_API_KEY }
    : undefined

  return {
  plugins: [
    vue(),
    appVersionManifest(buildId),
    AutoImport({
      dts: false,
      imports: ['vue', 'vue-router'],
      resolvers: [ElementPlusResolver()],
    }),
    Components({
      dts: false,
      resolvers: [ElementPlusResolver({ importStyle: 'css' })],
    }),
  ],
  define: {
    __AUTO_TRADE_BUILD_ID__: JSON.stringify(buildId),
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        headers: proxyHeaders,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
        headers: proxyHeaders,
      },
    },
  },
  build: {
    outDir: 'dist',
    rollupOptions: {
      output: {
        manualChunks,
      },
    },
  },
  }
})
