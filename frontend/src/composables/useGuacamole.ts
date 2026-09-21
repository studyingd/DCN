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
import { uploadBlobPipelined, type UploadPhase } from '@/utils/guacUpload'
import { backendWsBase } from '@/utils/wsBase'

export type { UploadPhase }

export function useGuacamole() {
  const isConnected: Ref<boolean> = ref(false)
  const connectionError: Ref<string | null> = ref(null)

  let tunnel: InstanceType<typeof Guacamole.WebSocketTunnel> | null = null
  let client: InstanceType<typeof Guacamole.Client> | null = null
  let keyboard: InstanceType<typeof Guacamole.Keyboard> | null = null
  let mouse: InstanceType<typeof Guacamole.Mouse> | null = null
  let _displayElement: HTMLElement | null = null
  // Guacamole.Mouse internally uses offsetLeft/offsetTop to calculate the
  // pointer position. Those layout coordinates do not account for CSS
  // transforms or some fullscreen/dialog offsets, so keep a geometry-based
  // point from the native event as the authoritative local position.
  let _nativeMousePoint: { x: number; y: number } | null = null
  let _nativeMouseCleanup: (() => void) | null = null
  // Tears down the clipboard listeners installed in connect(); invoked by disconnect().
  let _clipboardCleanup: (() => void) | null = null
  // Window-level capture keydown listener that handles Ctrl+V BEFORE
  // Guacamole's element listener can preventDefault it (which would suppress
  // the browser paste event). Stored so disconnect() can remove it.
  let _pasteKeyHandler: ((e: KeyboardEvent) => void) | null = null
  // GuacamoleFS virtual drive root object (a Guacamole.Object), set when guacd
  // sends the `filesystem` instruction. Drives the RDP file browser; null until
  // the drive is exposed. RDP file transfer goes through this object, NOT REST.
  const isFilesystemReady: Ref<boolean> = ref(false)
  let _filesystem: any = null
  // 最近一次 sendScale 用的容器尺寸。guacd 应用新桌面尺寸是异步的，
  // 必须等 Display.onresize 回调到达后按新 framebuffer 重新算一次 CSS 缩放。
  let _lastContainerSize: { w: number; h: number } | null = null
  let _onFilesystemReady: ((name: string) => void) | null = null

  // 直连后端而非走 Vite 代理：dev 代理会吃掉 Sec-WebSocket-Protocol，
  // guacamole-common-js 的子协议协商会失败。端口解析见 utils/wsBase。
  function _wsBaseUrl(): string {
    return backendWsBase()
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
    ticket?: string
    wsPath?: string
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
      ticket: providedTicket,
      wsPath = `/ws/terminal/${deviceId}`,
    } = opts

    const rect = container.getBoundingClientRect()
    const rdpWidth = Math.round(rect.width) || 1024
    const rdpHeight = Math.round(rect.height) || 768

    let ticket: string = providedTicket || ''
    try {
      if (!ticket) {
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
      }
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      const msg = detail || '获取终端票据失败'
      connectionError.value = msg
      onError?.(msg)
      onDisconnected?.()
      return
    }

    const base = _wsBaseUrl()
    const url = `${base}${wsPath}`

    tunnel = new Guacamole.WebSocketTunnel(url)
    client = new Guacamole.Client(tunnel)

    // Attach display to container
    const display = client.getDisplay()
    const displayElement = display.getElement() as HTMLElement
    _displayElement = displayElement
    container.appendChild(displayElement)

    // sendSize 是异步生效的：guacd 应用新桌面尺寸后会下发 size 指令，
    // Guacamole.Display 才真正改变 framebuffer 并触发 onresize。此前 sendScale
    // 用的是**旧**的 framebuffer 尺寸，缩放比会停在错误值上且不再被纠正——
    // 表现为「收起侧栏分辨率变了，再展开侧栏却不再变化」（画面停在旧比例）。
    // 这里在新尺寸到达时按记录的容器尺寸重算一次 CSS 缩放。
    //
    // 注意：guacamole-common-js 的 Client **没有** onsize 事件，正确的钩子是
    // Display.onresize（见 dist/cjs/guacamole-common.js 的 "size" 指令处理）。
    // 关键：onresize 在 size 指令处理器里被**同步调用且无 try-catch**——一旦
    // 这里的 sendScale/scale 抛异常，会中断整个指令分发循环，后续所有指令
    // （包括 sync 心跳）都停止，画面冻结在某一帧。必须兜底，绝不能让异常逃逸。
    display.onresize = () => {
      try {
        if (_lastContainerSize) {
          sendScale(_lastContainerSize.w, _lastContainerSize.h)
        }
      } catch (e) {
        // 吞掉并记录,绝不能中断 guacd 指令循环
        console.warn('[RDP] onresize rescale failed', e)
      }
    }

    let _hadError = false

    // Event handlers
    // States: IDLE=0, CONNECTING=1, WAITING=2, CONNECTED=3, DISCONNECTING=4, DISCONNECTED=5
    client.onstatechange = (state: number) => {
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
    keyboard = new Guacamole.Keyboard(displayElement)
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

    // ── Clipboard paste handling (browser → Windows) ──────────────────────
    // guacamole-common-js unconditionally preventDefaults every interpreted
    // keydown (guacamole-common.js:8190), which would suppress the browser
    // 'paste' event on Ctrl+V. Handle Ctrl+V at the WINDOW capture phase
    // (it runs before Guacamole's element-level listener) and stop its
    // propagation, so Guacamole never cancel()s the keydown and the browser
    // still dispatches 'paste'. That handler reads clipboardData — available
    // WITHOUT the clipboard-read permission that navigator.clipboard.readText()
    // needs (Chrome defaults clipboard-read to "prompt", i.e. denied) — pushes
    // it to the remote clipboard, then synthesizes a full Ctrl+V keystroke so
    // Windows pastes it.
    let kbPasteArmed = false
    _pasteKeyHandler = (e: KeyboardEvent) => {
      if (!_displayElement) return
      // 不再要求焦点必须落在 display 元素内：设备远程页有侧栏/工具栏等可聚焦
      // 元素，焦点一旦离开画面 Ctrl+V 就静默失效（虚拟机独立窗口没有其它可聚焦
      // 元素，所以"只有虚机能粘贴"）。改为只要焦点不在输入控件里就接管。
      const target = e.target as HTMLElement | null
      if (target) {
        const tag = target.tagName
        if (tag === 'INPUT' || tag === 'TEXTAREA' || target.isContentEditable) return
      }
      const isPaste = (e.ctrlKey || e.metaKey) && (e.key === 'v' || e.key === 'V' || e.code === 'KeyV')
      if (!isPaste) return
      kbPasteArmed = true
      // Stop Guacamole's capture-phase keydown on the display element (and any
      // later listeners) — but do NOT preventDefault; we want the paste event.
      e.stopImmediatePropagation()
    }
    window.addEventListener('keydown', _pasteKeyHandler, true)

    // Make the display focusable so keyboard events are captured
    displayElement.tabIndex = 1
    displayElement.style.outline = 'none'
    // Keep the browser's hardware cursor as the single source of truth for
    // pointer position. Guacamole's software cursor is useful for clients
    // that cannot install a CSS cursor, but rendering it in addition to the
    // native pointer allows delayed server-side `mouse` instructions to race
    // local events and appear to wrap at an edge.
    displayElement.style.cursor = 'default'
    displayElement.addEventListener('click', () => displayElement.focus())

    // Capture the pointer position before Guacamole.Mouse's bubbling
    // listeners run. getBoundingClientRect() reflects the actual on-screen
    // (scaled) bounds, unlike offsetLeft/offsetTop. The point remains in
    // local display units and is converted to the remote framebuffer below.
    const recordNativeMousePoint = (event: MouseEvent) => {
      const rect = displayElement.getBoundingClientRect()
      if (!rect.width || !rect.height) return
      _nativeMousePoint = {
        x: Math.max(0, Math.min(rect.width - 1, event.clientX - rect.left)),
        y: Math.max(0, Math.min(rect.height - 1, event.clientY - rect.top)),
      }
    }
    displayElement.addEventListener('mousemove', recordNativeMousePoint, true)
    displayElement.addEventListener('mousedown', recordNativeMousePoint, true)
    displayElement.addEventListener('mouseup', recordNativeMousePoint, true)
    _nativeMouseCleanup = () => {
      displayElement.removeEventListener('mousemove', recordNativeMousePoint, true)
      displayElement.removeEventListener('mousedown', recordNativeMousePoint, true)
      displayElement.removeEventListener('mouseup', recordNativeMousePoint, true)
      _nativeMousePoint = null
      _nativeMouseCleanup = null
    }

    // Mouse — keep the browser/native cursor as the only rendered pointer.
    mouse = new Guacamole.Mouse(displayElement)
    // Prefer the native CSS cursor (including the cursor image/hotspot sent
    // by the RDP server). The backend suppresses server-side cursor-position
    // instructions, so this callback only changes the shape.
    display.oncursor = (canvas: HTMLCanvasElement, hotspotX: number, hotspotY: number) => {
      try {
        mouse?.setCursor(canvas, hotspotX, hotspotY)
        display.showCursor(false)
      } catch (e) {
        console.warn('[RDP] oncursor failed', e)
      }
    }
    // Do not show a software cursor before the first server cursor shape is
    // received. The native browser cursor remains visible meanwhile. Never
    // fall back to the software layer: that would reintroduce the two-source
    // cursor race on browsers that reject custom CSS cursor URLs.
    display.showCursor(false)
    mouse.onmousedown =
      mouse.onmouseup =
      mouse.onmousemove =
        (mouseState: any) => {
          if (client) {
            // Guacamole.Mouse reports coordinates in the local (scaled) display
            // space. The display itself is scaled with CSS by sendScale(), so
            // convert to remote framebuffer coordinates explicitly. Without this,
            // enlarging the browser window can send coordinates beyond the RDP
            // desktop and the Windows pointer appears to wrap at the opposite edge.
            const scale = display.getScale()
            const remoteWidth = display.getWidth()
            const remoteHeight = display.getHeight()
            const safeScale = Number.isFinite(scale) && scale > 0 ? scale : 1
            const clampRemoteCoordinate = (value: unknown, remoteSize: number): number => {
              const numeric = Number(value)
              if (!Number.isFinite(numeric)) return 0
              if (!remoteSize || remoteSize < 1) return Math.max(0, numeric)
              // Do not send the exact four framebuffer edges. Some FreeRDP/RDP
              // cursor paths treat an absolute edge coordinate as a relative-mode
              // wrap point, making the pointer reappear on the opposite side.
              // A two-pixel inset is visually negligible and avoids those sentinel
              // edge values while preserving access to edge-aligned controls.
              const inset = remoteSize > 5 ? 2 : 0
              const minimum = inset
              const maximum = Math.max(minimum, remoteSize - 1 - inset)
              return Math.min(maximum, Math.max(minimum, numeric))
            }

            // Convert from the actual on-screen point to remote framebuffer
            // coordinates ourselves. Passing `false` below is intentional: the
            // coordinates are already remote units, so guacamole-common-js must
            // not divide by display.getScale() a second time.
            const localX = _nativeMousePoint?.x ?? mouseState?.x
            const localY = _nativeMousePoint?.y ?? mouseState?.y
            const safeMouseState = {
              ...mouseState,
              x: clampRemoteCoordinate(Number(localX) / safeScale, remoteWidth),
              y: clampRemoteCoordinate(Number(localY) / safeScale, remoteHeight),
            }

            client.sendMouseState(safeMouseState, false)
          }
        }

    // ── Clipboard: bidirectional text sync between browser and RDP desktop ──
    // Browser → RDP (paste into Windows): reads OS clipboard text from the
    //   'paste' event (no clipboard-read permission needed) and pushes it to
    //   guacd. When the paste came from a keyboard Ctrl+V (kbPasteArmed), the
    //   V keydown was handled above before Guacamole could forward it, so
    //   we synthesize a full Ctrl+V here (independent of real Ctrl key state)
    //   to paste the just-synced clipboard on the remote desktop.
    // RDP → browser: when the desktop clipboard changes, mirror it to the
    //   browser clipboard through navigator.clipboard.
    const onPaste = (e: ClipboardEvent) => {
      const text = e.clipboardData?.getData('text/plain') ?? ''
      if (!text || !client) return
      // 与 keydown 拦截同一口径：焦点在本地输入控件里时不接管，
      // 否则在文件管理器输入框粘贴会顺手覆盖远程剪贴板。
      const target = e.target as HTMLElement | null
      if (target) {
        const tag = target.tagName
        if (tag === 'INPUT' || tag === 'TEXTAREA' || target.isContentEditable) return
      }
      const c = client
      // Push text into the RDP clipboard via a Guacamole clipboard output
      // stream. StringWriter handles the UTF-8 → base64 chunking onto the stream.
      const stream = c.createClipboardStream('text/plain')
      const writer = new Guacamole.StringWriter(stream)
      writer.sendText(text)
      writer.sendEnd()
      if (kbPasteArmed) {
        kbPasteArmed = false
        // Synthesize the FULL Ctrl+V on the remote so the paste is independent
        // of the real Ctrl key state (the real Ctrl may already be released by
        // the time this fires). The clipboard stream above is queued first, so
        // guacd applies the CLIPRDR update before this keystroke combo.
        setTimeout(() => {
          c.sendKeyEvent(1, 0xffe3) // Control down
          c.sendKeyEvent(1, 0x0076) // 'v' down
          c.sendKeyEvent(0, 0x0076) // 'v' up
          c.sendKeyEvent(0, 0xffe3) // Control up
        }, 100)
      }
    }
    const onClipboardFromRdp = (stream: any, mimetype: string) => {
      if (!client) return
      if (!mimetype || mimetype.indexOf('text/plain') === -1) return
      let text = ''
      stream.onblob = (data: string) => {
        // data is base64-encoded UTF-8 text from guacd
        const binary = atob(data)
        const bytes = new Uint8Array(binary.length)
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
        text += new TextDecoder('utf-8').decode(bytes)
        client!.sendAck(stream.index, 'OK', 0x00000000)
      }
      stream.onend = () => {
        if (text)
          navigator.clipboard?.writeText(text).catch(() => {
            /* ignore */
          })
        client!.sendAck(stream.index, 'END', 0x00000000)
      }
      // Initial ack so guacd starts sending blobs (same pattern as the audio
      // stream in guacamole-common-js; without it the clipboard body stalls).
      client!.sendAck(stream.index, 'OK', 0x00000000)
    }
    client.onclipboard = onClipboardFromRdp
    // 同上：paste 事件在 document 捕获，焦点不在画面上时也能粘贴。
    document.addEventListener('paste', onPaste, true)
    _clipboardCleanup = () => {
      document.removeEventListener('paste', onPaste, true)
      if (_pasteKeyHandler) {
        window.removeEventListener('keydown', _pasteKeyHandler, true)
        _pasteKeyHandler = null
      }
      _displayElement?.removeEventListener('paste', onPaste)
      if (client) client.onclipboard = null
      _clipboardCleanup = null
    }

    // ── Filesystem: GuacamoleFS virtual drive for browser⇄Windows file transfer ──
    // guacd exposes the per-session drive (enable-drive) as a Guacamole.Object.
    // We hold onto it and drive list/upload/download through it (the file data
    // flows over the Guacamole protocol and isn't parsed server-side).
    client.onfilesystem = (object: any, name: string) => {
      try {
        _filesystem = object
        isFilesystemReady.value = true
        _onFilesystemReady?.(name)
      } catch (e) {
        console.warn('[RDP] onfilesystem failed', e)
      }
    }

    // Connect — the tunnel appends "?" + connectData to the URL. Pass only the
    // opaque ticket (no token / credentials in the URL).
    client.connect(`ticket=${ticket}`)
  }

  function disconnect(): void {
    if (_clipboardCleanup) {
      _clipboardCleanup()
      _clipboardCleanup = null
    }
    if (_nativeMouseCleanup) {
      _nativeMouseCleanup()
      _nativeMouseCleanup = null
    }
    _filesystem = null
    isFilesystemReady.value = false
    _onFilesystemReady = null
    _lastContainerSize = null
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
    if (client) {
      try {
        client.getDisplay().oncursor = null
        client.getDisplay().showCursor(false)
      } catch {
        // Ignore display cleanup errors during disconnect.
      }
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
    _nativeMousePoint = null
    isConnected.value = false
  }

  function sendSize(width: number, height: number): void {
    if (!client) return
    client.sendSize(width, height)
  }

  function sendScale(containerWidth: number, containerHeight: number, _retries: number = 0): void {
    if (!client) return
    _lastContainerSize = { w: containerWidth, h: containerHeight }
    const display = client.getDisplay()
    if (!display) return

    const guacWidth = display.getWidth()
    const guacHeight = display.getHeight()
    if (!guacWidth || !guacHeight) {
      if (_retries < 15) {
        // Max 15 retries = ~3 seconds
        setTimeout(() => sendScale(containerWidth, containerHeight, _retries + 1), 200)
      }
      return
    }

    const scaleX = containerWidth / guacWidth
    const scaleY = containerHeight / guacHeight
    const scale = Math.min(scaleX, scaleY)
    display.scale(scale)
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

  // ── RDP file transfer via the GuacamoleFS virtual drive ────────────────
  // The drive's directory bodies are JSON stream-indexes mapping child name →
  // mimetype; a child whose mimetype is this constant is itself a directory.
  const STREAM_INDEX_MIMETYPE = 'application/vnd.glyptodon.guacamole.stream-index+json'

  /** Entry shape compatible with the SSH file manager's FileEntry. */
  interface RdpFileEntry {
    name: string
    size: number
    type: 'dir' | 'file'
    mtime: number | null
  }

  function _normalizeFsPath(p: string): string {
    // Drive root is "/". Ensure a leading slash, drop trailing slash (except root).
    let s = (p || '/').trim()
    if (!s.startsWith('/')) s = '/' + s
    if (s.length > 1 && s.endsWith('/')) s = s.slice(0, -1)
    return s || '/'
  }

  /** Read a Guacamole InputStream fully into bytes, acking each blob. */
  function _readStreamBytes(inputStream: any): Promise<Uint8Array> {
    return new Promise((resolve) => {
      const parts: Uint8Array[] = []
      let total = 0
      inputStream.onblob = (data: string) => {
        const bin = atob(data)
        const bytes = new Uint8Array(bin.length)
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
        parts.push(bytes)
        total += bytes.length
        client!.sendAck(inputStream.index, 'OK', 0x00000000)
      }
      inputStream.onend = () => {
        client!.sendAck(inputStream.index, 'END', 0x00000000)
        const merged = new Uint8Array(total)
        let off = 0
        for (const part of parts) {
          merged.set(part, off)
          off += part.length
        }
        resolve(merged)
      }
      // Initial ack: signal we accept this server→client stream so guacd starts
      // sending blobs. Mirrors the audio-stream pattern in guacamole-common-js
      // (which acks the stream opening); without it, guacd's filesystem `body`
      // sits waiting and the listing never arrives.
      client!.sendAck(inputStream.index, 'OK', 0x00000000)
    })
  }

  /** Register a one-shot callback for when guacd exposes the filesystem. */
  function setOnFilesystemReady(cb: (name: string) => void): void {
    if (_filesystem) cb('GuacamoleFS')
    else _onFilesystemReady = cb
  }

  /** List a directory on the GuacamoleFS drive. Returns [] if not ready. */
  function listFiles(path: string): Promise<RdpFileEntry[]> {
    if (!_filesystem) return Promise.resolve([])
    const dir = _normalizeFsPath(path)
    return new Promise((resolve) => {
      _filesystem.requestInputStream(dir, async (inputStream: any, mimetype: string) => {
        if (mimetype !== STREAM_INDEX_MIMETYPE) {
          resolve([])
          return
        }
        const bytes = await _readStreamBytes(inputStream)
        const text = new TextDecoder('utf-8').decode(bytes)
        let map: Record<string, string> = {}
        try {
          map = JSON.parse(text)
        } catch {
          map = {}
        }
        // guacd's stream-index keys children with a leading '/' (e.g. "/Download");
        // strip it so the displayed name and path-joining stay clean.
        const entries: RdpFileEntry[] = Object.keys(map).map((raw) => ({
          name: raw.replace(/^\/+/, ''),
          type: map[raw] === STREAM_INDEX_MIMETYPE ? ('dir' as const) : ('file' as const),
          size: 0,
          mtime: null,
        }))
        entries.sort((a, b) => (a.type === 'dir' ? 0 : 1) - (b.type === 'dir' ? 0 : 1) || a.name.localeCompare(b.name))
        resolve(entries)
      })
    })
  }

  /**
   * 把一个文件上传到共享盘的 destDir，返回写入字节数。
   *
   * 状态机在 utils/guacUpload.ts（有单测覆盖：完整性、窗口上限、超时、错误 ack、
   * 进度单调与 flushing 阶段）。这里只负责把 guacd 的输出流接上去。
   */
  function uploadFile(
    file: File,
    destDir: string,
    onProgress?: (pct: number, phase: UploadPhase) => void,
  ): Promise<number> {
    if (!_filesystem || !client) return Promise.reject(new Error('文件系统未就绪'))
    const dir = _normalizeFsPath(destDir)
    const fullPath = dir === '/' ? `/${file.name}` : `${dir}/${file.name}`
    const stream = _filesystem.createOutputStream('application/octet-stream', fullPath)
    return uploadBlobPipelined(file, stream, { onProgress })
  }

  /** Download a file from the drive and trigger a browser save. Resolves with bytes written. */
  function downloadFile(path: string, filename: string): Promise<number> {
    if (!_filesystem) return Promise.reject(new Error('文件系统未就绪'))
    const fullPath = _normalizeFsPath(path)
    return new Promise((resolve) => {
      _filesystem.requestInputStream(fullPath, async (inputStream: any, mimetype: string) => {
        const bytes = await _readStreamBytes(inputStream)
        // TS 5.7 made Uint8Array generic (Uint8Array<ArrayBufferLike>); BlobPart
        // requires an ArrayBuffer-backed view. bytes is built from a fresh
        // Uint8Array(total) so .buffer is a real ArrayBuffer — assert to satisfy
        // the stricter lib.dom types without changing runtime behavior.
        const blob = new Blob([bytes.buffer as ArrayBuffer], { type: mimetype || 'application/octet-stream' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = filename.replace(/^\/+/, '')
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        URL.revokeObjectURL(url)
        resolve(bytes.length)
      })
    })
  }

  onUnmounted(() => {
    disconnect()
  })

  return {
    isConnected,
    connectionError,
    isFilesystemReady,
    connect,
    disconnect,
    sendSize,
    sendScale,
    getDisplayCanvas,
    getDisplaySize,
    setOnFilesystemReady,
    listFiles,
    uploadFile,
    downloadFile,
  }
}
