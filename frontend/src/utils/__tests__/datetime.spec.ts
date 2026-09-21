import { describe, expect, it } from 'vitest'
import { formatDateTime, toUtcIso } from '../datetime'

// vitest.config.ts 固定 TZ=Asia/Shanghai(UTC+8),以下本地时间断言在任何机器上可复现。

describe('formatDateTime', () => {
  it('对 null/undefined/空串返回 "-"', () => {
    expect(formatDateTime(null)).toBe('-')
    expect(formatDateTime(undefined)).toBe('-')
    expect(formatDateTime('')).toBe('-')
  })

  it('对非法输入返回 "-"', () => {
    expect(formatDateTime('not-a-date')).toBe('-')
    expect(formatDateTime(new Date('not-a-date'))).toBe('-')
    expect(formatDateTime(new Date(NaN))).toBe('-')
  })

  it('UTC ISO 字符串按本地时区(UTC+8)换算展示,不直接截取字符串', () => {
    expect(formatDateTime('2024-01-15T10:30:00Z')).toBe('2024-01-15 18:30')
  })

  it('跨日进位到第二天', () => {
    expect(formatDateTime('2024-01-15T20:00:00Z')).toBe('2024-01-16 04:00')
  })

  it('月/日/时/分补零', () => {
    expect(formatDateTime('2024-03-05T01:02:03Z')).toBe('2024-03-05 09:02')
  })

  it('withSeconds 为真时补到秒', () => {
    expect(formatDateTime('2024-03-05T01:02:03Z', true)).toBe('2024-03-05 09:02:03')
    expect(formatDateTime('2024-01-15T10:30:00Z', true)).toBe('2024-01-15 18:30:00')
  })

  it('接受 Date 实例', () => {
    expect(formatDateTime(new Date('2024-06-01T00:00:00Z'))).toBe('2024-06-01 08:00')
    expect(formatDateTime(new Date('2024-06-01T00:00:00Z'), true)).toBe('2024-06-01 08:00:00')
  })

  it('接受带时区偏移的 ISO 字符串', () => {
    expect(formatDateTime('2024-01-15T18:30:00+08:00')).toBe('2024-01-15 18:30')
    expect(formatDateTime('2024-01-15T05:30:00-05:00')).toBe('2024-01-15 18:30')
  })
})

describe('toUtcIso', () => {
  it('对 null/undefined/空串返回 null', () => {
    expect(toUtcIso(null)).toBeNull()
    expect(toUtcIso(undefined)).toBeNull()
    expect(toUtcIso('')).toBeNull()
  })

  it('对非法输入返回 null', () => {
    expect(toUtcIso('garbage')).toBeNull()
    expect(toUtcIso(new Date('garbage'))).toBeNull()
  })

  it('Date 实例转为 UTC ISO 字符串', () => {
    expect(toUtcIso(new Date('2024-01-15T10:30:00Z'))).toBe('2024-01-15T10:30:00.000Z')
  })

  it('本地墙钟字符串(表单输入)按本地时间解析后转 UTC', () => {
    // TZ=Asia/Shanghai:本地 18:30 → UTC 10:30
    expect(toUtcIso('2024-01-15 18:30:00')).toBe('2024-01-15T10:30:00.000Z')
  })

  it('带偏移的 ISO 字符串归一化为 UTC', () => {
    expect(toUtcIso('2024-01-15T18:30:00+08:00')).toBe('2024-01-15T10:30:00.000Z')
    expect(toUtcIso('2024-01-15T10:30:00Z')).toBe('2024-01-15T10:30:00.000Z')
  })
})
