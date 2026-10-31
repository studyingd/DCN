/** Agent 只读诊断类型(对应后端 /api/agent/*) */

export interface AgentDevice {
  id: number
  name: string
  ip_address: string | null
  os_system: string | null
  status: string
  has_credential: boolean
  /** device=机房设备行;pve_guest=PVE 虚机(id 为合成负数) */
  kind?: 'device' | 'pve_guest'
}

export interface AgentStatus {
  enabled: boolean
  configured: boolean
}

export interface AgentStep {
  tool: string // run_diagnostic / run_action / get_metrics / llm_usage
  key?: string
  label?: string
  ok: boolean
  error?: string
  preview?: string
  hours?: number
  truncated?: boolean
  params?: Record<string, unknown>
  command?: string
  duration_ms?: number // 单步耗时(ms,仅 run_diagnostic;run 1056 复盘后新增)
  risk?: string // l1 / l2(仅 run_action)
  reason?: string
  approval_id?: number
  /** run_action 生命周期:pending/executed/rejected/expired/refused/execution_failed */
  final?: string
  auto?: boolean
  decision_note?: string | null
}

/** 分级动作审批记录(对应 agent_action_approvals) */
export interface AgentActionApproval {
  id: number
  run_id: number | null
  action_key: string
  params: Record<string, unknown>
  command: string
  risk_level: string
  reason: string
  status: string // pending/approved/rejected/expired/executed/execution_failed
  requested_at: string | null
  expires_at: string | null
  decided_at: string | null
  decided_by: number | null
  decision_note: string | null
  output: string
  executed_at: string | null
}

/** 待审批列表项(GET /agent/approvals,徽标轮询) */
export interface PendingApprovalItem extends AgentActionApproval {
  device_name: string | null
  run_status: string | null
}

export type AgentActionMode = 'readonly' | 'standard' | 'strict'

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
  mode: AgentActionMode
}

/** 历史记录列表项 */
export interface AgentRunSummary {
  id: number
  device_id: number | null
  device_name: string
  device_ip: string | null
  question: string
  status: string // running / completed / failed
  mode?: AgentActionMode
  waiting_approval?: boolean
  duration_ms: number | null
  report: string | null
  error: string | null
  created_at: string | null
}

/** 单次诊断详情(轮询进度用) */
export interface AgentRunDetail extends AgentRunSummary {
  steps: AgentStep[]
  approvals: AgentActionApproval[]
}
