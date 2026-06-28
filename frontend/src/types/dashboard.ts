export interface DashboardOverview {
  room_count: number
  rack_count: number
  device_total: number
  device_online: number
  device_offline: number
  device_maintenance: number
  connection_count: number
  user_count: number
  online_user_count: number
}

export interface DeviceTypeItem {
  type: string
  label: string
  count: number
  online: number
  offline: number
  maintenance: number
}

export interface RoomSummaryItem {
  id: number
  name: string
  location: string | null
  rack_count: number
  device_count: number
  device_online: number
  device_offline: number
  device_maintenance: number
}

export interface TopologyNode {
  id: number
  name: string
  type: string
  ip: string | null
  status: string
  room_id: number | null
  room_name: string | null
}

export interface TopologyEdge {
  source: number
  target: number
  conn_type: string | null
  bandwidth: string | null
}

export interface AuditEventItem {
  id: number
  event_type: string
  username: string | null
  device_name: string | null
  command: string | null
  created_at: string
}

export interface LoginTrendItem {
  date: string
  login_count: number
}
