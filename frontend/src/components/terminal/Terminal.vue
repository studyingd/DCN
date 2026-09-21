<!--
  Terminal.vue — interactive browser-based terminal backed by xterm.js + WebSocket.

  Required packages (add to package.json):
    "xterm": "^5.5.0"
    "xterm-addon-fit": "^0.8.0"
    "xterm-addon-web-links": "^0.9.0"

  Install with:  npm install xterm@^5.5.0 xterm-addon-fit@^0.8.0 xterm-addon-web-links@^0.9.0
-->

<template>
  <div class="terminal-wrapper">
    <!-- Header bar -->
    <div class="terminal-header">
      <div class="terminal-header-left">
        <span class="terminal-status-dot" :class="statusClass"></span>
        <span class="terminal-conn-info"> {{ connType.toUpperCase() }} — {{ deviceIp }} </span>
        <span v-if="statusText" class="terminal-status-text">{{ statusText }}</span>
      </div>
      <div v-if="showToolbar" class="terminal-header-right">
        <el-button
          v-if="showWindowChrome"
          size="small"
          text
          class="terminal-header-btn power-btn-reboot"
          :loading="powerLoading === 'reboot'"
          @click="handlePower('reboot')"
        >
          重启
        </el-button>
        <el-button
          v-if="showWindowChrome"
          size="small"
          text
          class="terminal-header-btn power-btn-shutdown"
          :loading="powerLoading === 'shutdown'"
          @click="handlePower('shutdown')"
        >
          关机
        </el-button>
        <el-divider v-if="showWindowChrome" direction="vertical" />
        <el-button
          v-if="showFiles"
          size="small"
          :icon="FolderOpened"
          text
          class="terminal-header-btn"
          @click="fileManagerVisible = true"
        >
          文件
        </el-button>
        <el-button
          v-if="showWindowChrome"
          size="small"
          :icon="FullScreen"
          text
          class="terminal-header-btn"
          @click="$emit('fullscreen')"
        >
          全屏
        </el-button>
        <el-button
          v-if="showWindowChrome"
          size="small"
          :icon="Close"
          text
          class="terminal-header-btn"
          @click="$emit('close')"
        >
          关闭
        </el-button>
      </div>
    </div>

    <!-- RDP display container (shown instead of terminal for RDP connections) -->
    <div v-if="connType === 'rdp'" class="terminal-rdp-container">
      <div v-if="connectionError" class="rdp-error">
        <el-icon :size="32" color="var(--dcn-danger)"><WarningFilled /></el-icon>
        <p>{{ connectionError }}</p>
      </div>
      <div ref="rdpContainer" class="rdp-display"></div>
      <div class="session-watermark" :style="watermarkStyle" />
    </div>

    <!-- xterm.js container -->
    <div v-show="connType !== 'rdp'" class="terminal-container">
      <div ref="xtermContainer" class="terminal-xterm"></div>
      <div class="session-watermark" :style="watermarkStyle" />
    </div>

    <!-- File manager: SSH uses REST/SFTP, RDP uses the GuacamoleFS drive -->
    <FileManager
      v-if="showFiles"
      v-model="fileManagerVisible"
      :conn-type="connType"
      :device-id="deviceId"
      :credential-id="credentialId ?? null"
      :username="sshUsername"
      :password="sshPassword"
      :rdp-backend="rdpFileBackend"
    />
  </div>
</template>

<script setup lang="ts">
// Required packages (add to package.json):
// "xterm": "^5.5.0"
// "xterm-addon-fit": "^0.8.0"
// "xterm-addon-web-links": "^0.9.0"

import { ref, onMounted, onUnmounted, nextTick, computed } from 'vue'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import { WebLinksAddon } from 'xterm-addon-web-links'
import { FullScreen, Close, WarningFilled, FolderOpened } from '@element-plus/icons-vue'
import { ElMessageBox, ElMessage } from 'element-plus'
import 'xterm/css/xterm.css'

import { useTerminal } from '@/composables/useTerminal'
import { useGuacamole } from '@/composables/useGuacamole'
import FileManager from './FileManager.vue'
import { useAuthStore } from '@/stores/auth'
import axios from 'axios'

// ----------------------------------------------------------------
// Props & Emits
// ----------------------------------------------------------------

const props = defineProps<{
  deviceId: number
  connType: 'ssh' | 'rdp'
  deviceIp: string
  /** SSH credentials — passed through to the backend. */
  sshUsername?: string
  sshPassword?: string
  /** Stored credential ID — if set, backend resolves credentials server-side. */
  credentialId?: number | null
  /** 非空时连接后自动 docker exec 进入该容器(仅 SSH/Linux)。 */
  container?: string
  /**
   * 精简模式:嵌入抽屉等场景时隐藏电源/全屏/关闭等窗口工具栏。
   *
   * 注意：文件传输**不再**被 minimal 一刀切关掉——PVE 虚拟机控制台曾经因此
   * 只能敲命令、不能传文件，和普通设备的远程连接体验不一致。用下面的开关单独控制。
   */
  minimal?: boolean
  /** 文件传输是否可用；缺省跟随 !minimal。PVE 虚拟机传合成 target_id 即可用。 */
  enableFiles?: boolean
  /** Pre-issued ticket and WebSocket path (used by PVE guest consoles). */
  ticket?: string
  wsPath?: string
  /** Keep the server-side RDP desktop resolution fixed and scale locally. */
  fixedRdpResolution?: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'fullscreen'): void
  (e: 'connection-error', message: string): void
}>()

// 窗口级工具栏（电源/全屏/关闭）只在非嵌入模式下出现；文件传输可单独开启。
const showWindowChrome = computed(() => !props.minimal)
const showFiles = computed(() => props.enableFiles ?? !props.minimal)
const showToolbar = computed(() => showWindowChrome.value || showFiles.value)

// ----------------------------------------------------------------
// Power control
// ----------------------------------------------------------------

const powerLoading = ref<string | null>(null)

const actionLabels: Record<string, string> = {
  shutdown: '关机',
  reboot: '重启',
}

async function handlePower(action: string) {
  const label = actionLabels[action] || action
  try {
    await ElMessageBox.confirm(`确定要对设备 ${props.deviceIp} 执行「${label}」操作吗？`, '电源控制', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }

  powerLoading.value = action

  // 电源操作统一走后端 API:Windows 经 WinRM,Linux 经 SSH,
  // 指令发出后由后端探测端口确认真实离线。
  try {
    const res = await axios.post(
      `/api/devices/${props.deviceId}/power`,
      {
        action,
        username: props.sshUsername,
        password: props.sshPassword,
        credential_id: props.credentialId ?? undefined,
      },
      { withCredentials: true },
    )
    if (res.data.success) {
      ElMessage.success(res.data.message || `${label}指令已发送`)
    } else {
      ElMessage.error(res.data.message || `${label}操作失败`)
    }
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || `${label}请求失败`)
  } finally {
    powerLoading.value = null
  }
}

// ----------------------------------------------------------------
// Refs
// ----------------------------------------------------------------

const xtermContainer = ref<HTMLDivElement>()
const rdpContainer = ref<HTMLDivElement>()
const statusText = ref<string>('Connecting...')
const fileManagerVisible = ref(false)

// ----------------------------------------------------------------
// Watermark
// ----------------------------------------------------------------
const authStore = useAuthStore()

const watermarkStyle = computed(() => {
  // Watermark shows the LOGIN username (not display_name/role name) so the
  // account is always uniquely attributable for audit/traceability. Username
  // comes from the auth store profile (loaded from /api/auth/profile), not
  // from decoding the JWT (which the SPA no longer holds).
  const username = authStore.user?.username ?? ''
  // Sanitize against XML injection in SVG
  const safeName = username
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;')
  const now = new Date()
  const dateStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
  const text = `${safeName}  ${dateStr}`
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="260" height="100">
    <text x="130" y="50" text-anchor="middle" font-size="14" font-family="Arial,sans-serif" fill="rgba(255,255,255,0.13)" font-weight="bold">${text}</text>
  </svg>`
  const encoded = encodeURIComponent(svg)
  return {
    backgroundImage: `url("data:image/svg+xml,${encoded}")`,
  }
})

const { isConnected, connectionError: sshError, connect, disconnect, sendInput, sendPaste, sendResize } = useTerminal()

const {
  isConnected: rdpConnected,
  connectionError: rdpError,
  connect: rdpConnect,
  disconnect: rdpDisconnect,
  sendSize: rdpSendSize,
  sendScale: rdpSendScale,
  isFilesystemReady: rdpFsReady,
  listFiles: rdpListFiles,
  uploadFile: rdpUploadFile,
  downloadFile: rdpDownloadFile,
} = useGuacamole()

// Adapter handed to FileManager so the same dialog drives SSH (REST/SFTP) and
// RDP (GuacamoleFS via the guacd object-stream protocol).
const rdpFileBackend = {
  isReady: rdpFsReady,
  list: rdpListFiles,
  upload: rdpUploadFile,
  download: rdpDownloadFile,
}

const connectionError = computed(() => {
  return props.connType === 'rdp' ? rdpError.value : sshError.value
})

const statusClass = computed<'status-connecting' | 'status-connected' | 'status-disconnected'>(() => {
  if (connectionError.value) return 'status-disconnected'
  if (props.connType === 'rdp' ? rdpConnected.value : isConnected.value) return 'status-connected'
  return 'status-connecting'
})

// ----------------------------------------------------------------
// xterm.js instances
// ----------------------------------------------------------------

let term: Terminal | null = null
let fitAddon: FitAddon | null = null
// SSH clipboard-paste listener (capture phase) — kept so it can be torn down.
let _sshPasteHandler: ((e: ClipboardEvent) => void) | null = null

// ----------------------------------------------------------------
// Lifecycle
// ----------------------------------------------------------------

onMounted(async () => {
  document.title = `${props.connType.toUpperCase()} — ${props.deviceIp}`

  if (props.connType === 'rdp') {
    await nextTick()
    openRdpConnection()
  } else {
    await nextTick()
    initTerminal()
    openConnection()
  }

  window.addEventListener('resize', scheduleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', scheduleResize)
  destroyTerminal()
  rdpDisconnect()
  if (rdpResizeObserver) {
    rdpResizeObserver.disconnect()
    rdpResizeObserver = null
  }
})

// ----------------------------------------------------------------
// Terminal init / destroy
// ----------------------------------------------------------------

function initTerminal() {
  // NOTE: only xtermContainer is bound in the template (terminalContainer is
  // declared but unused). Guard against the actually-used ref here.
  if (!xtermContainer.value) return

  term = new Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: '"Cascadia Code", "Fira Code", "JetBrains Mono", Menlo, Monaco, "Courier New", monospace', // kept as xterm requires literal font string; matches --dcn-font-mono
    theme: {
      background: '#0d1422',
      foreground: '#dbeafe',
      cursor: '#60a5fa',
      selectionBackground: '#1d4ed8',
      black: '#000000',
      red: '#cd3131',
      green: '#0dbc79',
      yellow: '#e5e510',
      blue: '#2472c8',
      magenta: '#bc3fbc',
      cyan: '#11a8cd',
      white: '#e5e5e5',
      brightBlack: '#666666',
      brightRed: '#f14c4c',
      brightGreen: '#23d18b',
      brightYellow: '#f5f543',
      brightBlue: '#3b8eea',
      brightMagenta: '#d670d6',
      brightCyan: '#29b8db',
      brightWhite: '#ffffff',
    },
    allowProposedApi: true,
    scrollback: 5000,
  })

  // Addons
  fitAddon = new FitAddon()
  term.loadAddon(fitAddon)
  term.loadAddon(new WebLinksAddon())

  term.open(xtermContainer.value)

  // Fit after opening
  try {
    fitAddon.fit()
  } catch {
    // May fail if container is hidden
  }

  // Pipe keystrokes to WebSocket
  term.onData((data: string) => {
    sendInput(data)
  })

  // Forward SSH clipboard pastes through the dedicated message type. Capture
  // phase + stopPropagation prevents xterm's own paste handler from also
  // forwarding the text (which would double the input).
  _sshPasteHandler = (e: ClipboardEvent) => {
    const text = e.clipboardData?.getData('text/plain') ?? ''
    if (text) {
      e.preventDefault()
      e.stopPropagation()
      sendPaste(text)
    }
  }
  xtermContainer.value.addEventListener('paste', _sshPasteHandler, true)
}

function destroyTerminal() {
  if (_sshPasteHandler && xtermContainer.value) {
    xtermContainer.value.removeEventListener('paste', _sshPasteHandler, true)
    _sshPasteHandler = null
  }
  disconnect()
  if (term) {
    term.dispose()
    term = null
  }
  fitAddon = null
}

// ----------------------------------------------------------------
// WebSocket connection
// ----------------------------------------------------------------

function openConnection() {
  connect({
    deviceId: props.deviceId,
    connType: props.connType,
    // When embedded in container management, credentials are intentionally
    // omitted so the backend can resolve the device's saved remote username.
    // Falling back to a hard-coded root here overrides that binding and makes
    // non-root Docker hosts fail authentication.
    username: props.sshUsername,
    password: props.sshPassword ?? '',
    credentialId: props.credentialId ?? undefined,
    container: props.container || undefined,
    ticket: props.ticket,
    wsPath: props.wsPath,
    onOutput: (data: string) => {
      term?.write(data)
    },
    onError: (data: string) => {
      statusText.value = data
      term?.write(`\r\n\x1b[31m-- ${data} --\x1b[0m\r\n`)
      emit('connection-error', data)
    },
    onConnected: (message: string) => {
      statusText.value = message
    },
    onDisconnected: () => {
      statusText.value = 'Disconnected'
      term?.write('\r\n\x1b[33m-- Connection closed --\x1b[0m\r\n')
    },
  })
}

// ----------------------------------------------------------------
// RDP connection via Guacamole
// ----------------------------------------------------------------

function openRdpConnection() {
  if (!rdpContainer.value) return

  statusText.value = 'Connecting RDP...'

  rdpConnect({
    deviceId: props.deviceId,
    username: props.sshUsername ?? '',
    password: props.sshPassword ?? '',
    credentialId: props.credentialId,
    container: rdpContainer.value,
    ticket: props.ticket,
    wsPath: props.wsPath,
    onConnected: () => {
      statusText.value = `RDP — ${props.deviceIp}`
      scaleRdpDisplay()
      // guacd applies the display-update resize asynchronously. Recalculate
      // the local scale after the new framebuffer dimensions arrive; otherwise
      // the old aspect ratio can leave a black strip below the desktop.
      window.setTimeout(() => scaleRdpDisplay(), 350)
      window.setTimeout(() => scaleRdpDisplay(), 900)
      // Auto-scale when container resizes
      if (rdpContainer.value && !rdpResizeObserver) {
        rdpResizeObserver = new ResizeObserver(() => scheduleResize())
        rdpResizeObserver.observe(rdpContainer.value)
      }
    },
    onError: (message: string) => {
      statusText.value = message
      emit('connection-error', message)
    },
    onDisconnected: () => {
      statusText.value = 'Disconnected'
      handleRdpDisconnect()
    },
  })
}

function handleRdpDisconnect() {
  // The Guacamole composable owns session teardown; no recording is persisted.
}

async function gracefulClose() {
  if (props.connType === 'rdp') {
    rdpDisconnect()
  }
  disconnect()
}

defineExpose({ gracefulClose })

function scaleRdpDisplay() {
  if (!rdpContainer.value) return
  const rect = rdpContainer.value.getBoundingClientRect()
  const w = Math.round(rect.width)
  const h = Math.round(rect.height)
  // A larger server-side desktop means guacd must encode and push many more
  // pixels. PVE consoles use a capped remote resolution and only scale the
  // canvas locally when the browser window grows.
  if (!props.fixedRdpResolution) rdpSendSize(w, h)
  rdpSendScale(w, h)
}

// ----------------------------------------------------------------
// Resize handling
// ----------------------------------------------------------------

let resizeTimer: ReturnType<typeof setTimeout> | null = null
let rdpResizeObserver: ResizeObserver | null = null

function handleResize() {
  if (props.connType === 'rdp') {
    scaleRdpDisplay()
    return
  }

  if (!term || !fitAddon) return

  try {
    fitAddon.fit()
  } catch {
    return
  }

  const cols = term.cols
  const rows = term.rows
  sendResize(cols, rows)
}

// Debounced resize
function scheduleResize() {
  if (resizeTimer) clearTimeout(resizeTimer)
  resizeTimer = setTimeout(handleResize, 150)
}
</script>

<style scoped>
.terminal-wrapper {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  background: var(--dcn-bg-page);
  border-radius: var(--dcn-radius-md);
  overflow: hidden;
}

/* ---------- Header ---------- */
.terminal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--dcn-space-1) var(--dcn-space-3);
  background: var(--dcn-bg-section);
  border-bottom: 1px solid var(--dcn-border);
  flex-shrink: 0;
  user-select: none;
  position: relative;
  z-index: 10;
}

.terminal-header-left {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}

.terminal-header-right {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-1);
}

.terminal-status-dot {
  width: var(--dcn-space-2);
  height: var(--dcn-space-2);
  border-radius: var(--dcn-radius-full);
  flex-shrink: 0;
}

.status-connecting {
  background: var(--dcn-warning);
  animation: pulse 1.2s infinite;
}

.status-connected {
  background: var(--dcn-success);
}

.status-disconnected {
  background: var(--dcn-danger);
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}

.terminal-conn-info {
  font-size: var(--dcn-text-base);
  font-family: var(--dcn-font-mono);
  color: var(--dcn-text-primary);
}

.terminal-status-text {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
}

.terminal-header-btn {
  color: var(--dcn-text-secondary) !important;
}

.terminal-header-btn:hover {
  color: var(--dcn-text-primary) !important;
}

.power-btn-reboot:hover {
  color: var(--dcn-warning) !important;
}

.power-btn-shutdown:hover {
  color: var(--dcn-danger) !important;
}

.terminal-header :deep(.el-divider--vertical) {
  border-color: var(--dcn-border-strong);
  height: var(--dcn-text-lg);
  margin: 0 var(--dcn-space-1);
}

/* ---------- xterm container ---------- */
.terminal-container {
  flex: 1;
  min-height: 0;
  position: relative;
}

.terminal-xterm {
  padding: var(--dcn-space-1);
  width: 100%;
  height: 100%;
}

/* Make sure xterm fills its container */
.terminal-xterm :deep(.xterm) {
  height: 100%;
}

.terminal-xterm :deep(.xterm-viewport) {
  overflow-y: auto !important;
}

/* ---------- RDP display ---------- */
.terminal-rdp-container {
  flex: 1;
  min-height: 0;
  position: relative;
  background: #000;
  overflow: hidden;
}

.rdp-display {
  width: 100%;
  height: 100%;
  overflow: hidden;
}

.rdp-error {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--dcn-space-3);
  color: var(--dcn-danger);
  font-size: var(--dcn-text-md);
  z-index: 10;
}

.rdp-error p {
  margin: 0;
}

.clip-hint {
  margin: 0 0 12px;
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary, #909399);
  line-height: 1.5;
}

/* ---------- Session watermark ---------- */
.session-watermark {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 5;
  background-repeat: repeat;
  transform: rotate(-18deg);
  transform-origin: center;
  user-select: none;
}
</style>
