<template>
  <div class="device-selector">
    <!-- 侧栏:机房分组 + 虚拟化平台分组 -->
    <div class="device-selector-sidebar">
      <div class="device-selector-section-title">机房</div>
      <div
        v-for="room in rooms"
        :key="room.id"
        class="device-selector-item"
        :class="{ active: pane === 'room' && selectedRoomId === room.id }"
        @click="selectRoom(room.id)"
      >
        {{ room.name }}
      </div>
      <el-empty v-if="rooms.length === 0" description="暂无机房" :image-size="40" />
      <template v-if="connGroups.length > 0">
        <div class="device-selector-section-title">虚拟化</div>
        <div
          v-for="conn in connGroups"
          :key="conn.key"
          class="device-selector-item"
          :class="{ active: pane === 'pve' && selectedConnId === conn.connection_id }"
          @click="selectConn(conn.connection_id)"
        >
          {{ conn.label }}
        </div>
      </template>
    </div>

    <!-- 机房 → 机架 → 设备卡片 -->
    <div v-if="pane === 'room' && selectedRoomId" class="device-selector-main">
      <div class="device-selector-header">
        <el-checkbox v-model="allChecked" :indeterminate="indeterminate" @change="onAllChange"
          >全选 ({{ roomDevices.length }} 台设备)</el-checkbox
        >
      </div>
      <div v-loading="devicesLoading" class="device-selector-body">
        <template v-for="rackGroup in groupedByRack" :key="rackGroup.rack_id">
          <div class="rack-group">
            <div class="rack-group-header">
              <span class="rack-group-name">{{ rackGroup.rack_name }}</span>
              <span class="rack-group-count">{{ rackGroup.devices.length }} 台</span>
            </div>
            <div class="rack-devices">
              <div
                v-for="dev in rackGroup.devices"
                :key="dev.id"
                class="device-card"
                :class="{ checked: tempIds.has(dev.id), disabled: !selectable(dev.id, false) }"
                @click="toggle(dev.id, false)"
              >
                <el-checkbox
                  :model-value="tempIds.has(dev.id)"
                  :disabled="!selectable(dev.id, false)"
                  @click.stop
                  @change="() => toggle(dev.id, false)"
                />
                <div class="device-card-info">
                  <div class="device-card-name">{{ dev.name }}</div>
                  <div class="device-card-meta">
                    <span class="device-card-ip">{{ dev.ip_address || '-' }}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </template>
        <el-empty v-if="roomDevices.length === 0 && !devicesLoading" description="该机房暂无设备" :image-size="60" />
      </div>
    </div>

    <!-- 虚拟化平台 → 虚拟机卡片 -->
    <div v-else-if="pane === 'pve' && selectedConnId" class="device-selector-main">
      <div class="device-selector-header">
        <el-checkbox v-model="guestAllChecked" :indeterminate="guestIndeterminate" @change="onGuestAllChange"
          >全选 ({{ connGuests.length }} 台虚拟机)</el-checkbox
        >
      </div>
      <div v-loading="guestsLoading" class="device-selector-body">
        <div class="rack-group">
          <div class="rack-group-header">
            <span class="rack-group-name">{{ activeConnLabel }}</span>
            <span class="rack-group-count">{{ connGuests.length }} 台</span>
          </div>
          <div class="rack-devices">
            <div
              v-for="guest in connGuests"
              :key="guest.id"
              class="device-card"
              :class="{ checked: tempIds.has(guest.id), disabled: !selectable(guest.id, true, guest) }"
              @click="toggle(guest.id, true, guest)"
            >
              <el-checkbox
                :model-value="tempIds.has(guest.id)"
                :disabled="!selectable(guest.id, true, guest)"
                @click.stop
                @change="() => toggle(guest.id, true, guest)"
              />
              <div class="device-card-info">
                <div class="device-card-name">{{ guest.name }}</div>
                <div class="device-card-meta">
                  <span class="device-type-tag tag-vm"> {{ 'VM' }} {{ guest.vmid }} </span>
                  <span class="device-card-ip">{{ guest.ip_address || guest.node || '-' }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
        <el-empty v-if="connGuests.length === 0 && !guestsLoading" description="该平台暂无虚拟机" :image-size="60" />
      </div>
    </div>

    <div v-else class="device-selector-main">
      <el-empty description="请选择机房或虚拟化平台" :image-size="60" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { roomAPI, deviceAPI, automationAPI } from '@/api'

/**
 * 统一目标选择器(内联):机房设备(正数 id) + 虚拟化平台虚拟机(合成负数 id)。
 * 复刻角色管理的分组卡片选择体验。勾选即通过 confirm 事件实时产出单一 id 数组
 * (设备 id 与虚拟机合成 id 混合),由调用方直接提交给自动化/告警等接口。
 *
 * 虚拟机合成 id 公式与后端一致: -(connection_id * 1_000_000 + vmid)。
 */

const props = defineProps<{
  /** 已选中的 id 列表(回显/外部修改时同步)。 */
  selectedIds: number[]
  /** 判断某目标是否可选(按任务类型)。isGuest 标识是否虚拟机。 */
  selectableFn?: (id: number, isGuest: boolean, guest?: GuestItem) => boolean
  /**
   * 虚拟机候选注入接口:默认走 automationAPI.devices()(需 automation/
   * device:remote 权限)。权限面不同的调用方(如业务监控,只需 pve 权限)
   * 可传入自己的候选加载器,返回结构同 GuestItem(机房/设备侧不注入,
   * 仍走 roomAPI/deviceAPI)。注入的加载器需自行过滤 ACL 与已停用连接。
   */
  loadGuests?: () => Promise<GuestItem[]>
}>()

const emit = defineEmits<{
  (e: 'confirm', ids: number[]): void
}>()

interface RoomItem {
  id: number
  name: string
}
interface DeviceItem {
  id: number
  name: string
  ip_address: string | null
  rack_id: number
  rack_name: string
  os_system?: string | null
  has_credential?: boolean
  /** Windows 设备最近一轮采集明确失败(WinRM 确认不通) */
  metrics_failed?: boolean
}
interface GuestItem {
  id: number
  connection_id: number
  guest_type: string
  vmid: number
  name: string
  node: string | null
  ip_address: string | null
  has_credential?: boolean
}

// 虚拟机合成负数 id 由后端 candidates 接口直接返回,前端无需自行合成。

const rooms = ref<RoomItem[]>([])
const guests = ref<GuestItem[]>([])
const roomDevices = ref<DeviceItem[]>([])
const pane = ref<'room' | 'pve' | null>(null)
const selectedRoomId = ref<number | null>(null)
const selectedConnId = ref<number | null>(null)
const devicesLoading = ref(false)
const guestsLoading = ref(false)
const tempIds = ref<Set<number>>(new Set(props.selectedIds))

// 暴露给调用方的 selectableFn 使用:跨机房累积的设备候选(id -> 元数据),
// 否则调用方(如告警中心)拿不到 metrics_failed 等通道状态,无法做置灰判断。
const deviceCache = new Map<number, DeviceItem>()
watch(
  roomDevices,
  (items) => {
    for (const d of items) deviceCache.set(d.id, d)
  },
  { deep: false },
)

// 同 deviceCache:跨连接累积的虚拟机候选(id -> 元数据,含 has_credential),
// 供调用方(告警中心)在切换指标时清理已选但当前不可选的虚拟机目标。
const guestCache = new Map<number, GuestItem>()
watch(
  guests,
  (items) => {
    for (const g of items) guestCache.set(g.id, g)
  },
  { deep: false },
)

defineExpose({ deviceCache, guestCache })

function selectable(id: number, isGuest: boolean, guest?: GuestItem): boolean {
  if (props.selectableFn) return props.selectableFn(id, isGuest, guest)
  return true
}

const groupedByRack = computed(() => {
  const map = new Map<number, { rack_id: number; rack_name: string; devices: DeviceItem[] }>()
  for (const d of roomDevices.value) {
    if (!map.has(d.rack_id)) map.set(d.rack_id, { rack_id: d.rack_id, rack_name: d.rack_name, devices: [] })
    map.get(d.rack_id)!.devices.push(d)
  }
  return [...map.values()]
})

const connGroups = computed(() => {
  const byConn = new Map<number, GuestItem[]>()
  for (const g of guests.value) {
    if (!byConn.has(g.connection_id)) byConn.set(g.connection_id, [])
    byConn.get(g.connection_id)!.push(g)
  }
  return [...byConn].map(([connId, items]) => ({
    key: `pve-${connId}`,
    connection_id: connId,
    label: (items[0] as any)?.connection_name || `PVE ${connId}`,
    items,
  }))
})

const connGuests = computed(() => connGroups.value.find((g) => g.connection_id === selectedConnId.value)?.items || [])
const activeConnLabel = computed(
  () => connGroups.value.find((g) => g.connection_id === selectedConnId.value)?.label || '',
)

// 全选状态(设备)
const allChecked = computed({
  get: () => roomDevices.value.length > 0 && roomDevices.value.every((d) => tempIds.value.has(d.id)),
  set: () => {},
})
const indeterminate = computed(() => {
  const c = roomDevices.value.filter((d) => tempIds.value.has(d.id)).length
  return c > 0 && c < roomDevices.value.length
})
function onAllChange(val: boolean) {
  for (const d of roomDevices.value) {
    if (!selectable(d.id, false)) continue
    if (val) tempIds.value.add(d.id)
    else tempIds.value.delete(d.id)
  }
  emitSelection()
}

// 全选状态(虚拟机)
const guestAllChecked = computed({
  get: () => connGuests.value.length > 0 && connGuests.value.every((g) => tempIds.value.has(g.id)),
  set: () => {},
})
const guestIndeterminate = computed(() => {
  const c = connGuests.value.filter((g) => tempIds.value.has(g.id)).length
  return c > 0 && c < connGuests.value.length
})
function onGuestAllChange(val: boolean) {
  for (const g of connGuests.value) {
    if (!selectable(g.id, true, g)) continue
    if (val) tempIds.value.add(g.id)
    else tempIds.value.delete(g.id)
  }
  emitSelection()
}

function toggle(id: number, isGuest: boolean, guest?: GuestItem) {
  if (!selectable(id, isGuest, guest)) return
  if (tempIds.value.has(id)) tempIds.value.delete(id)
  else tempIds.value.add(id)
  emitSelection()
}

function emitSelection() {
  emit('confirm', [...tempIds.value])
}

async function selectRoom(roomId: number) {
  pane.value = 'room'
  selectedRoomId.value = roomId
  devicesLoading.value = true
  roomDevices.value = []
  try {
    const res = await deviceAPI.listByRoom(roomId)
    roomDevices.value = (res.data || []).map((d: any) => ({
      id: d.id,
      name: d.name,
      ip_address: d.ip_address,
      rack_id: d.rack_id,
      rack_name: d.rack_name,
      os_system: d.os_system,
      has_credential: d.has_credential,
      metrics_failed: d.metrics_failed,
    }))
  } catch {
    roomDevices.value = []
  } finally {
    devicesLoading.value = false
  }
}

function selectConn(connId: number) {
  pane.value = 'pve'
  selectedConnId.value = connId
}

async function loadData() {
  try {
    const res = await roomAPI.list()
    rooms.value = (res.data || []).map((r: any) => ({ id: r.id, name: r.name }))
  } catch {
    rooms.value = []
  }
  guestsLoading.value = true
  try {
    if (props.loadGuests) {
      // 调用方注入:业务监控等权限面不同的场景(pve 权限即可,不必
      // 拿 automation 权限),结构同 GuestItem,已含 ACL 过滤。
      guests.value = await props.loadGuests()
    } else {
      // 用自动化的候选接口:它返回带合成负数 id 的虚拟机,且已过 ACL 过滤。
      const res = await automationAPI.devices()
      guests.value = (res.data || [])
        .filter((d: any) => d.target_type === 'pve_guest')
        .map((d: any) => ({
          id: d.id,
          connection_id: d.pve_connection_id,
          guest_type: d.pve_guest_type || 'qemu',
          vmid: d.pve_vmid,
          name: d.name,
          node: d.pve_node,
          ip_address: d.ip_address,
          has_credential: d.has_credential,
          connection_name: (d.name || '').match(/^\[(.+?)\]/)?.[1] || '',
        }))
    }
  } catch {
    guests.value = []
  } finally {
    guestsLoading.value = false
  }
  // 默认选中第一个机房(没有机房则选第一个虚拟化平台),免去一次点击。
  if (rooms.value.length > 0) await selectRoom(rooms.value[0].id)
  else if (connGroups.value.length > 0) selectConn(connGroups.value[0].connection_id)
}

// 外部清空/移除已选(芯片关闭、清空按钮)时同步回显。
watch(
  () => props.selectedIds,
  (ids) => {
    tempIds.value = new Set(ids)
  },
)

onMounted(loadData)
</script>

<style scoped>
.device-selector {
  display: flex;
  gap: var(--dcn-space-3);
  min-height: 320px;
}
.device-selector-sidebar {
  width: 180px;
  flex-shrink: 0;
  border-right: 1px solid var(--dcn-border);
  padding-right: var(--dcn-space-2);
  overflow-y: auto;
  max-height: 420px;
}
.device-selector-section-title {
  font-size: 12px;
  color: var(--dcn-text-secondary);
  padding: 8px 8px 4px;
  font-weight: 600;
}
.device-selector-item {
  padding: 7px 10px;
  border-radius: var(--dcn-radius-sm);
  cursor: pointer;
  color: var(--dcn-text-primary);
  font-size: 13px;
  transition: background 0.15s;
}
.device-selector-item:hover {
  background: var(--dcn-bg-hover);
}
.device-selector-item.active {
  background: var(--dcn-bg-active);
  color: var(--dcn-primary);
  font-weight: 600;
}
.device-selector-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.device-selector-header {
  padding-bottom: var(--dcn-space-2);
  border-bottom: 1px solid var(--dcn-border);
}
.device-selector-body {
  flex: 1;
  overflow-y: auto;
  max-height: 380px;
  padding-top: var(--dcn-space-2);
  /* 右侧留出滚动条与卡片之间的呼吸位,否则滑块贴着最右列卡片边缘 */
  padding-right: var(--dcn-space-2);
  padding-bottom: var(--dcn-space-2);
}
.rack-group {
  margin-bottom: var(--dcn-space-3);
}
.rack-group-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 2px;
  margin-bottom: 6px;
}
.rack-group-name {
  font-weight: 600;
  font-size: 13px;
  color: var(--dcn-text-primary);
}
.rack-group-count {
  font-size: 12px;
  color: var(--dcn-text-secondary);
}
.rack-devices {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 8px;
}
.device-card {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-sm);
  cursor: pointer;
  transition: all 0.15s;
  background: var(--dcn-bg-section);
}
.device-card:hover {
  border-color: var(--dcn-primary);
}
.device-card.checked {
  border-color: var(--dcn-primary);
  background: var(--dcn-bg-active);
}
.device-card.disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.device-card-info {
  flex: 1;
  min-width: 0;
}
.device-card-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--dcn-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.device-card-meta {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-top: 2px;
}
.device-card-ip {
  font-size: 11px;
  color: var(--dcn-text-secondary);
}
.device-type-tag {
  font-size: 11px;
  padding: 0 5px;
  border-radius: 3px;
  background: var(--dcn-bg-hover);
  color: var(--dcn-text-secondary);
}
.tag-vm {
  background: rgba(139, 92, 246, 0.15);
  color: #8b5cf6;
}
</style>
