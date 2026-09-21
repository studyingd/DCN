import path from 'path'
import { defineConfig } from 'vitest/config'

// 独立的 vitest 配置(不合并 vite.config.ts,避免把 dev/build 插件带进测试进程)。
export default defineConfig({
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  test: {
    // sanitize.ts 依赖 DOMPurify,需要 DOM API;jsdom 是 DOMPurify 官方验证过的环境。
    environment: 'jsdom',
    include: ['src/**/__tests__/**/*.spec.ts'],
    // 固定时区,保证时间格式化断言在开发机与 CI 上结果一致(UTC+8)。
    env: {
      TZ: 'Asia/Shanghai',
    },
  },
})
