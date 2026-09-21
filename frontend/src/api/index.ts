import axios from 'axios'
import router from '@/router'
import type {
  LoginRequest,
  LoginResponse,
  User,
  Room,
  Rack,
  Device,
  Role,
  DeviceSavePayload,
  Webhook,
  WebhookCreate,
  WebhookUpdate,
  AlertRule,
  AlertEvent,
  AlertEventsPage,
  MaintenanceWindow,
  AlertOverview,
  FeishuDirectoryRequest,
  FeishuDepartmentPage,
  FeishuUserPage,
} from '@/types'
import type { DeviceContainersEntry } from '@/types/containers'
import type { BusinessDetail, BusinessHealthItem, InterfaceForm, PveGuestCandidate } from '@/types/business'
import type {
  PveConnection,
  PveGuest,
  PveGuestCloneResult,
  PveGuestConfigUpdate,
  PveGuestConfigUpdateResult,
  PveGuestCreate,
  PveGuestCreateResult,
  PveGuestDeleteResult,
  PveGuestVolumePreview,
  PveNode,
  PveNodeStorage,
  PveRrdPoint,
  PveSnapshot,
  PveTaskStatus,
} from '@/types/pve'
import type { DashboardOverview, DeviceTypeItem, RoomSummaryItem } from '@/types/dashboard'
import type {
  InspectionDeviceItem,
  InspectionItemInfo,
  InspectionRunRequest,
  InspectionRunResult,
  InspectionRecord,
  InspectionRecordDetail,
  InspectionReport,
  MetricThresholdsResponse,
} from '@/types/inspection'
import type {
  MetricsDeviceList,
  MetricsDeviceDetail,
  MetricsHistory,
  MetricsRange,
  MetricsTrend,
} from '@/types/metrics'
import type {
  AgentDevice,
  AgentStatus,
  AgentDiagnoseStart,
  AgentRunSummary,
  AgentRunDetail,
  AgentConfig,
} from '@/types/agent'
import type {
  AutomationDevice,
  AutomationJob,
  AutomationJobListItem,
  AutomationJobType,
  AutomationSchedule,
} from '@/types/automation'

const api = axios.create({
  baseURL: '/api',
  timeout: 15000,
  withCredentials: true, // send the httpOnly auth cookies (access + refresh)
  headers: {
    'Content-Type': 'application/json',
  },
})

/** 首次启动数据库引导(安装模式)。正常模式下 status 恒为 setup_required=false。 */
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
  },
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
  /** 已授权虚拟机的 "conn:gtype:vmid" 编码，device_scope='all' 时为空数组。 */
  pve_guests: string[]
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
    container?: string
  }): Promise<{ data: { ticket: string } }> {
    return api.post('/terminal/ticket', data)
  },
}

// ---------- Files (SFTP transfer over SSH) ----------
export interface FileEntry {
  name: string
  size: number
  type: 'dir' | 'file'
  mtime: number | null
}
export const fileAPI = {
  list(
    deviceId: number,
    params: {
      path?: string
      credential_id?: number | null
      username?: string
      password?: string
    },
  ): Promise<{ data: { path: string; entries: FileEntry[] } }> {
    return api.post(`/devices/${deviceId}/files/list`, params)
  },

  upload(
    deviceId: number,
    form: FormData,
    onProgress?: (pct: number) => void,
  ): Promise<{ data: { filename: string; path: string; size: number } }> {
    return api.post(`/devices/${deviceId}/files/upload`, form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000,
      onUploadProgress: (e: { loaded: number; total?: number }) => {
        if (onProgress && e.total) onProgress(Math.round((e.loaded / e.total) * 100))
      },
    })
  },

  download(
    deviceId: number,
    params: {
      path: string
      credential_id?: number | null
      username?: string
      password?: string
    },
  ): Promise<{ data: Blob }> {
    return api.post(`/devices/${deviceId}/files/download`, params, {
      responseType: 'blob',
      timeout: 300000,
    })
  },
}

// ---------- Rooms ----------
export const roomAPI = {
  list(): Promise<{ data: Room[] }> {
    return api.get('/rooms')
  },

  tree(): Promise<{ data: Room[] }> {
    return api.get('/rooms/tree')
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

  create(rackId: number, data: DeviceSavePayload): Promise<{ data: Device }> {
    return api.post(`/racks/${rackId}/devices`, data)
  },

  get(id: number): Promise<{ data: Device }> {
    return api.get(`/devices/${id}`)
  },

  update(id: number, data: DeviceSavePayload): Promise<{ data: Device }> {
    return api.put(`/devices/${id}`, data)
  },

  reorder(rackId: number, positions: { device_id: number; position_u: number }[]): Promise<void> {
    return api.put(`/racks/${rackId}/devices/reorder`, { positions })
  },

  delete(id: number): Promise<void> {
    return api.delete(`/devices/${id}`)
  },

  detectOS(
    ipAddress: string,
    options: {
      deviceId?: number | null
      credentialId?: number | null
      username?: string
      password?: string
      winrmPort?: number
    } = {},
  ): Promise<{
    data: {
      ip_address: string
      os_system: string
      os_version: string
      ssh_banner: string | null
      ssh_port_open: boolean
      winrm_port_open: boolean
      rdp_port_open: boolean
      smb_port_open: boolean
      confidence: string
      detail: string
    }
  }> {
    return api.post('/devices/detect-os', {
      ip_address: ipAddress,
      device_id: options.deviceId || null,
      username: options.username || null,
      password: options.password || null,
      winrm_port: options.winrmPort || null,
    })
  },

  downloadWinrmSetupScript(
    targetIp: string,
    winrmPort = 5985,
  ): Promise<{ data: Blob; headers: Record<string, string> }> {
    return api.get('/devices/winrm-setup-script', {
      params: { target_ip: targetIp, winrm_port: winrmPort },
      responseType: 'blob',
    })
  },
}

// ---------- Docker Containers ----------
export const containersAPI = {
  list(): Promise<{ data: { items: DeviceContainersEntry[] } }> {
    return api.get('/containers')
  },
  device(id: number): Promise<{ data: DeviceContainersEntry }> {
    return api.get(`/containers/${id}`)
  },
  control(
    id: number,
    action: string,
    name: string,
    keepVolumes?: boolean,
  ): Promise<{ data: { success: boolean; message: string } }> {
    return api.post(
      `/containers/${id}/control`,
      // remove 之外的 action 后端会忽略 keep_volumes,不必条件性省略
      { action, name, keep_volumes: keepVolumes },
      { timeout: 60000 },
    )
  },
  logs(
    id: number,
    name: string,
    tail: number,
  ): Promise<{ data: { container: string; tail: number; exit_code: number; log: string } }> {
    return api.get(`/containers/${id}/logs/${encodeURIComponent(name)}`, { params: { tail }, timeout: 60000 })
  },
  // PVE 虚机容器终端票据(kind=pve-remote + container;设备目标走 terminalAPI.ticket)
  terminalTicket(id: number, container: string): Promise<{ data: { ticket: string } }> {
    return api.post(`/containers/${id}/terminal-ticket`, { container }, { timeout: 15000 })
  },
}

// ---------- Business Monitoring(业务/接口) ----------
export const businessAPI = {
  list(): Promise<{ data: BusinessHealthItem[] }> {
    return api.get('/businesses')
  },
  detail(id: number): Promise<{ data: BusinessDetail }> {
    return api.get(`/businesses/${id}`)
  },
  create(data: { name: string; description?: string }): Promise<{ data: { id: number } }> {
    return api.post('/businesses', data)
  },
  update(id: number, data: { name?: string; description?: string }): Promise<void> {
    return api.put(`/businesses/${id}`, data)
  },
  remove(id: number): Promise<void> {
    return api.delete(`/businesses/${id}`)
  },
  createInterface(businessId: number, data: InterfaceForm): Promise<{ data: BusinessDetail }> {
    return api.post(`/businesses/${businessId}/interfaces`, data)
  },
  linkServers(businessId: number, deviceIds: number[]): Promise<{ data: BusinessDetail }> {
    return api.post(`/businesses/${businessId}/servers/batch`, { ids: deviceIds })
  },
  unlinkServer(businessId: number, deviceId: number): Promise<{ data: BusinessDetail }> {
    return api.delete(`/businesses/${businessId}/servers/${deviceId}`)
  },
  guestCandidates(): Promise<{ data: PveGuestCandidate[] }> {
    return api.get('/businesses/pve-guest-candidates')
  },
  linkGuests(
    businessId: number,
    items: Array<{ connection_id: number; guest_type: string; vmid: number; name?: string }>,
  ): Promise<{ data: BusinessDetail }> {
    return api.post(`/businesses/${businessId}/guests/batch`, { items })
  },
  unlinkGuest(businessId: number, linkId: number): Promise<{ data: BusinessDetail }> {
    return api.delete(`/businesses/${businessId}/guests/${linkId}`)
  },
  unlinkInterface(businessId: number, interfaceId: number): Promise<{ data: BusinessDetail }> {
    return api.delete(`/businesses/${businessId}/interfaces/${interfaceId}`)
  },
}

// 接口生命周期从属业务:创建走 businessAPI.createInterface(新建并关联),
// 编辑/删除走这里;曾经存在独立的「接口管理页」API(list/create/remove)与
// 「关联已有接口」API(linkInterfaces/linkInterface),前端从未接入,端点已
// 于 2026-09-17 删除,勿加回。
export const interfaceAPI = {
  update(id: number, data: Partial<InterfaceForm>): Promise<void> {
    return api.put(`/service-interfaces/${id}`, data)
  },
}

// ---------- PVE(Proxmox 虚拟化平台) ----------
export const pveAPI = {
  listConnections(): Promise<{ data: PveConnection[] }> {
    return api.get('/pve/connections')
  },
  createConnection(data: Partial<PveConnection> & { token_secret: string }): Promise<{ data: PveConnection }> {
    return api.post('/pve/connections', data)
  },
  updateConnection(
    id: number,
    data: Partial<PveConnection> & { token_secret?: string },
  ): Promise<{ data: PveConnection }> {
    return api.put(`/pve/connections/${id}`, data)
  },
  deleteConnection(id: number): Promise<void> {
    return api.delete(`/pve/connections/${id}`)
  },
  testConnection(id: number): Promise<{ data: { ok: boolean; version: string } }> {
    return api.post(`/pve/connections/${id}/test`)
  },
  overview(connId: number): Promise<{ data: { nodes: PveNode[]; guests: PveGuest[] } }> {
    // force=true 跳过后端 cluster/resources 的 12s 缓存。
    // 前端 10s 轮询若命中缓存,所有 IO 计数器会与上次完全一致,
    // 速率计算要么归 0、要么停在「采样中」——必须每次拿实时数据。
    return api.get(`/pve/connections/${connId}/overview`, { params: { force: true } })
  },
  listNodes(connId: number): Promise<{ data: PveNode[] }> {
    return api.get(`/pve/connections/${connId}/nodes`)
  },
  guestDetail(
    connId: number,
    gtype: string,
    vmid: number,
  ): Promise<{
    data: {
      status: Record<string, unknown>
      config: Record<string, unknown>
      guest_agent: import('@/types/pve').PveGuest['guest_agent']
    }
  }> {
    return api.get(`/pve/connections/${connId}/guests/${gtype}/${vmid}`)
  },
  guestBinding(
    connId: number,
    gtype: string,
    vmid: number,
    node?: string,
  ): Promise<{ data: import('@/types/pve').PveGuestBinding }> {
    return api.get(`/pve/connections/${connId}/guests/${gtype}/${vmid}/binding`, {
      params: node ? { node } : undefined,
    })
  },
  detectGuestBindingOs(
    connId: number,
    gtype: string,
    vmid: number,
    data: {
      ip_address?: string
      username?: string
      password?: string
      winrm_port?: number
    },
  ): Promise<{
    data: {
      ip_address?: string
      os_system: string
      os_version: string
      ssh_banner: string | null
      ssh_port_open: boolean
      winrm_port_open: boolean
      rdp_port_open: boolean
      smb_port_open: boolean
      confidence: string
      detail: string
    }
  }> {
    return api.post(`/pve/connections/${connId}/guests/${gtype}/${vmid}/binding/detect-os`, data)
  },
  updateGuestBinding(
    connId: number,
    gtype: string,
    vmid: number,
    data: {
      ip_address?: string
      os_system: 'linux' | 'windows' | 'unknown'
      ssh_port: number
      winrm_port: number
      username: string
      password?: string
      ssh_key?: string
      enabled: boolean
    },
    node?: string,
  ): Promise<{ data: import('@/types/pve').PveGuestBinding }> {
    return api.put(`/pve/connections/${connId}/guests/${gtype}/${vmid}/binding`, data, {
      params: node ? { node } : undefined,
    })
  },
  testGuestBinding(connId: number, gtype: string, vmid: number): Promise<{ data: { ok: boolean; message: string } }> {
    return api.post(`/pve/connections/${connId}/guests/${gtype}/${vmid}/binding/test`, null, { timeout: 30000 })
  },
  downloadGuestWinrmSetupScript(
    connId: number,
    gtype: string,
    vmid: number,
  ): Promise<{ data: Blob; headers: Record<string, string> }> {
    return api.get(`/pve/connections/${connId}/guests/${gtype}/${vmid}/winrm-setup-script`, {
      responseType: 'blob',
    })
  },
  guestHistory(
    connId: number,
    gtype: string,
    vmid: number,
    range: string,
    node?: string,
  ): Promise<{ data: { range: string; timeframe: string; items: PveRrdPoint[] } }> {
    return api.get(`/pve/connections/${connId}/guests/${gtype}/${vmid}/history`, {
      params: { range, ...(node ? { node } : {}) },
    })
  },
  // 全部运行中 guest 的最新 RRD 速率(批量),用于列表页首轮预填,避免差值基线空转 ~10s
  guestsRrdLatest(connId: number): Promise<{ data: { items: Record<string, PveRrdPoint> } }> {
    return api.get(`/pve/connections/${connId}/guests/rrd-latest`)
  },
  power(
    connId: number,
    gtype: string,
    vmid: number,
    action: string,
  ): Promise<{ data: { success: boolean; message: string } }> {
    return api.post(`/pve/connections/${connId}/guests/${gtype}/${vmid}/power`, { action })
  },
  snapshots(connId: number, gtype: string, vmid: number, node?: string): Promise<{ data: PveSnapshot[] }> {
    return api.get(`/pve/connections/${connId}/guests/${gtype}/${vmid}/snapshots`, {
      params: node ? { node } : undefined,
    })
  },
  createSnapshot(connId: number, gtype: string, vmid: number, snapname: string, description: string): Promise<unknown> {
    return api.post(`/pve/connections/${connId}/guests/${gtype}/${vmid}/snapshots`, { snapname, description })
  },
  deleteSnapshot(connId: number, gtype: string, vmid: number, snapname: string): Promise<unknown> {
    return api.delete(`/pve/connections/${connId}/guests/${gtype}/${vmid}/snapshots/${encodeURIComponent(snapname)}`)
  },
  rollbackSnapshot(connId: number, gtype: string, vmid: number, snapname: string): Promise<unknown> {
    return api.post(
      `/pve/connections/${connId}/guests/${gtype}/${vmid}/snapshots/${encodeURIComponent(snapname)}/rollback`,
    )
  },
  createGuest(connId: number, gtype: 'qemu', body: PveGuestCreate): Promise<{ data: PveGuestCreateResult }> {
    return api.post(`/pve/connections/${connId}/guests/${gtype}`, body)
  },
  cloneGuest(
    connId: number,
    gtype: string,
    vmid: number,
    body: { newid?: number; name?: string; full?: boolean; description?: string },
  ): Promise<{ data: PveGuestCloneResult }> {
    return api.post(`/pve/connections/${connId}/guests/${gtype}/${vmid}/clone`, body)
  },
  /** 删除前预览：该虚机引用了哪些卷、销毁时哪些会被清掉、平台侧还有哪些引用。 */
  guestVolumes(connId: number, gtype: string, vmid: number, node?: string): Promise<{ data: PveGuestVolumePreview }> {
    return api.get(`/pve/connections/${connId}/guests/${gtype}/${vmid}/volumes`, {
      params: node ? { node } : undefined,
    })
  },
  /**
   * 销毁虚机/LXC。purge 会连带删除虚机拥有的磁盘卷与快照，
   * destroyUnreferencedDisks 再清掉 unused* 残留卷；force 表示运行中先强制停机。
   * 后端会同步等 PVE 销毁任务终态:强制停机最长 60s + 任务等待 60s，
   * 故超时放宽到 180s。
   */
  deleteGuest(
    connId: number,
    gtype: string,
    vmid: number,
    options: { purge?: boolean; destroyUnreferencedDisks?: boolean; force?: boolean } = {},
  ): Promise<{ data: PveGuestDeleteResult }> {
    const { purge = true, destroyUnreferencedDisks = true, force = false } = options
    return api.delete(`/pve/connections/${connId}/guests/${gtype}/${vmid}`, {
      params: { purge, destroy_unreferenced_disks: destroyUnreferencedDisks, force },
      timeout: 180000,
    })
  },
  /** 调整虚机 CPU/内存/磁盘容量(磁盘只扩不缩;部分改动需重启生效)。 */
  updateGuestConfig(
    connId: number,
    gtype: string,
    vmid: number,
    body: PveGuestConfigUpdate,
    node?: string,
  ): Promise<{ data: PveGuestConfigUpdateResult }> {
    return api.put(`/pve/connections/${connId}/guests/${gtype}/${vmid}/config`, body, {
      params: node ? { node } : undefined,
    })
  },
  /** 查询 PVE 异步任务(创建/克隆/销毁/快照)的执行状态。 */
  taskStatus(connId: number, node: string, upid: string): Promise<{ data: PveTaskStatus }> {
    return api.get(`/pve/connections/${connId}/task-status`, { params: { node, upid } })
  },
  nextId(connId: number): Promise<{ data: { vmid: number } }> {
    return api.get(`/pve/connections/${connId}/nextid`)
  },
  isos(connId: number, node: string, storage: string): Promise<{ data: { volid: string }[] }> {
    return api.get(`/pve/connections/${connId}/nodes/${node}/isos`, { params: { storage } })
  },
  /** 节点存储列表(新建虚机的存储下拉用;后端 30s 节点缓存,重复打开无压力)。 */
  storage(connId: number, node: string): Promise<{ data: PveNodeStorage[] }> {
    return api.get(`/pve/connections/${connId}/nodes/${node}/storage`)
  },
  consoleTicket(
    connId: number,
    gtype: string,
    vmid: number,
    forcePve = false,
    width?: number,
    height?: number,
  ): Promise<{
    data: {
      mode: 'pve' | 'ssh' | 'rdp'
      ticket: string
      vncticket?: string
      node?: string
      /** SSH/RDP 模式下的合成目标 ID（负数），文件传输接口按它寻址虚拟机。 */
      target_id?: number
    }
  }> {
    return api.post(`/pve/connections/${connId}/guests/${gtype}/${vmid}/console-ticket`, null, {
      params: {
        ...(forcePve ? { force_pve: true } : {}),
        ...(width ? { width } : {}),
        ...(height ? { height } : {}),
      },
    })
  },
}

// ---------- 顶栏全局搜索 ----------
export const searchAPI = {
  global(q: string): Promise<{ data: import('@/types/search').SearchResults }> {
    return api.get('/search', { params: { q } })
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
}

export default api

// ---------- Users ----------
export const userAPI = {
  list(): Promise<{ data: User[] }> {
    return api.get('/users')
  },
  create(data: Partial<User> & { password: string }): Promise<{ data: User }> {
    return api.post('/users', data)
  },
  get(id: number): Promise<{ data: User }> {
    return api.get(`/users/${id}`)
  },
  update(id: number, data: Partial<User> & { password?: string }): Promise<{ data: User }> {
    return api.put(`/users/${id}`, data)
  },
  delete(id: number): Promise<void> {
    return api.delete(`/users/${id}`)
  },
  toggleActive(id: number, active: boolean): Promise<void> {
    return api.put(`/users/${id}/toggle-active`, { is_active: active ? 1 : 0 })
  },
}

// ---------- Roles ----------
export const roleAPI = {
  list(): Promise<{ data: Role[] }> {
    return api.get('/roles')
  },
  create(data: Partial<Role>): Promise<{ data: Role }> {
    return api.post('/roles', data)
  },
  get(id: number): Promise<{ data: Role }> {
    return api.get(`/roles/${id}`)
  },
  update(id: number, data: Partial<Role>): Promise<{ data: Role }> {
    return api.put(`/roles/${id}`, data)
  },
  delete(id: number): Promise<void> {
    return api.delete(`/roles/${id}`)
  },
  permissions(): Promise<{
    data: { permissions: { key: string; label: string }[]; groups: { label: string; permissions: string[] }[] }
  }> {
    return api.get('/permissions')
  },
  /** 角色授权用的虚拟机候选项（不做调用者自身 ACL 过滤）。 */
  pveGuestCandidates(): Promise<{ data: PveGuestCandidate[] }> {
    return api.get('/roles/pve-guest-candidates')
  },
}

// ---------- Scripts ----------
export const scriptAPI = {
  listDevices(): Promise<{
    data: {
      id: number
      name: string
      ip_address: string | null
      type: string
      status: string
      os_system: string | null
      has_credential: boolean
    }[]
  }> {
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
  getReport(params?: {
    start_date?: string
    end_date?: string
    target_type?: string
  }): Promise<{ data: InspectionReport }> {
    return api.get('/inspection/report', { params })
  },
  // 巡检阈值表:指标页 / PVE / 容器页共用一套配色口径,权限是 device:view 而非 automation:manage
  getThresholds(): Promise<{ data: MetricThresholdsResponse }> {
    return api.get('/inspection/thresholds')
  },
}

// ---------- Server Metrics ----------
export const metricsAPI = {
  list(): Promise<{ data: MetricsDeviceList }> {
    return api.get('/metrics/devices')
  },
  /** 手动触发一轮真实采集(耗时可达一个采集周期,复用后端串行锁) */
  collect(): Promise<{ data: { total: number; available: number } }> {
    return api.post('/metrics/collect', null, { timeout: 120000 })
  },
  detail(id: number): Promise<{ data: MetricsDeviceDetail }> {
    return api.get(`/metrics/devices/${id}`)
  },
  history(id: number, range: MetricsRange): Promise<{ data: MetricsHistory }> {
    return api.get(`/metrics/devices/${id}/history`, { params: { range } })
  },
  /** 全部服务器 CPU / 内存曲线(首页概览，一次请求返回) */
  trend(range: MetricsRange): Promise<{ data: MetricsTrend }> {
    return api.get('/metrics/history', { params: { range } })
  },
}

// ---------- Agent(只读诊断) ----------
export const agentAPI = {
  status(): Promise<{ data: AgentStatus }> {
    return api.get('/agent/status')
  },
  devices(): Promise<{ data: AgentDevice[] }> {
    return api.get('/agent/devices')
  },
  diagnose(deviceId: number, question: string): Promise<{ data: AgentDiagnoseStart }> {
    // 异步执行:立即返回 run_id,前端轮询 getRun 获取实时进度
    return api.post('/agent/diagnose', { device_id: deviceId, question }, { timeout: 15000 })
  },
  getRun(runId: number): Promise<{ data: AgentRunDetail }> {
    return api.get(`/agent/runs/${runId}`)
  },
  listRuns(params?: {
    device_id?: number
    page?: number
    page_size?: number
  }): Promise<{ data: { total: number; items: AgentRunSummary[] } }> {
    return api.get('/agent/runs', { params })
  },
  getConfig(): Promise<{ data: AgentConfig }> {
    return api.get('/agent/config')
  },
  updateConfig(data: {
    enabled: boolean
    base_url: string
    model: string
    api_key: string
    max_steps: number
  }): Promise<{ data: AgentConfig }> {
    return api.put('/agent/config', data)
  },
  testConfig(): Promise<{ data: { ok: boolean; message: string } }> {
    return api.post('/agent/config/test', null, { timeout: 30000 })
  },
  listModels(baseUrl: string, apiKey: string): Promise<{ data: { models: string[] } }> {
    return api.post('/agent/config/models', { base_url: baseUrl, api_key: apiKey }, { timeout: 20000 })
  },
}

// ---------- Webhook alerts ----------
export const webhookAPI = {
  list(): Promise<{ data: Webhook[] }> {
    return api.get('/webhooks')
  },
  create(data: WebhookCreate): Promise<{ data: Webhook }> {
    return api.post('/webhooks', data)
  },
  update(id: number, data: WebhookUpdate): Promise<{ data: Webhook }> {
    return api.put(`/webhooks/${id}`, data)
  },
  remove(id: number): Promise<void> {
    return api.delete(`/webhooks/${id}`)
  },
  test(id: number): Promise<{
    data: {
      ok: boolean
      status_code: number | null
      message: string
      duration_ms: number
      response_preview: string | null
    }
  }> {
    return api.post(`/webhooks/${id}/test`)
  },
  /** 飞书通讯录:部门与成员，用于直接选择告警接收人 */
  feishuDepartments(data: FeishuDirectoryRequest): Promise<{ data: FeishuDepartmentPage }> {
    return api.post('/webhooks/feishu/departments', data)
  },
  feishuUsers(data: FeishuDirectoryRequest): Promise<{ data: FeishuUserPage }> {
    return api.post('/webhooks/feishu/users', data)
  },
}

export const alertAPI = {
  overview(): Promise<{ data: AlertOverview }> {
    return api.get('/alerts/overview')
  },
  containers(): Promise<{
    data: {
      container_id: string
      name: string
      device_id: number | null
      device_name: string
      state: string
      status: string
      /** 宿主身份:设备为正 id,虚拟机为合成负数 id */
      target_id: number
      host_kind: 'device' | 'pve_guest'
    }[]
  }> {
    return api.get('/alerts/containers')
  },
  businesses(): Promise<{ data: { business_id: number; name: string }[] }> {
    return api.get('/alerts/businesses')
  },
  rules(): Promise<{ data: AlertRule[] }> {
    return api.get('/alerts/rules')
  },
  createRule(data: Partial<AlertRule>): Promise<{ data: AlertRule }> {
    return api.post('/alerts/rules', data)
  },
  updateRule(id: number, data: Partial<AlertRule>): Promise<{ data: AlertRule }> {
    return api.put(`/alerts/rules/${id}`, data)
  },
  deleteRule(id: number): Promise<{ data: { message: string; closed_event_count: number } }> {
    return api.delete(`/alerts/rules/${id}`)
  },
  /** 告警事件列表;带 page 时返回分页对象(历史检索),不带时返回纯数组(旧兼容) */
  events(params?: {
    status?: string
    severity?: string
    metric?: string
    q?: string
    since?: string
    until?: string
    page?: number
    page_size?: number
  }): Promise<{ data: AlertEventsPage }> {
    return api.get('/alerts/events', { params })
  },
  /** 单条告警详情(含 Agent 归因分析全文) */
  event(id: number): Promise<{ data: AlertEvent }> {
    return api.get(`/alerts/events/${id}`)
  },
  /** 手动触发一次自动处置:离线虚拟机 / 容器立即尝试拉起 */
  remediateEvent(id: number): Promise<{ data: AlertEvent }> {
    return api.post(`/alerts/events/${id}/remediate`)
  },
  /** 手动触发一次 Agent 只读归因分析(CPU / 内存 / 磁盘过高) */
  analyzeEvent(id: number): Promise<{ data: AlertEvent }> {
    return api.post(`/alerts/events/${id}/analyze`)
  },
  /** 站内「已知晓」:该事件恢复前不再重复提醒 */
  ackEvent(id: number): Promise<{ data: AlertEvent }> {
    return api.post(`/alerts/events/${id}/ack`)
  },
  maintenanceWindows(): Promise<{ data: MaintenanceWindow[] }> {
    return api.get('/alerts/maintenance-windows')
  },
  /** 展示文案字典(后端为唯一真源,避免两份维护漂移) */
  labels(): Promise<{
    data: {
      metrics: Record<string, string>
      severities: Record<string, string>
      remediation_states: Record<string, string>
      analysis_states: Record<string, string>
      notify_status: Record<string, string>
    }
  }> {
    return api.get('/alerts/labels')
  },
  createMaintenanceWindow(data: {
    name: string
    target_ids: number[] | null
    start_at: string
    end_at: string
  }): Promise<{ data: MaintenanceWindow }> {
    return api.post('/alerts/maintenance-windows', data)
  },
  finishMaintenanceWindow(id: number): Promise<{ data: MaintenanceWindow }> {
    return api.post(`/alerts/maintenance-windows/${id}/finish`)
  },
  deleteMaintenanceWindow(id: number): Promise<{ data: { message: string } }> {
    return api.delete(`/alerts/maintenance-windows/${id}`)
  },
}

// ---------- Unified Automation ----------
export const automationAPI = {
  devices(): Promise<{ data: AutomationDevice[] }> {
    return api.get('/automation/devices')
  },
  createJob(data: {
    name: string
    job_type: AutomationJobType
    device_ids: number[]
    config: Record<string, any>
  }): Promise<{ data: AutomationJob }> {
    return api.post('/automation/jobs', data)
  },
  jobs(params?: {
    job_type?: AutomationJobType | ''
    status?: string
    page?: number
    page_size?: number
  }): Promise<{ data: { items: AutomationJobListItem[]; total: number; page: number; page_size: number } }> {
    return api.get('/automation/jobs', { params })
  },
  job(id: number): Promise<{ data: AutomationJob }> {
    return api.get(`/automation/jobs/${id}`)
  },
  schedules(): Promise<{ data: AutomationSchedule[] }> {
    return api.get('/automation/schedules')
  },
  createSchedule(data: {
    name: string
    job_type: AutomationJobType
    device_ids: number[]
    config: Record<string, any>
    schedule_type: 'once' | 'recurring'
    scheduled_at?: string
    cron_expression?: string
  }): Promise<{ data: AutomationSchedule }> {
    return api.post('/automation/schedules', data)
  },
  deleteSchedule(id: number): Promise<void> {
    return api.delete(`/automation/schedules/${id}`)
  },
  updateSchedule(
    id: number,
    data: {
      name?: string
      device_ids?: number[]
      config?: Record<string, any>
      schedule_type?: 'once' | 'recurring'
      scheduled_at?: string
      cron_expression?: string
    },
  ): Promise<{ data: AutomationSchedule }> {
    return api.put(`/automation/schedules/${id}`, data)
  },
  setScheduleStatus(id: number, status: 'active' | 'paused'): Promise<{ data: AutomationSchedule }> {
    return api.patch(`/automation/schedules/${id}/status`, { status })
  },
}
