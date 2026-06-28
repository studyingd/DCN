import { ref } from 'vue'
import { dashboardAPI } from '@/api'
import type {
  DashboardOverview,
  DeviceTypeItem,
  RoomSummaryItem,
  TopologyNode,
  TopologyEdge,
  AuditEventItem,
  LoginTrendItem,
} from '@/types/dashboard'

const POLL_INTERVAL_MS = 30_000

// ── Shared singleton state ─────────────────────────────────────────
// Multiple components (HomePanel, BigScreenView) may mount simultaneously.
// Using module-level state ensures only one poller runs and both consume
// the same data, preventing duplicate API requests.
const overview = ref<DashboardOverview>({
  room_count: 0, rack_count: 0, device_total: 0,
  device_online: 0, device_offline: 0, device_maintenance: 0,
  connection_count: 0, user_count: 0, online_user_count: 0,
})
const distribution = ref<DeviceTypeItem[]>([])
const rooms = ref<RoomSummaryItem[]>([])
const topologyNodes = ref<TopologyNode[]>([])
const topologyEdges = ref<TopologyEdge[]>([])
const recentEvents = ref<AuditEventItem[]>([])
const eventCountsToday = ref<Record<string, number>>({})
const loginTrend = ref<LoginTrendItem[]>([])
const lastUpdated = ref('')
const loading = ref(false)

let timerId: ReturnType<typeof setInterval> | null = null
let _refCount = 0

async function refresh() {
  loading.value = true
  try {
    const [
      overviewRes, distributionRes, roomsRes,
      topologyRes, auditRes, trendRes,
    ] = await Promise.all([
      dashboardAPI.getOverview(),
      dashboardAPI.getDeviceTypeDistribution(),
      dashboardAPI.getRoomSummary(),
      dashboardAPI.getConnectionTopology(),
      dashboardAPI.getAuditTimeline(),
      dashboardAPI.getLoginTrend(7),
    ])

    overview.value = overviewRes.data
    distribution.value = distributionRes.data.distribution
    rooms.value = roomsRes.data.rooms
    topologyNodes.value = topologyRes.data.nodes
    topologyEdges.value = topologyRes.data.edges
    recentEvents.value = auditRes.data.recent_events
    eventCountsToday.value = auditRes.data.event_counts_today
    loginTrend.value = trendRes.data.trend
    lastUpdated.value = new Date().toLocaleString('zh-CN', { hour12: false })
  } catch {
    // Silently ignore — network blip or auth expiry
  } finally {
    loading.value = false
  }
}

function start() {
  _refCount++
  if (timerId) return  // Already polling
  refresh()
  timerId = setInterval(refresh, POLL_INTERVAL_MS)
}

function stop() {
  _refCount = Math.max(0, _refCount - 1)
  if (_refCount > 0) return  // Another consumer is still active
  if (timerId !== null) {
    clearInterval(timerId)
    timerId = null
  }
}

export function useBigScreenPoller() {
  return {
    overview, distribution, rooms,
    topologyNodes, topologyEdges,
    recentEvents, eventCountsToday, loginTrend,
    lastUpdated, loading,
    start, stop, refresh,
  }
}
