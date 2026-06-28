/**
 * useTerminal — composable that manages a WebSocket-based terminal session.
 *
 * The WebSocket speaks the JSON protocol defined by the backend:
 *   Client → Server:  {"type":"input","data":"..."}   |   {"type":"resize","cols":N,"rows":N}
 *   Server → Client:  {"type":"output","data":"..."}  |   {"type":"error","data":"..."}  |  {"type":"connected","message":"..."}
 */

import { ref, onUnmounted, type Ref } from 'vue'
import { terminalAPI } from '@/api'

/** Protocol message coming from the server. */
export interface TerminalMessage {
  type: 'output' | 'error' | 'connected'
  data?: string
  message?: string
}

export function useTerminal() {
  const isConnected: Ref<boolean> = ref(false)
  const connectionError: Ref<string | null> = ref(null)
  const connectionInfo: Ref<string | null> = ref(null)

  let ws: WebSocket | null = null
  /** Callbacks set by the consumer (Terminal.vue) after calling connect(). */
  let _onOutput: ((data: string) => void) | null = null
  let _onError: ((data: string) => void) | null = null
  let _onConnected: ((message: string) => void) | null = null
  let _onDisconnected: (() => void) | null = null

  // ----------------------------------------------------------------
  // helpers
  // ----------------------------------------------------------------

  /**
   * Derive the WebSocket base URL from the current page location so it
   * works in both development (via Vite proxy) and production.
   */
  function _wsBaseUrl(): string {
    const loc = window.location
    const protocol = loc.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${protocol}//${loc.host}`
  }

  // ----------------------------------------------------------------
  // public API
  // ----------------------------------------------------------------

  interface ConnectOptions {
    deviceId: number
    connType?: 'ssh' | 'rdp'
    username?: string
    password?: string
    credentialId?: number
    width?: number
    height?: number
    onOutput: (data: string) => void
    onError: (data: string) => void
    onConnected: (message: string) => void
    onDisconnected: () => void
  }

  /**
   * Open a WebSocket terminal session to the given device.
   *
   * Auth is via the httpOnly cookie (sent automatically, same-origin). A short-
   * lived single-use ticket is obtained first so the WS URL carries neither the
   * JWT nor the device password; the backend resolves credentials server-side.
   */
  async function connect(opts: ConnectOptions): Promise<void> {
    // Tear down any existing connection first.
    disconnect()

    const {
      deviceId,
      connType = 'ssh',
      username,
      password,
      credentialId,
      width,
      height,
      onOutput,
      onError,
      onConnected,
      onDisconnected,
    } = opts

    _onOutput = onOutput
    _onError = onError
    _onConnected = onConnected
    _onDisconnected = onDisconnected

    connectionError.value = null
    connectionInfo.value = null

    // Obtain a single-use ticket (cookie-auth). Server resolves credentials.
    let ticket: string
    try {
      const res = await terminalAPI.ticket({
        device_id: deviceId,
        conn_type: connType,
        credential_id: credentialId ?? null,
        username,
        password,
        width,
        height,
      })
      ticket = res.data.ticket
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      connectionError.value = detail || '获取终端票据失败'
      _onError?.(connectionError.value)
      _onDisconnected?.()
      return
    }

    const base = _wsBaseUrl()
    const params = new URLSearchParams({ ticket })
    const url = `${base}/ws/terminal/${deviceId}?${params.toString()}`

    ws = new WebSocket(url)

    ws.onopen = () => {
      isConnected.value = true
    }

    ws.onmessage = (event: MessageEvent) => {
      try {
        const msg: TerminalMessage = JSON.parse(event.data)

        switch (msg.type) {
          case 'output':
            _onOutput?.(msg.data ?? '')
            break
          case 'error':
            connectionError.value = msg.data ?? 'Unknown error'
            _onError?.(msg.data ?? 'Unknown error')
            break
          case 'connected':
            connectionInfo.value = msg.message ?? 'Connected'
            _onConnected?.(msg.message ?? 'Connected')
            break
        }
      } catch {
        // If the server sends raw text, just pass it through.
        _onOutput?.(event.data)
      }
    }

    ws.onclose = () => {
      isConnected.value = false
      _onDisconnected?.()
    }

    ws.onerror = () => {
      connectionError.value = 'WebSocket connection error'
      isConnected.value = false
      _onDisconnected?.()
    }
  }

  /**
   * Send user input (keystrokes) to the SSH session.
   */
  function sendInput(data: string): void {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'input', data }))
    }
  }

  /**
   * Send a terminal resize event to the backend.
   */
  function sendResize(cols: number, rows: number): void {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'resize', cols, rows }))
    }
  }

  /**
   * Close the WebSocket connection.
   */
  function disconnect(): void {
    if (ws) {
      ws.onclose = null
      ws.onerror = null
      ws.onmessage = null
      ws.close()
      ws = null
    }
    isConnected.value = false
  }

  // Clean up on component unmount (if used inside setup()).
  onUnmounted(() => {
    disconnect()
  })

  return {
    isConnected,
    connectionError,
    connectionInfo,
    connect,
    disconnect,
    sendInput,
    sendResize,
  }
}
