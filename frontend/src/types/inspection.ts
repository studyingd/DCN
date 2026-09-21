// ── Inspection Types ──

export interface InspectionItemResult {
  id: number
  record_id: number
  item_type: string
  success: boolean
  value: string | null
  unit: string | null
  status: 'normal' | 'warning' | 'critical' | 'error'
  details: Record<string, any> | null
  raw_output: string | null
  error_message: string | null
  command_used: string | null
  executed_at: string | null
  duration_ms: number | null
}

export interface InspectionRecord {
  id: number
  // PVE 虚拟机巡检记录 device_id 为 NULL(迁移 0036)
  device_id: number | null
  device_name: string
  device_ip: string | null
  target_type: 'linux' | 'windows'
  // core：自动化运维的健康巡检固定使用的模式，检查项由后端按操作系统自动决定
  mode: 'standard' | 'custom' | 'quick' | 'core'
  status: 'running' | 'completed' | 'failed' | 'partial'
  total_items: number
  normal_count: number
  warning_count: number
  critical_count: number
  error_count: number
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  triggered_by: number | null
  batch_id: string | null
}

export interface InspectionRecordDetail extends InspectionRecord {
  items: InspectionItemResult[]
}

export interface InspectionDeviceItem {
  id: number
  name: string
  ip_address: string | null
  type: string
  status: string
  os_system: string | null
  credential_id: number | null
  target_type: 'linux' | 'windows'
}

export interface InspectionItemInfo {
  type: string
  label: string
  quick: boolean
}

export interface InspectionRunRequest {
  device_ids: number[]
  mode: 'standard' | 'custom' | 'quick'
  items?: string[]
  timeout?: number
  username?: string
  password?: string
  credential_id?: number
}

export interface InspectionRunResult {
  batch_id: string | null
  results: InspectionDeviceResult[]
  total: number
  succeeded: number
  failed: number
}

export interface InspectionDeviceResult {
  // PVE 虚拟机巡检记录 device_id 为 NULL(迁移 0036)
  device_id: number | null
  device_name: string
  ip_address: string | null
  target_type: string
  record_id?: number
  status: string
  total_items: number
  normal_count: number
  warning_count: number
  critical_count: number
  error_count: number
  duration_ms: number
  items: InspectionItemResult[]
  error?: string
}

export interface InspectionReport {
  total_inspections: number
  total_items_checked: number
  overall_health: {
    normal_pct: number
    warning_pct: number
    critical_pct: number
    error_pct: number
  }
  by_target_type: Record<string, { count: number; warning_rate: number; critical_rate: number }>
  top_warnings: Array<{
    device_name: string
    target_type: string
    warning_count: number
    critical_count: number
    timestamp: string | null
  }>
  trend: Array<{
    date: string
    normal: number
    warning: number
    critical: number
    error: number
  }>
}

// ── 阈值表 ──
// 由 GET /api/inspection/thresholds 下发(权限 device:view),key 是巡检项 item_type
// (cpu / memory / disk / failed_services …),不是指标名 cpu_pct / mem_pct,映射由前端负责。

/** 单个巡检项的告警 / 严重阈值,边界一律按 >= 判定 */
export interface MetricThreshold {
  warning: number
  critical: number
}

export type MetricThresholdTable = Record<string, MetricThreshold>

export interface MetricThresholdsResponse {
  items: MetricThresholdTable
}
