<template>
  <el-dialog
    v-model="visible"
    :title="`控制台 — ${guestName}`"
    fullscreen
    append-to-body
    class="pve-console-dialog"
    @open="connect"
    @close="disconnect"
  >
    <div class="console-area">
      <div v-if="errorText" class="console-error">
        <el-icon><WarningFilled /></el-icon>
        <div>
          <div class="console-error-title">控制台连接失败</div>
          <div class="console-error-msg">{{ errorText }}</div>
        </div>
      </div>
      <div v-if="connecting && !errorText" class="console-status">正在连接控制台…</div>
      <Terminal
        v-if="mode === 'ssh' || mode === 'rdp'"
        :device-id="guestTargetId"
        :conn-type="mode"
        :device-ip="guestName"
        :ticket="remoteTicket"
        :ws-path="mode === 'ssh' ? '/ws/pve-terminal' : '/ws/pve-rdp'"
        :fixed-rdp-resolution="mode === 'rdp'"
        minimal
        :enable-files="guestTargetId !== 0"
        @connection-error="fallbackToPve"
      />
      <div v-else ref="screenEl" class="console-screen"></div>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { WarningFilled } from '@element-plus/icons-vue'
import RFB from '@novnc/novnc'
import { pveAPI } from '@/api'
import Terminal from '@/components/terminal/Terminal.vue'

const props = defineProps<{
  connId: number | null
  gtype: string
  vmid: number | null
  guestName: string
}>()

const visible = ref(false)
const connecting = ref(false)
const errorText = ref('')
const mode = ref<'pve' | 'ssh' | 'rdp'>('pve')
const remoteTicket = ref('')
const screenEl = ref<HTMLDivElement>()
/** SSH/RDP 模式下虚拟机的合成 target_id；0 表示后端未回传（老版本兼容）。 */
const guestTargetId = ref(0)
let rfb: RFB | null = null
let remoteWindow: Window | null = null

function open() {
  // Reserve the terminal window during the click gesture. This lets the PVE
  // remote session use the same standalone rendering path as room management
  // without being rejected by popup blockers after the async ticket request.
  remoteWindow = window.open('about:blank', '_blank')
  visible.value = true
}

async function connect() {
  errorText.value = ''
  connecting.value = true
  try {
    if (props.connId == null || props.vmid == null) {
      throw new Error('缺少 PVE 连接或 VMID')
    }
    const { width, height } = consoleViewportSize()
    const res = await pveAPI.consoleTicket(props.connId, props.gtype, props.vmid, false, width, height)
    const { ticket, vncticket } = res.data
    mode.value = res.data.mode || 'pve'
    // 后端在 SSH/RDP 模式下回传合成 target_id（负数）。带上它，独立终端窗口里的
    // 文件管理器才能对这台虚拟机走 /api/devices/{id}/files/*，和普通设备一致。
    guestTargetId.value = res.data.target_id ?? 0
    if (mode.value !== 'pve') {
      if (remoteWindow && !remoteWindow.closed) {
        const params = new URLSearchParams({
          deviceId: String(guestTargetId.value),
          connType: mode.value,
          deviceIp: props.guestName,
          pveTicket: ticket,
          pveWsPath: mode.value === 'ssh' ? '/ws/pve-terminal' : '/ws/pve-rdp',
        })
        remoteWindow.location.href = `/terminal?${params.toString()}`
        remoteWindow.opener = null
        remoteWindow = null
        connecting.value = false
        visible.value = false
        return
      }
      remoteTicket.value = ticket
      connecting.value = false
      return
    }
    if (remoteWindow && !remoteWindow.closed) remoteWindow.close()
    remoteWindow = null
    await connectPve(ticket, vncticket || '')
  } catch (e: unknown) {
    if (remoteWindow && !remoteWindow.closed) remoteWindow.close()
    remoteWindow = null
    connecting.value = false
    const err = e as { response?: { data?: { detail?: string } } & Error }
    errorText.value = err.response?.data?.detail || (err as Error).message || '连接失败'
  }
}

async function connectPve(ticket: string, vncticket: string) {
  try {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const url = `${proto}://${window.location.host}/ws/pve-console?ticket=${ticket}`
    if (!screenEl.value) throw new Error('渲染区域未就绪')

    rfb = new RFB(screenEl.value, url, {
      credentials: { password: vncticket },
    })
    rfb.scaleViewport = true
    rfb.resizeSession = true
    rfb.addEventListener('disconnect', (e) => {
      const detail = (e as CustomEvent<{ clean?: boolean }>).detail
      if (!detail?.clean) {
        errorText.value = '控制台连接意外断开'
      }
    })
    rfb.addEventListener('credentialsrequired', () => {
      rfb?.sendCredentials({ password: vncticket })
    })
    connecting.value = false
  } catch (e: unknown) {
    connecting.value = false
    const err = e as { response?: { data?: { detail?: string } } & Error }
    errorText.value = err.response?.data?.detail || (err as Error).message || '连接失败'
  }
}

async function fallbackToPve(_message?: string) {
  if (mode.value === 'pve' || props.connId == null || props.vmid == null) return
  mode.value = 'pve'
  remoteTicket.value = ''
  errorText.value = ''
  connecting.value = true
  try {
    await nextTick()
    const { width, height } = consoleViewportSize()
    const res = await pveAPI.consoleTicket(props.connId, props.gtype, props.vmid, true, width, height)
    await connectPve(res.data.ticket, res.data.vncticket || '')
  } catch (e: any) {
    connecting.value = false
    errorText.value = e?.response?.data?.detail || 'PVE 控制台连接失败'
  }
}

function consoleViewportSize() {
  // Remote SSH/RDP is opened in the standalone TerminalView. Its minimal
  // header is roughly one compact row, not the 96px used by the embedded PVE
  // dialog. Matching that actual content ratio prevents Guacamole letterbox
  // space (the black strip that otherwise appears below the desktop).
  const terminalHeaderHeight = 34
  return {
    width: Math.max(320, Math.min(Math.round(window.innerWidth), 7680)),
    height: Math.max(240, Math.min(Math.round(window.innerHeight - terminalHeaderHeight), 4320)),
  }
}

function disconnect() {
  mode.value = 'pve'
  remoteTicket.value = ''
  guestTargetId.value = 0
  if (remoteWindow && !remoteWindow.closed) remoteWindow.close()
  remoteWindow = null
  if (rfb) {
    try {
      rfb.disconnect()
    } catch {
      /* ignore */
    }
    rfb = null
  }
}

defineExpose({ open })
</script>

<style scoped>
.console-area {
  position: relative;
  width: 100%;
  height: calc(100vh - 96px);
  background: var(--dcn-bg-page);
  overflow: hidden;
}
.console-screen {
  width: 100%;
  height: 100%;
}
.console-screen :deep(canvas) {
  display: block;
  margin: 0 auto;
}
.console-status {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--dcn-text-secondary);
  font-size: 14px;
  z-index: 1;
}
.console-error {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--dcn-danger);
  z-index: 2;
  background: rgba(8, 13, 23, 0.78);
}
.console-error .el-icon {
  font-size: 28px;
}
.console-error-title {
  font-size: 15px;
  font-weight: 600;
}
.console-error-msg {
  font-size: 13px;
  color: var(--dcn-danger-text);
  margin-top: 4px;
}
</style>
