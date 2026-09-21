/**
 * 后端 WebSocket 直连地址。
 *
 * 开发环境（Vite dev）为什么不能走同源 + 代理：RDP 用 guacamole-common-js，
 * 它依赖 `Sec-WebSocket-Protocol` 做子协议协商，而 Vite 的 dev 代理会把这个头吃掉，
 * 协商直接失败。SSH 终端（xterm）不需要子协议，所以它走同源代理没问题——
 * 这也是为什么曾经出现「SSH 正常、RDP 异常」的割裂现象。
 * 所以 DEV 构建必须直连后端端口，解析顺序与 `vite.config.ts` 的代理目标一致：
 *   VITE_WS_PORT（docker compose 构建时显式注入）
 *   → VITE_BACKEND_PORT（本地开发覆盖用）
 *   → 8004（本地 uvicorn 默认端口，见 README 开发章节）
 *
 * 生产构建（Docker/nginx）则必须走**同源**：nginx 的 /ws 反代会完整透传
 * `Sec-WebSocket-Protocol`（实测 101 握手回显 guacamole 子协议，guacd 指令
 * 正常下发）。而直连后端端口依赖宿主发布端口——compose 默认只把 backend 绑在
 * 127.0.0.1，浏览器从别的机器（或本机用非回环地址）访问时 RDP 的 WebSocket
 * TCP 层就连不上：一进会话就 disconnect，SSH 却正常（同源代理不受影响）。
 * 注意 compose 构建注入的 VITE_WS_PORT 在 PROD 下**不再参与解析**——它默认
 * 跟随 BACKEND_PORT（8002），若 PROD 仍按它拼 URL，就回到上面的直连死路。
 */

export const DEFAULT_BACKEND_PORT = '8004'

export interface WsPortEnv {
  VITE_WS_PORT?: string
  VITE_BACKEND_PORT?: string
  /** Vite 构建期注入的 import.meta.env.PROD。生产构建恒为 true。 */
  PROD?: boolean
}

export function resolveBackendPort(env: WsPortEnv): string {
  return env.VITE_WS_PORT || env.VITE_BACKEND_PORT || DEFAULT_BACKEND_PORT
}

export function backendWsBase(
  env: WsPortEnv = (import.meta as any).env ?? {},
  loc: Pick<Location, 'protocol' | 'hostname' | 'host'> = window.location,
): string {
  const protocol = loc.protocol === 'https:' ? 'wss:' : 'ws:'
  if (env.PROD) {
    // 生产构建：页面恒由 nginx 提供，/ws 反代透传子协议握手 → 同源即可。
    // loc.host 已含端口（http://x:8080 → "x:8080"；80/443 默认端口时只有主机名）。
    return `${protocol}//${loc.host}`
  }
  // 开发构建：Vite dev 代理会吃掉 Sec-WebSocket-Protocol → 必须直连后端端口。
  return `${protocol}//${loc.hostname}:${resolveBackendPort(env)}`
}
