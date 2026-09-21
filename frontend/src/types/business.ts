export interface BusinessHealthItem {
  id: number
  name: string
  description: string | null
  created_at: string | null
  server_total: number
  server_online: number
  interface_total: number
  interface_up: number
  health: string // healthy / degraded / down / unknown
}

export interface ServerInBusiness {
  kind: 'device' | 'pve' // 机房设备 / PVE 虚拟机·容器，界面统一按服务器展示
  device_id: number | null // kind === 'device' 时有值，移除关联用
  link_id: number | null // kind === 'pve' 时有值(business_pve_guests 关联行 id)
  name: string
  ip_address: string | null
  status: string // device: online/offline;pve: running/stopped/unknown
  online: number
  reachable: number // 0 = 所属 PVE 连接不可达，状态未知
  connection_id: number | null
  connection_name: string | null
  guest_type: string | null // qemu(LXC 已移除)
  vmid: number | null
  node: string | null
  os_system: string | null
  cpu: number | null
  mem: number | null
  maxmem: number | null
}

export interface PveGuestCandidate {
  connection_id: number
  connection_name: string
  guest_type: string
  vmid: number
  name: string
  node: string | null
  status: string
  ip_address: string | null
  os_system: string | null
  cpu: number | null
  mem: number | null
  maxmem: number | null
  reachable: number
}

export interface InterfaceInBusiness {
  interface_id: number
  name: string
  url: string
  method: string
  expected_status: number
  timeout: number
  enabled: number
  request_headers: Record<string, string>
  auth_type: string
  auth_username: string | null
  has_auth_secret: boolean
  request_body: string | null
  response_contains: string | null
  description: string | null
  up: number
  status_code: number | null
  latency_ms: number | null
  error: string | null
  checked_at: string | null
}

export interface BusinessDetail {
  id: number
  name: string
  description: string | null
  created_at: string | null
  servers: ServerInBusiness[]
  interfaces: InterfaceInBusiness[]
  server_total: number
  server_online: number
  interface_total: number
  interface_up: number
  health: string
}

export interface InterfaceForm {
  name: string
  url: string
  method: string
  expected_status: number
  timeout: number
  enabled: number
  request_headers: Record<string, string>
  auth_type: string
  auth_username?: string
  auth_password?: string
  auth_token?: string
  request_body?: string
  response_contains?: string
  description: string
}
