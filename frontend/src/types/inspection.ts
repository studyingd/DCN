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
  device_id: number
  device_name: string
  device_ip: string | null
  target_type: 'network' | 'linux' | 'windows'
  vendor: string | null
  mode: 'standard' | 'custom' | 'quick'
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
  target_type: 'network' | 'linux' | 'windows'
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
  enable_password?: string
}

export interface InspectionRunResult {
  batch_id: string | null
  results: InspectionDeviceResult[]
  total: number
  succeeded: number
  failed: number
}

export interface InspectionDeviceResult {
  device_id: number
  device_name: string
  ip_address: string | null
  target_type: string
  vendor: string | null
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
