export interface DashboardOverview {
  room_count: number
  rack_count: number
  device_total: number
  device_online: number
  device_offline: number
  device_maintenance: number
  user_count: number
  online_user_count: number
  pve_guest_total: number
  pve_guest_running: number
  pve_guest_stopped: number
  pve_platform_count: number
  alert_events_7d: number
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
