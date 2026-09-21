import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig(() => {
  // Load .env from the project root (parent of frontend/)
  const envDir = path.resolve(__dirname, '..')

  // 后端端口:当前用 8004(见 README/开发备忘;旧端口上有不重载的旧进程)。
  // 可用 VITE_BACKEND_PORT 覆盖。
  const backendPort = process.env.VITE_BACKEND_PORT || '8004'

  return {
    plugins: [
      vue(),
      Components({ resolvers: [ElementPlusResolver({ importStyle: 'css' })] }),
    ],
    // @novnc/novnc 用了 top-level await(ES2022)。生产构建与开发服务器依赖预打包都要 es2022。
    esbuild: { target: 'es2022' },
    optimizeDeps: { esbuildOptions: { target: 'es2022' } },
    resolve: {
      alias: {
        '@': path.resolve(__dirname, 'src'),
      },
    },
    envDir,
    server: {
      // Frontend development server port.
      // Bind the dual-stack wildcard so IPv4 (http://127.0.0.1:5040) and IPv6
      // (http://[::1]:5040) both work; `localhost` resolves to either one.
      // Set VITE_DEV_HOST='::1' where IPv4 port 5040 is already taken (e.g.
      // Windows CDPSvc), or VITE_DEV_HOST=127.0.0.1 to stay loopback-only.
      host: process.env.VITE_DEV_HOST || '::',
      port: 5040,
      strictPort: true,
      proxy: {
        '/api': {
          // Use IPv4 explicitly on Windows. `localhost` may resolve to ::1
          // while the local API is bound on IPv4, causing proxy connection
          // failures that the login page reports as a generic service error.
          target: `http://127.0.0.1:${backendPort}`,
          changeOrigin: true,
        },
        '/ws': {
          target: `ws://127.0.0.1:${backendPort}`,
          ws: true,
        },
      },
    },
    build: {
      // @novnc/novnc 依赖 top-level await(ES2022)。内部管理工具,提升到 es2022
      // (2021 年后的浏览器)以兼容;不影响现有代码。
      target: 'es2022',
      // Do not ship source maps to production (avoids leaking source).
      sourcemap: false,
      // terminal(359kB)与 echarts(675kB)都是懒加载路由,非首屏阻塞;
      // 阈值提到 700 消除噪音,真正需要警惕的是某个入口 chunk 膨胀。
      chunkSizeWarningLimit: 700,
      rollupOptions: {
        output: {
          manualChunks: {
            terminal: ['xterm', 'xterm-addon-fit', 'xterm-addon-web-links', 'guacamole-common-js'],
            rdp: ['@novnc/novnc'],
            // echarts 单独成 chunk(可视化页才加载);element-plus 已由
            // unplugin-vue-components 按需拆分,再手动合并反而会打回全量。
            echarts: ['echarts', 'vue-echarts'],
          },
        },
      },
    },
  }
})
