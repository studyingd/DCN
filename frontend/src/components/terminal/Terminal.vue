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
        <span class="terminal-conn-info">
          {{ connType.toUpperCase() }} — {{ deviceIp }}
        </span>
        <span v-if="statusText" class="terminal-status-text">{{ statusText }}</span>
      </div>
      <div class="terminal-header-right">
        <el-button
          size="small"
          text
          class="terminal-header-btn power-btn-reboot"
          @click="handlePower('reboot')"
          :loading="powerLoading === 'reboot'"
        >
          重启
        </el-button>
        <el-button
          size="small"
          text
          class="terminal-header-btn power-btn-shutdown"
          @click="handlePower('shutdown')"
          :loading="powerLoading === 'shutdown'"
        >
          关机
        </el-button>
        <el-divider direction="vertical" />
        <el-button
          size="small"
          :icon="FullScreen"
          text
          class="terminal-header-btn"
          @click="$emit('fullscreen')"
        >
          全屏
        </el-button>
        <el-button
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
        <el-icon :size="32" color="#f56c6c"><WarningFilled /></el-icon>
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
import { FullScreen, Close, Monitor, WarningFilled } from '@element-plus/icons-vue'
import { ElMessageBox, ElMessage } from 'element-plus'
import 'xterm/css/xterm.css'

import { useTerminal } from '@/composables/useTerminal'
import { useGuacamole } from '@/composables/useGuacamole'
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
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'fullscreen'): void
}>()

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
    await ElMessageBox.confirm(
      `确定要对设备 ${props.deviceIp} 执行「${label}」操作吗？`,
      '电源控制',
      { confirmButtonText: '确定', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }

  powerLoading.value = action

  // For RDP: send shutdown keys through the existing Guacamole session
  if (props.connType === 'rdp' && rdpConnected.value) {
    try {
      await rdpSendShutdown(action === 'reboot')
      ElMessage.success(`${label}指令已通过 RDP 发送`)
    } catch {
      ElMessage.error(`${label}指令发送失败`)
    } finally {
      powerLoading.value = null
    }
    return
  }

  // For SSH (or RDP fallback): call the backend API (auth via httpOnly cookie)
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

const terminalContainer = ref<HTMLDivElement>()
const xtermContainer = ref<HTMLDivElement>()
const rdpContainer = ref<HTMLDivElement>()
const statusText = ref<string>('Connecting...')

// ----------------------------------------------------------------
// Watermark
// ----------------------------------------------------------------
const authStore = useAuthStore()

const watermarkStyle = computed(() => {
  // Username comes from the auth store profile (loaded from /api/auth/profile),
  // not from decoding the JWT (which the SPA no longer holds).
  const username = authStore.user?.username ?? ''
  const displayName = authStore.user?.display_name || username
  // Sanitize against XML injection in SVG
  const safeName = displayName.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&apos;')
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

const {
  isConnected,
  connectionError: sshError,
  connect,
  disconnect,
  sendInput,
  sendResize,
} = useTerminal()

const {
  isConnected: rdpConnected,
  connectionError: rdpError,
  connect: rdpConnect,
  disconnect: rdpDisconnect,
  sendSize: rdpSendSize,
  sendScale: rdpSendScale,
  sendShutdown: rdpSendShutdown,
  getDisplayCanvas: rdpGetDisplayCanvas,
} = useGuacamole()

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
      background: '#1e1e1e',
      foreground: '#d4d4d4',
      cursor: '#d4d4d4',
      selectionBackground: '#264f78',
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
}

function destroyTerminal() {
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
    username: props.sshUsername ?? 'root',
    password: props.sshPassword ?? '',
    credentialId: props.credentialId ?? undefined,
    onOutput: (data: string) => {
      term?.write(data)
    },
    onError: (data: string) => {
      statusText.value = data
      term?.write(`\r\n\x1b[31m-- ${data} --\x1b[0m\r\n`)
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
    onConnected: () => {
      statusText.value = `RDP — ${props.deviceIp}`
      scaleRdpDisplay()
      // Auto-scale when container resizes
      if (rdpContainer.value && !rdpResizeObserver) {
        rdpResizeObserver = new ResizeObserver(() => scaleRdpDisplay())
        rdpResizeObserver.observe(rdpContainer.value)
      }
    },
    onError: (message: string) => {
      statusText.value = message
    },
    onDisconnected: () => {
      statusText.value = 'Disconnected'
      handleRdpDisconnect()
    },
  })
}

function handleRdpDisconnect() {
  // Server-side Guacamole instruction recording handles everything
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
  rdpSendSize(w, h)
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
  background: #1e1e1e;
  border-radius: var(--dcn-radius-md);
  overflow: hidden;
}

/* ---------- Header ---------- */
.terminal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--dcn-space-1) var(--dcn-space-3);
  background: #2d2d2d;
  border-bottom: 1px solid #3c3c3c;
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
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.terminal-conn-info {
  font-size: var(--dcn-text-base);
  font-family: var(--dcn-font-mono);
  color: #d4d4d4;
}

.terminal-status-text {
  font-size: var(--dcn-text-sm);
  color: #808080;
}

.terminal-header-btn {
  color: #a0a4a8 !important;
}

.terminal-header-btn:hover {
  color: #d4d4d4 !important;
}

.power-btn-reboot:hover {
  color: var(--dcn-warning) !important;
}

.power-btn-shutdown:hover {
  color: var(--dcn-danger) !important;
}

.terminal-header :deep(.el-divider--vertical) {
  border-color: #4c4c4c;
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
