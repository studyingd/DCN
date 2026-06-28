<template>
  <div class="dashboard">
    <!-- Top Bar -->
    <header class="top-bar">
      <div class="top-bar-left">
        <el-icon :size="24" color="#fff"><Monitor /></el-icon>
        <span class="top-bar-title">DCN 数据中心网络可视化</span>
      </div>
      <div class="top-bar-center">
        <el-popover placement="bottom" :width="420" trigger="focus" :visible="searchVisible" @update:visible="searchVisible = $event">
          <template #reference>
            <el-input v-model="searchQuery" placeholder="搜索设备（IP 或名称）..." prefix-icon="Search" clearable size="default" class="search-input" @input="onSearchInput" @focus="searchVisible = true" @clear="searchResults = []" />
          </template>
          <div v-if="searchResults.length > 0" class="search-results">
            <div v-for="item in searchResults" :key="item.device.id" class="search-result-item" @click="onSearchSelect(item)">
              <div class="search-result-main">
                <span class="status-dot" :class="statusDotClass(item.device.status)" />
                <span class="search-result-name">{{ item.device.name }}</span>
                <el-tag size="small" :type="deviceTypeTagType(item.device.type)">{{ deviceTypeLabel(item.device.type) }}</el-tag>
              </div>
              <div class="search-result-meta">
                <span>{{ item.device.ip_address || '-' }}</span>
                <span class="search-result-path">{{ item.roomName }} / {{ item.rackName }}</span>
              </div>
            </div>
          </div>
          <div v-else-if="searchQuery" class="search-empty"><span>未找到匹配设备</span></div>
          <div v-else class="search-empty"><span>输入设备名称或 IP 地址搜索</span></div>
        </el-popover>
      </div>
      <div class="top-bar-right">
        <el-dropdown trigger="click" @command="onUserCommand">
          <div class="user-avatar-btn">
            <el-icon :size="18"><UserFilled /></el-icon>
            <span class="user-avatar-name">{{ authStore.user?.username || '用户' }}</span>
          </div>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="password">
                <el-icon><Lock /></el-icon>
                修改密码
              </el-dropdown-item>
              <el-dropdown-item divided command="logout">
                <el-icon><SwitchButton /></el-icon>
                退出登录
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>

    <!-- Main Content -->
    <div class="main-content">
      <aside class="left-sidebar" :style="{ width: sidebarCollapsed ? 'var(--dcn-sidebar-collapsed-width)' : 'var(--dcn-sidebar-width)' }">
        <NavTree :active-panel="activePanel" :collapsed="sidebarCollapsed" @nav-panel="onNavPanel" @toggle-collapse="sidebarCollapsed = !sidebarCollapsed" />
      </aside>

      <main class="center-area">
        <HomePanel v-if="activePanel === 'home'" :rooms="roomStore.rooms" @select-room="onSelectRoom" />
        <RoomsPanel v-else-if="activePanel === 'rooms'" :rooms="roomStore.rooms" @select-room="onSelectRoom" @created="roomStore.fetchRooms" />
        <Scene2D v-else-if="activePanel === 'scene'" ref="sceneRef" @device-click="onDeviceClick" @data-changed="onDataChanged" @nav-change="onNavChange" />
        <CredentialPanel v-else-if="activePanel === 'credentials'" @data-changed="onDataChanged" />
        <AuditPanel v-else-if="activePanel === 'audit'" v-model="auditTab" :audit-users="auditUsers" @open-replay="onOpenReplay" />
        <UserManagePanel v-else-if="activePanel === 'users'" :initial-tab="usersTab" @tab-change="onUsersTabChange" />
        <ScriptPanel v-else-if="activePanel === 'scripts'" />
        <InspectionPanel v-else-if="activePanel === 'inspection'" />
      </main>

      <aside v-if="activePanel === 'scene'" class="right-sidebar">
        <DeviceDetail @connect-ssh="openTerminal('ssh')" @connect-rdp="openTerminal('rdp')" />
      </aside>
    </div>

    <!-- Credential Dialog (for terminal connections) -->
    <el-dialog v-model="credDialog.visible" :title="credDialog.connType === 'ssh' ? 'SSH 连接凭据' : 'RDP 连接凭据'" width="420px" :close-on-click-modal="false" @closed="onCredDialogClosed">
      <el-form label-width="90px" @submit.prevent="submitCredentials">
        <el-form-item label="目标主机"><el-input :value="credDialog.deviceIp" disabled /></el-form-item>
        <el-form-item label="用户名"><el-input v-model="credDialog.username" placeholder="请输入用户名" ref="credUsernameRef" /></el-form-item>
        <el-form-item label="密码"><el-input v-model="credDialog.password" type="password" show-password placeholder="请输入密码" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="credDialog.visible = false">取消</el-button>
        <el-button type="primary" @click="submitCredentials">连接</el-button>
      </template>
    </el-dialog>

    <!-- Replay Modal -->
    <ReplayModal ref="replayModalRef" />

    <!-- Change Password Dialog -->
    <el-dialog v-model="changePasswordVisible" title="修改密码" width="400px" :close-on-click-modal="false">
      <el-form label-width="80px" @submit.prevent="submitChangePassword">
        <el-form-item label="原密码"><el-input v-model="changePasswordForm.oldPassword" type="password" show-password placeholder="请输入原密码" /></el-form-item>
        <el-form-item label="新密码"><el-input v-model="changePasswordForm.newPassword" type="password" show-password placeholder="请输入新密码（至少6位）" /></el-form-item>
        <el-form-item label="确认密码"><el-input v-model="changePasswordForm.confirmPassword" type="password" show-password placeholder="再次输入新密码" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="changePasswordVisible = false">取消</el-button>
        <el-button type="primary" :loading="changePasswordLoading" @click="submitChangePassword">确认修改</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Monitor, UserFilled, SwitchButton, Search, Lock } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { useDeviceStore } from '@/stores/device'
import { useRoomStore } from '@/stores/room'
import NavTree from '@/components/panel/NavTree.vue'
import HomePanel from '@/components/panel/HomePanel.vue'
import RoomsPanel from '@/components/panel/RoomsPanel.vue'
import DeviceDetail from '@/components/panel/DeviceDetail.vue'
import Scene2D from '@/components/scene/Scene2D.vue'
import CredentialPanel from '@/components/panel/CredentialPanel.vue'
import AuditPanel from '@/components/panel/AuditPanel.vue'
import UserManagePanel from '@/components/panel/UserManagePanel.vue'
import ScriptPanel from '@/components/panel/ScriptPanel.vue'
import InspectionPanel from '@/components/panel/InspectionPanel.vue'
import ReplayModal from '@/components/panel/ReplayModal.vue'
import { useDeviceMonitor } from '@/composables/useDeviceMonitor'
import { auditAPI, authAPI } from '@/api'
import { ElMessage } from 'element-plus'
import type { Room, Device } from '@/types'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const deviceStore = useDeviceStore()
const roomStore = useRoomStore()
const deviceMonitor = useDeviceMonitor()

const sceneRef = ref<InstanceType<typeof Scene2D>>()
const replayModalRef = ref<InstanceType<typeof ReplayModal>>()

// ---- Sidebar collapse ----
const sidebarCollapsed = ref(false)

// ---- Panel routing ----
const _initPanel = (() => {
  const p = route.query.panel as string | undefined
  if (p && p !== 'scene') return p
  if (route.query.room) return 'scene'
  return 'home'
})()
const activePanel = ref<string>(_initPanel)

const _initAuditTab = (() => {
  const t = route.query.tab as string | undefined
  if (t && ['settings', 'logs', 'recordings', 'scripts', 'logins'].includes(t)) return t
  return 'settings'
})()
const auditTab = ref(_initAuditTab)

const _initUsersTab = (() => {
  const t = route.query.tab as string | undefined
  if (t && ['users', 'roles', 'groups'].includes(t)) return t
  return 'users'
})()
const usersTab = ref(_initUsersTab)

// ---- Audit users (shared across audit tabs) ----
const auditUsers = ref<string[]>([])
async function loadAuditUsers() {
  try { auditUsers.value = (await auditAPI.listAuditUsers()).data } catch { /* ignore */ }
}

// ---- Device search ----
const searchQuery = ref('')
const searchVisible = ref(false)
const searchResults = ref<{ device: Device; roomId: number; roomName: string; rackId: number; rackName: string }[]>([])

function onSearchInput() {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) { searchResults.value = []; return }
  const results: typeof searchResults.value = []
  for (const room of roomStore.rooms) {
    for (const rack of room.racks ?? []) {
      for (const dev of rack.devices ?? []) {
        if (dev.name.toLowerCase().includes(q) || dev.ip_address?.toLowerCase().includes(q)) {
          results.push({ device: dev, roomId: room.id, roomName: room.name, rackId: rack.id, rackName: rack.name })
        }
      }
    }
  }
  searchResults.value = results.slice(0, 20)
}

async function onSearchSelect(item: typeof searchResults.value[0]) {
  searchVisible.value = false
  searchQuery.value = ''
  const room = roomStore.rooms.find(r => r.id === item.roomId)
  if (room) roomStore.setCurrentRoom(room)
  await nextTick()
  sceneRef.value?.openRackById(item.rackId)
  await nextTick()
  deviceStore.setCurrentDevice(item.device)
}

function statusDotClass(status: string): string {
  const s = status?.toLowerCase()
  if (s === 'online') return 'dot-online'
  if (s === 'offline') return 'dot-offline'
  return 'dot-maintenance'
}

function deviceTypeLabel(type: string): string {
  const map: Record<string, string> = { server: '服务器', switch: '交换机', router: '路由器', firewall: '防火墙', host: '主机' }
  return map[type?.toLowerCase()] ?? type
}

function deviceTypeTagType(type: string): string {
  const map: Record<string, string> = { server: 'info', switch: 'primary', router: 'success', firewall: 'danger', host: 'info' }
  return map[type?.toLowerCase()] ?? 'info'
}

// ---- Navigation ----
function onNavChange(state: { roomId?: number; rackId?: number; deviceId?: number }) {
  const query: Record<string, string> = { panel: 'scene' }
  if (state.roomId) query.room = String(state.roomId)
  if (state.rackId) query.rack = String(state.rackId)
  if (state.deviceId) query.device = String(state.deviceId)
  router.replace({ path: '/', query })
}

function onNavPanel(panel: string) {
  activePanel.value = panel
  if (panel === 'scene') return
  roomStore.setCurrentRoom(null)
  if (panel === 'home') {
    router.replace({ path: '/', query: { panel: 'home' } })
  } else if (panel === 'audit') {
    router.replace({ path: '/', query: { panel: 'audit', tab: auditTab.value } })
  } else if (panel === 'users') {
    router.replace({ path: '/', query: { panel: 'users', tab: usersTab.value } })
  } else {
    router.replace({ path: '/', query: { panel } })
  }
}

function onSelectRoom(room: Room) {
  activePanel.value = 'scene'
  roomStore.setCurrentRoom(room)
  router.replace({ path: '/', query: { panel: 'scene', room: String(room.id) } })
}

function onDeviceClick(payload: { device: Device }) { deviceStore.setCurrentDevice(payload.device) }
function onDataChanged() { roomStore.fetchRooms() }

// ---- Terminal credential dialog ----
const credDialog = reactive({ visible: false, connType: 'ssh' as 'ssh' | 'rdp', deviceId: 0, deviceIp: '', rackId: 0, username: '', password: '' })
const credUsernameRef = ref<InstanceType<typeof import('element-plus')['ElInput']>>()

function openTerminal(type: 'ssh' | 'rdp') {
  const device = deviceStore.currentDevice
  if (!device) return
  if (device.credential_id) { openTerminalWithCredential(device, type); return }
  credDialog.connType = type
  credDialog.deviceId = device.id
  credDialog.deviceIp = device.ip_address || ''
  credDialog.rackId = device.rack_id
  credDialog.username = type === 'ssh' ? 'root' : ''
  credDialog.password = ''
  credDialog.visible = true
  nextTick(() => { credUsernameRef.value?.focus() })
}

function openTerminalWithCredential(device: Device, connType: 'ssh' | 'rdp') {
  const query = new URLSearchParams({ deviceId: String(device.id), connType, deviceIp: device.ip_address || '', credentialId: String(device.credential_id), rackId: String(device.rack_id) }).toString()
  window.open(`${router.resolve({ name: 'terminal' }).path}?${query}`, '_blank')
}

function submitCredentials() {
  credDialog.visible = false
  // Use BroadcastChannel for cross-tab credential passing instead of sessionStorage.
  // This avoids persisting plaintext credentials in browser storage.
  const channel = new BroadcastChannel('dcn_terminal_cred')
  const query = new URLSearchParams({
    deviceId: String(credDialog.deviceId),
    connType: credDialog.connType,
    deviceIp: credDialog.deviceIp,
    rackId: String(credDialog.rackId || ''),
    hasCred: '1',
  }).toString()
  const win = window.open(`${router.resolve({ name: 'terminal' }).path}?${query}`, '_blank')
  if (win) {
    // Post credentials to the new window after a short delay to allow it to set up the listener.
    // BroadcastChannel is transient + same-origin and auto-closed; plaintext is
    // NEVER written to persistent storage.
    channel.postMessage({ username: credDialog.username, password: credDialog.password })
    setTimeout(() => channel.close(), 5000)
  } else {
    // Popup blocked: do NOT fall back to sessionStorage (would persist plaintext
    // passwords). Prompt the user to allow popups, or use a stored credential.
    channel.close()
    ElMessage.warning('终端弹窗被拦截，请允许本站弹窗后重试，或使用已保存的凭据连接。')
  }
}

function onCredDialogClosed() { credDialog.username = ''; credDialog.password = '' }

// ---- Replay ----
function onOpenReplay(row: any) { replayModalRef.value?.open(row) }

// ---- Watchers ----
watch(activePanel, (val) => {
  if (val === 'audit') loadAuditUsers()
})

watch(auditTab, (val) => {
  if (activePanel.value === 'audit') router.replace({ path: '/', query: { panel: 'audit', tab: val } })
})

function onUsersTabChange(tab: string) {
  usersTab.value = tab
  if (activePanel.value === 'users') router.replace({ path: '/', query: { panel: 'users', tab } })
}

// ---- Lifecycle ----
onMounted(async () => {
  if (!authStore.user) await authStore.fetchUser()
  await roomStore.fetchRooms()
  deviceMonitor.start()

  if (activePanel.value === 'scene') {
    const roomId = route.query.room ? Number(route.query.room) : null
    const rackId = route.query.rack ? Number(route.query.rack) : null
    const deviceId = route.query.device ? Number(route.query.device) : null
    if (roomId && !isNaN(roomId)) {
      const room = roomStore.rooms.find(r => r.id === roomId)
      if (room) {
        roomStore.setCurrentRoom(room)
        if (rackId) { await nextTick(); sceneRef.value?.openRackById(rackId) }
        if (deviceId) { await nextTick(); const dev = room.racks?.flatMap(r => r.devices ?? []).find(d => d.id === deviceId); if (dev) deviceStore.setCurrentDevice(dev) }
      }
    }
  }
})

onUnmounted(() => { deviceMonitor.stop() })

// ---- User dropdown ----
const changePasswordVisible = ref(false)
const changePasswordForm = reactive({ oldPassword: '', newPassword: '', confirmPassword: '' })
const changePasswordLoading = ref(false)

function onUserCommand(command: string) {
  if (command === 'logout') handleLogout()
  else if (command === 'password') {
    changePasswordForm.oldPassword = ''
    changePasswordForm.newPassword = ''
    changePasswordForm.confirmPassword = ''
    changePasswordVisible.value = true
  }
}

async function submitChangePassword() {
  if (!changePasswordForm.newPassword || !changePasswordForm.oldPassword) return
  if (changePasswordForm.newPassword !== changePasswordForm.confirmPassword) {
    ElMessage.error('两次输入的新密码不一致')
    return
  }
  changePasswordLoading.value = true
  try {
    await authAPI.changePassword(changePasswordForm.oldPassword, changePasswordForm.newPassword)
    ElMessage.success('密码修改成功，请重新登录')
    changePasswordVisible.value = false
    await authStore.logout()
    router.push('/login')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '密码修改失败')
  } finally {
    changePasswordLoading.value = false
  }
}

async function handleLogout() { await authStore.logout(); router.push('/login') }
</script>

<style scoped>
/* Layout-level CSS only: dashboard shell, top-bar, sidebars, search */
.dashboard { display: flex; flex-direction: column; height: 100vh; overflow: hidden; background: var(--dcn-bg-page); }

.top-bar {
  display: flex; align-items: center; justify-content: space-between;
  height: var(--dcn-topbar-height); padding: 0 var(--dcn-space-4);
  background: var(--dcn-topbar-bg); color: var(--dcn-text-primary);
  flex-shrink: 0;
  border-bottom: 1px solid var(--dcn-topbar-border);
  position: relative; z-index: 20;
}

.top-bar-left { display: flex; align-items: center; gap: var(--dcn-space-3); }
.top-bar-title { font-size: var(--dcn-text-lg); font-weight: 600; letter-spacing: -0.01em; color: var(--dcn-text-primary); }
.top-bar-right { display: flex; align-items: center; gap: var(--dcn-space-2); flex-shrink: 0; }
.top-bar-center { flex: 1; display: flex; justify-content: center; padding: 0 var(--dcn-space-4); }
.search-input { max-width: 360px; width: 100%; }
.search-input :deep(.el-input__wrapper) { background: rgba(11, 11, 17, 0.4); border: 1px solid var(--dcn-border); border-radius: var(--dcn-radius-md); box-shadow: none; transition: all var(--dcn-transition-fast); }
.search-input :deep(.el-input__wrapper:hover), .search-input :deep(.el-input__wrapper.is-focus) { background: rgba(11, 11, 17, 0.6); border-color: var(--dcn-border-strong); }
.search-input :deep(.el-input__inner) { color: var(--dcn-text-primary); }
.search-input :deep(.el-input__inner::placeholder) { color: var(--dcn-text-placeholder); }
.search-input :deep(.el-icon) { color: var(--dcn-text-placeholder); }
.search-results { max-height: 360px; overflow-y: auto; }
.search-result-item { display: flex; flex-direction: column; gap: var(--dcn-space-1); padding: 10px var(--dcn-space-3); cursor: pointer; border-radius: var(--dcn-radius-md); transition: background var(--dcn-transition-fast); }
.search-result-item:hover { background: var(--dcn-sidebar-hover-bg); }
.search-result-main { display: flex; align-items: center; gap: var(--dcn-space-2); }
.search-result-name { font-size: var(--dcn-text-md); font-weight: 500; color: var(--dcn-text-primary); }
.search-result-meta { display: flex; gap: var(--dcn-space-3); font-size: var(--dcn-text-sm); color: var(--dcn-text-secondary); padding-left: var(--dcn-space-4); }
.search-result-path { color: var(--dcn-text-placeholder); }
.search-empty { text-align: center; padding: var(--dcn-space-5); color: var(--dcn-text-secondary); font-size: var(--dcn-text-base); }
.status-dot { width: 8px; height: 8px; border-radius: var(--dcn-radius-full); flex-shrink: 0; }
.dot-online { background: var(--dcn-dot-online); }
.dot-offline { background: var(--dcn-dot-offline); }
.dot-maintenance { background: var(--dcn-dot-maintenance); }
.user-avatar-btn {
  display: flex; align-items: center; gap: var(--dcn-space-2);
  padding: 4px var(--dcn-space-3) 4px var(--dcn-space-2);
  border-radius: var(--dcn-radius-pill);
  background: var(--dcn-primary-bg-deep);
  cursor: pointer; transition: all var(--dcn-transition-fast); color: var(--dcn-primary-light);
}
.user-avatar-btn:hover { background: var(--dcn-primary-bg); color: var(--dcn-primary); }
.user-avatar-name { font-size: var(--dcn-text-sm); font-weight: 500; white-space: nowrap; }

.main-content { display: flex; flex: 1; overflow: hidden; }
.left-sidebar { flex-shrink: 0; background: var(--dcn-bg-card); border-right: 1px solid var(--dcn-border); overflow-y: auto; transition: width var(--dcn-transition-normal); }
.center-area { flex: 1; overflow: hidden; position: relative; background: var(--dcn-bg-page); }
.right-sidebar { width: var(--dcn-detail-width); flex-shrink: 0; background: var(--dcn-bg-card); border-left: 1px solid var(--dcn-border); overflow-y: auto; }
</style>
