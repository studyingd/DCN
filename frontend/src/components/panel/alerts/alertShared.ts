import { reactive } from 'vue'
import { alertAPI } from '@/api'
import type { AlertEvent } from '@/types'

/** 数值型指标(阈值/操作符只对它们有意义) */
export const numericMetrics = ['cpu_pct', 'mem_pct', 'disk_max_pct']
/** 可自动处置的指标(「尝试拉起」按钮) */
export const healMetrics = ['host_status', 'container_status']

// ── 展示文案:后端为唯一真源(/api/alerts/labels),拉取前用本地兜底 ──
// 避免前后端两份字典漂移(曾出现前端多 host_online 等后端没有的键)。
export const FALLBACK_METRIC_LABELS: Record<string, string> = {
  cpu_pct: 'CPU',
  mem_pct: '内存',
  disk_max_pct: '磁盘',
  host_status: '主机状态异常',
  container_status: '容器状态异常',
  business_status: '业务状态异常',
}
export const FALLBACK_SEVERITY_LABELS: Record<string, string> = { info: '信息', warning: '警告', critical: '严重' }
export const FALLBACK_NOTIFY_LABELS: Record<string, string> = {
  sent: '已发送',
  failed: '失败',
  skipped: '跳过',
  pending: '待发送',
}
export const FALLBACK_REMEDIATION_LABELS: Record<string, string> = {
  '': '未处置',
  running: '自动处置中',
  verifying: '等待恢复确认',
  succeeded: '处置成功',
  failed: '处置失败',
  skipped: '无法自动处置',
}
export const FALLBACK_ANALYSIS_LABELS: Record<string, string> = {
  '': '未分析',
  running: 'AI 归因中',
  completed: 'AI 归因完成',
  failed: 'AI 归因失败',
  skipped: '未启用 Agent',
}
/** 模块级共享字典:AlertCenter 与事件子表共用同一份(原为组件实例各自持有) */
export const labelDicts = reactive({
  metrics: FALLBACK_METRIC_LABELS,
  severities: FALLBACK_SEVERITY_LABELS,
  notify_status: FALLBACK_NOTIFY_LABELS,
  remediation_states: FALLBACK_REMEDIATION_LABELS,
  analysis_states: FALLBACK_ANALYSIS_LABELS,
})
/** 模块级缓存:多实例挂载只拉一次 */
let _labelsPromise: Promise<void> | null = null
export function loadAlertLabels() {
  if (!_labelsPromise) {
    _labelsPromise = alertAPI
      .labels()
      .then(({ data }) => {
        Object.assign(labelDicts.metrics, data.metrics)
        Object.assign(labelDicts.severities, data.severities)
        Object.assign(labelDicts.notify_status, data.notify_status)
        Object.assign(labelDicts.remediation_states, data.remediation_states)
        Object.assign(labelDicts.analysis_states, data.analysis_states)
      })
      .catch(() => {
        _labelsPromise = null // 失败不缓存,下次挂载重试
      })
  }
  return _labelsPromise
}

export const metricLabel = (m: string) => labelDicts.metrics[m] || m
export const severityLabel = (s: string) => labelDicts.severities[s] || s
export const severityType = (s: string) => (s === 'critical' ? 'danger' : s === 'warning' ? 'warning' : 'info')
export const notifyLabel = (s: string) => labelDicts.notify_status[s] || s
export const remediationLabel = (s?: string) => labelDicts.remediation_states[s || ''] || '未处置'
export const remediationTag = (s?: string) =>
  s === 'succeeded' ? 'success' : s === 'failed' ? 'danger' : s === 'running' || s === 'verifying' ? 'warning' : 'info'
export const analysisLabel = (s?: string) => labelDicts.analysis_states[s || ''] || '未分析'
export const analysisTag = (s?: string) =>
  s === 'completed' ? 'success' : s === 'failed' ? 'danger' : s === 'running' ? 'warning' : 'info'

export const canRemediate = (row: AlertEvent) => healMetrics.includes(row.metric)
export const canAnalyze = (row: AlertEvent) => numericMetrics.includes(row.metric)

export const healHint = (row: AlertEvent) =>
  [row.remediation_target, row.remediation_attempts ? `已尝试 ${row.remediation_attempts} 次` : '']
    .filter(Boolean)
    .join(' · ')

export function errText(e: unknown, fallback: string) {
  return (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || fallback
}

function dateParts(value: string) {
  const date = value ? new Date(value) : null
  return date && !Number.isNaN(date.getTime()) ? date : null
}
export const fmtDate = (v: string) => {
  const d = dateParts(v)
  return d
    ? `${d.getFullYear()}/${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')}`
    : '-'
}
export const fmtClock = (v: string) => {
  const d = dateParts(v)
  return d ? `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}` : '--:--'
}
