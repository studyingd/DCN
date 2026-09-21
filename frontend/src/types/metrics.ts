/** 服务器指标监控类型(对应后端 /api/metrics/*) */

export interface MetricsDeviceItem {
  device_id: number
  device_name: string
  ip_address: string | null
  type: string
  os_system: string | null
  rack_id: number | null
  rack_name: string
  room_id: number | null
  room_name: string
  status: string
  available: boolean
  error: string | null
  fetched_at: string | null
  source: string | null // ssh / winrm
  cpu_pct: number | null
  mem_pct: number | null
  disk_max_pct: number | null
  load1: number | null
  uptime_sec: number | null
  net_rx_bps: number | null
  net_tx_bps: number | null
  disk_read_bps: number | null
  disk_write_bps: number | null
}

export interface MetricsDeviceList {
  items: MetricsDeviceItem[]
  total: number
}

export interface DiskUsage {
  mount: string
  size_bytes: number
  used_bytes: number
  pct: number
}

export interface MetricsDeviceDetail extends MetricsDeviceItem {
  mem_used_mb: number | null
  mem_total_mb: number | null
  load5: number | null
  load15: number | null
  disks: DiskUsage[]
}

export type MetricsRange = '1h' | '6h' | '24h' | '7d'

export interface MetricSamplePoint {
  ts: string
  cpu_pct: number | null
  mem_pct: number | null
  disk_max_pct: number | null
  load1: number | null
  net_rx_bps: number | null
  net_tx_bps: number | null
  disk_read_bps: number | null
  disk_write_bps: number | null
}

export interface MetricsHistory {
  device_id: number
  range: string
  points: MetricSamplePoint[]
}

/** 首页概览:全部服务器的 CPU / 内存曲线 */
export interface MetricTrendPoint {
  ts: string
  cpu_pct: number | null
  mem_pct: number | null
}

export interface DeviceMetricTrend {
  device_id: number
  device_name: string
  points: MetricTrendPoint[]
}

export interface MetricsTrend {
  range: string
  total: number
  series: DeviceMetricTrend[]
}
