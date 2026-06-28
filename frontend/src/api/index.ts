import axios from 'axios'
import router from '@/router'
import type { LoginRequest, LoginResponse, User, Room, Rack, Device, Connection, Credential, Role, UserGroup, ScheduledTask } from '@/types'
import type { DashboardOverview, DeviceTypeItem, RoomSummaryItem, TopologyNode, TopologyEdge, AuditEventItem, LoginTrendItem } from '@/types/dashboard'
import type { InspectionDeviceItem, InspectionItemInfo, InspectionRunRequest, InspectionRunResult, InspectionRecord, InspectionRecordDetail, InspectionReport } from '@/types/inspection'
import type { InterfaceStatusResponse } from '@/composables/useInterfaceStatus'

const api = axios.create({
  baseURL: '/api',
  timeout: 15000,
  withCredentials: true, // send the httpOnly auth cookies (access + refresh)
  headers: {
    'Content-Type': 'application/json',
  },
})

// ── Auth model ────────────────────────────────────────────────────
// Access/refresh tokens live ONLY in httpOnly cookies set by the backend, so
// the SPA never reads, stores, or attaches them — an XSS cannot exfiltrate a
// token. The 401-refresh flow below relies entirely on the cookie.

let _refreshing: Promise<boolean> | null = null
function refreshAccess(): Promise<boolean> {
  // Coalesce concurrent 401s into a single refresh request.
  if (_refreshing) return _refreshing
  _refreshing = axios
    .post('/api/auth/refresh', null, { withCredentials: true })
    .then(() => true)
    .catch(() => false)
    .finally(() => {
      _refreshing = null
    })
  return _refreshing
}

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config
    const url: string | undefined = originalRequest?.url
    if (
      error.response?.status === 401 &&
      !originalRequest._retry &&
      !url?.includes('/auth/refresh') &&
      !url?.includes('/auth/login')
    ) {
      originalRequest._retry = true
      const ok = await refreshAccess()
      if (ok) {
        // The cookie now carries the rotated access token; just retry.
        return api(originalRequest)
      }
      router.push({ name: 'login' })
      return Promise.reject(error)
    }
    return Promise.reject(error)
  }
)

// ---------- Auth ----------
export interface AuthProfile {
  id: number
  username: string
  display_name: string | null
  role: string
  is_admin: boolean
  permissions: string[]
  device_scope: string
  device_ids: number[]
  must_change_password: boolean
}

export const authAPI = {
  login(data: LoginRequest): Promise<{ data: LoginResponse }> {
    return api.post('/auth/login', { username: data.username, password: data.password })
  },

  logout(): Promise<void> {
    // Refresh token is in the httpOnly cookie; backend clears it. No body needed.
    return api.post('/auth/logout')
  },

  getMe(): Promise<{ data: User }> {
    return api.get('/auth/me')
  },

  getProfile(): Promise<{ data: AuthProfile }> {
    return api.get('/auth/profile')
  },

  getOnlineUsers(): Promise<{ data: { user_id: number; username: string; role_name: string }[] }> {
    return api.get('/auth/online-users')
  },

  changePassword(oldPassword: string, newPassword: string): Promise<{ data: { message: string } }> {
    return api.post('/auth/change-password', { old_password: oldPassword, new_password: newPassword })
  },
}

// ---------- Terminal (single-use WS ticket; no token/creds in URL) ----------
export const terminalAPI = {
  ticket(data: {
    device_id: number
    conn_type: 'ssh' | 'rdp'
    credential_id?: number | null
    username?: string
    password?: string
    width?: number
    height?: number
  }): Promise<{ data: { ticket: string } }> {
    return api.post('/terminal/ticket', data)
  },
}

// ---------- Rooms ----------
export const roomAPI = {
  list(): Promise<{ data: Room[] }> {
    return api.get('/rooms')
  },

  create(data: Partial<Room>): Promise<{ data: Room }> {
    return api.post('/rooms', data)
  },

  get(id: number): Promise<{ data: Room }> {
    return api.get(`/rooms/${id}`)
  },

  update(id: number, data: Partial<Room>): Promise<{ data: Room }> {
    return api.put(`/rooms/${id}`, data)
  },

  delete(id: number): Promise<void> {
    return api.delete(`/rooms/${id}`)
  },
}

// ---------- Racks ----------
export const rackAPI = {
  list(roomId: number): Promise<{ data: Rack[] }> {
    return api.get(`/rooms/${roomId}/racks`)
  },

  create(roomId: number, data: Partial<Rack>): Promise<{ data: Rack }> {
    return api.post(`/rooms/${roomId}/racks`, data)
  },

  update(id: number, data: Partial<Rack>): Promise<{ data: Rack }> {
    return api.put(`/racks/${id}`, data)
  },

  delete(id: number): Promise<void> {
    return api.delete(`/racks/${id}`)
  },
}

// ---------- Devices ----------
export const deviceAPI = {
  list(rackId: number): Promise<{ data: Device[] }> {
    return api.get(`/racks/${rackId}/devices`)
  },

  listByRoom(roomId: number): Promise<{ data: any[] }> {
    return api.get(`/rooms/${roomId}/devices`)
  },

  create(rackId: number, data: Partial<Device>): Promise<{ data: Device }> {
    return api.post(`/racks/${rackId}/devices`, data)
  },

  get(id: number): Promise<{ data: Device }> {
    return api.get(`/devices/${id}`)
  },

  update(id: number, data: Partial<Device>): Promise<{ data: Device }> {
    return api.put(`/devices/${id}`, data)
  },

  delete(id: number): Promise<void> {
    return api.delete(`/devices/${id}`)
  },

  detectOS(ipAddress: string, credentialId?: number): Promise<{ data: {
    ip_address: string
    os_system: string
    os_version: string
    ssh_banner: string | null
    ssh_port_open: boolean
    rdp_port_open: boolean
    smb_port_open: boolean
    confidence: string
    detail: string
  } }> {
    return api.post('/devices/detect-os', { ip_address: ipAddress, credential_id: credentialId || null })
  },
}

// ---------- Connections ----------
export const connectionAPI = {
  list(): Promise<{ data: Connection[] }> {
    return api.get('/connections')
  },

  create(data: Partial<Connection>): Promise<{ data: Connection }> {
    return api.post('/connections', data)
  },

  delete(id: number): Promise<void> {
    return api.delete(`/connections/${id}`)
  },
}

// ---------- Monitor ----------
export const monitorAPI = {
  getStatuses(): Promise<{ data: { statuses: Record<string, string> } }> {
    return api.get('/monitor/statuses')
  },

  triggerScan(): Promise<{ data: { statuses: Record<string, string> } }> {
    return api.post('/monitor/scan')
  },

  getStats(): Promise<{ data: { total: number; online: number; offline: number; maintenance: number } }> {
    return api.get('/monitor/stats')
  },

  getInterfaces(rackId: number): Promise<{ data: InterfaceStatusResponse }> {
    return api.get(`/monitor/interfaces`, { params: { rack_id: rackId } })
  },
}

// ---------- Credentials ----------
export const credentialAPI = {
  list(): Promise<{ data: Credential[] }> {
    return api.get('/credentials')
  },

  create(data: Partial<Credential> & { password?: string; ssh_key?: string }): Promise<{ data: Credential }> {
    return api.post('/credentials', data)
  },

  get(id: number): Promise<{ data: Credential }> {
    return api.get(`/credentials/${id}`)
  },

  update(id: number, data: Partial<Credential> & { password?: string; ssh_key?: string }): Promise<{ data: Credential }> {
    return api.put(`/credentials/${id}`, data)
  },

  delete(id: number): Promise<void> {
    return api.delete(`/credentials/${id}`)
  },

  decrypt(id: number): Promise<{ data: { username: string; password: string; ssh_key: string } }> {
    return api.get(`/credentials/${id}/decrypt`)
  },

  bindDevices(id: number, deviceIds: number[]): Promise<void> {
    return api.put(`/credentials/${id}/devices`, { device_ids: deviceIds })
  },

  allDevicesForBinding(): Promise<{ data: { id: number; name: string; ip_address: string | null; type: string; credential_id: number | null; rack_id: number }[] }> {
    return api.get('/credentials/all-devices')
  },
}

export default api

// ---------- Settings ----------
export const settingsAPI = {
  get(): Promise<{ data: Record<string, string> }> {
    return api.get('/settings')
  },
  update(settings: Record<string, string>): Promise<{ data: Record<string, string> }> {
    return api.put('/settings', { settings })
  },
}

// ---------- Audit ----------
export const auditAPI = {
  listLogs(params?: { device_id?: number; event_type?: string; username?: string; limit?: number }): Promise<{ data: any[] }> {
    return api.get('/audit-logs', { params })
  },
  listRecordings(params?: { username?: string; start_date?: string; end_date?: string }): Promise<{ data: any[] }> {
    return api.get('/recordings', { params })
  },
  getRecordingData(id: number): Promise<{ data: { data: string } }> {
    return api.get(`/recordings/${id}/download`)
  },
  listSessions(params?: { username?: string; start_date?: string; end_date?: string; limit?: number }): Promise<{ data: any[] }> {
    return api.get('/audit-sessions', { params })
  },
  getSessionCommands(sessionId: string): Promise<{ data: any[] }> {
    return api.get(`/audit-sessions/${sessionId}/commands`)
  },
  listAuditUsers(): Promise<{ data: string[] }> {
    return api.get('/audit-users')
  },

  listLoginHistory(params?: { username?: string; start_date?: string; end_date?: string; limit?: number }): Promise<{ data: { id: number; username: string; ip: string | null; created_at: string }[] }> {
    return api.get('/login-history', { params })
  },

  listScriptRecords(params?: { username?: string; start_date?: string; end_date?: string; limit?: number }): Promise<{ data: any[] }> {
    return api.get('/script-records', { params })
  },
}

// ---------- Users ----------
export const userAPI = {
  list(): Promise<{ data: User[] }> { return api.get('/users') },
  create(data: Partial<User> & { password: string }): Promise<{ data: User }> { return api.post('/users', data) },
  get(id: number): Promise<{ data: User }> { return api.get(`/users/${id}`) },
  update(id: number, data: Partial<User> & { password?: string }): Promise<{ data: User }> { return api.put(`/users/${id}`, data) },
  delete(id: number): Promise<void> { return api.delete(`/users/${id}`) },
  toggleActive(id: number, active: boolean): Promise<void> {
    return api.put(`/users/${id}/toggle-active`, { is_active: active ? 1 : 0 })
  },
}

// ---------- Roles ----------
export const roleAPI = {
  list(): Promise<{ data: Role[] }> { return api.get('/roles') },
  create(data: Partial<Role>): Promise<{ data: Role }> { return api.post('/roles', data) },
  get(id: number): Promise<{ data: Role }> { return api.get(`/roles/${id}`) },
  update(id: number, data: Partial<Role>): Promise<{ data: Role }> { return api.put(`/roles/${id}`, data) },
  delete(id: number): Promise<void> { return api.delete(`/roles/${id}`) },
  permissions(): Promise<{ data: { permissions: { key: string; label: string }[]; groups: { label: string; permissions: string[] }[] } }> { return api.get('/permissions') },
}

// ---------- User Groups ----------
export const groupAPI = {
  list(): Promise<{ data: UserGroup[] }> { return api.get('/user-groups') },
  create(data: Partial<UserGroup>): Promise<{ data: UserGroup }> { return api.post('/user-groups', data) },
  update(id: number, data: Partial<UserGroup>): Promise<{ data: UserGroup }> { return api.put(`/user-groups/${id}`, data) },
  delete(id: number): Promise<void> { return api.delete(`/user-groups/${id}`) },
}

// ---------- Scripts ----------
export const scriptAPI = {
  listDevices(): Promise<{ data: { id: number; name: string; ip_address: string | null; type: string; status: string; os_system: string | null; credential_id: number | null }[] }> {
    return api.get('/scripts/devices')
  },
  execute(data: {
    device_ids: number[]
    command: string
    timeout?: number
    username?: string
    password?: string
    credential_id?: number
  }): Promise<{ data: { results: any[]; total: number; succeeded: number; failed: number } }> {
    return api.post('/scripts/execute', data, { timeout: 300000 })
  },
  power(data: {
    device_ids: number[]
    action: 'shutdown' | 'reboot'
  }): Promise<{ data: { action: string; results: any[]; total: number; succeeded: number; failed: number } }> {
    return api.post('/scripts/power', data, { timeout: 300000 })
  },
}

// ---------- Scheduled Tasks ----------
export const scheduledTaskAPI = {
  list(): Promise<{ data: ScheduledTask[] }> {
    return api.get('/scheduled-tasks')
  },
  create(data: {
    name: string
    command: string
    device_ids: number[]
    schedule_type: string
    scheduled_at?: string
    cron_expression?: string
    timeout?: number
    credential_id?: number
  }): Promise<{ data: ScheduledTask }> {
    return api.post('/scheduled-tasks', data)
  },
  update(id: number, data: Record<string, any>): Promise<{ data: ScheduledTask }> {
    return api.put(`/scheduled-tasks/${id}`, data)
  },
  delete(id: number): Promise<void> {
    return api.delete(`/scheduled-tasks/${id}`)
  },
}

// ---------- Dashboard (Big Screen) ----------
export const dashboardAPI = {
  getOverview(): Promise<{ data: DashboardOverview }> {
    return api.get('/dashboard/overview')
  },
  getDeviceTypeDistribution(): Promise<{ data: { distribution: DeviceTypeItem[] } }> {
    return api.get('/dashboard/device-type-distribution')
  },
  getRoomSummary(): Promise<{ data: { rooms: RoomSummaryItem[] } }> {
    return api.get('/dashboard/room-summary')
  },
  getConnectionTopology(): Promise<{ data: { nodes: TopologyNode[]; edges: TopologyEdge[] } }> {
    return api.get('/dashboard/connection-topology')
  },
  getAuditTimeline(): Promise<{ data: { recent_events: AuditEventItem[]; event_counts_today: Record<string, number> } }> {
    return api.get('/dashboard/audit-timeline')
  },
  getLoginTrend(days: number = 7): Promise<{ data: { trend: LoginTrendItem[] } }> {
    return api.get('/dashboard/login-trend', { params: { days } })
  },
}

// ---------- Inspection ----------
export const inspectionAPI = {
  listDevices(): Promise<{ data: InspectionDeviceItem[] }> {
    return api.get('/inspection/devices')
  },
  getItemsCatalog(targetType: string): Promise<{ data: { target_type: string; items: InspectionItemInfo[] } }> {
    return api.get(`/inspection/items/${targetType}`)
  },
  run(data: InspectionRunRequest): Promise<{ data: InspectionRunResult }> {
    return api.post('/inspection/run', data, { timeout: 300000 })
  },
  listRecords(params?: {
    device_id?: number
    target_type?: string
    status?: string
    batch_id?: string
    page?: number
    page_size?: number
  }): Promise<{ data: { items: InspectionRecord[]; total: number; page: number; page_size: number } }> {
    return api.get('/inspection/records', { params })
  },
  getRecordDetail(id: number): Promise<{ data: InspectionRecordDetail }> {
    return api.get(`/inspection/records/${id}`)
  },
  deleteRecord(id: number): Promise<void> {
    return api.delete(`/inspection/records/${id}`)
  },
  getReport(params?: { start_date?: string; end_date?: string; target_type?: string }): Promise<{ data: InspectionReport }> {
    return api.get('/inspection/report', { params })
  },
}
