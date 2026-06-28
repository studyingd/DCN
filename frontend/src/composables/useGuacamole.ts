/**
 * useGuacamole — composable that manages a Guacamole RDP session.
 *
 * Uses guacamole-common-js to connect to the backend WebSocket tunnel,
 * which proxies the Guacamole protocol to guacd for RDP.
 */

import { ref, onUnmounted, type Ref } from 'vue'

// guacamole-common-js
import Guacamole from 'guacamole-common-js'
import { terminalAPI } from '@/api'

export function useGuacamole() {
  const isConnected: Ref<boolean> = ref(false)
  const connectionError: Ref<string | null> = ref(null)

  let tunnel: InstanceType<typeof Guacamole.WebSocketTunnel> | null = null
  let client: InstanceType<typeof Guacamole.Client> | null = null
  let keyboard: InstanceType<typeof Guacamole.Keyboard> | null = null
  let mouse: InstanceType<typeof Guacamole.Mouse> | null = null
  let _displayElement: HTMLElement | null = null

  function _wsBaseUrl(): string {
    // Connect directly to the backend, not through Vite proxy.
    // The Vite dev proxy strips Sec-WebSocket-Protocol headers,
    // which breaks guacamole-common-js's subprotocol negotiation.
    const backendPort = import.meta.env.VITE_WS_PORT || '8000'
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${protocol}//${window.location.hostname}:${backendPort}`
  }

  interface ConnectOptions {
    deviceId: number
    username?: string
    password?: string
    credentialId?: number | null
    container: HTMLElement
    onConnected?: () => void
    onError?: (message: string) => void
    onDisconnected?: () => void
  }

  /**
   * Connect an RDP session. A single-use ticket is obtained first (cookie-auth
   * HTTP), so the WebSocket URL carries only the opaque ticket — never the JWT,
   * username, or password. The WS may connect directly to the backend port
   * (preserving the guacamole subprotocol); the ticket alone authenticates it.
   */
  async function connect(opts: ConnectOptions): Promise<void> {
    disconnect()

    const {
      deviceId,
      username = '',
      password = '',
      credentialId,
      container,
      onConnected,
      onError,
      onDisconnected,
    } = opts

    const rect = container.getBoundingClientRect()
    const rdpWidth = Math.round(rect.width) || 1024
    const rdpHeight = Math.round(rect.height) || 768

    let ticket: string
    try {
      const res = await terminalAPI.ticket({
        device_id: deviceId,
        conn_type: 'rdp',
        credential_id: credentialId ?? null,
        username,
        password,
        width: rdpWidth,
        height: rdpHeight,
      })
      ticket = res.data.ticket
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      const msg = detail || '获取终端票据失败'
      connectionError.value = msg
      onError?.(msg)
      onDisconnected?.()
      return
    }

    const base = _wsBaseUrl()
    const url = `${base}/ws/terminal/${deviceId}`

    tunnel = new Guacamole.WebSocketTunnel(url)
    client = new Guacamole.Client(tunnel)

    // Attach display to container
    const display = client.getDisplay()
    _displayElement = display.getElement()
    container.appendChild(_displayElement)

    let _hadError = false

    // Event handlers
    // States: IDLE=0, CONNECTING=1, WAITING=2, CONNECTED=3, DISCONNECTING=4, DISCONNECTED=5
    client.onstatechange = (state: number) => {
      console.log('[RDP] State changed to', state)

      if (state === 2) {
        // WAITING — data is flowing, connection is active
        isConnected.value = true
      } else if (state === 3) {
        // CONNECTED — sync received, fully connected
        isConnected.value = true
        connectionError.value = null
        onConnected?.()
      } else if (state === 5) {
        // DISCONNECTED
        isConnected.value = false
        // Don't call onDisconnected if we already reported an error —
        // the error callback already set the status text.
        if (!_hadError) {
          onDisconnected?.()
        }
      }
    }

    client.onerror = (status: any) => {
      const msg = status?.message || 'RDP 连接失败'
      console.error('[RDP] Error:', msg, '(code:', status?.code, ')')
      _hadError = true
      connectionError.value = msg
      onError?.(msg)
    }

    // Keyboard — attach to display element so only keystrokes while the
    // RDP canvas is focused are forwarded.
    keyboard = new Guacamole.Keyboard(_displayElement)
    keyboard.onkeydown = (keysym: number) => {
      if (client) {
        client.sendKeyEvent(1, keysym)
        return false // Prevent browser default
      }
      return true
    }
    keyboard.onkeyup = (keysym: number) => {
      if (client) {
        client.sendKeyEvent(0, keysym)
      }
    }

    // Make the display focusable so keyboard events are captured
    _displayElement.tabIndex = 1
    _displayElement.style.outline = 'none'
    _displayElement.style.cursor = 'none'
    _displayElement.addEventListener('click', () => _displayElement?.focus())

    // Mouse — hide local cursor over the RDP canvas (RDP renders its own)
    mouse = new Guacamole.Mouse(_displayElement)
    mouse.onmousedown =
    mouse.onmouseup =
    mouse.onmousemove = (mouseState: any) => {
      if (client) {
        client.sendMouseState(mouseState)
      }
    }

    // Connect — the tunnel appends "?" + connectData to the URL. Pass only the
    // opaque ticket (no token / credentials in the URL).
    client.connect(`ticket=${ticket}`)
  }

  function disconnect(): void {
    if (keyboard) {
      keyboard.onkeydown = null
      keyboard.onkeyup = null
      keyboard = null
    }
    if (mouse) {
      mouse.onmousedown = null
      mouse.onmouseup = null
      mouse.onmousemove = null
      mouse = null
    }
    if (_displayElement && _displayElement.parentNode) {
      _displayElement.parentNode.removeChild(_displayElement)
      _displayElement = null
    }
    if (client) {
      try {
        client.disconnect()
      } catch {
        // Ignore disconnect errors
      }
      client = null
    }
    tunnel = null
    isConnected.value = false
  }

  function sendSize(width: number, height: number): void {
    if (!client) return
    client.sendSize(width, height)
  }

  function sendScale(containerWidth: number, containerHeight: number, _retries: number = 0): void {
    if (!client) return
    const display = client.getDisplay()
    if (!display) return

    const guacWidth = display.getWidth()
    const guacHeight = display.getHeight()
    if (!guacWidth || !guacHeight) {
      if (_retries < 15) {  // Max 15 retries = ~3 seconds
        setTimeout(() => sendScale(containerWidth, containerHeight, _retries + 1), 200)
      }
      return
    }

    const scaleX = containerWidth / guacWidth
    const scaleY = containerHeight / guacHeight
    const scale = Math.min(scaleX, scaleY)
    display.scale(scale)
  }

  function _sendKey(keysym: number, pressed: boolean): void {
    if (!client) return
    client.sendKeyEvent(pressed ? 1 : 0, keysym)
  }

  function _typeChar(code: number): void {
    _sendKey(code, true)
    _sendKey(code, false)
  }

  async function sendShutdown(reboot: boolean): Promise<void> {
    if (!client) return

    // Win+R to open Run dialog
    _sendKey(65515, true)   // Win down
    _sendKey(114, true)     // r down
    _sendKey(114, false)    // r up
    _sendKey(65515, false)  // Win up

    await _delay(1200)

    // Type shutdown command
    const cmd = reboot ? 'shutdown /r /f /t 0' : 'shutdown /s /f /t 0'
    for (const ch of cmd) {
      const code = ch.charCodeAt(0)
      if (code >= 32 && code <= 126) {
        _typeChar(code)
      }
    }

    await _delay(300)

    // Press Enter
    _typeChar(65293)
  }

  function _delay(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms))
  }

  function getDisplayCanvas(): HTMLCanvasElement | null {
    if (!client) return null
    const display = client.getDisplay()
    if (!display) return null
    try {
      const layer = display.getDefaultLayer()
      return layer ? layer.getCanvas() : null
    } catch {
      return null
    }
  }

  function getDisplaySize(): { width: number; height: number } | null {
    if (!client) return null
    const display = client.getDisplay()
    if (!display) return null
    const w = display.getWidth()
    const h = display.getHeight()
    if (!w || !h) return null
    return { width: w, height: h }
  }

  onUnmounted(() => {
    disconnect()
  })

  return {
    isConnected,
    connectionError,
    connect,
    disconnect,
    sendSize,
    sendScale,
    sendShutdown,
    getDisplayCanvas,
    getDisplaySize,
  }
}
