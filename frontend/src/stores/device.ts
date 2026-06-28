import { ref } from 'vue'
import { defineStore } from 'pinia'
import { deviceAPI } from '@/api'
import type { Device } from '@/types'

export const useDeviceStore = defineStore('device', () => {
  const devices = ref<Device[]>([])
  const currentDevice = ref<Device | null>(null)
  const loading = ref(false)

  async function fetchDevices(rackId: number): Promise<Device[]> {
    loading.value = true
    try {
      const res = await deviceAPI.list(rackId)
      devices.value = res.data
      return res.data
    } catch {
      devices.value = []
      return []
    } finally {
      loading.value = false
    }
  }

  async function fetchDevice(id: number): Promise<Device | null> {
    loading.value = true
    try {
      const res = await deviceAPI.get(id)
      currentDevice.value = res.data
      return res.data
    } catch {
      return null
    } finally {
      loading.value = false
    }
  }

  async function createDevice(rackId: number, data: Partial<Device>): Promise<Device | null> {
    try {
      const res = await deviceAPI.create(rackId, data)
      devices.value.push(res.data)
      return res.data
    } catch {
      return null
    }
  }

  async function updateDevice(id: number, data: Partial<Device>): Promise<Device | null> {
    try {
      const res = await deviceAPI.update(id, data)
      const index = devices.value.findIndex((d) => d.id === id)
      if (index !== -1) {
        devices.value[index] = res.data
      }
      if (currentDevice.value?.id === id) {
        currentDevice.value = res.data
      }
      return res.data
    } catch {
      return null
    }
  }

  async function deleteDevice(id: number): Promise<boolean> {
    try {
      await deviceAPI.delete(id)
      devices.value = devices.value.filter((d) => d.id !== id)
      if (currentDevice.value?.id === id) {
        currentDevice.value = null
      }
      return true
    } catch {
      return false
    }
  }

  function setCurrentDevice(device: Device | null) {
    currentDevice.value = device
  }

  return {
    devices,
    currentDevice,
    loading,
    fetchDevices,
    fetchDevice,
    createDevice,
    updateDevice,
    deleteDevice,
    setCurrentDevice,
  }
})
