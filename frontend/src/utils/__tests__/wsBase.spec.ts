import { describe, expect, it } from 'vitest'
import { DEFAULT_BACKEND_PORT, backendWsBase, resolveBackendPort } from '../wsBase'

const http = { protocol: 'http:', hostname: 'localhost', host: 'localhost:5173' }
const https = { protocol: 'https:', hostname: 'dcn.example.com', host: 'dcn.example.com' }
const docker = { protocol: 'http:', hostname: '192.168.1.114', host: '192.168.1.114:8080' }

describe('resolveBackendPort', () => {
  it('未设置任何变量时回落到本地 uvicorn 默认端口，而不是 Docker 内部端口', () => {
    // 回归：曾经硬编码回落到 8002（docker compose 的内部端口），本地开发时
    // 没有进程监听，RDP 的 WebSocket 直接连不上，而 SSH 走同源代理照常工作。
    expect(resolveBackendPort({})).toBe('8004')
    expect(resolveBackendPort({})).toBe(DEFAULT_BACKEND_PORT)
    expect(resolveBackendPort({})).not.toBe('8002')
  })

  it('VITE_WS_PORT 优先（docker compose 构建时注入）', () => {
    expect(resolveBackendPort({ VITE_WS_PORT: '8002', VITE_BACKEND_PORT: '9000' })).toBe('8002')
  })

  it('其次 VITE_BACKEND_PORT，与 vite.config.ts 的代理目标同源', () => {
    expect(resolveBackendPort({ VITE_BACKEND_PORT: '9000' })).toBe('9000')
  })

  it('空串视为未设置', () => {
    expect(resolveBackendPort({ VITE_WS_PORT: '', VITE_BACKEND_PORT: '' })).toBe(DEFAULT_BACKEND_PORT)
  })
})

describe('backendWsBase', () => {
  it('开发构建：http 页面用 ws，https 页面用 wss，直连后端端口', () => {
    expect(backendWsBase({}, http)).toBe('ws://localhost:8004')
    expect(backendWsBase({}, https)).toBe('wss://dcn.example.com:8004')
  })

  it('开发构建：端口跟随环境变量', () => {
    expect(backendWsBase({ VITE_BACKEND_PORT: '8004' }, http)).toBe('ws://localhost:8004')
    expect(backendWsBase({ VITE_WS_PORT: '8002' }, http)).toBe('ws://localhost:8002')
  })

  it('生产构建：走同源（nginx /ws 反代透传子协议），不再拼接后端端口', () => {
    // 回归：docker 部署里 backend 只绑 127.0.0.1，浏览器从其他机器/用非回环地址
    // 访问页面时，直连 ws://<LAN IP>:8002 TCP 层不可达 → RDP 一进会话就 disconnect
    // （SSH 走同源代理不受影响）。生产恒由 nginx 提供 /ws 反代，实测 101 握手
    // 会回显 Sec-WebSocket-Protocol: guacamole。
    expect(backendWsBase({ PROD: true }, docker)).toBe('ws://192.168.1.114:8080')
    expect(backendWsBase({ PROD: true }, https)).toBe('wss://dcn.example.com')
  })

  it('生产构建：compose 注入的 VITE_WS_PORT 不再参与解析（默认即直连死路）', () => {
    expect(backendWsBase({ PROD: true, VITE_WS_PORT: '8002' }, docker)).toBe('ws://192.168.1.114:8080')
  })
})
