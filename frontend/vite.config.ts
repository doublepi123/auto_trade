import { defineConfig, loadEnv } from 'vite'
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

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const proxyHeaders = env.AUTO_TRADE_API_KEY
    ? { 'X-API-Key': env.AUTO_TRADE_API_KEY }
    : undefined

  return {
  plugins: [
    vue(),
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
