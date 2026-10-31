import type { Component } from 'vue'
import { ChatLineSquare, DataAnalysis, Promotion, SwitchButton } from '@element-plus/icons-vue'
import type { AutomationJobType } from '@/types/automation'

/** 图标色调(automation.css 的 .atm-icon--* 修饰类) */
export type JobTypeTone = 'primary' | 'success' | 'warning' | 'danger'

export interface JobTypeMeta {
  key: AutomationJobType
  label: string
  description: string
  icon: Component
  readOnly: boolean
  permission: string
  /** 图标色调:agent=蓝 / inspection=绿 / script=橙 / power=红,与风险等级对应 */
  tone: JobTypeTone
}

/**
 * 全量任务类型元数据(**含 agent**):历史列表/计划列表/详情抽屉仍会渲染
 * agent 类型的记录,不要从全量表里删它。
 */
export const allJobTypes: JobTypeMeta[] = [
  {
    key: 'agent',
    label: 'Agent 诊断任务',
    description:
      '批量/定时场景:自然语言描述问题,Agent 只读取证并生成报告(单台实时诊断与变更审批请用「Agent 分析诊断」)',
    icon: ChatLineSquare,
    readOnly: true,
    permission: 'device:remote',
    tone: 'primary',
  },
  {
    key: 'inspection',
    label: '健康巡检',
    description: '按标准检查项批量验证设备健康状态并识别异常',
    icon: DataAnalysis,
    readOnly: true,
    permission: 'automation:manage',
    tone: 'success',
  },
  {
    key: 'script',
    label: '批量执行',
    description: '在多台设备上执行明确的 Shell 或 PowerShell 命令',
    icon: Promotion,
    readOnly: false,
    permission: 'automation:manage',
    tone: 'warning',
  },
  {
    key: 'power',
    label: '电源操作',
    description: '批量重启或关闭目标服务器，并验证设备离线状态',
    icon: SwitchButton,
    readOnly: false,
    permission: 'automation:manage',
    tone: 'danger',
  },
]

export const jobTypeMeta = Object.fromEntries(allJobTypes.map((item) => [item.key, item])) as Record<
  AutomationJobType,
  JobTypeMeta
>

/**
 * 手动创建入口可见的类型:过滤掉 agent(2026-10-25 入口合并——单台实时
 * 诊断统一走「Agent 分析诊断」,定时经诊断完成后的「存为定时计划」)再按权限过滤。
 */
export function availableJobTypesFor(hasPermission: (perm: string) => boolean): JobTypeMeta[] {
  return allJobTypes.filter((item) => item.key !== 'agent' && hasPermission(item.permission))
}
