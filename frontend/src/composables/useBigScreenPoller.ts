import { ref } from 'vue'
import { businessAPI, dashboardAPI } from '@/api'
import type { DashboardOverview, DeviceTypeItem, RoomSummaryItem } from '@/types/dashboard'
import type { BusinessHealthItem } from '@/types/business'

const POLL_INTERVAL_MS = 30_000

// ── Shared singleton state ─────────────────────────────────────────
// Multiple components (HomePanel, BigScreenView) may mount simultaneously.
// Using module-level state ensures only one poller runs and both consume
// the same data, preventing duplicate API requests.
const overview = ref<DashboardOverview>({
  room_count: 0,
  rack_count: 0,
  device_total: 0,
  device_online: 0,
  device_offline: 0,
  device_maintenance: 0,
  user_count: 0,
  online_user_count: 0,
  pve_guest_total: 0,
  pve_guest_running: 0,
  pve_guest_stopped: 0,
  pve_platform_count: 0,
  alert_events_7d: 0,
})
const distribution = ref<DeviceTypeItem[]>([])
const rooms = ref<RoomSummaryItem[]>([])
const businesses = ref<BusinessHealthItem[]>([])
const lastUpdated = ref('')
const loading = ref(false)
const error = ref('')

let timerId: ReturnType<typeof setInterval> | null = null
let _refCount = 0
let refreshInFlight: Promise<void> | null = null

async function refresh() {
  if (refreshInFlight) return refreshInFlight
  const request = (async () => {
    loading.value = true
    error.value = ''
    try {
      const results = await Promise.allSettled([
        dashboardAPI.getOverview(),
        dashboardAPI.getDeviceTypeDistribution(),
        dashboardAPI.getRoomSummary(),
        businessAPI.list(),
      ])

      const [overviewRes, distributionRes, roomsRes, businessesRes] = results
      if (overviewRes.status === 'fulfilled') overview.value = overviewRes.value.data
      if (distributionRes.status === 'fulfilled') distribution.value = distributionRes.value.data.distribution
      if (roomsRes.status === 'fulfilled') rooms.value = roomsRes.value.data.rooms
      if (businessesRes.status === 'fulfilled') businesses.value = businessesRes.value.data

      const failed = results.filter((result) => result.status === 'rejected').length
      const succeeded = results.length - failed
      if (failed > 0) {
        error.value =
          failed === results.length
            ? '首页数据暂时无法加载，请检查登录状态或后端服务。'
            : `${failed} 项首页数据暂时无法加载，已显示其余可用数据。`
      }
      if (succeeded > 0) {
        lastUpdated.value = new Date().toLocaleString('zh-CN', { hour12: false })
      }
    } catch {
      error.value = '首页数据暂时无法加载，请检查登录状态或后端服务。'
    } finally {
      loading.value = false
    }
  })()
  refreshInFlight = request
  try {
    await request
  } finally {
    if (refreshInFlight === request) refreshInFlight = null
  }
}

function start() {
  _refCount++
  if (timerId) return // Already polling
  refresh()
  timerId = setInterval(() => {
    if (!document.hidden) void refresh()
  }, POLL_INTERVAL_MS)
}

function stop() {
  _refCount = Math.max(0, _refCount - 1)
  if (_refCount > 0) return // Another consumer is still active
  if (timerId !== null) {
    clearInterval(timerId)
    timerId = null
  }
}

export function useBigScreenPoller() {
  return {
    overview,
    distribution,
    rooms,
    businesses,
    lastUpdated,
    loading,
    error,
    start,
    stop,
    refresh,
  }
}
