import type { InspectionDeviceResult, InspectionRecordDetail } from '@/types/inspection'

/**
 * 把巡检记录详情（GET /inspection/records/{id}）适配成 InspectionResultCard 需要的
 * InspectionDeviceResult（POST /inspection/run 的返回形状）。
 *
 * 两个接口描述的是同一次巡检，但字段名不同：详情用 `id` / `device_ip`，运行结果用
 * `record_id` / `ip_address`；详情的 `duration_ms` 还允许为 null，而卡片要拿它做除法。
 * 统一在这里做一次纯映射，避免在组件模板里散落改名逻辑。
 */
export function toDeviceResult(record: InspectionRecordDetail): InspectionDeviceResult {
  return {
    device_id: record.device_id,
    device_name: record.device_name,
    // 详情接口叫 device_ip，卡片读的是 ip_address
    ip_address: record.device_ip,
    target_type: record.target_type,
    // 详情接口的主键 id 就是巡检记录 id
    record_id: record.id,
    status: record.status,
    total_items: record.total_items,
    normal_count: record.normal_count,
    warning_count: record.warning_count,
    critical_count: record.critical_count,
    error_count: record.error_count,
    // 记录尚未结束（running）时后端给 null，卡片要算秒数，兜底成 0
    duration_ms: record.duration_ms ?? 0,
    items: record.items,
  }
}

/** 每台设备汇总条的一个单元格；tone 为空串表示沿用父级的次要文字色 */
export interface InspectionSummaryCell {
  text: string
  tone: '' | 'sum-normal' | 'sum-warning' | 'sum-critical'
}

/**
 * 把自动化任务 target.result_json 里后端已经算好的计数拼成汇总条单元格。
 * 只在完整巡检记录还没拉到（或拉取失败）时兜底展示，避免与 InspectionResultCard
 * 头部的 ✓/⚠/✕/? 计数重复。非巡检目标（result_json 为 null）返回空数组。
 */
export function toSummaryCells(resultJson: Record<string, any> | null | undefined): InspectionSummaryCell[] {
  if (!resultJson || typeof resultJson.total_items !== 'number') return []
  return [
    { text: `共 ${resultJson.total_items} 项`, tone: '' },
    { text: `正常 ${resultJson.normal_count ?? 0}`, tone: 'sum-normal' },
    { text: `警告 ${resultJson.warning_count ?? 0}`, tone: 'sum-warning' },
    { text: `严重 ${resultJson.critical_count ?? 0}`, tone: 'sum-critical' },
    { text: `错误 ${resultJson.error_count ?? 0}`, tone: '' },
  ]
}
