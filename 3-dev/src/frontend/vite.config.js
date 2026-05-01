import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { readFileSync } from 'fs'

const pkg = JSON.parse(readFileSync('./package.json', 'utf-8'))

export default defineConfig({
  plugins: [vue()],
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  server: {
    port: 15798,
    proxy: {
      '/api': {
        target: 'http://localhost:15797',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://localhost:15797',
        changeOrigin: true,
      },
      '/metrics': {
        target: 'http://localhost:15797',
        changeOrigin: true,
      },
    },
  },
})
