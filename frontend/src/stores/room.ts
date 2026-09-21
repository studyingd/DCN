import { ref } from 'vue'
import { defineStore } from 'pinia'
import { roomAPI } from '@/api'
import type { Room } from '@/types'

export const useRoomStore = defineStore('room', () => {
  const rooms = ref<Room[]>([])
  const currentRoom = ref<Room | null>(null)
  const loading = ref(false)
  // Generation guard so a slow earlier fetchRooms() can't overwrite a newer one
  // (e.g. rapid route re-entry) — only the latest result is committed.
  let _gen = 0

  async function fetchRooms(): Promise<Room[]> {
    loading.value = true
    const gen = ++_gen
    try {
      const res = await roomAPI.tree()
      if (gen !== _gen) return rooms.value
      rooms.value = res.data
      return rooms.value
    } catch {
      if (gen === _gen) rooms.value = []
      return []
    } finally {
      if (gen === _gen) loading.value = false
    }
  }

  async function fetchRoom(id: number): Promise<Room | null> {
    loading.value = true
    try {
      const res = await roomAPI.get(id)
      currentRoom.value = res.data
      return res.data
    } catch {
      return null
    } finally {
      loading.value = false
    }
  }

  async function createRoom(data: Partial<Room>): Promise<Room | null> {
    try {
      const res = await roomAPI.create(data)
      rooms.value.push(res.data)
      return res.data
    } catch {
      return null
    }
  }

  async function updateRoom(id: number, data: Partial<Room>): Promise<Room | null> {
    try {
      const res = await roomAPI.update(id, data)
      const index = rooms.value.findIndex((r) => r.id === id)
      if (index !== -1) {
        rooms.value[index] = res.data
      }
      if (currentRoom.value?.id === id) {
        currentRoom.value = res.data
      }
      return res.data
    } catch {
      return null
    }
  }

  async function deleteRoom(id: number): Promise<boolean> {
    try {
      await roomAPI.delete(id)
      rooms.value = rooms.value.filter((r) => r.id !== id)
      if (currentRoom.value?.id === id) {
        currentRoom.value = null
      }
      return true
    } catch {
      return false
    }
  }

  function setCurrentRoom(room: Room | null) {
    currentRoom.value = room
  }

  return {
    rooms,
    currentRoom,
    loading,
    fetchRooms,
    fetchRoom,
    createRoom,
    updateRoom,
    deleteRoom,
    setCurrentRoom,
  }
})
