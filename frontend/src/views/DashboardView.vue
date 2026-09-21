<template>
  <div class="dashboard">
    <!-- Top Bar -->
    <header class="top-bar">
      <div class="top-bar-left">
        <span class="brand-mark"
          ><el-icon :size="20"><Monitor /></el-icon
        ></span>
        <span class="brand-copy">
          <strong class="top-bar-title">DCN</strong>
          <small>数据中心运维控制台</small>
        </span>
      </div>
      <div class="top-bar-center">
        <el-popover
          placement="bottom"
          :width="420"
          trigger="focus"
          :visible="searchVisible"
          @update:visible="searchVisible = $event"
        >
          <template #reference>
            <el-input
              v-model="searchQuery"
              placeholder="搜索设备 / 虚拟机（IP 或名称）..."
              prefix-icon="Search"
              clearable
              size="default"
              class="search-input"
              @input="onSearchInput"
              @focus="searchVisible = true"
              @clear="resetSearch"
            />
          </template>
          <div v-if="searchLoading" class="search-empty"><span>搜索中…</span></div>
          <div v-else-if="searchDevices.length || searchGuests.length" class="search-results">
            <template v-if="searchDevices.length">
              <div class="search-group-title">设备</div>
              <button
                v-for="dev in searchDevices"
                :key="`dev-${dev.id}`"
                type="button"
                class="search-result-item"
                @click="onSearchSelectDevice(dev)"
              >
                <div class="search-result-main">
                  <span class="status-dot" :class="statusDotClass(dev.status)" />
                  <span class="search-result-name">{{ dev.name }}</span>
                  <el-tag size="small" :type="deviceTypeTagType(dev.type)">{{ deviceTypeLabel(dev.type) }}</el-tag>
                </div>
                <div class="search-result-meta">
                  <span>{{ dev.ip_address || '-' }}</span>
                  <span class="search-result-path">{{ dev.room_name }} / {{ dev.rack_name }}</span>
                </div>
              </button>
            </template>
            <template v-if="searchGuests.length">
              <div class="search-group-title">虚拟机</div>
              <button
                v-for="g in searchGuests"
                :key="`vm-${g.connection_id}-${g.vmid}`"
                type="button"
                class="search-result-item"
                @click="onSearchSelectGuest(g)"
              >
                <div class="search-result-main">
                  <span class="status-dot" :class="guestStatusClass(g.status)" />
                  <span class="search-result-name">{{ g.name }}</span>
                  <el-tag size="small" type="success">VM</el-tag>
                </div>
                <div class="search-result-meta">
                  <span>{{ g.ip_address || g.last_known_ip || '-' }}</span>
                  <span class="search-result-path"
                    >{{ g.connection_name }}<template v-if="g.node"> / {{ g.node }}</template></span
                  >
                </div>
              </button>
            </template>
          </div>
          <div v-else-if="searchQuery" class="search-empty"><span>未找到匹配的设备或虚拟机</span></div>
          <div v-else class="search-empty"><span>输入名称或 IP 地址，同时搜索设备与虚拟机</span></div>
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
      <aside
        class="left-sidebar"
        :style="{ width: sidebarCollapsed ? 'var(--dcn-sidebar-collapsed-width)' : 'var(--dcn-sidebar-width)' }"
      >
        <NavTree
          :active-panel="activeNavKey"
          :collapsed="sidebarCollapsed"
          @nav-panel="onNavPanel"
          @toggle-collapse="sidebarCollapsed = !sidebarCollapsed"
        />
      </aside>

      <main class="center-area">
        <HomePanel v-if="activePanel === 'home'" @navigate="onNavPanel" />
        <MetricsPanel v-else-if="activePanel === 'metrics'" />
        <RoomsPanel
          v-else-if="activePanel === 'rooms'"
          :rooms="roomStore.rooms"
          @select-room="onSelectRoom"
          @created="roomStore.fetchRooms"
        />
        <ContainersPanel v-else-if="activePanel === 'containers'" />
        <PvePanel v-else-if="activePanel === 'pve'" :focus-guest="pveFocus" @focused="pveFocus = null" />
        <BusinessPanel v-else-if="activePanel === 'business'" />
        <AlertCenter v-else-if="activePanel === 'alerts'" />
        <Scene2D
          v-else-if="activePanel === 'scene'"
          ref="sceneRef"
          @device-click="onDeviceClick"
          @data-changed="onDataChanged"
          @nav-change="onNavChange"
        />
        <SystemPanel
          v-else-if="activePanel === 'system'"
          :initial-users-tab="usersTab"
          :tab="systemTab"
          @users-tab-change="onUsersTabChange"
        />
        <AutomationPanel v-else-if="activePanel === 'automation'" :tab="automationTab" />
      </main>

      <aside v-if="activePanel === 'scene'" class="right-sidebar">
        <DeviceDetail
          @connect-ssh="openTerminal('ssh')"
          @connect-rdp="openTerminal('rdp')"
          @open-metrics="onOpenMetrics"
        />
      </aside>
    </div>

    <!-- Credential Dialog (for terminal connections) -->
    <el-dialog
      v-model="credDialog.visible"
      :title="credDialog.connType === 'ssh' ? 'SSH 连接凭据' : 'RDP 连接凭据'"
      width="420px"
      :close-on-click-modal="false"
      @closed="onCredDialogClosed"
    >
      <el-form label-width="90px" @submit.prevent="submitCredentials">
        <el-form-item label="目标主机"><el-input :value="credDialog.deviceIp" disabled /></el-form-item>
        <el-form-item label="用户名"
          ><el-input ref="credUsernameRef" v-model="credDialog.username" placeholder="请输入用户名"
        /></el-form-item>
        <el-form-item label="密码"
          ><el-input v-model="credDialog.password" type="password" show-password placeholder="请输入密码"
        /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="credDialog.visible = false">取消</el-button>
        <el-button type="primary" @click="submitCredentials">连接</el-button>
      </template>
    </el-dialog>

    <!-- Change Password Dialog -->
    <el-dialog v-model="changePasswordVisible" title="修改密码" width="400px" :close-on-click-modal="false">
      <el-form label-width="80px" @submit.prevent="submitChangePassword">
        <el-form-item label="原密码"
          ><el-input v-model="changePasswordForm.oldPassword" type="password" show-password placeholder="请输入原密码"
        /></el-form-item>
        <el-form-item label="新密码"
          ><el-input
            v-model="changePasswordForm.newPassword"
            type="password"
            show-password
            placeholder="至少8位，包含至少3类字符"
        /></el-form-item>
        <el-form-item label="确认密码"
          ><el-input
            v-model="changePasswordForm.confirmPassword"
            type="password"
            show-password
            placeholder="再次输入新密码"
        /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="changePasswordVisible = false">取消</el-button>
        <el-button type="primary" :loading="changePasswordLoading" @click="submitChangePassword">确认修改</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, nextTick, defineAsyncComponent } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Monitor, UserFilled, SwitchButton, Lock } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { useDeviceStore } from '@/stores/device'
import { useRoomStore } from '@/stores/room'
import { deviceTypeLabel } from '@/utils/deviceLabels'
import NavTree from '@/components/panel/NavTree.vue'
const HomePanel = defineAsyncComponent(() => import('@/components/panel/HomePanel.vue'))
const MetricsPanel = defineAsyncComponent(() => import('@/components/panel/MetricsPanel.vue'))
const RoomsPanel = defineAsyncComponent(() => import('@/components/panel/RoomsPanel.vue'))
const DeviceDetail = defineAsyncComponent(() => import('@/components/panel/DeviceDetail.vue'))
const Scene2D = defineAsyncComponent(() => import('@/components/scene/Scene2D.vue'))
const SystemPanel = defineAsyncComponent(() => import('@/components/panel/SystemPanel.vue'))
const AutomationPanel = defineAsyncComponent(() => import('@/components/panel/AutomationPanel.vue'))
const ContainersPanel = defineAsyncComponent(() => import('@/components/panel/ContainersPanel.vue'))
const BusinessPanel = defineAsyncComponent(() => import('@/components/panel/BusinessPanel.vue'))
const AlertCenter = defineAsyncComponent(() => import('@/components/panel/AlertCenter.vue'))
const PvePanel = defineAsyncComponent(() => import('@/components/panel/PvePanel.vue'))
import { useDeviceMonitor } from '@/composables/useDeviceMonitor'
import { authAPI, searchAPI } from '@/api'
import { ElMessage } from 'element-plus'
import type { Room, Device } from '@/types'
import type { SearchDeviceItem, SearchGuestItem } from '@/types/search'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const deviceStore = useDeviceStore()
const roomStore = useRoomStore()
const deviceMonitor = useDeviceMonitor()

const sceneRef = ref<InstanceType<typeof Scene2D>>()

// ---- Sidebar collapse ----
const sidebarCollapsed = ref(false)

// ---- Panel routing ----
// 侧边栏子项为复合键(面板:页签),两个面板的激活页签在此持有
// 直接进入“自动化运维”时应展示运维概览；只有旧入口显式携带
// agent/scripts/inspection 页签时，才由 AutomationPanel 切到新建任务。
const automationTab = ref('overview')
const systemTab = ref('overview')

const _initPanel = (() => {
  const p = route.query.panel as string | undefined
  const t = route.query.tab as string | undefined
  // 旧入口映射:scripts/inspection → 自动化执行对应页签;users → 系统管理
  if (p === 'scripts') {
    automationTab.value = 'scripts'
    return 'automation'
  }
  if (p === 'inspection') {
    automationTab.value = 'inspection'
    return 'automation'
  }
  if (p === 'users') {
    systemTab.value = 'users'
    return 'system'
  }
  if (p === 'credentials') return 'rooms'
  if (p === 'automation') {
    if (t && ['agent', 'scripts', 'inspection'].includes(t)) automationTab.value = t
    return 'automation'
  }
  if (p === 'system') {
    systemTab.value = t === 'agent' ? 'agent' : t === 'webhooks' ? 'webhooks' : t === 'users' ? 'users' : 'overview'
    return 'system'
  }
  if (p && ['home', 'metrics', 'rooms', 'containers', 'pve', 'business', 'alerts', 'scene'].includes(p)) return p
  if (route.query.room) return 'scene'
  return 'home'
})()
const activePanel = ref<string>(_initPanel)

// 传给 NavTree 的激活键:子项为 面板:页签,普通项为面板名
const activeNavKey = computed(() => {
  if (activePanel.value === 'automation') return 'automation'
  if (activePanel.value === 'system') return 'system'
  return activePanel.value
})

const _initUsersTab = (() => {
  const t = route.query.tab as string | undefined
  if (t && ['users', 'roles'].includes(t)) return t
  return 'users'
})()
const usersTab = ref(_initUsersTab)

// ---- Global search（设备 + PVE 虚拟机）----
// 旧实现只遍历 roomStore 的机房/机柜/设备树，虚机永远搜不到；现在改为
// 防抖调后端 /api/search（ACL/权限在服务端收口），结果分「设备/虚拟机」两组。
const searchQuery = ref('')
const searchVisible = ref(false)
const searchLoading = ref(false)
const searchDevices = ref<SearchDeviceItem[]>([])
const searchGuests = ref<SearchGuestItem[]>([])
// 顶栏搜索选中虚机后定位到 PvePanel（切连接 + 打开详情抽屉）
const pveFocus = ref<{ connectionId: number; vmid: number } | null>(null)
let searchTimer: number | undefined
let searchSeq = 0

function resetSearch() {
  searchQuery.value = ''
  searchDevices.value = []
  searchGuests.value = []
  searchLoading.value = false
  if (searchTimer) {
    window.clearTimeout(searchTimer)
    searchTimer = undefined
  }
}

function onSearchInput() {
  if (searchTimer) window.clearTimeout(searchTimer)
  const q = searchQuery.value.trim()
  if (!q) {
    searchDevices.value = []
    searchGuests.value = []
    searchLoading.value = false
    return
  }
  searchTimer = window.setTimeout(() => void runSearch(q), 300)
}

async function runSearch(q: string) {
  // 序号守卫：快速输入时只采纳最后一次请求的结果
  const seq = ++searchSeq
  searchLoading.value = true
  try {
    const res = await searchAPI.global(q)
    if (seq !== searchSeq) return
    searchDevices.value = res.data.devices
    searchGuests.value = res.data.guests
  } catch {
    if (seq === searchSeq) {
      searchDevices.value = []
      searchGuests.value = []
    }
  } finally {
    if (seq === searchSeq) searchLoading.value = false
  }
}

async function onSearchSelectDevice(dev: SearchDeviceItem) {
  searchVisible.value = false
  resetSearch()
  // 旧实现不切面板：从非 scene 面板搜索选中设备时 sceneRef 为空，
  // openRackById 静默失败；现在统一切到机房 2D 场景再定位。
  activePanel.value = 'scene'
  let room = roomStore.rooms.find((r) => r.id === dev.room_id)
  if (!room) {
    await roomStore.fetchRooms()
    room = roomStore.rooms.find((r) => r.id === dev.room_id)
  }
  if (room) roomStore.setCurrentRoom(room)
  await nextTick()
  sceneRef.value?.openRackById(dev.rack_id)
  router.replace({
    path: '/',
    query: { panel: 'scene', room: String(dev.room_id), rack: String(dev.rack_id), device: String(dev.id) },
  })
  await nextTick()
  const full = room?.racks?.flatMap((r) => r.devices ?? []).find((d) => d.id === dev.id)
  if (full) deviceStore.setCurrentDevice(full)
}

function onSearchSelectGuest(g: SearchGuestItem) {
  searchVisible.value = false
  resetSearch()
  onNavPanel('pve')
  pveFocus.value = { connectionId: g.connection_id, vmid: g.vmid }
}

function guestStatusClass(status: string): string {
  return status === 'running' ? 'dot-online' : 'dot-offline'
}

function statusDotClass(status: string): string {
  const s = status?.toLowerCase()
  if (s === 'online') return 'dot-online'
  if (s === 'offline') return 'dot-offline'
  return 'dot-offline'
}

function deviceTypeTagType(type: string): string {
  const map: Record<string, string> = {
    server: 'info',
    cloud_server: 'primary',
    host: 'info',
  }
  const key = type?.toLowerCase()
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : 'info'
}

// ---- Navigation ----
function onNavChange(state: { roomId?: number; rackId?: number; deviceId?: number }) {
  const query: Record<string, string> = { panel: 'scene' }
  if (state.roomId) query.room = String(state.roomId)
  if (state.rackId) query.rack = String(state.rackId)
  if (state.deviceId) query.device = String(state.deviceId)
  router.replace({ path: '/', query })
}

function onNavPanel(raw: string) {
  // 复合键(面板:页签)拆解开,面板切换 + 页签定位
  const [panel, tab] = raw.split(':')
  activePanel.value = panel
  if (panel === 'automation') automationTab.value = tab || 'overview'
  if (panel === 'system' && tab) systemTab.value = tab
  if (panel === 'scene') return
  roomStore.setCurrentRoom(null)
  const query: Record<string, string> = { panel }
  if (tab) query.tab = tab
  router.replace({ path: '/', query })
}

function onSelectRoom(room: Room) {
  activePanel.value = 'scene'
  roomStore.setCurrentRoom(room)
  router.replace({ path: '/', query: { panel: 'scene', room: String(room.id) } })
}

function onDeviceClick(payload: { device: Device }) {
  deviceStore.setCurrentDevice(payload.device)
}
function onDataChanged() {
  roomStore.fetchRooms()
}

// U 位图详情面板的"监控详情"——切到服务器监控面板并带上设备 id(自动开抽屉)。
// 先更新 URL 再切面板,保证 MetricsPanel 挂载时能读到 device 参数。
async function onOpenMetrics(deviceId: number) {
  roomStore.setCurrentRoom(null)
  await router.replace({ path: '/', query: { panel: 'metrics', device: String(deviceId) } })
  activePanel.value = 'metrics'
}

// ---- Terminal credential dialog ----
const credDialog = reactive({
  visible: false,
  connType: 'ssh' as 'ssh' | 'rdp',
  deviceId: 0,
  deviceIp: '',
  rackId: 0,
  username: '',
  password: '',
})
const credUsernameRef = ref<InstanceType<(typeof import('element-plus'))['ElInput']>>()

function openTerminal(type: 'ssh' | 'rdp') {
  const device = deviceStore.currentDevice
  if (!device) return
  // Device credentials are now stored directly on the device. Older records
  // may still expose a legacy credential_id, but the reliable flag is
  // has_credential; the backend resolves the encrypted secret server-side.
  if (device.has_credential || !!device.credential_username) {
    openTerminalWithCredential(device, type)
    return
  }
  credDialog.connType = type
  credDialog.deviceId = device.id
  credDialog.deviceIp = device.ip_address || ''
  credDialog.rackId = device.rack_id
  credDialog.username = type === 'ssh' ? 'root' : ''
  credDialog.password = ''
  credDialog.visible = true
  nextTick(() => {
    credUsernameRef.value?.focus()
  })
}

function openTerminalWithCredential(device: Device, connType: 'ssh' | 'rdp') {
  const params = new URLSearchParams({
    deviceId: String(device.id),
    connType,
    deviceIp: device.ip_address || '',
    rackId: String(device.rack_id),
  })
  if (device.credential_id) params.set('credentialId', String(device.credential_id))
  const query = params.toString()
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

function onCredDialogClosed() {
  credDialog.username = ''
  credDialog.password = ''
}

// ---- Watchers ----
function onUsersTabChange(tab: string) {
  usersTab.value = tab
  if (activePanel.value === 'system') router.replace({ path: '/', query: { panel: 'system', tab } })
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
      const room = roomStore.rooms.find((r) => r.id === roomId)
      if (room) {
        roomStore.setCurrentRoom(room)
        if (rackId) {
          await nextTick()
          sceneRef.value?.openRackById(rackId)
        }
        if (deviceId) {
          await nextTick()
          const dev = room.racks?.flatMap((r) => r.devices ?? []).find((d) => d.id === deviceId)
          if (dev) deviceStore.setCurrentDevice(dev)
        }
      }
    }
  }
})

onUnmounted(() => {
  deviceMonitor.stop()
  if (searchTimer) window.clearTimeout(searchTimer)
})

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

async function handleLogout() {
  await authStore.logout()
  router.push('/login')
}
</script>

<style scoped>
/* Layout-level CSS only: dashboard shell, top-bar, sidebars, search */
.dashboard {
  display: flex;
  flex-direction: column;
  height: 100dvh;
  overflow: hidden;
  background: var(--dcn-bg-page);
}

.top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--dcn-topbar-height);
  padding: 0 var(--dcn-space-4);
  background: var(--dcn-topbar-bg);
  color: var(--dcn-text-primary);
  flex-shrink: 0;
  border-bottom: 1px solid var(--dcn-topbar-border);
  position: relative;
  z-index: 20;
  backdrop-filter: blur(14px);
}

.top-bar-left {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
}
.brand-mark {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border: 1px solid rgba(96, 165, 250, 0.34);
  border-radius: var(--dcn-radius-lg);
  background: linear-gradient(145deg, rgba(59, 130, 246, 0.28), rgba(37, 99, 235, 0.12));
  color: var(--dcn-primary-light);
  box-shadow: inset 0 1px rgba(255, 255, 255, 0.05);
}
.brand-copy {
  display: flex;
  flex-direction: column;
  line-height: 1.15;
}
.brand-copy small {
  margin-top: 3px;
  color: var(--dcn-text-placeholder);
  font-size: 10px;
  letter-spacing: 0.6px;
}
.top-bar-title {
  font: 600 var(--dcn-text-lg) var(--dcn-font-mono);
  letter-spacing: 0.12em;
  color: var(--dcn-text-primary);
}
.top-bar-right {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  flex-shrink: 0;
}
.top-bar-center {
  flex: 1;
  display: flex;
  justify-content: center;
  padding: 0 var(--dcn-space-4);
}
.search-input {
  max-width: 440px;
  width: 100%;
}
.search-input :deep(.el-input__wrapper) {
  background: var(--dcn-input-bg);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  box-shadow: none;
  transition: all var(--dcn-transition-fast);
}
.search-input :deep(.el-input__wrapper:hover),
.search-input :deep(.el-input__wrapper.is-focus) {
  background: var(--dcn-input-bg-hover);
  border-color: var(--dcn-border-strong);
}
.search-input :deep(.el-input__inner) {
  color: var(--dcn-text-primary);
}
.search-input :deep(.el-input__inner::placeholder) {
  color: var(--dcn-text-placeholder);
}
.search-input :deep(.el-icon) {
  color: var(--dcn-text-placeholder);
}
.search-results {
  max-height: 360px;
  overflow-y: auto;
}
.search-group-title {
  padding: var(--dcn-space-2) var(--dcn-space-3) var(--dcn-space-1);
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
  letter-spacing: 0.05em;
}
.search-result-item {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-1);
  width: 100%;
  padding: 10px var(--dcn-space-3);
  cursor: pointer;
  border: 0;
  background: transparent;
  color: inherit;
  border-radius: var(--dcn-radius-md);
  transition: background var(--dcn-transition-fast);
  text-align: left;
}
.search-result-item:hover {
  background: var(--dcn-sidebar-hover-bg);
}
.search-result-main {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}
.search-result-name {
  font-size: var(--dcn-text-md);
  font-weight: 500;
  color: var(--dcn-text-primary);
}
.search-result-meta {
  display: flex;
  gap: var(--dcn-space-3);
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  padding-left: var(--dcn-space-4);
}
.search-result-path {
  color: var(--dcn-text-placeholder);
}
.search-empty {
  text-align: center;
  padding: var(--dcn-space-5);
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-base);
}
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: var(--dcn-radius-full);
  flex-shrink: 0;
}
.dot-online {
  background: var(--dcn-dot-online);
}
.dot-offline {
  background: var(--dcn-dot-offline);
}
.dot-maintenance {
  background: var(--dcn-dot-maintenance);
}
.user-avatar-btn {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  min-height: 36px;
  padding: 4px var(--dcn-space-3) 4px var(--dcn-space-2);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-section);
  cursor: pointer;
  transition: all var(--dcn-transition-fast);
  color: var(--dcn-primary-light);
}
.user-avatar-btn:hover {
  border-color: var(--dcn-border-strong);
  background: var(--dcn-bg-muted);
  color: var(--dcn-text-primary);
}
.user-avatar-name {
  font-size: var(--dcn-text-sm);
  font-weight: 500;
  white-space: nowrap;
}

.main-content {
  display: flex;
  flex: 1;
  overflow: hidden;
}
.left-sidebar {
  flex-shrink: 0;
  background: var(--dcn-bg-section);
  border-right: 1px solid var(--dcn-border-light);
  overflow-y: auto;
  transition: width var(--dcn-transition-normal);
}
.center-area {
  flex: 1;
  overflow: hidden;
  position: relative;
  background: var(--dcn-bg-page);
}
.right-sidebar {
  width: var(--dcn-detail-width);
  flex-shrink: 0;
  background: var(--dcn-bg-section);
  border-left: 1px solid var(--dcn-border-light);
  overflow-y: auto;
}

@media (max-width: 900px) {
  .brand-copy small {
    display: none;
  }
  .top-bar-center {
    padding: 0 var(--dcn-space-2);
  }
  .search-input {
    max-width: 320px;
  }
  .user-avatar-name {
    display: none;
  }
}
</style>
