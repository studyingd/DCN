<template>
  <div class="terminal-page" data-theme="dark">
    <!-- Device Sidebar -->
    <aside v-if="sidebarVisible" class="terminal-sidebar">
      <div class="sidebar-header">
        <span>同机柜设备</span>
        <el-button text size="small" aria-label="收起设备侧栏" @click="sidebarVisible = false">
          <el-icon><ArrowLeft /></el-icon>
        </el-button>
      </div>
      <div v-loading="siblingLoading" class="sidebar-body">
        <div
          v-for="dev in siblingDevices"
          :key="dev.id"
          class="sidebar-device"
          :class="{ active: dev.id === state.deviceId }"
          role="button"
          tabindex="0"
          :aria-label="`连接设备 ${dev.name}`"
          @click="connectDevice(dev)"
          @keydown.enter="connectDevice(dev)"
          @keydown.space.prevent="connectDevice(dev)"
        >
          <span class="status-dot" :class="'status-' + dev.status" />
          <div class="sidebar-device-info">
            <span class="sidebar-device-name">{{ dev.name }}</span>
            <span class="sidebar-device-meta">
              <span class="sidebar-device-type">{{ typeLabels[dev.type] || dev.type }}</span>
              <span class="sidebar-device-ip">{{ dev.ip_address || '-' }}</span>
            </span>
          </div>
          <div class="sidebar-device-actions">
            <el-button
              v-if="isWindowsOs(dev.os_system)"
              text
              size="small"
              :disabled="dev.status === 'offline'"
              @click.stop="connectDevice(dev, 'rdp')"
              >RDP</el-button
            >
            <el-button
              v-else
              text
              size="small"
              :disabled="dev.status === 'offline'"
              @click.stop="connectDevice(dev, 'ssh')"
              >SSH</el-button
            >
          </div>
        </div>
        <div v-if="siblingDevices.length === 0 && !siblingLoading" class="sidebar-empty">暂无其他设备</div>
      </div>
    </aside>

    <!-- Main terminal area -->
    <div class="terminal-main">
      <Terminal
        v-if="credReady"
        ref="terminalRef"
        :key="terminalKey"
        :device-id="state.deviceId"
        :conn-type="state.connType"
        :device-ip="state.deviceIp"
        :ssh-username="state.username"
        :ssh-password="state.password"
        :credential-id="state.credentialId"
        :container="containerName"
        :ticket="pveTicket"
        :ws-path="pveWsPath"
        :fixed-rdp-resolution="isPveRemote"
        :minimal="isPveRemote"
        :enable-files="isPveRemote ? state.deviceId !== 0 : undefined"
        @close="handleClose"
        @fullscreen="toggleFullscreen"
      />
    </div>

    <!-- Toggle sidebar button (when hidden) -->
    <button
      v-if="!sidebarVisible && !isPveRemote"
      type="button"
      class="sidebar-toggle"
      aria-label="展开设备侧栏"
      @click="sidebarVisible = true"
    >
      <el-icon><ArrowRight /></el-icon>
    </button>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowLeft, ArrowRight } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import Terminal from '@/components/terminal/Terminal.vue'
import { deviceAPI } from '@/api'
import type { Device } from '@/types'

const typeLabels: Record<string, string> = {
  server: '服务器',
  cloud_server: '云服务器',
  host: '台式主机',
}

const errorMessage = ref('')
const router = useRouter()
const closing = ref(false)

// Read URL params
const params = new URLSearchParams(window.location.search)
const deviceId = params.get('deviceId')
const connType = params.get('connType')
const containerName = params.get('container') || undefined
const pveTicket = params.get('pveTicket') || undefined
const pveWsPath = params.get('pveWsPath') || undefined
const isPveRemote = !!pveTicket && !!pveWsPath

if (!deviceId || !connType) {
  // Render an error message instead of calling window.close(), which is silently
  // ignored when the window wasn't opened by script.
  errorMessage.value = '缺少必要参数 (deviceId, connType)'
}

// Receive ad-hoc credentials via BroadcastChannel (transient, same-origin,
// auto-closed by the opener). Plaintext credentials are NEVER written to
// persistent browser storage; if the channel times out, only a stored
// credential_id is used (resolved server-side via the terminal ticket).
let storedUsername = ''
let storedPassword = ''
const hasCred = params.get('hasCred') === '1'
// hasCred 时凭据经 BroadcastChannel 异步送达;门控 Terminal 的挂载,
// 否则 openConnection 会带着空 username/password 发出首张票据请求。
const credReady = ref(!hasCred)

if (hasCred) {
  const bc = new BroadcastChannel('dcn_terminal_cred')
  const credPromise = new Promise<void>((resolve) => {
    bc.onmessage = (event) => {
      storedUsername = event.data?.username || ''
      storedPassword = event.data?.password || ''
      bc.close()
      resolve()
    }
    setTimeout(() => {
      bc.close()
      resolve()
    }, 3000)
  })
  credPromise.then(() => {
    state.username = storedUsername
    state.password = storedPassword
    credReady.value = true
  })
}

const state = reactive({
  deviceId: Number(deviceId || 0),
  connType: (connType || 'ssh') as 'ssh' | 'rdp',
  deviceIp: params.get('deviceIp') || '',
  rackId: Number(params.get('rackId') || 0),
  username: storedUsername,
  password: storedPassword,
  credentialId: params.get('credentialId') ? Number(params.get('credentialId')) : null,
})

const terminalKey = ref(0)

// Sidebar state
const sidebarVisible = ref(!isPveRemote)
const siblingDevices = ref<Device[]>([])
const siblingLoading = ref(false)

async function loadSiblings() {
  if (!state.rackId) return
  siblingLoading.value = true
  try {
    const res = await deviceAPI.list(state.rackId)
    siblingDevices.value = res.data
  } catch {
    // silently fail — sidebar is not critical
  } finally {
    siblingLoading.value = false
  }
}

// os_system 可能是精确版本名(如 "Microsoft Windows Server 2019 Standard"、
// "Windows (SMB 2.x+)"),必须包含匹配——等值判断会漏判并错选协议。
function isWindowsOs(os: string | null | undefined): boolean {
  return (os || '').toLowerCase().includes('windows')
}

function connectDevice(dev: Device, forceType?: 'ssh' | 'rdp') {
  const type = forceType || (isWindowsOs(dev.os_system) ? 'rdp' : 'ssh')
  if (dev.id === state.deviceId && type === state.connType) return

  state.deviceId = dev.id
  state.connType = type
  state.deviceIp = dev.ip_address || ''
  state.credentialId = dev.credential_id ?? null
  state.username = ''
  state.password = ''
  terminalKey.value++

  const query = new URLSearchParams({
    deviceId: String(dev.id),
    connType: type,
    deviceIp: dev.ip_address || '',
    rackId: String(state.rackId),
  })
  if (dev.credential_id) {
    query.set('credentialId', String(dev.credential_id))
  }
  history.replaceState(null, '', `${window.location.pathname}?${query}`)
}

const terminalRef = ref<InstanceType<typeof Terminal> | null>(null)

async function handleClose() {
  if (closing.value) return
  closing.value = true

  try {
    if (terminalRef.value?.gracefulClose) {
      await terminalRef.value.gracefulClose()
    }
    ElMessage.success('会话已关闭')
  } catch {
    ElMessage.warning('会话正在关闭')
  }

  window.close()
  window.setTimeout(() => {
    if (!window.closed) {
      document.title = 'DCN - 数据中心网络可视化'
      router.replace('/')
    }
  }, 150)
}

function toggleFullscreen() {
  if (document.fullscreenElement) {
    document.exitFullscreen()
  } else {
    document.documentElement.requestFullscreen()
  }
}

onMounted(loadSiblings)
</script>

<style scoped>
.terminal-page {
  display: flex;
  width: 100vw;
  height: 100dvh;
  background: var(--dcn-bg-page);
  overflow: hidden;
  position: relative;
}

.terminal-sidebar {
  width: 264px;
  min-width: 264px;
  background: var(--dcn-bg-section);
  border-right: 1px solid var(--dcn-border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 52px;
  padding: var(--dcn-space-2) var(--dcn-space-4);
  font-size: var(--dcn-text-base);
  font-weight: 600;
  color: var(--dcn-text-primary);
  border-bottom: 1px solid var(--dcn-border);
  user-select: none;
}

.sidebar-header .el-button {
  color: var(--dcn-text-secondary) !important;
  padding: var(--dcn-radius-xs);
}

.sidebar-header .el-button:hover {
  color: var(--dcn-text-primary) !important;
}

.sidebar-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--dcn-space-2);
}

.sidebar-device {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  min-height: 52px;
  padding: var(--dcn-space-2) var(--dcn-space-3);
  border: 1px solid transparent;
  border-radius: var(--dcn-radius-lg);
  cursor: pointer;
  transition: background var(--dcn-transition-fast);
  margin-bottom: var(--dcn-space-1);
}

.sidebar-device:hover {
  background: var(--dcn-bg-muted);
}
.sidebar-device:focus-visible {
  outline: 2px solid var(--dcn-primary-light);
  outline-offset: 2px;
}

.sidebar-device.active {
  background: var(--dcn-primary-bg);
  border-color: rgba(96, 165, 250, 0.34);
}

.sidebar-device-info {
  flex: 1;
  min-width: 0;
}

.sidebar-device-name {
  display: block;
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sidebar-device-meta {
  display: flex;
  gap: var(--dcn-space-2);
  margin-top: var(--dcn-radius-xs);
}

.sidebar-device-type {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-secondary);
}

.sidebar-device-ip {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
  font-family: var(--dcn-font-mono);
}

.sidebar-device-actions .el-button {
  color: var(--dcn-primary) !important;
  font-size: var(--dcn-text-sm);
  padding: var(--dcn-radius-xs) var(--dcn-space-1);
}

.sidebar-device-actions .el-button:hover {
  color: var(--dcn-primary-light) !important;
}

.sidebar-device-actions .el-button:disabled {
  color: var(--el-text-color-disabled) !important;
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: var(--dcn-radius-full);
  flex-shrink: 0;
  margin-top: 1px;
}

.status-online {
  background: var(--dcn-dot-online);
}
.status-offline {
  background: var(--dcn-dot-offline);
}
.status-maintenance {
  background: var(--dcn-dot-maintenance);
}

.sidebar-empty {
  text-align: center;
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-base);
  padding: var(--dcn-space-6) 0;
}

.terminal-main {
  flex: 1;
  min-width: 0;
}

.sidebar-toggle {
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 26px;
  height: 48px;
  background: var(--dcn-bg-section);
  border: 1px solid var(--dcn-border);
  border-left: none;
  border-radius: 0 var(--dcn-radius-sm) var(--dcn-radius-sm) 0;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: var(--dcn-text-secondary);
  z-index: 10;
  padding: 0;
}

.sidebar-toggle:hover {
  background: var(--dcn-bg-muted);
  color: var(--dcn-text-primary);
}
</style>
