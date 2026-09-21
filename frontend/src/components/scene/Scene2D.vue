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
            <el-tag size="small" type="info" style="margin-left: 8px">
              {{ rackList.length }} 个机柜 · {{ totalDeviceCount }} 台设备
            </el-tag>
          </span>
        </div>
        <div class="view-header-actions">
          <el-button :loading="statusScanning" @click="onScanStatuses">
            <el-icon v-if="!statusScanning"><Refresh /></el-icon>
            刷新状态
          </el-button>
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
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" style="color: var(--dcn-text-placeholder)">
          <rect x="4" y="2" width="16" height="20" rx="1.5" stroke="currentColor" stroke-width="1.5" />
          <rect x="6.5" y="4.5" width="11" height="3" rx="0.5" fill="currentColor" opacity="0.7" />
          <rect x="6.5" y="9" width="11" height="3" rx="0.5" fill="currentColor" opacity="0.7" />
          <rect x="6.5" y="13.5" width="11" height="3" rx="0.5" fill="currentColor" opacity="0.7" />
          <circle cx="15.5" cy="6" r="0.7" fill="currentColor" />
          <circle cx="15.5" cy="10.5" r="0.7" fill="currentColor" />
          <circle cx="15.5" cy="15" r="0.7" fill="currentColor" />
        </svg>
        <p>暂无机柜，点击上方按钮新建</p>
      </div>

      <div v-else :key="'grid-' + refreshKey" class="cabinet-grid">
        <RackCabinet
          v-for="rack in rackList"
          :key="rack.id"
          :rack="rack"
          :opening="openingRackId === rack.id"
          @click="onCabinetClick"
          @contextmenu="onRackContext"
        />
      </div>
    </div>

    <!-- Rack Detail (U-slot view) -->
    <div
      v-else-if="viewMode === 'rack' && selectedRack"
      :key="'rack-' + selectedRack.id"
      class="rack-detail dcn-anim-scale"
    >
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
          <el-tag v-if="selectedRack.type === 'cabinet'" type="info" size="small">
            {{ RACK_CAPACITY_U }}U · 每台 {{ RACK_DEVICE_SIZE_U }}U
          </el-tag>
        </div>
        <div class="view-header-actions">
          <el-button :loading="statusScanning" @click="onScanStatuses">
            <el-icon v-if="!statusScanning"><Refresh /></el-icon>
            刷新状态
          </el-button>
          <el-button v-if="authStore.hasPermission('device:manage')" type="primary" @click="handleCreateDevice">
            <el-icon><Plus /></el-icon>
            新建设备
          </el-button>
        </div>
      </div>

      <!-- Shelf type: simple device list -->
      <div v-if="selectedRack.type === 'shelf'" :key="'shelf-' + refreshKey" class="shelf-view">
        <div v-if="devicesInRack.length === 0" class="empty-state" style="padding: 40px">
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
      <div v-else ref="cabinetViewRef" :key="'cab-' + refreshKey" class="cabinet-view">
        <div ref="uSlotContainerRef" class="u-slot-container" :style="{ '--capacity': RACK_CAPACITY_U }">
          <!-- U number column (one cell per U) -->
          <div
            v-for="u in RACK_CAPACITY_U"
            :key="`u-${u}`"
            class="u-slot-number"
            :style="{ gridRow: u, gridColumn: 1 }"
          >
            {{ u }}
          </div>

          <!-- Devices column: chassis span N rows, empty rows fill single U -->
          <template v-for="row in renderedRows" :key="`r-${row.u}-${row.type}`">
            <ChassisRenderer
              v-if="row.type === 'chassis'"
              class="u-slot-chassis-cell"
              :style="{ gridRow: `${row.u} / span ${row.heightU}`, gridColumn: 2 }"
              :device="row.device"
              :height-u="row.heightU"
              :status="row.device.status"
              :draggable="authStore.hasPermission('device:manage')"
              @click="handleDeviceClick"
              @contextmenu="onDeviceContext($event, row.device)"
              @dragstart="handleDeviceDragStart($event, row.device)"
              @dragover.prevent
              @drop.stop.prevent="handleDeviceDrop($event, row.u)"
            />
            <!-- 溢出兜底：机柜已满仍存在的设备（脏数据/并发），不占网格位，附在容器底部可点击 -->
            <div
              v-else-if="row.type === 'overflow'"
              class="u-slot-overflow"
              role="button"
              tabindex="0"
              :title="`机柜已满，无法为 ${row.device.name} 分配槽位`"
              @click="handleDeviceClick(row.device)"
              @contextmenu.prevent="onDeviceContext($event, row.device)"
            >
              <span class="status-dot" :class="statusClass(row.device.status)" />
              <span class="u-slot-overflow-name">{{ row.device.name }}</span>
              <el-tag size="small" type="danger">未分配槽位</el-tag>
            </div>
            <div
              v-else
              class="u-slot-empty-cell"
              :style="{ gridRow: `${row.u} / span 2`, gridColumn: 2 }"
              role="button"
              tabindex="0"
              aria-label="在此机架位置新建设备"
              @click="handleCreateDeviceAt(row.u)"
              @keydown.enter="handleCreateDeviceAt(row.u)"
              @keydown.space.prevent="handleCreateDeviceAt(row.u)"
              @dragover.prevent
              @drop.prevent="handleDeviceDrop($event, row.u)"
            >
              <span class="u-slot-placeholder">+</span>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- Context Menu -->
    <div v-if="contextMenu.visible" class="ctx-menu" :style="ctxMenuStyle" @click.stop>
      <button
        v-for="item in contextMenu.items"
        :key="item.action"
        type="button"
        class="ctx-menu__item"
        @click="handleContextAction(item.action)"
      >
        <el-icon v-if="item.icon"><component :is="item.icon" /></el-icon>
        <span>{{ item.label }}</span>
      </button>
    </div>

    <!-- Rack Form Dialog -->
    <RackForm v-model:visible="rackFormVisible" :room-id="rackFormRoomId" :rack="rackFormData" @saved="onFormSaved" />

    <!-- Device Form Dialog -->
    <DeviceForm
      v-model:visible="deviceFormVisible"
      :rack-id="deviceFormRackId"
      :device="deviceFormData"
      :rack-type="deviceFormRackType"
      :preset-u="deviceFormPresetU"
      @saved="onFormSaved"
    />

    <!-- Room Form Dialog -->
    <RoomForm v-model:visible="roomFormVisible" :room="roomFormData" @saved="onFormSaved" />
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { Monitor, Plus, ArrowLeft, Edit, Delete, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoomStore } from '@/stores/room'
import { useDeviceStore } from '@/stores/device'
import { useAuthStore } from '@/stores/auth'
import { useDeviceMonitor } from '@/composables/useDeviceMonitor'
import { deviceAPI, rackAPI } from '@/api'
import { deviceTypeLabel, deviceTypeTagType } from '@/utils/deviceLabels'
import {
  RACK_CAPACITY_U,
  RACK_DEVICE_SIZE_U,
  RACK_SLOT_STARTS,
  nextFreeSlot,
  resolveRackLayout,
  snapSlotStart,
} from '@/utils/rackLayout'
import RackForm from '@/components/panel/RackForm.vue'
import DeviceForm from '@/components/panel/DeviceForm.vue'
import RoomForm from '@/components/panel/RoomForm.vue'
import ChassisRenderer from '@/components/scene/ChassisRenderer.vue'
import RackCabinet from '@/components/scene/RackCabinet.vue'
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
// 与 DashboardView 共享同一单例(模块级状态),仅用 scanNow,不 start()
const deviceMonitor = useDeviceMonitor()

const statusScanning = ref(false)

async function onScanStatuses() {
  if (statusScanning.value) return
  statusScanning.value = true
  try {
    const statuses = await deviceMonitor.scanNow()
    if (statuses) {
      const total = Object.keys(statuses).length
      const online = Object.values(statuses).filter((s) => s === 'online').length
      ElMessage.success(`状态已刷新：${online}/${total} 台在线`)
    }
  } catch {
    ElMessage.error('设备状态扫描失败')
  } finally {
    statusScanning.value = false
  }
}

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

// ---- Rendered-rows model for CSS Grid layout (multi-U spans) ----
type RenderedRow =
  | { u: number; type: 'chassis'; device: Device; heightU: number }
  | { u: number; type: 'overflow'; device: Device }
  | { u: number; type: 'empty' }

// 布局算法单一真源:utils/rackLayout.ts（与 RackCabinet 门后预览共用）。
// resolveRackLayout 已处理 null 顺延、冲突重排、全满越界(start=null)。
const renderedRows = computed<RenderedRow[]>(() => {
  if (!selectedRack.value) return []
  const positioned = resolveRackLayout(selectedRack.value.devices ?? [])

  const rows: RenderedRow[] = []
  let cursor = 0 // 指向 RACK_SLOT_STARTS 的游标,逐槽推进
  for (const item of positioned) {
    // 先补齐 start 之前的空槽
    while (cursor < RACK_SLOT_STARTS.length && RACK_SLOT_STARTS[cursor] < (item.start ?? RACK_CAPACITY_U + 1)) {
      rows.push({ u: RACK_SLOT_STARTS[cursor], type: 'empty' })
      cursor += 1
    }
    if (item.start == null || cursor >= RACK_SLOT_STARTS.length) {
      // 机柜已满仍多出来的设备(后端 24U 已满时不允许新增,这里只兜底
      // 历史脏数据/并发写入,渲染到末尾但不占格)
      rows.push({ u: RACK_CAPACITY_U + 1, type: 'overflow', device: item.device })
      continue
    }
    rows.push({ u: item.start, type: 'chassis', device: item.device, heightU: RACK_DEVICE_SIZE_U })
    cursor += 1
  }
  while (cursor < RACK_SLOT_STARTS.length) {
    rows.push({ u: RACK_SLOT_STARTS[cursor], type: 'empty' })
    cursor += 1
  }
  return rows
})

// ---- Device helpers ----
// 后端 schema 把 status 序列化成严格二值(online/offline)，不需要再匹配
// running/active/critical/maintenance——那些是已下线状态的死分支。
function statusClass(status: string): string {
  return String(status).toLowerCase() === 'online' ? 'status-online' : 'status-offline'
}

// ---- Navigation ----

function emitNavState() {
  emit('nav-change', {
    roomId: currentRoom.value?.id,
    rackId: selectedRack.value?.id,
    deviceId: deviceStore.currentDevice?.id,
  })
}

// ---- Cabinet open-door orchestration ----
const openingRackId = ref<number | null>(null)

function onCabinetClick(rack: Rack) {
  if (openingRackId.value != null) return // 开门动画进行中，忽略重复点击
  if (rack.type === 'shelf') {
    // 货架无门，直接进入
    openRackDetail(rack)
    return
  }
  openingRackId.value = rack.id // 触发 RackCabinet 的 is-open → 播开门动画
  setTimeout(() => {
    openRackDetail(rack) // 开门动画结束后再切到 U 位视图
    openingRackId.value = null
  }, 560) // 略大于开门 transition(.55s)
}

function openRackDetail(rack: Rack) {
  selectedRack.value = rack
  viewMode.value = 'rack'
  emit('rack-click', { rack })
  emitNavState()

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

function onRoomContext(_e: MouseEvent) {
  // No context menu for room header currently
}

function onRackContext(e: MouseEvent, rack: Rack) {
  contextMenu.position = { x: e.clientX, y: e.clientY }
  const items = [
    { label: '编辑机柜', action: 'edit-rack', icon: Edit, perm: 'device:manage' },
    { label: '删除机柜', action: 'delete-rack', icon: Delete, perm: 'device:manage' },
  ]
  contextMenu.items = items.filter((i) => !i.perm || authStore.hasPermission(i.perm)) as {
    label: string
    action: string
    icon: unknown
  }[]
  contextMenu.target = { type: 'rack', id: rack.id, data: rack }
  contextMenu.visible = true
}

function onDeviceContext(e: MouseEvent, device: Device) {
  contextMenu.position = { x: e.clientX, y: e.clientY }
  const items = [
    { label: '编辑设备', action: 'edit-device', icon: Edit, perm: 'device:manage' },
    { label: '删除设备', action: 'delete-device', icon: Delete, perm: 'device:manage' },
  ]
  contextMenu.items = items.filter((i) => !i.perm || authStore.hasPermission(i.perm)) as {
    label: string
    action: string
    icon: unknown
  }[]
  contextMenu.target = { type: 'device', id: device.id, data: device }
  contextMenu.visible = true
}

const ctxMenuStyle = computed(() => {
  const MENU_W = 160
  const MENU_H = contextMenu.items.length * 34 + 8
  const x = Math.min(contextMenu.position.x, window.innerWidth - MENU_W - 8)
  const y = Math.min(contextMenu.position.y, window.innerHeight - MENU_H - 8)
  return {
    left: `${Math.max(8, x)}px`,
    top: `${Math.max(8, y)}px`,
  }
})

async function handleContextAction(action: string) {
  contextMenu.visible = false
  const target = contextMenu.target
  if (!target) return

  if (action === 'edit-rack' && target.id) {
    const freshRack = rackList.value.find((r) => r.id === target.id)
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
    const freshDevice = devicesInRack.value.find((d) => d.id === target.id)
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
const deviceFormRackType = ref<Rack['type']>('cabinet')
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
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
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
  const nextPosition = selectedRack.value.type === 'cabinet' ? findNextDevicePosition() : null
  if (selectedRack.value.type === 'cabinet' && nextPosition == null) {
    ElMessage.warning('当前 24U 机柜已满')
    return
  }
  deviceFormRackId.value = selectedRack.value.id
  deviceFormData.value = null
  deviceFormRackType.value = selectedRack.value.type
  deviceFormPresetU.value = nextPosition
  deviceFormVisible.value = true
}

function handleCreateDeviceAt(u: number) {
  if (!selectedRack.value) return
  deviceFormRackId.value = selectedRack.value.id
  deviceFormData.value = null
  deviceFormRackType.value = selectedRack.value.type
  deviceFormPresetU.value = snapSlotStart(u)
  deviceFormVisible.value = true
}

function findNextDevicePosition(): number | null {
  return nextFreeSlot(devicesInRack.value)
}

function handleDeviceDragStart(event: DragEvent, device: Device) {
  if (!authStore.hasPermission('device:manage')) {
    event.preventDefault()
    return
  }
  event.dataTransfer?.setData('text/plain', String(device.id))
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
}

async function handleDeviceDrop(event: DragEvent, targetU: number) {
  const deviceId = Number(event.dataTransfer?.getData('text/plain'))
  if (!deviceId) return
  const target = snapSlotStart(targetU)
  const dragged = devicesInRack.value.find((device) => device.id === deviceId)
  if (!dragged) return
  const source =
    renderedRows.value.filter((row) => row.type === 'chassis').find((row) => row.device.id === deviceId)?.u ??
    snapSlotStart(dragged.position_u ?? 1)
  if (source === target) return
  const targetDevice = renderedRows.value
    .filter((row) => row.type === 'chassis')
    .find((row) => row.u === target)?.device
  const positions = [{ device_id: dragged.id, position_u: target }]
  if (targetDevice && targetDevice.id !== dragged.id) {
    positions.push({ device_id: targetDevice.id, position_u: source })
  }
  try {
    if (!selectedRack.value) return
    await deviceAPI.reorder(selectedRack.value.id, positions)
    await onFormSaved()
  } catch {
    ElMessage.error('设备位置更新失败')
  }
}

async function onFormSaved() {
  await roomStore.fetchRooms()
  // Always sync currentRoom to fresh data
  const targetId = currentRoom.value?.id
  if (targetId) {
    const freshRoom = roomStore.rooms.find((r) => r.id === targetId)
    if (freshRoom) roomStore.setCurrentRoom(freshRoom)
  }
  // Refresh selected rack if in rack detail view
  if (viewMode.value === 'rack' && selectedRack.value) {
    const room = roomStore.rooms.find((r) => r.id === selectedRack.value!.room_id)
    if (room) {
      const rack = room.racks?.find((r) => r.id === selectedRack.value!.id)
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

// ---- Click outside / Escape to close context menu ----
function onDocumentClick() {
  closeContextMenu()
}

function onDocumentKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') closeContextMenu()
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  document.addEventListener('keydown', onDocumentKeydown)
})

onUnmounted(() => {
  document.removeEventListener('click', onDocumentClick)
  document.removeEventListener('keydown', onDocumentKeydown)
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

/* ---- Cabinet grid (机柜阵列) ---- */
.cabinet-grid {
  flex: 1;
  overflow-y: auto;
  padding: var(--dcn-space-5) var(--dcn-space-6);
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: var(--dcn-space-5);
  align-content: start;
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
  grid-template-rows: repeat(var(--capacity, 24), 24px);
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
  border-left: 0;
  border-right: 0;
  border-top: 0;
  color: inherit;
  font: inherit;
  text-align: left;
}
.u-slot-empty-cell:hover {
  background: var(--dcn-primary-bg-deep);
}
.u-slot-empty-cell:focus-visible {
  outline: 2px solid var(--dcn-primary);
  outline-offset: -2px;
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

/* 溢出设备（机柜已满）：附在网格末尾的告警行，不占 U 位 */
.u-slot-overflow {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  padding: var(--dcn-space-2) var(--dcn-space-3);
  margin-top: var(--dcn-space-2);
  background: var(--dcn-bg-card);
  border: 1px dashed var(--dcn-danger);
  border-radius: var(--dcn-radius-md);
  cursor: pointer;
}
.u-slot-overflow-name {
  font-size: var(--dcn-text-sm);
  font-weight: 500;
  color: var(--dcn-text-primary);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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

.status-online {
  background: var(--dcn-dot-online);
  box-shadow: 0 0 4px rgba(103, 194, 58, 0.5);
}
.status-offline {
  background: var(--dcn-dot-offline);
}

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
  width: 100%;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
}

.ctx-menu__item:hover {
  background-color: var(--dcn-primary-bg);
  color: var(--dcn-primary);
}
</style>
