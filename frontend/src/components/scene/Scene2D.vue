<template>
  <div class="scene-2d">
    <!-- Empty state: no room selected -->
    <div v-if="!currentRoom" class="empty-state">
      <el-icon :size="48" color="var(--dcn-text-placeholder)"><Monitor /></el-icon>
      <p>请从左侧导航选择一个机房</p>
    </div>

    <!-- Room Overview -->
    <div v-else-if="viewMode === 'room'" class="room-overview">
      <div class="view-header">
        <div class="view-header-info" @contextmenu.prevent="onRoomContext($event)">
          <h2>{{ currentRoom.name }}</h2>
          <span class="view-header-sub">
            {{ currentRoom.location || '无位置信息' }}
            <el-tag size="small" type="info" style="margin-left: 8px;">
              {{ rackList.length }} 个机柜 · {{ totalDeviceCount }} 台设备
            </el-tag>
          </span>
        </div>
        <div class="view-header-actions">
          <el-button v-if="authStore.hasPermission('device:manage')" @click="openEditRoom">
            <el-icon><Edit /></el-icon>
            编辑
          </el-button>
          <el-button v-if="authStore.hasPermission('device:manage')" type="danger" plain @click="handleDeleteRoom">
            <el-icon><Delete /></el-icon>
            删除机房
          </el-button>
          <el-button v-if="authStore.hasPermission('device:manage')" type="primary" @click="handleCreateRack">
            <el-icon><Plus /></el-icon>
            新建机柜
          </el-button>
        </div>
      </div>

      <div v-if="rackList.length === 0" class="empty-state">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" style="color: var(--dcn-text-placeholder);">
          <rect x="4" y="2" width="16" height="20" rx="1.5" stroke="currentColor" stroke-width="1.5"/>
          <rect x="6.5" y="4.5" width="11" height="3" rx="0.5" fill="currentColor" opacity="0.7"/>
          <rect x="6.5" y="9" width="11" height="3" rx="0.5" fill="currentColor" opacity="0.7"/>
          <rect x="6.5" y="13.5" width="11" height="3" rx="0.5" fill="currentColor" opacity="0.7"/>
          <circle cx="15.5" cy="6" r="0.7" fill="currentColor"/>
          <circle cx="15.5" cy="10.5" r="0.7" fill="currentColor"/>
          <circle cx="15.5" cy="15" r="0.7" fill="currentColor"/>
        </svg>
        <p>暂无机柜，点击上方按钮新建</p>
      </div>

      <div v-else class="rack-sections" :key="'grid-' + refreshKey">
        <div
          v-for="rack in rackList"
          :key="rack.id"
          class="rack-section"
        >
          <div
            class="rack-section-header"
            @click="openRackDetail(rack)"
            @contextmenu.prevent="onRackContext($event, rack)"
          >
            <div class="rack-section-left">
              <svg class="rack-section-icon" viewBox="0 0 16 16" width="16" height="16"><rect x="2" y="1" width="12" height="14" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/><line x1="5" y1="4" x2="11" y2="4" stroke="currentColor" stroke-width="1"/><line x1="5" y1="8" x2="11" y2="8" stroke="currentColor" stroke-width="1"/><line x1="5" y1="12" x2="11" y2="12" stroke="currentColor" stroke-width="1"/></svg>
              <span class="rack-section-name">{{ rack.name }}</span>
              <el-tag :type="rack.type === 'cabinet' ? 'primary' : 'warning'" size="small">
                {{ rack.type === 'cabinet' ? '机柜' : '货架' }}
              </el-tag>
              <span class="rack-section-stats">{{ rack.devices?.length ?? 0 }} 台设备</span>
              <span v-if="rack.type === 'cabinet' && rack.capacity_u" class="rack-section-u">{{ usedU(rack) }}/{{ rack.capacity_u }}U</span>
            </div>
            <div class="rack-section-right">
              <div v-if="rack.type === 'cabinet' && rack.capacity_u" class="capacity-bar-sm">
                <div class="capacity-bar-fill" :style="{ width: usagePercent(rack) + '%', background: usageColor(rack) }" />
              </div>
              <el-button text size="small" @click.stop="openRackDetail(rack)">U位视图 →</el-button>
            </div>
          </div>
          <div v-if="(rack.devices?.length ?? 0) > 0" class="rack-device-grid">
            <div
              v-for="device in rack.devices"
              :key="device.id"
              class="room-device-card"
              :class="'type-' + device.type"
              @click="handleDeviceClick(device)"
              @contextmenu.prevent="onDeviceContext($event, device)"
            >
              <div class="room-device-icon" :class="'icon-' + device.type">
                <component :is="getDeviceIcon(device.type)" />
              </div>
              <div class="room-device-info">
                <div class="room-device-name">
                  <span class="status-dot small" :class="statusClass(device.status)" />
                  {{ device.name }}
                </div>
                <div class="room-device-meta">
                  <span class="room-device-type" :class="'tag-' + device.type">{{ deviceTypeLabel(device.type) }}</span>
                  <span class="room-device-ip">{{ device.ip_address || '-' }}</span>
                </div>
              </div>
            </div>
          </div>
          <div v-else class="rack-empty-hint">暂无设备</div>
        </div>
      </div>
    </div>

    <!-- Rack Detail (U-slot view) -->
    <div v-else-if="viewMode === 'rack' && selectedRack" class="rack-detail" :key="'cabinet-' + refreshKey">
      <div class="view-header">
        <div class="view-header-info">
          <el-button text @click="backToRoom">
            <el-icon><ArrowLeft /></el-icon>
            返回
          </el-button>
          <h2>{{ selectedRack.name }}</h2>
          <el-tag :type="selectedRack.type === 'cabinet' ? 'primary' : 'warning'" size="small">
            {{ selectedRack.type === 'cabinet' ? '机柜' : '货架' }}
          </el-tag>
        </div>
        <el-button v-if="authStore.hasPermission('device:manage')" type="primary" @click="handleCreateDevice">
          <el-icon><Plus /></el-icon>
          新建设备
        </el-button>
      </div>

      <!-- Shelf type: simple device list -->
      <div v-if="selectedRack.type === 'shelf'" class="shelf-view">
        <div v-if="devicesInRack.length === 0" class="empty-state" style="padding: 40px;">
          <p>暂无设备，点击上方按钮新建</p>
        </div>
        <div v-else class="shelf-device-list">
          <div
            v-for="device in devicesInRack"
            :key="device.id"
            class="shelf-device-item"
            @click="handleDeviceClick(device)"
            @contextmenu.prevent="onDeviceContext($event, device)"
          >
            <span class="status-dot" :class="statusClass(device.status)" />
            <span class="shelf-device-name">{{ device.name }}</span>
            <el-tag size="small" :type="deviceTypeTagType(device.type)">{{ deviceTypeLabel(device.type) }}</el-tag>
            <span class="shelf-device-ip">{{ device.ip_address || '-' }}</span>
          </div>
        </div>
      </div>

      <!-- Cabinet type: U-slot layout with chassis visualization -->
      <div v-else ref="cabinetViewRef" class="cabinet-view">
        <div
          ref="uSlotContainerRef"
          class="u-slot-container"
          :style="{ '--capacity': selectedRack.capacity_u ?? 42 }"
        >
          <!-- U number column (one cell per U) -->
          <div
            v-for="u in (selectedRack.capacity_u ?? 42)"
            :key="`u-${u}`"
            class="u-slot-number"
            :style="{ gridRow: u, gridColumn: 1 }"
          >{{ u }}</div>

          <!-- Devices column: chassis span N rows, empty rows fill single U -->
          <template v-for="row in renderedRows" :key="`r-${row.u}`">
            <ChassisRenderer
              v-if="row.type === 'chassis'"
              class="u-slot-chassis-cell"
              :style="{ gridRow: `${row.u} / span ${row.heightU}`, gridColumn: 2 }"
              :device="row.device"
              :height-u="row.heightU"
              :interface-status="interfaceStatus.map.value[row.device.id]"
              :status="row.device.status"
              @click="handleDeviceClick"
              @contextmenu="onDeviceContext($event, row.device)"
            />
            <div
              v-else
              class="u-slot-empty-cell"
              :style="{ gridRow: row.u, gridColumn: 2 }"
              @click="handleCreateDeviceAt(row.u)"
            >
              <span class="u-slot-placeholder">+</span>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- Context Menu -->
    <div v-if="contextMenu.visible" class="ctx-menu" :style="ctxMenuStyle" @click.stop>
      <div
        v-for="item in contextMenu.items"
        :key="item.action"
        class="ctx-menu__item"
        @click="handleContextAction(item.action)"
      >
        <el-icon v-if="item.icon"><component :is="item.icon" /></el-icon>
        <span>{{ item.label }}</span>
      </div>
    </div>

    <!-- Rack Form Dialog -->
    <RackForm
      v-model:visible="rackFormVisible"
      :roomId="rackFormRoomId"
      :rack="rackFormData"
      @saved="onFormSaved"
    />

    <!-- Device Form Dialog -->
    <DeviceForm
      v-model:visible="deviceFormVisible"
      :rackId="deviceFormRackId"
      :device="deviceFormData"
      :rackType="deviceFormRackType"
      :presetU="deviceFormPresetU"
      @saved="onFormSaved"
    />

    <!-- Room Form Dialog -->
    <RoomForm
      v-model:visible="roomFormVisible"
      :room="roomFormData"
      @saved="onFormSaved"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick, h } from 'vue'
import { Monitor, Plus, ArrowLeft, Edit, Delete } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoomStore } from '@/stores/room'
import { useDeviceStore } from '@/stores/device'
import { useAuthStore } from '@/stores/auth'
import { rackAPI } from '@/api'
import RackForm from '@/components/panel/RackForm.vue'
import DeviceForm from '@/components/panel/DeviceForm.vue'
import RoomForm from '@/components/panel/RoomForm.vue'
import ChassisRenderer from '@/components/scene/ChassisRenderer.vue'
import { useInterfaceStatus } from '@/composables/useInterfaceStatus'
import type { Room, Rack, Device } from '@/types'

const emit = defineEmits<{
  'device-click': [payload: { device: Device }]
  'rack-click': [payload: { rack: Rack }]
  'data-changed': []
  'nav-change': [state: { roomId?: number; rackId?: number; deviceId?: number }]
}>()

const roomStore = useRoomStore()
const deviceStore = useDeviceStore()
const authStore = useAuthStore()

const viewMode = ref<'room' | 'rack'>('room')
const selectedRack = ref<Rack | null>(null)
const cabinetViewRef = ref<HTMLElement | null>(null)
const uSlotContainerRef = ref<HTMLElement | null>(null)
const refreshKey = ref(0)

const currentRoom = computed<Room | null>(() => {
  if (roomStore.currentRoom) return roomStore.currentRoom
  if (roomStore.rooms.length > 0) return roomStore.rooms[0]
  return null
})

// Sync room navigation to URL, reset to room view on room change
watch(currentRoom, (newRoom, oldRoom) => {
  if (newRoom?.id !== oldRoom?.id) {
    viewMode.value = 'room'
    selectedRack.value = null
  }
  if (roomStore.currentRoom) {
    emitNavState()
  }
})

const rackList = computed<Rack[]>(() => {
  if (!currentRoom.value) return []
  return currentRoom.value.racks ?? []
})

const devicesInRack = computed<Device[]>(() => {
  if (!selectedRack.value) return []
  return selectedRack.value.devices ?? []
})

const totalDeviceCount = computed(() => {
  return rackList.value.reduce((sum, r) => sum + (r.devices?.length ?? 0), 0)
})

function getDeviceIcon(type: string) {
  const t = type.toLowerCase()
  const icons: Record<string, () => ReturnType<typeof h>> = {
    server: () => h('svg', { viewBox: '0 0 40 40', width: 32, height: 32 }, [
      h('rect', { x: 6, y: 2, width: 28, height: 36, rx: 2, fill: '#e8ecf1', stroke: '#b0b8c4', 'stroke-width': 1 }),
      h('rect', { x: 10, y: 6, width: 20, height: 6, rx: 1, fill: '#d5dbe3' }),
      h('circle', { cx: 13, cy: 9, r: 1.5, fill: '#67c23a' }),
      h('rect', { x: 16, y: 7.5, width: 12, height: 3, rx: 0.5, fill: '#a8b2be' }),
      h('rect', { x: 10, y: 15, width: 20, height: 6, rx: 1, fill: '#d5dbe3' }),
      h('circle', { cx: 13, cy: 18, r: 1.5, fill: '#409eff' }),
      h('rect', { x: 16, y: 16.5, width: 12, height: 3, rx: 0.5, fill: '#a8b2be' }),
      h('rect', { x: 10, y: 24, width: 20, height: 6, rx: 1, fill: '#d5dbe3' }),
      h('circle', { cx: 13, cy: 27, r: 1.5, fill: '#409eff' }),
      h('rect', { x: 16, y: 25.5, width: 12, height: 3, rx: 0.5, fill: '#a8b2be' }),
    ]),
    switch: () => h('svg', { viewBox: '0 0 40 40', width: 32, height: 32 }, [
      h('rect', { x: 2, y: 8, width: 36, height: 24, rx: 2, fill: '#e0e8f0', stroke: '#8fa4b8', 'stroke-width': 1 }),
      h('rect', { x: 6, y: 13, width: 3, height: 5, rx: 0.5, fill: '#409eff' }),
      h('rect', { x: 11, y: 13, width: 3, height: 5, rx: 0.5, fill: '#409eff' }),
      h('rect', { x: 16, y: 13, width: 3, height: 5, rx: 0.5, fill: '#67c23a' }),
      h('rect', { x: 21, y: 13, width: 3, height: 5, rx: 0.5, fill: '#67c23a' }),
      h('rect', { x: 26, y: 13, width: 3, height: 5, rx: 0.5, fill: '#409eff' }),
      h('rect', { x: 31, y: 13, width: 3, height: 5, rx: 0.5, fill: '#e6a23c' }),
      h('rect', { x: 6, y: 22, width: 3, height: 5, rx: 0.5, fill: '#409eff' }),
      h('rect', { x: 11, y: 22, width: 3, height: 5, rx: 0.5, fill: '#409eff' }),
      h('rect', { x: 16, y: 22, width: 3, height: 5, rx: 0.5, fill: '#67c23a' }),
      h('rect', { x: 21, y: 22, width: 3, height: 5, rx: 0.5, fill: '#67c23a' }),
      h('rect', { x: 26, y: 22, width: 3, height: 5, rx: 0.5, fill: '#409eff' }),
      h('rect', { x: 31, y: 22, width: 3, height: 5, rx: 0.5, fill: '#e6a23c' }),
    ]),
    router: () => h('svg', { viewBox: '0 0 40 40', width: 32, height: 32 }, [
      h('rect', { x: 4, y: 14, width: 32, height: 20, rx: 3, fill: '#e0e8f0', stroke: '#8fa4b8', 'stroke-width': 1 }),
      h('path', { d: 'M14 14 L14 8 Q14 4 18 4 L22 4 Q26 4 26 8 L26 14', fill: 'none', stroke: '#8fa4b8', 'stroke-width': 1.5 }),
      h('circle', { cx: 20, cy: 4, r: 2, fill: '#8fa4b8' }),
      h('circle', { cx: 12, cy: 24, r: 2, fill: '#67c23a' }),
      h('circle', { cx: 20, cy: 24, r: 2, fill: '#409eff' }),
      h('circle', { cx: 28, cy: 24, r: 2, fill: '#409eff' }),
      h('rect', { x: 8, y: 18, width: 24, height: 2, rx: 0.5, fill: '#b8c4d0' }),
    ]),
    firewall: () => h('svg', { viewBox: '0 0 40 40', width: 32, height: 32 }, [
      h('path', { d: 'M20 2 L36 10 L36 22 Q36 34 20 38 Q4 34 4 22 L4 10 Z', fill: '#fef0e8', stroke: '#e6a23c', 'stroke-width': 1.5 }),
      h('path', { d: 'M20 8 L28 12 L28 20 Q28 28 20 31 Q12 28 12 20 L12 12 Z', fill: '#fde2c8', stroke: '#e6a23c', 'stroke-width': 0.8 }),
      h('rect', { x: 15, y: 16, width: 10, height: 3, rx: 0.5, fill: '#e6a23c' }),
      h('rect', { x: 15, y: 21, width: 10, height: 3, rx: 0.5, fill: '#e6a23c' }),
      h('circle', { cx: 20, cy: 13, r: 1.5, fill: '#f56c6c' }),
    ]),
    host: () => h('svg', { viewBox: '0 0 40 40', width: 32, height: 32 }, [
      h('rect', { x: 6, y: 4, width: 28, height: 24, rx: 2, fill: '#e8ecf1', stroke: '#b0b8c4', 'stroke-width': 1 }),
      h('rect', { x: 9, y: 7, width: 22, height: 15, rx: 1, fill: '#c5cdd8' }),
      h('rect', { x: 11, y: 9, width: 18, height: 11, rx: 0.5, fill: '#dce2ea' }),
      h('circle', { cx: 20, cy: 25, r: 1.5, fill: '#409eff' }),
      h('rect', { x: 14, y: 28, width: 12, height: 2, rx: 0.5, fill: '#b0b8c4' }),
      h('rect', { x: 10, y: 30, width: 20, height: 2, rx: 1, fill: '#a0a8b4' }),
    ]),
  }
  return icons[t] || icons.host
}

// ---- Rendered-rows model for CSS Grid layout (multi-U spans) ----
type RenderedRow =
  | { u: number; type: 'chassis'; device: Device; heightU: number }
  | { u: number; type: 'empty' }

const renderedRows = computed<RenderedRow[]>(() => {
  if (!selectedRack.value) return []
  const capacity = selectedRack.value.capacity_u ?? 42
  const devices = selectedRack.value.devices ?? []

  // Build map: top-U -> device, with auto-assignment for null position_u
  const deviceMap = new Map<number, Device>()
  const occupiedSet = new Set<number>()

  // First pass: register explicit positions
  for (const d of devices) {
    if (d.position_u != null) {
      const sizeU = d.size_u ?? 1
      for (let i = 0; i < sizeU; i++) occupiedSet.add(d.position_u + i)
    }
  }

  // Second pass: auto-assign positions for null position_u (skip occupied)
  let autoU = 1
  for (const d of devices) {
    let posU = d.position_u
    if (posU == null) {
      while (occupiedSet.has(autoU) && autoU <= capacity) autoU++
      posU = autoU
      const sizeU = d.size_u ?? 1
      for (let i = 0; i < sizeU; i++) occupiedSet.add(posU! + i)
    }
    deviceMap.set(posU, d)
  }

  // Walk U=1..capacity, emit chassis (with span) or empty
  const rows: RenderedRow[] = []
  let u = 1
  while (u <= capacity) {
    const dev = deviceMap.get(u)
    if (dev) {
      // Clamp size_u so the chassis doesn't exceed capacity
      const heightU = Math.max(1, Math.min(dev.size_u ?? 1, capacity - u + 1))
      rows.push({ u, type: 'chassis', device: dev, heightU })
      u += heightU
    } else {
      rows.push({ u, type: 'empty' })
      u += 1
    }
  }
  return rows
})

// ---- Live interface status (port LEDs) ----
const interfaceStatus = useInterfaceStatus()

// ---- Rack helpers ----
function usedU(rack: Rack): number {
  const devices = rack.devices ?? []
  return devices.reduce((sum, d) => sum + (d.size_u ?? 1), 0)
}

function usagePercent(rack: Rack): number {
  if (!rack.capacity_u) return 0
  return Math.min(100, Math.round((usedU(rack) / rack.capacity_u) * 100))
}

function usageColor(rack: Rack): string {
  const pct = usagePercent(rack)
  if (pct > 90) return 'var(--dcn-danger)'
  if (pct > 70) return 'var(--dcn-warning)'
  return 'var(--dcn-success)'
}

// ---- Device helpers ----
const DEVICE_COLORS: Record<string, string> = {
  server: 'var(--dcn-device-host)',
  host: 'var(--dcn-device-host)',
  switch: 'var(--dcn-device-switch)',
  router: 'var(--dcn-device-router)',
  firewall: 'var(--dcn-device-firewall)',
}

function deviceColor(type: string): string {
  return DEVICE_COLORS[type.toLowerCase()] ?? 'var(--dcn-text-regular)'
}

function statusClass(status: string): string {
  const s = status.toLowerCase()
  if (['online', 'running', 'active'].includes(s)) return 'status-online'
  if (['offline', 'inactive', 'critical'].includes(s)) return 'status-offline'
  return 'status-maintenance'
}

function deviceTypeLabel(type: string): string {
  const map: Record<string, string> = {
    server: '服务器', switch: '交换机', router: '路由器',
    firewall: '防火墙', host: '主机',
  }
  return map[type.toLowerCase()] ?? type
}

function deviceTypeTagType(type: string): string {
  const map: Record<string, string> = {
    server: 'info', switch: 'primary', router: 'success',
    firewall: 'danger', host: 'info',
  }
  return map[type.toLowerCase()] ?? 'info'
}

// ---- Navigation ----

function emitNavState() {
  emit('nav-change', {
    roomId: currentRoom.value?.id,
    rackId: selectedRack.value?.id,
    deviceId: deviceStore.currentDevice?.id,
  })
}

function openRackDetail(rack: Rack) {
  selectedRack.value = rack
  viewMode.value = 'rack'
  emit('rack-click', { rack })
  emitNavState()

  // Start live interface-status polling for this rack
  interfaceStatus.start(rack.id)

  // Auto-scroll to first occupied slot
  nextTick(() => {
    if (!cabinetViewRef.value || !uSlotContainerRef.value) return
    const firstOccupied = uSlotContainerRef.value.querySelector('.u-slot-occupied')
    if (firstOccupied) {
      firstOccupied.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }
  })
}

function backToRoom() {
  viewMode.value = 'room'
  selectedRack.value = null
  // Stop interface-status polling when leaving rack view
  interfaceStatus.stop()
  emitNavState()
}

// ---- Device click ----
function handleDeviceClick(device: Device) {
  deviceStore.setCurrentDevice(device)
  emit('device-click', { device })
  emitNavState()
}

// ---- Context menu ----
const contextMenu = reactive({
  visible: false,
  position: { x: 0, y: 0 },
  items: [] as { label: string; action: string; icon: unknown }[],
  target: null as { type: string; id: number; data: any } | null,
})

function closeContextMenu() {
  contextMenu.visible = false
}

function onRoomContext(e: MouseEvent) {
  // No context menu for room header currently
}

function onRackContext(e: MouseEvent, rack: Rack) {
  contextMenu.position = { x: e.clientX, y: e.clientY }
  const items = [
    { label: '编辑机柜', action: 'edit-rack', icon: Edit, perm: 'device:manage' },
    { label: '删除机柜', action: 'delete-rack', icon: Delete, perm: 'device:manage' },
  ]
  contextMenu.items = items.filter(i => !i.perm || authStore.hasPermission(i.perm)) as { label: string; action: string; icon: unknown }[]
  contextMenu.target = { type: 'rack', id: rack.id, data: rack }
  contextMenu.visible = true
}

function onDeviceContext(e: MouseEvent, device: Device) {
  contextMenu.position = { x: e.clientX, y: e.clientY }
  const items = [
    { label: '编辑设备', action: 'edit-device', icon: Edit, perm: 'device:manage' },
    { label: '删除设备', action: 'delete-device', icon: Delete, perm: 'device:manage' },
  ]
  contextMenu.items = items.filter(i => !i.perm || authStore.hasPermission(i.perm)) as { label: string; action: string; icon: unknown }[]
  contextMenu.target = { type: 'device', id: device.id, data: device }
  contextMenu.visible = true
}

const ctxMenuStyle = computed(() => ({
  left: `${contextMenu.position.x}px`,
  top: `${contextMenu.position.y}px`,
}))

async function handleContextAction(action: string) {
  contextMenu.visible = false
  const target = contextMenu.target
  if (!target) return

  if (action === 'edit-rack' && target.id) {
    const freshRack = rackList.value.find(r => r.id === target.id)
    const rack = freshRack || target.data
    rackFormRoomId.value = rack.room_id
    rackFormData.value = rack
    rackFormVisible.value = true
  } else if (action === 'delete-rack' && target.id) {
    try {
      await ElMessageBox.confirm('确定要删除此机柜吗？删除后不可恢复。', '确认删除', {
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
        type: 'warning',
      })
      await rackAPI.delete(target.id)
      ElMessage.success('机柜已删除')
      onFormSaved()
    } catch (err) {
      if (err !== 'cancel') ElMessage.error('删除失败')
    }
  } else if (action === 'edit-device' && target.id) {
    // Look up fresh device data from current rack
    const freshDevice = devicesInRack.value.find(d => d.id === target.id)
    const device = freshDevice || target.data
    deviceFormRackId.value = device.rack_id
    deviceFormData.value = device
    deviceFormRackType.value = selectedRack.value?.type ?? 'cabinet'
    deviceFormVisible.value = true
  } else if (action === 'delete-device' && target.id) {
    try {
      await ElMessageBox.confirm('确定要删除此设备吗？删除后不可恢复。', '确认删除', {
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
        type: 'warning',
      })
      await deviceStore.deleteDevice(target.id)
      ElMessage.success('设备已删除')
      onFormSaved()
    } catch (err) {
      if (err !== 'cancel') ElMessage.error('删除失败')
    }
  }
}

// ---- Form state ----
const roomFormVisible = ref(false)
const roomFormData = ref<Room | null>(null)

const rackFormVisible = ref(false)
const rackFormRoomId = ref(0)
const rackFormData = ref<Rack | null>(null)

const deviceFormVisible = ref(false)
const deviceFormRackId = ref(0)
const deviceFormData = ref<Device | null>(null)
const deviceFormRackType = ref('cabinet')
const deviceFormPresetU = ref<number | null>(null)

function handleCreateRack() {
  if (!currentRoom.value) return
  rackFormRoomId.value = currentRoom.value.id
  rackFormData.value = null
  rackFormVisible.value = true
}

function openEditRoom() {
  if (!currentRoom.value) return
  roomFormData.value = currentRoom.value
  roomFormVisible.value = true
}

async function handleDeleteRoom() {
  if (!currentRoom.value) return
  try {
    await ElMessageBox.confirm(
      `确定要删除机房「${currentRoom.value.name}」吗？该操作将同时删除其下所有机柜和设备。`,
      '删除确认',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await roomStore.deleteRoom(currentRoom.value.id)
    ElMessage.success('机房已删除')
    emit('data-changed')
    emitNavState()
  } catch {
    ElMessage.error('删除失败')
  }
}

function handleCreateDevice() {
  if (!selectedRack.value) return
  deviceFormRackId.value = selectedRack.value.id
  deviceFormData.value = null
  deviceFormRackType.value = selectedRack.value.type
  deviceFormPresetU.value = null
  deviceFormVisible.value = true
}

function handleCreateDeviceAt(u: number) {
  if (!selectedRack.value) return
  deviceFormRackId.value = selectedRack.value.id
  deviceFormData.value = null
  deviceFormRackType.value = selectedRack.value.type
  deviceFormPresetU.value = u
  deviceFormVisible.value = true
}

async function onFormSaved() {
  await roomStore.fetchRooms()
  // Always sync currentRoom to fresh data
  const targetId = currentRoom.value?.id
  if (targetId) {
    const freshRoom = roomStore.rooms.find(r => r.id === targetId)
    if (freshRoom) roomStore.setCurrentRoom(freshRoom)
  }
  // Refresh selected rack if in rack detail view
  if (viewMode.value === 'rack' && selectedRack.value) {
    const room = roomStore.rooms.find(r => r.id === selectedRack.value!.room_id)
    if (room) {
      const rack = room.racks?.find(r => r.id === selectedRack.value!.id)
      if (rack) selectedRack.value = rack
    }
  }
  // Refresh current device if one is selected
  if (deviceStore.currentDevice) {
    await deviceStore.fetchDevice(deviceStore.currentDevice.id)
  }
  // Force re-render of rack grid / cabinet view
  refreshKey.value++
  emit('data-changed')
}

// ---- Click outside to close context menu ----
function onDocumentClick() {
  closeContextMenu()
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
})

onUnmounted(() => {
  document.removeEventListener('click', onDocumentClick)
  interfaceStatus.stop()
})

function openRackById(rackId: number) {
  const rack = rackList.value.find((r) => r.id === rackId)
  if (rack) openRackDetail(rack)
}

defineExpose({ openRackById })
</script>

<style scoped>
.scene-2d {
  width: 100%;
  height: 100%;
  overflow-y: auto;
  background: var(--dcn-bg-section);
  font-family: var(--dcn-font-sans);
}

/* ---- Empty state ---- */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  min-height: 300px;
  color: var(--dcn-text-secondary);
  gap: var(--dcn-space-3);
}

.empty-state p {
  font-size: var(--dcn-text-md);
}

/* ---- View header ---- */
.view-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--dcn-space-5) var(--dcn-space-6) var(--dcn-space-4);
  border-bottom: 1px solid var(--dcn-border-strong);
  background: var(--dcn-bg-card);
  position: sticky;
  top: 0;
  z-index: 10;
}

.view-header-info {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
}

.view-header-info h2 {
  font-size: var(--dcn-text-xl);
  font-weight: 600;
  color: var(--dcn-text-primary);
  margin: 0;
}

.view-header-sub {
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-secondary);
}

/* ---- Rack grid ---- */
.rack-sections {
  flex: 1;
  overflow-y: auto;
  padding: var(--dcn-space-4) var(--dcn-space-6);
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-4);
}

.rack-section {
  background: var(--dcn-bg-card);
  border-radius: var(--dcn-radius-lg);
  border: 1px solid var(--dcn-border-strong);
  overflow: hidden;
}

.rack-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--dcn-space-3) var(--dcn-space-4);
  background: var(--dcn-bg-muted);
  border-bottom: 1px solid var(--dcn-border);
  cursor: pointer;
  transition: background var(--dcn-transition-fast);
}

.rack-section-header:hover {
  background: var(--dcn-primary-bg);
}

.rack-section-left {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}

.rack-section-icon {
  color: var(--dcn-text-secondary);
}

.rack-section-name {
  font-size: var(--dcn-text-md);
  font-weight: 600;
  color: var(--dcn-text-primary);
}

.rack-section-stats {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
}

.rack-section-u {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
}

.rack-section-right {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
}

.capacity-bar-sm {
  width: 80px;
  height: 5px;
  background: var(--dcn-border);
  border-radius: var(--dcn-radius-xs);
  overflow: hidden;
}

.rack-device-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: var(--dcn-space-2);
  padding: var(--dcn-space-3) var(--dcn-space-4);
}

.room-device-card {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  padding: var(--dcn-space-2) 10px;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  cursor: pointer;
  transition: all var(--dcn-transition-fast);
  background: var(--dcn-bg-elevated);
}

.room-device-card:hover {
  border-color: var(--dcn-border-strong);
  box-shadow: var(--dcn-shadow-sm);
  background: var(--dcn-bg-card);
}

.room-device-icon {
  width: 32px;
  height: 32px;
  border-radius: var(--dcn-radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  overflow: hidden;
}

.room-device-icon.icon-server { background: var(--dcn-device-server-bg); }
.room-device-icon.icon-switch { background: var(--dcn-device-switch-bg); }
.room-device-icon.icon-router { background: var(--dcn-device-router-bg); }
.room-device-icon.icon-firewall { background: var(--dcn-device-firewall-bg); }
.room-device-icon.icon-host { background: var(--dcn-device-host-bg); }

.room-device-info {
  flex: 1;
  min-width: 0;
}

.room-device-name {
  font-size: var(--dcn-text-base);
  font-weight: 500;
  color: var(--dcn-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: flex;
  align-items: center;
  gap: var(--dcn-space-1);
}

.room-device-meta {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-1);
  margin-top: var(--dcn-radius-xs);
}

.room-device-type {
  font-size: var(--dcn-text-xs);
  padding: 0 var(--dcn-space-1);
  border-radius: var(--dcn-radius-xs);
  line-height: 18px;
  white-space: nowrap;
}

.room-device-type.tag-server { background: var(--dcn-device-host-bg); color: var(--dcn-text-regular); }
.room-device-type.tag-switch { background: var(--dcn-device-switch-bg); color: var(--dcn-device-switch); }
.room-device-type.tag-router { background: var(--dcn-device-router-bg); color: var(--dcn-device-router); }
.room-device-type.tag-firewall { background: var(--dcn-device-firewall-bg); color: var(--dcn-device-firewall); }
.room-device-type.tag-host { background: var(--dcn-device-host-bg); color: var(--dcn-text-regular); }

.room-device-ip {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.rack-empty-hint {
  padding: var(--dcn-space-3) var(--dcn-space-4);
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-placeholder);
}

.capacity-bar {
  height: 6px;
  background: var(--dcn-border);
  border-radius: var(--dcn-radius-xs);
  overflow: hidden;
}

.capacity-bar-fill {
  height: 100%;
  border-radius: var(--dcn-radius-xs);
  transition: width 0.3s;
}

/* ---- Rack detail ---- */
.rack-detail {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.cabinet-view {
  flex: 1;
  overflow-y: auto;
  padding: var(--dcn-space-5) var(--dcn-space-6);
  display: flex;
  justify-content: center;
}

.u-slot-container {
  width: 540px;
  display: grid;
  grid-template-columns: 36px 1fr;
  grid-template-rows: repeat(var(--capacity, 42), 24px);
  border: 2px solid var(--dcn-border-strong);
  border-radius: var(--dcn-radius-md);
  overflow: hidden;
  background: var(--dcn-bg-muted);
}

/* U number column cells */
.u-slot-number {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-secondary);
  border-right: 1px solid var(--dcn-border);
  border-bottom: 1px solid var(--dcn-border);
  background: var(--dcn-bg-card);
  user-select: none;
}
.u-slot-number:last-of-type {
  border-bottom: none;
}

/* Empty slot cells (column 2) */
.u-slot-empty-cell {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  padding-left: 6px;
  cursor: pointer;
  border-bottom: 1px solid var(--dcn-border);
  background: var(--dcn-bg-muted);
}
.u-slot-empty-cell:hover {
  background: var(--dcn-primary-bg-deep);
}

/* Chassis cell — span multiple grid rows; NO border-bottom */
.u-slot-chassis-cell {
  width: 100%;
  height: 100%;
  /* No border-bottom — the chassis itself is the visual frame */
}

.u-slot-placeholder {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-placeholder);
}

/* ---- Shelf view ---- */
.shelf-view {
  flex: 1;
  padding: var(--dcn-space-5) var(--dcn-space-6);
}

.shelf-device-list {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-2);
}

.shelf-device-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: var(--dcn-space-3) var(--dcn-space-4);
  background: var(--dcn-bg-card);
  border: 1px solid var(--dcn-border-strong);
  border-radius: var(--dcn-radius-md);
  cursor: pointer;
  transition: border-color var(--dcn-transition-fast);
}

.shelf-device-item:hover {
  border-color: var(--dcn-primary);
}

.shelf-device-name {
  font-size: var(--dcn-text-md);
  font-weight: 500;
  color: var(--dcn-text-primary);
  flex: 1;
}

.shelf-device-ip {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  font-family: var(--dcn-font-mono);
}

/* ---- Status dots ---- */
.status-dot {
  width: var(--dcn-space-2);
  height: var(--dcn-space-2);
  border-radius: var(--dcn-radius-full);
  flex-shrink: 0;
}

.status-dot.small {
  width: 6px;
  height: 6px;
}

.status-online { background: var(--dcn-dot-online); box-shadow: 0 0 4px rgba(103, 194, 58, 0.5); }
.status-offline { background: var(--dcn-dot-offline); }
.status-maintenance { background: var(--dcn-dot-maintenance); }

/* ---- Context menu ---- */
.ctx-menu {
  position: fixed;
  z-index: 3000;
  background: var(--dcn-bg-card);
  border-radius: var(--dcn-radius-sm);
  box-shadow: var(--dcn-shadow-md);
  border: 1px solid var(--dcn-border-strong);
  min-width: 140px;
  padding: var(--dcn-space-1) 0;
}

.ctx-menu__item {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-1);
  padding: var(--dcn-space-2) var(--dcn-space-4);
  font-size: var(--dcn-text-md);
  color: var(--dcn-text-primary);
  cursor: pointer;
  transition: background-color var(--dcn-transition-fast);
}

.ctx-menu__item:hover {
  background-color: var(--dcn-primary-bg);
  color: var(--dcn-primary);
}
</style>
