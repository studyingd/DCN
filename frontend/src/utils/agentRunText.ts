/**
 * 自动化运维 / Agent 诊断的状态文案与色调映射(唯一真源)。
 * AutomationPanel 各分区与 AgentRunTimeline 共用;
 * 新增状态时只改这里,不要在组件里再写内联 map。
 */

// ── 自动化任务(job)状态 ──
const JOB_STATUS_LABELS: Record<string, string> = {
  pending: '等待中',
  running: '执行中',
  completed: '已完成',
  partial: '部分异常',
  failed: '执行失败',
  cancelled: '已取消',
}
const JOB_STATUS_TAGS: Record<string, string> = {
  pending: 'info',
  running: 'primary',
  completed: 'success',
  partial: 'warning',
  failed: 'danger',
  cancelled: 'info',
}
export function jobStatusLabel(status: string): string {
  return JOB_STATUS_LABELS[status] || status
}
export function jobStatusTagType(status: string): string {
  return JOB_STATUS_TAGS[status] || 'info'
}

// ── job 内单目标(target)状态 ──
const TARGET_STATUS_LABELS: Record<string, string> = {
  pending: '等待中',
  running: '执行中',
  completed: '成功',
  warning: '存在异常',
  failed: '失败',
}
const TARGET_STATUS_TAGS: Record<string, string> = {
  pending: 'info',
  running: 'primary',
  completed: 'success',
  warning: 'warning',
  failed: 'danger',
}
export function targetStatusLabel(status: string): string {
  return TARGET_STATUS_LABELS[status] || status
}
export function targetStatusTagType(status: string): string {
  return TARGET_STATUS_TAGS[status] || 'info'
}

// ── 定时计划状态 ──
const SCHEDULE_STATUS_LABELS: Record<string, string> = {
  active: '启用',
  paused: '已暂停',
  completed: '已完成',
  disabled: '已停用',
}
export function scheduleStatusLabel(status: string): string {
  return SCHEDULE_STATUS_LABELS[status] || status
}
/** disabled(授权失效系统停用)与 paused(用户主动暂停)分色:前者需要排查,后者随时可恢复 */
export function scheduleStatusTagType(status: string): string {
  if (status === 'active') return 'success'
  if (status === 'paused') return 'warning'
  return 'info'
}

/** el-progress 状态:与 jobProgress/targetsProgress 的终态口径一致 */
export function progressStatus(status: string): 'success' | 'exception' | undefined {
  return status === 'completed' ? 'success' : status === 'failed' ? 'exception' : undefined
}

// ── Agent 诊断运行(run)状态 / 动作模式 ──
export function runStatusText(status: string): string {
  return status === 'running' ? '运行中' : status === 'completed' ? '已完成' : status === 'failed' ? '失败' : status
}
export function runStatusTagType(status: string): string {
  return status === 'completed' ? 'success' : status === 'failed' ? 'danger' : 'info'
}
export function actionModeText(mode?: string): string {
  return mode === 'standard' ? '标准' : mode === 'strict' ? '严格' : '只读'
}

// ── 分级动作审批终态(run_action 的 final 字段) ──
const ACTION_FINAL_LABELS: Record<string, string> = {
  pending: '待审批',
  executed: '已执行',
  execution_failed: '执行失败',
  rejected: '已拒绝',
  expired: '审批超时',
  refused: '已拦截',
}
const ACTION_FINAL_TAGS: Record<string, string> = {
  pending: 'warning',
  executed: 'success',
  execution_failed: 'danger',
  rejected: 'info',
  expired: 'info',
  refused: 'danger',
}
export function actionFinalText(final?: string): string {
  return ACTION_FINAL_LABELS[final || ''] || final || ''
}
export function actionFinalTagType(final?: string): string {
  return ACTION_FINAL_TAGS[final || ''] || 'info'
}
