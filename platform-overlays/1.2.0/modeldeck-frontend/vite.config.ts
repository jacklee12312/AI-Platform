import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

const uiBuild = new Date().toISOString()
// https://vite.dev/config/
export default defineConfig({
  define: { 'import.meta.env.VITE_UI_BUILD': JSON.stringify(uiBuild) },
  plugins: [vue(), {
    name: 'modeldeck-ui-build',
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'build.json', source: JSON.stringify({ build: uiBuild }) })
    },
  }],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:17780',
      '/health': 'http://127.0.0.1:17780',
    },
  },
})
