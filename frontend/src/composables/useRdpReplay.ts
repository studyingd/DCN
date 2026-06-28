/**
 * useRdpReplay — composable that replays RDP session recordings
 * using the Guacamole client with a custom ReplayTunnel.
 *
 * Instead of a hand-written instruction renderer, this reuses
 * guacamole-common-js's Client which handles all protocol parsing,
 * layer management, and canvas rendering correctly.
 */

import { ref, onScopeDispose, type Ref } from 'vue'
import Guacamole from 'guacamole-common-js'

// ---------------------------------------------------------------------------
// Composable
// ---------------------------------------------------------------------------

export function useRdpReplay() {
  const isPlaying: Ref<boolean> = ref(false)
  const currentTime: Ref<number> = ref(0)
  const duration: Ref<number> = ref(0)
  const playbackSpeed: Ref<number> = ref(1)
  const loadError: Ref<boolean> = ref(false)

  let tunnel: ReplayTunnelInstance | null = null
  let client: InstanceType<typeof Guacamole.Client> | null = null
  let _displayElement: HTMLElement | null = null
  let _resizeObserver: ResizeObserver | null = null
  let _progressTimer: ReturnType<typeof setInterval> | null = null
  let _infoCallback: InfoCallback | null = null
  let _lastContainerWidth = 0
  let _lastDisplayWidth = 0
  let _lastDisplayHeight = 0

  function onInfoReceived(info: { action: string; duration: number; width: number; height: number }) {
    if (info.duration && info.duration > 0) {
      duration.value = Math.round(info.duration / 100) / 10 // ms → seconds, 0.1s precision
    }
    if (_infoCallback) {
      _infoCallback(info)
    }
  }

  function syncContainerHeight(): void {
    const displayEl = _displayElement
    if (!displayEl) return
    const display = client?.getDisplay()
    if (!display) return

    const guacW = display.getWidth()
    const guacH = display.getHeight()
    if (!guacW || !guacH) return

    const modalBody = displayEl.closest('.replay-modal-body') as HTMLElement | null
    const modal = displayEl.closest('.replay-modal') as HTMLElement | null
    if (!modalBody || !modal) return

    const header = modal.querySelector('.replay-modal-header') as HTMLElement | null
    const controls = modal.querySelector('.replay-modal-controls') as HTMLElement | null
    const headerH = header?.getBoundingClientRect().height || 0
    const controlsH = controls?.getBoundingClientRect().height || 0

    const maxW = window.innerWidth * 0.92
    const maxH = window.innerHeight * 0.92
    const availableH = maxH - headerH - controlsH

    const aspectRatio = guacW / guacH

    let targetW = maxW
    let targetH = targetW / aspectRatio

    if (targetH > availableH) {
      targetH = availableH
      targetW = targetH * aspectRatio
    }

    // Only update when dimensions change — avoids ResizeObserver loops.
    const tw = Math.round(targetW)
    const th = Math.round(targetH)
    const currentModalW = Math.round(modal.getBoundingClientRect().width)
    if (tw === _lastContainerWidth && currentModalW === tw && guacW === _lastDisplayWidth && guacH === _lastDisplayHeight) return
    _lastContainerWidth = tw
    _lastDisplayWidth = guacW
    _lastDisplayHeight = guacH

    modal.style.width = tw + 'px'
    modal.style.height = (th + headerH + controlsH) + 'px'
    modalBody.style.height = th + 'px'

    const scale = targetW / guacW
    requestAnimationFrame(() => {
      display.scale(scale)
    })
  }

  function updateScale(): void {
    syncContainerHeight()
  }

  function startProgressTimer() {
    stopProgressTimer()
    // Use wall-clock reference to avoid drift from setInterval imprecision.
    const startWallClock = Date.now()
    const startPosition = currentTime.value
    _progressTimer = setInterval(() => {
      if (isPlaying.value && duration.value > 0) {
        const elapsed = (Date.now() - startWallClock) * playbackSpeed.value / 1000
        currentTime.value = Math.min(startPosition + elapsed, duration.value)
        if (currentTime.value >= duration.value) {
          isPlaying.value = false
          stopProgressTimer()
        }
      }
    }, 100)
  }

  function stopProgressTimer() {
    if (_progressTimer !== null) {
      clearInterval(_progressTimer)
      _progressTimer = null
    }
  }

  // -------------------------------------------------------------------------
  // ReplayTunnel — feeds pre-recorded Guacamole instructions to a Client
  // -------------------------------------------------------------------------

  interface ReplayTunnelInstance {
    connect: (data?: string) => void
    disconnect: () => void
    sendMessage: (elements?: any) => void
    sendCommand: (cmd: object) => void
    oninstruction: ((opcode: string, parameters: string[]) => void) | null
    state: number
    receiveTimeout: number
    uuid: string
  }

  function createReplayTunnel(recordingId: number): ReplayTunnelInstance {
    const tunnel: ReplayTunnelInstance = {
      oninstruction: null,
      state: Guacamole.Tunnel.State.CONNECTING,
      receiveTimeout: 300000,
      uuid: crypto.randomUUID(),
      connect: () => {},
      disconnect: () => {},
      sendMessage: () => {},
      sendCommand: () => {},
    }

    let ws: WebSocket | null = null

    // Guacamole wire-format parser state
    let parseBuffer = ''

    function setState(s: number) {
      tunnel.state = s
    }

    function parseInstructions(message: string) {
      parseBuffer += message

      let startIndex = 0
      while (startIndex < parseBuffer.length) {
        const dotIdx = parseBuffer.indexOf('.', startIndex)
        if (dotIdx < 0) break

        const lengthStr = parseBuffer.substring(startIndex, dotIdx)
        const length = parseInt(lengthStr, 10)
        if (isNaN(length)) break

        const valueStart = dotIdx + 1
        const valueEnd = valueStart + length
        if (valueEnd > parseBuffer.length) break

        const delimIdx = valueEnd
        if (delimIdx >= parseBuffer.length) break

        const delim = parseBuffer.charCodeAt(delimIdx)

        if (delim === 0x2C /* ',' */ || delim === 0x3B /* ';' */) {
          startIndex = delimIdx + 1

          if (delim === 0x3B) {
            const instruction = parseBuffer.substring(0, delimIdx + 1)
            dispatchInstruction(instruction)
            parseBuffer = parseBuffer.substring(delimIdx + 1)
            startIndex = 0
          }
        } else {
          break
        }
      }
    }

    function dispatchInstruction(instruction: string) {
      const elements: string[] = []
      let pos = 0
      while (pos < instruction.length) {
        const dotIdx = instruction.indexOf('.', pos)
        if (dotIdx < 0) break

        const length = parseInt(instruction.substring(pos, dotIdx), 10)
        if (isNaN(length)) break

        const valueStart = dotIdx + 1
        const value = instruction.substring(valueStart, valueStart + length)
        elements.push(value)

        pos = valueStart + length + 1 // skip delimiter (, or ;)
      }

      if (elements.length === 0) return

      const opcode = elements[0]
      const args = elements.slice(1)

      if (tunnel.oninstruction) {
        tunnel.oninstruction(opcode, args)
      }
    }

    tunnel.connect = function (_data?: string) {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      // Same-origin WebSocket: the httpOnly access cookie is sent automatically,
      // so no token in the URL. Backend requires audit:manage.
      const url = `${protocol}//${window.location.host}/ws/rdp-recording/${recordingId}`

      ws = new WebSocket(url, 'guacamole')

      ws.onopen = () => {
        setState(Guacamole.Tunnel.State.OPEN)
        ws!.send(JSON.stringify({ action: 'info' }))
      }

      ws.onmessage = (event: MessageEvent) => {
        const message = event.data as string

        // Handle JSON control messages (info responses)
        if (message.startsWith('{')) {
          try {
            const msg = JSON.parse(message)
            if (msg.action === 'info') {
              onInfoReceived(msg)
            }
          } catch { /* ignore */ }
          return
        }

        // Guacamole wire-format instructions — parse and forward to client
        parseInstructions(message)
      }

      ws.onclose = () => {
        setState(Guacamole.Tunnel.State.CLOSED)
      }

      ws.onerror = () => {
        setState(Guacamole.Tunnel.State.CLOSED)
      }

      setState(Guacamole.Tunnel.State.CONNECTING)
    }

    tunnel.disconnect = function () {
      if (ws) {
        ws.close()
        ws = null
      }
      parseBuffer = ''
      setState(Guacamole.Tunnel.State.CLOSED)
    }

    tunnel.sendMessage = function () {
      // Replay is read-only
    }

    tunnel.sendCommand = function (cmd: object) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(cmd))
      }
    }

    return tunnel
  }

  function init(recordingId: number, container: HTMLElement): void {
    destroy()

    tunnel = createReplayTunnel(recordingId)

    // @ts-expect-error — ReplayTunnel matches Guacamole.Tunnel interface but isn't a class instance
    client = new Guacamole.Client(tunnel)

    // Attach display to container
    const display = client.getDisplay()
    _displayElement = display.getElement()
    _displayElement.style.width = '100%'
    _displayElement.style.height = '100%'
    container.appendChild(_displayElement)

    // Track display dimensions for scaling after initial instructions arrive
    _infoCallback = (info) => {
      if (info.width && info.height) {
        setTimeout(() => updateScale(), 100)
        setTimeout(() => updateScale(), 500)
      }
    }

    client.onstatechange = (_state: number) => {
      const display = client?.getDisplay()
      if (display && display.getWidth() && display.getHeight()) {
        setTimeout(() => updateScale(), 50)
        setTimeout(() => updateScale(), 300)
      }
    }

    client.onerror = () => {
      loadError.value = true
    }

    // @ts-expect-error — ReplayTunnel provides a connect method not declared on Guacamole.Tunnel
    client.connect()

    // Scale display when container resizes
    _resizeObserver = new ResizeObserver(() => updateScale())
    _resizeObserver.observe(container)

    startProgressTimer()
  }

  function play(): void {
    if (tunnel) {
      tunnel.sendCommand({ action: 'play' })
    }
    isPlaying.value = true
    startProgressTimer()
  }

  function pause(): void {
    if (tunnel) {
      tunnel.sendCommand({ action: 'pause' })
    }
    isPlaying.value = false
  }

  function seek(time: number): void {
    if (tunnel) {
      tunnel.sendCommand({ action: 'seek', time: Math.round(time * 1000) })
    }
    currentTime.value = time
  }

  function setSpeed(speed: number): void {
    if (tunnel) {
      tunnel.sendCommand({ action: 'speed', value: speed })
    }
    playbackSpeed.value = speed
  }

  function destroy(): void {
    stopProgressTimer()
    if (_resizeObserver) {
      _resizeObserver.disconnect()
      _resizeObserver = null
    }
    if (_displayElement && _displayElement.parentNode) {
      _displayElement.parentNode.removeChild(_displayElement)
      _displayElement = null
    }
    if (client) {
      try { client.disconnect() } catch { /* ignore */ }
      client = null
    }
    if (tunnel) {
      tunnel.disconnect()
      tunnel = null
    }
    _infoCallback = null
    _lastContainerWidth = 0
    _lastDisplayWidth = 0
    _lastDisplayHeight = 0
    isPlaying.value = false
    currentTime.value = 0
    duration.value = 0
    playbackSpeed.value = 1
    loadError.value = false
  }

  // Ensure the WebSocket / ResizeObserver / timer are always torn down when the
  // owning scope ends, even if the consumer forgets to call destroy().
  onScopeDispose(() => destroy())

  return {
    isPlaying,
    currentTime,
    duration,
    playbackSpeed,
    loadError,
    init,
    play,
    pause,
    seek,
    setSpeed,
    destroy,
  }
}

// ---------------------------------------------------------------------------
// Info callback type
// ---------------------------------------------------------------------------

type InfoCallback = (info: { duration: number; width: number; height: number }) => void
