import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

export default defineConfig(({ mode }) => {
  // Load .env from the project root (parent of frontend/)
  const envDir = path.resolve(__dirname, '..')
  const env = loadEnv(mode, envDir, ['VITE_'])

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, 'src'),
      },
    },
    envDir,
    server: {
      proxy: {
        '/api': {
          target: 'http://localhost:8002',
          changeOrigin: true,
        },
        '/ws': {
          target: 'ws://localhost:8002',
          ws: true,
        },
      },
    },
    build: {
      // Do not ship source maps to production (avoids leaking source).
      sourcemap: false,
    },
  }
})
