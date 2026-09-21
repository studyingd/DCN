export interface User {
  id: number
  username: string
  role: string
  display_name: string | null
  is_active: number
  role_id: number | null
  role_name: string | null
  created_at: string
}

/** 角色授权的虚拟机身份（与后端 role_pve_guest_access 同构）。 */
export interface RolePveGuest {
  connection_id: number
  guest_type: string
  vmid: number
  guest_name?: string | null
}

export interface Role {
  id: number
  name: string
  description: string | null
  permissions: string[]
  /** 'all' = 设备与虚拟机均不受限；'selected' = 仅两张白名单内的资源。 */
  device_scope: string
  device_ids: number[]
  pve_guests: RolePveGuest[]
  is_builtin: number
  created_at: string
  user_count: number
}

export interface PermissionInfo {
  key: string
  label: string
}

export interface PermissionGroup {
  label: string
  permissions: string[]
}

export interface Room {
  id: number
  name: string
  location: string
  description: string
  created_at: string
  racks?: Rack[]
  rack_count?: number
}

export interface Rack {
  id: number
  room_id: number
  name: string
  type: 'cabinet' | 'shelf'
  capacity_u: number | null
  devices?: Device[]
}

export interface Device {
  id: number
  rack_id: number
  name: string
  type: string
  ip_address: string
  os_system: string
  status: string
  position_u: number | null
  size_u: number | null
  ssh_port: number
  rdp_port: number
  winrm_port: number
  credential_id?: number | null
  credential_username?: string | null
  has_credential?: boolean
  created_at: string
}

export interface DeviceCredentialPayload {
  username?: string
  password?: string
  ssh_key?: string
}

export interface DeviceSavePayload extends Partial<Device> {
  remote_credential?: DeviceCredentialPayload | null
}

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  refresh_token: string
  token_type: string
}

export interface TreeNodeData {
  id: number
  label: string
  type: 'room' | 'rack' | 'device'
  isLeaf?: boolean
  data: Room | Rack | Device
}

export interface Webhook {
  id: number
  name: string
  url: string
  provider: string
  events: string[]
  headers: Record<string, string>
  config: WebhookProviderConfig
  enabled: boolean
  secret_set: boolean
  secret_preview: string
  last_test_at: string | null
  last_test_status: string | null
  created_by: number | null
  created_at: string
  updated_at: string
}

export interface WebhookCreate {
  name: string
  url: string
  provider?: string
  secret?: string
  events?: string[]
  headers?: Record<string, string>
  config?: WebhookProviderConfig
  enabled?: boolean
}

export type WebhookUpdate = Partial<WebhookCreate> & { enabled?: boolean }

/** provider 专属配置;目前只有飞书自建应用(私聊指定人)会用到 */
export interface WebhookProviderConfig {
  app_id?: string
  /** 省略或 auto 时，后端按每个接收人的形态自动识别 ID 类型 */
  receive_id_type?: string
  receivers?: string[]
  /** 接收人 ID → 通讯录姓名，只用于界面回显 */
  receiver_names?: Record<string, string>
  /** 消息样式，固定卡片;仅保留字段兼容旧数据 */
  style?: string
  /** 级别路由:配置了只推所选级别(critical/warning/info)的告警,缺省不过滤 */
  severities?: string[]
}

/** 飞书通讯录:告警接收人直接从企业组织架构里挑，不用手抄 open_id */
export interface FeishuDepartment {
  open_department_id: string
  name: string
  parent_department_id: string
}
export interface FeishuUser {
  open_id: string
  user_id: string
  union_id: string
  name: string
  email: string
  enterprise_email: string
  avatar: string
  department_ids: string[]
}
/** 凭据可来自已保存的通道(webhook_id)，也可来自表单里现填的 App ID / Secret */
export interface FeishuDirectoryRequest {
  webhook_id?: number | null
  base?: string
  app_id?: string
  app_secret?: string
  department_id?: string
  page_token?: string
  page_size?: number
}
/** scope_limited=true 表示应用只被授权了部分通讯录，内容是按授权范围兜底出来的 */
/** name_scope_missing=true 表示飞书按字段级权限裁掉了名称，只能显示邮箱前缀或 ID */
export interface FeishuDepartmentPage {
  items: FeishuDepartment[]
  page_token: string
  has_more: boolean
  scope_limited: boolean
  name_scope_missing: boolean
}
export interface FeishuUserPage {
  items: FeishuUser[]
  page_token: string
  has_more: boolean
  scope_limited: boolean
  name_scope_missing: boolean
}

export type AlertMetric =
  | 'cpu_pct'
  | 'mem_pct'
  | 'disk_max_pct'
  | 'host_status'
  | 'container_status'
  | 'business_status'
export type AlertSeverity = 'info' | 'warning' | 'critical'
export type RemediationState = '' | 'running' | 'verifying' | 'succeeded' | 'failed' | 'skipped'
export type AnalysisState = '' | 'running' | 'completed' | 'failed' | 'skipped'
export interface AlertRule {
  id: number
  name: string
  metric: AlertMetric
  operator: 'gt' | 'gte'
  threshold: number
  severity: AlertSeverity
  target_device_ids: number[] | null
  target_container_ids: string[] | null
  target_business_ids: number[] | null
  cooldown_seconds: number
  sustain_seconds: number
  enabled: boolean
  active_event_count: number
  /** 本次更新因「停用规则」自动关闭的进行中告警条数；仅停用时会返回非 0 */
  closed_event_count?: number
  created_by: number | null
  created_at: string
  updated_at: string
}
export interface AlertEvent {
  /** 规则被删除后事件历史保留，rule_id 置空、rule_name 用规则名快照 */
  id: number
  rule_id: number | null
  rule_name: string
  device_name: string
  device_ip: string | null
  /** PVE 虚机/业务类事件没有本地设备行，device_id 为 null */
  device_id: number | null
  metric: AlertMetric
  value: number
  threshold: number
  severity: AlertSeverity
  status: 'open' | 'resolved'
  message: string
  resource_type?: string
  resource_id?: string | null
  resource_name?: string | null
  first_triggered_at: string
  last_seen_at: string
  resolved_at: string | null
  occurrence_count: number
  notification_status: 'pending' | 'sent' | 'failed' | 'skipped'
  last_notified_at: string | null
  /** 自动处置:离线虚拟机/容器自动拉起 */
  remediation_state?: RemediationState
  remediation_detail?: string | null
  remediation_target?: string | null
  remediation_attempts?: number
  /** 已知晓(snooze):恢复前不再重复提醒 */
  snoozed?: boolean
  snoozed_at?: string | null
  snoozed_by_name?: string | null
  /** 指标过高时的 Agent 归因分析;列表只给摘要，详情接口返回全文 */
  analysis_state?: AnalysisState
  analysis_summary?: string | null
  analysis_text?: string | null
  agent_run_id?: number | null
}
export interface AlertOverview {
  active_rules: number
  enabled_rules: number
  open_events: number
  critical_events: number
  events_24h: number
  failed_notifications: number
}
export interface AlertEventsPage {
  /** 分页模式(请求带 page)返回对象;旧调用(不带 page)直接返回 AlertEvent[] */
  items: AlertEvent[]
  total: number
  page: number
  page_size: number
}
export interface MaintenanceWindow {
  id: number
  name: string
  target_ids: number[] | null
  start_at: string
  end_at: string
  enabled: boolean
  muted_count: number
  summary_state: string
  state: 'pending' | 'active' | 'ended'
  target_label: string
  created_at: string
}
