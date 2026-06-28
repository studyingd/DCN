export interface User {
  id: number
  username: string
  role: string
  display_name: string | null
  is_active: number
  role_id: number | null
  role_name: string | null
  group_id: number | null
  group_name: string | null
  created_at: string
}

export interface Role {
  id: number
  name: string
  description: string | null
  permissions: string[]
  device_scope: string
  device_ids: number[]
  is_builtin: number
  created_at: string
  user_count: number
}

export interface UserGroup {
  id: number
  name: string
  description: string | null
  created_at: string
  user_count: number
}

export interface PermissionInfo {
  key: string
  label: string
}

export interface PermissionGroup {
  label: string
  permissions: string[]
}

export interface Room {
  id: number
  name: string
  location: string
  description: string
  floor_plan: object
  created_at: string
  racks?: Rack[]
  rack_count?: number
}

export interface Rack {
  id: number
  room_id: number
  name: string
  type: 'cabinet' | 'shelf'
  position_x: number
  position_y: number
  position_z: number
  rotation: number
  capacity_u: number | null
  devices?: Device[]
}

export interface Device {
  id: number
  rack_id: number
  name: string
  type: string
  ip_address: string
  purpose: string
  os_system: string
  status: string
  position_u: number | null
  size_u: number | null
  ssh_port: number
  rdp_port: number
  web_url: string
  owner: string
  credential_id: number | null
  created_at: string
}

export interface Connection {
  id: number
  device_a_id: number
  device_b_id: number
  conn_type: string
  bandwidth: string
  note: string
  device_a_name?: string
  device_b_name?: string
}

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  refresh_token: string
  token_type: string
}

export interface TreeNodeData {
  id: number
  label: string
  type: 'room' | 'rack' | 'device'
  isLeaf?: boolean
  data: Room | Rack | Device
}

export interface Credential {
  id: number
  name: string
  username: string
  has_password: boolean
  has_ssh_key: boolean
  created_at: string
  device_count: number
}

export interface ScheduledTask {
  id: number
  name: string
  command: string
  device_ids: number[]
  schedule_type: 'once' | 'recurring'
  scheduled_at: string | null
  cron_expression: string | null
  timeout: number
  status: 'pending' | 'active' | 'paused' | 'completed' | 'disabled'
  last_run_at: string | null
  next_run_at: string | null
  credential_id: number | null
  created_by: number | null
  created_at: string
}
