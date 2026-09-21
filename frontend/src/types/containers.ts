export interface ContainerItem {
  container_id: string
  name: string
  image: string | null
  state: string | null
  status: string | null
  ports: string | null
  cpu_pct: number | null
  mem_used_mb: number | null
  mem_limit_mb: number | null
  mem_pct: number | null
}

export interface DeviceContainersEntry {
  device_id: number
  device_name: string
  ip_address: string | null
  os_system: string | null
  device_status: string
  credential_id: number | null
  has_credential: boolean
  available: boolean
  version: string | null
  container_count: number
  last_error: string | null
  updated_at: string | null
  target_type: 'device' | 'pve_guest'
  pve_connection_id: number | null
  pve_guest_type: 'qemu' | null
  pve_vmid: number | null
  guest_status?: string | null
  containers: ContainerItem[]
  commands: Record<string, { success: boolean; error: string | null }>
}
