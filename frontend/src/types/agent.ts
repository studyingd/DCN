/** Agent 只读诊断类型(对应后端 /api/agent/*) */

export interface AgentDevice {
  id: number
  name: string
  ip_address: string | null
  os_system: string | null
  status: string
  has_credential: boolean
}

export interface AgentStatus {
  enabled: boolean
  configured: boolean
}

export interface AgentStep {
  tool: string // run_diagnostic / get_metrics
  key?: string
  label?: string
  ok: boolean
  error?: string
  preview?: string
  hours?: number
  truncated?: boolean
}

export interface AgentDiagnoseResult {
  run_id: number
  status: string
  report: string | null
  error: string | null
  steps: AgentStep[]
  duration_ms: number
}

/** Agent 参数配置(系统管理页;api_key 只回显掩码,写入时留空表示不变) */
export interface AgentConfig {
  enabled: boolean
  base_url: string
  model: string
  max_steps: number
  api_key_set: boolean
  api_key_preview: string
}

/** POST /diagnose 的响应(异步:立即返回 run_id) */
export interface AgentDiagnoseStart {
  run_id: number
  status: string
}

/** 历史记录列表项 */
export interface AgentRunSummary {
  id: number
  device_id: number | null
  device_name: string
  device_ip: string | null
  question: string
  status: string // running / completed / failed
  duration_ms: number | null
  report: string | null
  error: string | null
  created_at: string | null
}

/** 单次诊断详情(轮询进度用) */
export interface AgentRunDetail extends AgentRunSummary {
  steps: AgentStep[]
}
