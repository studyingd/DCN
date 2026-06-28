import { ref, onUnmounted } from 'vue'
import { monitorAPI } from '@/api'

// ── Types ──

export interface PortStatus {
  name: string
  status: 'up' | 'down'
}

export interface InterfaceStatusEntry {
  device_id: number
  status: string                 // device-level online/offline
  fetched_at: string | null
  interfaces: PortStatus[]
  total: number
  up: number
  down: number
  source: 'ssh' | 'cache' | 'unavailable'
  error: string | null
}

export interface InterfaceStatusMap {
  [deviceId: number]: InterfaceStatusEntry
}

export interface InterfaceStatusResponse {
  fetched_at: string
  ttl_seconds: number
  devices: Record<string, InterfaceStatusEntry>
}

// ── Composable ──

const POLL_INTERVAL_MS = 30_000  // matches useDeviceMonitor

export function useInterfaceStatus() {
  const map = ref<InterfaceStatusMap>({})
  const loading = ref(false)
  let timer: ReturnType<typeof setInterval> | null = null
  let currentRackId: number | null = null

  async function refresh() {
    if (currentRackId == null) return
    loading.value = true
    try {
      const res = await monitorAPI.getInterfaces(currentRackId)
      const next: InterfaceStatusMap = {}
      for (const [k, v] of Object.entries(res.data.devices)) {
        next[Number(k)] = v
      }
      map.value = next
    } catch {
      // silent fail — chassis still renders without LEDs
    } finally {
      loading.value = false
    }
  }

  function start(rackId: number) {
    if (currentRackId === rackId && timer) return
    stop()
    currentRackId = rackId
    refresh()
    timer = setInterval(refresh, POLL_INTERVAL_MS)
  }

  function stop() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
    currentRackId = null
    map.value = {}
  }

  // Auto-cleanup on component unmount
  onUnmounted(stop)

  return { map, loading, refresh, start, stop }
}
