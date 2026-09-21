// 顶栏全局搜索(/api/search)结果类型
export interface SearchDeviceItem {
  id: number
  name: string
  type: string
  status: string
  ip_address: string | null
  room_id: number
  room_name: string
  rack_id: number
  rack_name: string
}

export interface SearchGuestItem {
  connection_id: number
  connection_name: string
  guest_type: 'qemu'
  vmid: number
  name: string
  node: string | null
  status: string
  ip_address: string | null
  last_known_ip: string | null
}

export interface SearchResults {
  devices: SearchDeviceItem[]
  guests: SearchGuestItem[]
}
