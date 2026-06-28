<template>
  <div class="terminal-page">
    <!-- Device Sidebar -->
    <aside v-if="sidebarVisible" class="terminal-sidebar">
      <div class="sidebar-header">
        <span>同机柜设备</span>
        <el-button text size="small" @click="sidebarVisible = false">
          <el-icon><ArrowLeft /></el-icon>
        </el-button>
      </div>
      <div class="sidebar-body" v-loading="siblingLoading">
        <div
          v-for="dev in siblingDevices"
          :key="dev.id"
          class="sidebar-device"
          :class="{ active: dev.id === state.deviceId }"
          @click="connectDevice(dev)"
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
              v-if="dev.os_system === 'linux'"
              text size="small"
              :disabled="dev.status === 'offline'"
              @click.stop="connectDevice(dev, 'ssh')"
            >SSH</el-button>
            <el-button
              v-if="dev.os_system === 'windows'"
              text size="small"
              :disabled="dev.status === 'offline'"
              @click.stop="connectDevice(dev, 'rdp')"
            >RDP</el-button>
          </div>
        </div>
        <div v-if="siblingDevices.length === 0 && !siblingLoading" class="sidebar-empty">
          暂无其他设备
        </div>
      </div>
    </aside>

    <!-- Main terminal area -->
    <div class="terminal-main">
      <Terminal
        ref="terminalRef"
        :key="terminalKey"
        :deviceId="state.deviceId"
        :connType="state.connType"
        :deviceIp="state.deviceIp"
        :sshUsername="state.username"
        :sshPassword="state.password"
        :credentialId="state.credentialId"
        @close="handleClose"
        @fullscreen="toggleFullscreen"
      />
    </div>

    <!-- Toggle sidebar button (when hidden) -->
    <div v-if="!sidebarVisible" class="sidebar-toggle" @click="sidebarVisible = true">
      <el-icon><ArrowRight /></el-icon>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, onMounted } from 'vue'
import { ArrowLeft, ArrowRight } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import Terminal from '@/components/terminal/Terminal.vue'
import { deviceAPI } from '@/api'
import type { Device } from '@/types'

const typeLabels: Record<string, string> = {
  server: '服务器', switch: '交换机', router: '路由器', firewall: '防火墙', host: '主机',
}

const errorMessage = ref('')

// Read URL params
const params = new URLSearchParams(window.location.search)
const deviceId = params.get('deviceId')
const connType = params.get('connType')

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

if (hasCred) {
  const bc = new BroadcastChannel('dcn_terminal_cred')
  const credPromise = new Promise<void>((resolve) => {
    bc.onmessage = (event) => {
      storedUsername = event.data?.username || ''
      storedPassword = event.data?.password || ''
      bc.close()
      resolve()
    }
    setTimeout(() => { bc.close(); resolve() }, 3000)
  })
  credPromise.then(() => {
    state.username = storedUsername
    state.password = storedPassword
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
const sidebarVisible = ref(true)
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

function connectDevice(dev: Device, forceType?: 'ssh' | 'rdp') {
  const type = forceType || (dev.os_system === 'windows' ? 'rdp' : 'ssh')
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
  if (terminalRef.value?.gracefulClose) {
    await terminalRef.value.gracefulClose()
  }
  window.close()
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
  height: 100vh;
  background: #1e1e1e;
  overflow: hidden;
  position: relative;
}

.terminal-sidebar {
  width: 240px;
  min-width: 240px;
  background: #252526;
  border-right: 1px solid #3c3c3c;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--dcn-space-2) var(--dcn-space-3);
  font-size: var(--dcn-text-base);
  font-weight: 600;
  color: #d4d4d4;
  border-bottom: 1px solid #3c3c3c;
  user-select: none;
}

.sidebar-header .el-button {
  color: #808080 !important;
  padding: var(--dcn-radius-xs);
}

.sidebar-header .el-button:hover {
  color: #d4d4d4 !important;
}

.sidebar-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--dcn-space-1);
}

.sidebar-device {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  padding: var(--dcn-space-2);
  border-radius: var(--dcn-radius-sm);
  cursor: pointer;
  transition: background var(--dcn-transition-fast);
  margin-bottom: var(--dcn-radius-xs);
}

.sidebar-device:hover {
  background: #2a2d2e;
}

.sidebar-device.active {
  background: #37373d;
  border: 1px solid var(--dcn-primary);
}

.sidebar-device-info {
  flex: 1;
  min-width: 0;
}

.sidebar-device-name {
  display: block;
  font-size: var(--dcn-text-base);
  color: #d4d4d4;
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
  color: #808080;
}

.sidebar-device-ip {
  font-size: var(--dcn-text-xs);
  color: #606060;
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
  color: #555 !important;
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: var(--dcn-radius-full);
  flex-shrink: 0;
  margin-top: 1px;
}

.status-online { background: var(--dcn-dot-online); }
.status-offline { background: #606266; }
.status-maintenance { background: var(--dcn-dot-maintenance); }

.sidebar-empty {
  text-align: center;
  color: #606060;
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
  width: 20px;
  height: 48px;
  background: #252526;
  border: 1px solid #3c3c3c;
  border-left: none;
  border-radius: 0 var(--dcn-radius-sm) var(--dcn-radius-sm) 0;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: #808080;
  z-index: 10;
}

.sidebar-toggle:hover {
  background: #2a2d2e;
  color: #d4d4d4;
}
</style>
