export type AutomationJobType = 'agent' | 'inspection' | 'script' | 'power'
export type AutomationJobStatus = 'pending' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled'

export interface AutomationDevice {
  id: number
  name: string
  ip_address: string | null
  type: string
  status: string
  os_system: string | null
  has_credential: boolean
  rack_id: number
  target_type?: 'device' | 'pve_guest'
  pve_connection_id?: number
  pve_guest_type?: string
  pve_vmid?: number
  guest_status?: string
  /** Windows 设备最近一轮指标采集明确失败(WinRM 确认不通);未知/成功为 false */
  metrics_failed?: boolean
}

export interface AutomationStep {
  id: number
  step_type: string
  step_name: string
  status: string
  command: string | null
  exit_code: number | null
  output: string | null
  error_message: string | null
  duration_ms: number | null
  created_at: string
}

export interface AutomationTarget {
  id: number
  device_id: number | null
  device_name: string
  device_ip: string | null
  status: string
  result_json: Record<string, any> | null
  error_message: string | null
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  steps: AutomationStep[]
}

export interface AutomationJob {
  id: number
  name: string
  job_type: AutomationJobType
  trigger_type: string
  status: AutomationJobStatus
  risk_level: string
  config_json: Record<string, any>
  summary_json: Record<string, number> | null
  created_by: number | null
  created_by_name: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  targets: AutomationTarget[]
}

export interface AutomationJobListItem {
  id: number
  name: string
  job_type: AutomationJobType
  trigger_type: string
  status: AutomationJobStatus
  risk_level: string
  created_by_name: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  target_total: number
  succeeded: number
  // 巡检发现异常时后端把 target 状态写成 warning，既不在 succeeded 也不在 failed 里；
  // 前端算进度必须把它当终态计入（见 @/utils/automationProgress）。
  warning: number
  failed: number
  running: number
  // Agent 任务运行中目标的累计步骤数(其它类型恒 0);列表页进度据此实时爬升。
  steps_executed?: number
}

export interface AutomationSchedule {
  id: number
  name: string
  job_type: AutomationJobType
  target_ids: number[]
  config_json: Record<string, any>
  schedule_type: 'once' | 'recurring'
  scheduled_at: string | null
  cron_expression: string | null
  status: string
  last_run_at: string | null
  next_run_at: string | null
  created_by: number | null
  created_by_name: string | null
  created_at: string
}
