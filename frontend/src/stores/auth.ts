import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { authAPI, type AuthProfile } from '@/api'
import type { User } from '@/types'

export const useAuthStore = defineStore('auth', () => {
  // Auth tokens live in httpOnly cookies (see api/index.ts). The store only
  // holds the NON-secret profile needed to drive the UI; it is loaded from
  // /api/auth/profile after login and on app boot. The SPA never decodes a JWT.
  const user = ref<User | null>(null)
  const permissions = ref<string[]>([])
  const deviceScope = ref<string>('all')
  const deviceIds = ref<number[]>([])
  const jwtRole = ref<string>('')
  const isAdmin = ref<boolean>(false)
  const profileLoaded = ref<boolean>(false)

  const isLoggedIn = computed(() => profileLoaded.value)
  const userRole = computed(() => user.value?.role ?? jwtRole.value ?? '')

  function hasPermission(perm: string): boolean {
    if (isAdmin.value) return true
    return permissions.value.includes(perm)
  }

  function _applyProfile(p: AuthProfile) {
    permissions.value = p.permissions || []
    deviceScope.value = p.device_scope || 'all'
    deviceIds.value = p.device_ids || []
    jwtRole.value = p.role || ''
    isAdmin.value = !!p.is_admin
    user.value = {
      id: p.id,
      username: p.username,
      role: p.role,
      display_name: p.display_name,
      is_active: true,
    } as User
    profileLoaded.value = true
  }

  function _clear() {
    user.value = null
    permissions.value = []
    deviceScope.value = 'all'
    deviceIds.value = []
    jwtRole.value = ''
    isAdmin.value = false
    profileLoaded.value = false
  }

  /** Load the auth profile from the backend (uses the httpOnly cookie). */
  async function fetchProfile(): Promise<boolean> {
    try {
      const res = await authAPI.getProfile()
      _applyProfile(res.data)
      return true
    } catch {
      _clear()
      return false
    }
  }

  async function login(username: string, password: string): Promise<boolean> {
    try {
      await authAPI.login({ username, password })
      // Cookies are set; load the non-secret profile to drive the UI.
      await fetchProfile()
      return true
    } catch {
      _clear()
      return false
    }
  }

  async function logout() {
    try {
      await authAPI.logout()
    } catch {
      // Ignore logout API errors — clear local state regardless.
    } finally {
      _clear()
    }
  }

  async function fetchUser(): Promise<User | null> {
    try {
      const res = await authAPI.getMe()
      user.value = res.data
      return res.data
    } catch {
      user.value = null
      return null
    }
  }

  return {
    user,
    permissions,
    deviceScope,
    deviceIds,
    isLoggedIn,
    userRole,
    isAdmin,
    hasPermission,
    login,
    logout,
    fetchUser,
    fetchProfile,
  }
})
