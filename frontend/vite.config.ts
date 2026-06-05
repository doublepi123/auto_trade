import { defineConfig } from 'vite'
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
    if (id.includes('/date-picker') || id.includes('/time-picker') || id.includes('/time-select') || id.includes('/calendar')) {
      return 'el-date'
    }
    if (id.includes('/table') || id.includes('/table-v2')) {
      return 'el-table'
    }
    if (id.includes('/select') || id.includes('/select-v2') || id.includes('/tree-select') || id.includes('/cascader') || id.includes('/autocomplete')) {
      return 'el-select'
    }
    if (id.includes('/form') || id.includes('/input') || id.includes('/checkbox') || id.includes('/radio') || id.includes('/switch')) {
      return 'el-form'
    }
    if (id.includes('/dialog') || id.includes('/drawer') || id.includes('/popper') || id.includes('/tooltip') || id.includes('/popover') || id.includes('/message') || id.includes('/notification')) {
      return 'el-overlay'
    }
    return 'el-core'
  }
  if (id.includes('node_modules/vue-router/')) return 'vue-router'
  if (id.includes('node_modules/vue/') || id.includes('node_modules/@vue/')) return 'vue-core'
  if (id.includes('node_modules/axios/')) return 'network'
  if (id.includes('node_modules/@vueuse/')) return 'vueuse'

  return undefined
}

export default defineConfig({
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
      '/api': 'http://localhost:8000',
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
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
})