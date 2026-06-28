import { ref } from 'vue'
import { monitorAPI } from '@/api'
import { useRoomStore } from '@/stores/room'
import { useDeviceStore } from '@/stores/device'

const POLL_INTERVAL_MS = 30_000

export function useDeviceMonitor() {
  const roomStore = useRoomStore()
  const deviceStore = useDeviceStore()
  const polling = ref(false)

  let timerId: ReturnType<typeof setInterval> | null = null

  function mergeStatuses(statusMap: Record<string, string>) {
    for (const room of roomStore.rooms) {
      for (const rack of room.racks ?? []) {
        for (const device of rack.devices ?? []) {
          const newStatus = statusMap[String(device.id)]
          if (newStatus && newStatus !== device.status) {
            device.status = newStatus
          }
        }
      }
    }

    if (deviceStore.currentDevice) {
      const newStatus = statusMap[String(deviceStore.currentDevice.id)]
      if (newStatus) {
        deviceStore.currentDevice.status = newStatus
      }
    }
  }

  async function poll() {
    try {
      const res = await monitorAPI.getStatuses()
      mergeStatuses(res.data.statuses)
    } catch {
      // Silently ignore — network blip or auth expiry
    }
  }

  function start() {
    if (polling.value) return
    polling.value = true
    poll()
    timerId = setInterval(poll, POLL_INTERVAL_MS)
  }

  function stop() {
    if (timerId !== null) {
      clearInterval(timerId)
      timerId = null
    }
    polling.value = false
  }

  return { polling, start, stop, refresh: poll }
}
