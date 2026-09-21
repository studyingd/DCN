import { describe, expect, it } from 'vitest'
import { toDeviceResult, toSummaryCells } from '../inspectionAdapter'
import type { InspectionItemResult, InspectionRecordDetail } from '../../types/inspection'

function makeItem(overrides: Partial<InspectionItemResult> = {}): InspectionItemResult {
  return {
    id: 11,
    record_id: 7,
    item_type: 'cpu',
    success: true,
    value: '12.5',
    unit: '%',
    status: 'normal',
    details: null,
    raw_output: 'top -bn1 | head -5',
    error_message: null,
    command_used: 'top -bn1 | head -5',
    executed_at: '2026-01-01T00:00:00Z',
    duration_ms: 320,
    ...overrides,
  }
}

function makeRecord(overrides: Partial<InspectionRecordDetail> = {}): InspectionRecordDetail {
  return {
    id: 7,
    device_id: 3,
    device_name: 'web-01',
    device_ip: '10.0.0.11',
    target_type: 'linux',
    mode: 'core',
    status: 'partial',
    total_items: 6,
    normal_count: 4,
    warning_count: 1,
    critical_count: 1,
    error_count: 0,
    started_at: '2026-01-01T00:00:00Z',
    finished_at: '2026-01-01T00:00:06Z',
    duration_ms: 6200,
    triggered_by: 1,
    batch_id: null,
    items: [makeItem()],
    ...overrides,
  }
}

describe('toDeviceResult', () => {
  it('把详情接口的字段名改成卡片要的名字', () => {
    const result = toDeviceResult(makeRecord())
    // device_ip → ip_address
    expect(result.ip_address).toBe('10.0.0.11')
    // 详情接口的主键 id 就是巡检记录 id → record_id
    expect(result.record_id).toBe(7)
  })

  it('device_ip 为 null 时保持 null，卡片自己会渲染成 -', () => {
    expect(toDeviceResult(makeRecord({ device_ip: null })).ip_address).toBeNull()
  })

  it('duration_ms 为 null 时兜底成 0，避免卡片算秒数得到 NaN', () => {
    expect(toDeviceResult(makeRecord({ duration_ms: null })).duration_ms).toBe(0)
  })

  it('duration_ms 有值时原样透传', () => {
    expect(toDeviceResult(makeRecord({ duration_ms: 1500 })).duration_ms).toBe(1500)
  })

  it('其余字段完整透传', () => {
    const record = makeRecord()
    const result = toDeviceResult(record)
    expect(result).toEqual({
      device_id: 3,
      device_name: 'web-01',
      ip_address: '10.0.0.11',
      target_type: 'linux',
      record_id: 7,
      status: 'partial',
      total_items: 6,
      normal_count: 4,
      warning_count: 1,
      critical_count: 1,
      error_count: 0,
      duration_ms: 6200,
      items: record.items,
    })
  })

  it('items 直接透传，不做深拷贝也不丢字段', () => {
    const record = makeRecord({ items: [makeItem(), makeItem({ id: 12, item_type: 'logs', status: 'warning' })] })
    const result = toDeviceResult(record)
    expect(result.items).toBe(record.items)
    expect(result.items).toHaveLength(2)
    expect(result.items[1]?.item_type).toBe('logs')
  })

  it('源类型上没有 error 字段，适配结果里也不该凭空多出来', () => {
    expect('error' in toDeviceResult(makeRecord())).toBe(false)
  })

  it('Windows 记录同样适配', () => {
    const result = toDeviceResult(makeRecord({ target_type: 'windows', mode: 'core', status: 'completed' }))
    expect(result.target_type).toBe('windows')
    expect(result.status).toBe('completed')
  })
})

describe('toSummaryCells', () => {
  const resultJson = {
    record_id: 7,
    target_type: 'linux',
    total_items: 6,
    normal_count: 4,
    warning_count: 1,
    critical_count: 1,
    error_count: 0,
  }

  it('result_json 为 null（非巡检目标或尚未跑完）时返回空数组', () => {
    expect(toSummaryCells(null)).toEqual([])
    expect(toSummaryCells(undefined)).toEqual([])
  })

  it('缺 total_items 时返回空数组，不渲染半截汇总条', () => {
    expect(toSummaryCells({ record_id: 7 })).toEqual([])
    // 后端偶发把计数写成字符串，typeof 校验挡掉
    expect(toSummaryCells({ total_items: '6' })).toEqual([])
  })

  it('按「共 N 项 · 正常 · 警告 · 严重 · 错误」顺序输出', () => {
    expect(toSummaryCells(resultJson).map((cell) => cell.text)).toEqual([
      '共 6 项',
      '正常 4',
      '警告 1',
      '严重 1',
      '错误 0',
    ])
  })

  it('异常计数带 tone，总计与错误数沿用父级次要色', () => {
    expect(toSummaryCells(resultJson).map((cell) => cell.tone)).toEqual([
      '',
      'sum-normal',
      'sum-warning',
      'sum-critical',
      '',
    ])
  })

  it('计数字段缺失时兜底成 0', () => {
    expect(toSummaryCells({ total_items: 6 }).map((cell) => cell.text)).toEqual([
      '共 6 项',
      '正常 0',
      '警告 0',
      '严重 0',
      '错误 0',
    ])
  })

  it('全正常的设备也完整展示五项计数', () => {
    const cells = toSummaryCells({ ...resultJson, normal_count: 6, warning_count: 0, critical_count: 0 })
    expect(cells.map((cell) => cell.text)).toContain('正常 6')
    expect(cells).toHaveLength(5)
  })
})
