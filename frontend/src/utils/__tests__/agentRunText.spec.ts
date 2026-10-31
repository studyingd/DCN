import { describe, expect, it } from 'vitest'
import {
  actionFinalTagType,
  actionFinalText,
  actionModeText,
  jobStatusLabel,
  jobStatusTagType,
  progressStatus,
  runStatusTagType,
  runStatusText,
  scheduleStatusLabel,
  scheduleStatusTagType,
  targetStatusLabel,
  targetStatusTagType,
} from '@/utils/agentRunText'

describe('agentRunText', () => {
  it('job 状态文案/色调覆盖全量状态,未知值原样/兜底', () => {
    expect(jobStatusLabel('partial')).toBe('部分异常')
    expect(jobStatusLabel('cancelled')).toBe('已取消')
    expect(jobStatusLabel('weird')).toBe('weird')
    expect(jobStatusTagType('running')).toBe('primary')
    expect(jobStatusTagType('weird')).toBe('info')
  })

  it('target 状态文案区分 completed=成功 与 warning=存在异常', () => {
    expect(targetStatusLabel('completed')).toBe('成功')
    expect(targetStatusLabel('warning')).toBe('存在异常')
    expect(targetStatusTagType('warning')).toBe('warning')
  })

  it('计划状态 paused 与 disabled 分色(用户暂停 vs 系统停用)', () => {
    expect(scheduleStatusLabel('paused')).toBe('已暂停')
    expect(scheduleStatusLabel('disabled')).toBe('已停用')
    expect(scheduleStatusTagType('active')).toBe('success')
    expect(scheduleStatusTagType('paused')).toBe('warning')
    expect(scheduleStatusTagType('disabled')).toBe('info')
  })

  it('进度条终态只有 completed/failed 两种', () => {
    expect(progressStatus('completed')).toBe('success')
    expect(progressStatus('failed')).toBe('exception')
    expect(progressStatus('running')).toBeUndefined()
  })

  it('run 状态/模式文案', () => {
    expect(runStatusText('running')).toBe('运行中')
    expect(runStatusText('completed')).toBe('已完成')
    expect(runStatusText('failed')).toBe('失败')
    expect(runStatusTagType('completed')).toBe('success')
    expect(actionModeText('standard')).toBe('标准')
    expect(actionModeText('strict')).toBe('严格')
    expect(actionModeText('readonly')).toBe('只读')
    expect(actionModeText(undefined)).toBe('只读')
  })

  it('审批终态文案/色调覆盖六种 final', () => {
    expect(actionFinalText('pending')).toBe('待审批')
    expect(actionFinalText('execution_failed')).toBe('执行失败')
    expect(actionFinalText('refused')).toBe('已拦截')
    expect(actionFinalText(undefined)).toBe('')
    expect(actionFinalTagType('executed')).toBe('success')
    expect(actionFinalTagType('refused')).toBe('danger')
    expect(actionFinalTagType(undefined)).toBe('info')
  })
})
