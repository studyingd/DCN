import { ref } from 'vue'
import { monitorAPI } from '@/api'
import { useRoomStore } from '@/stores/room'
import { useDeviceStore } from '@/stores/device'

const POLL_INTERVAL_MS = 30_000

// ---- 模块级单例状态 ----
// DashboardView 持有 start/stop 生命周期；其它组件（如机房场景的
// 「刷新状态」按钮）通过 useDeviceMonitor() 拿到的是同一份状态与函数，
// 调用 scanNow/refresh 不会额外起第二个轮询定时器。
const polling = ref(false)
let timerId: ReturnType<typeof setInterval> | null = null
let pollInFlight = false
let scanInFlight = false
let deviceIndex = new Map<number, { status: string }>()
let indexedRoomCount = -1
let indexedDeviceCount = -1
// 设备集合的指纹:仅靠"房间数 + 设备总数"判失效会漏掉「同一次刷新里
// 删一台又加一台」(总数不变)的情况,导致新设备永远拿不到状态、旧设备
// 的引用指向已分离对象。改为比较排序后的 id 序列。
let indexedDeviceKey = ''

function rebuildIndex() {
  const roomStore = useRoomStore()
  let count = 0
  const ids: number[] = []
  const next = new Map<number, { status: string }>()
  for (const room of roomStore.rooms) {
    for (const rack of room.racks ?? []) {
      for (const device of rack.devices ?? []) {
        count += 1
        ids.push(device.id)
        next.set(device.id, device)
      }
    }
  }
  deviceIndex = next
  indexedRoomCount = roomStore.rooms.length
  indexedDeviceCount = count
  indexedDeviceKey = ids.sort((a, b) => a - b).join(',')
}

function mergeStatuses(statusMap: Record<string, string>) {
  const roomStore = useRoomStore()
  const deviceStore = useDeviceStore()
  let count = 0
  const ids: number[] = []
  for (const room of roomStore.rooms)
    for (const rack of room.racks ?? [])
      for (const device of rack.devices ?? []) {
        count += 1
        ids.push(device.id)
      }
  const key =
    indexedRoomCount !== roomStore.rooms.length || indexedDeviceCount !== count
      ? null // 计数已变,必然重建,无需再算指纹
      : ids.sort((a, b) => a - b).join(',')
  if (key === null || key !== indexedDeviceKey) rebuildIndex()
  for (const [id, device] of deviceIndex) {
    const newStatus = statusMap[String(id)]
    if (newStatus && newStatus !== device.status) device.status = newStatus
  }

  if (deviceStore.currentDevice) {
    const newStatus = statusMap[String(deviceStore.currentDevice.id)]
    if (newStatus) {
      deviceStore.currentDevice.status = newStatus
    }
  }
}

async function poll() {
  if (pollInFlight) return
  pollInFlight = true
  try {
    const res = await monitorAPI.getStatuses()
    mergeStatuses(res.data.statuses)
  } catch {
    // Silently ignore — network blip or auth expiry
  } finally {
    pollInFlight = false
  }
}

// 立即触发一次后端端口探测扫描并合并结果（POST /api/monitor/scan）。
// 与周期轮询的区别:poll 只读上一轮扫描的内存缓存,scanNow 会真的重新
// 探测全部设备,离线设备的探测要等端口超时,可能需要数秒。
async function scanNow(): Promise<Record<string, string> | undefined> {
  if (scanInFlight) return undefined
  scanInFlight = true
  try {
    const res = await monitorAPI.triggerScan()
    mergeStatuses(res.data.statuses)
    return res.data.statuses
  } finally {
    scanInFlight = false
  }
}

function onVisibilityChange() {
  // 标签页切回前台立即拉一次,消除「回来后还要等下一个轮询 tick」的延迟
  if (!document.hidden) void poll()
}

function start() {
  if (polling.value) return
  polling.value = true
  poll()
  document.addEventListener('visibilitychange', onVisibilityChange)
  timerId = setInterval(() => {
    if (!document.hidden) void poll()
  }, POLL_INTERVAL_MS)
}

function stop() {
  if (timerId !== null) {
    clearInterval(timerId)
    timerId = null
  }
  document.removeEventListener('visibilitychange', onVisibilityChange)
  polling.value = false
}

export function useDeviceMonitor() {
  return { polling, start, stop, refresh: poll, scanNow }
}
