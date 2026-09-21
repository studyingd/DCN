import { describe, expect, it } from 'vitest'
import { barColor, fmtBps, fmtBytes, fmtClock, fmtMb, fmtPointTs, fmtUptime, type ThresholdMetric } from '../metrics'
import type { MetricThresholdTable } from '@/types/inspection'

// fmtClock/fmtPointTs 依赖本地时区,vitest.config.ts 固定 TZ=Asia/Shanghai(UTC+8)。

// 后端 GET /inspection/thresholds 下发的分指标阈值(契约里的那一份)
const TABLE: MetricThresholdTable = {
  cpu: { warning: 70, critical: 85 },
  memory: { warning: 75, critical: 90 },
  disk: { warning: 80, critical: 95 },
  failed_services: { warning: 1, critical: 3 },
}

const GREEN = 'var(--dcn-success)'
const YELLOW = 'var(--dcn-warning)'
const RED = 'var(--dcn-danger)'
const BLUE = 'var(--dcn-primary-light)'

describe('barColor 空值', () => {
  it('null / undefined 返回中性蓝,与有没有阈值表无关', () => {
    expect(barColor(null)).toBe(BLUE)
    expect(barColor(undefined)).toBe(BLUE)
    expect(barColor(null, 'cpu', TABLE)).toBe(BLUE)
    expect(barColor(undefined, 'disk', TABLE)).toBe(BLUE)
  })
})

describe('barColor 无阈值表时回退统一 70/90', () => {
  it('<70 绿、[70,90) 黄、>=90 红,等同接入阈值接口之前的行为', () => {
    expect(barColor(0)).toBe(GREEN)
    expect(barColor(-5)).toBe(GREEN)
    expect(barColor(69.9)).toBe(GREEN)
    expect(barColor(70)).toBe(YELLOW)
    expect(barColor(89.9)).toBe(YELLOW)
    expect(barColor(90)).toBe(RED)
    expect(barColor(100)).toBe(RED)
  })

  it('首屏表还没加载(undefined / null / 空对象)时,带了 metric 也走兜底', () => {
    // CPU 85 在阈值表里是 critical,但表没到位时只能按旧的 70/90 判成黄
    expect(barColor(85, 'cpu')).toBe(YELLOW)
    expect(barColor(85, 'cpu', undefined)).toBe(YELLOW)
    expect(barColor(85, 'cpu', null)).toBe(YELLOW)
    expect(barColor(85, 'cpu', {})).toBe(YELLOW)
  })

  it('不传 metric 时即使有表也不查表,避免拿错指标的阈值', () => {
    expect(barColor(85, undefined, TABLE)).toBe(YELLOW)
    expect(barColor(94, undefined, TABLE)).toBe(RED)
  })

  it('表里没有该指标项时单独回退', () => {
    expect(barColor(85, 'unknown' as ThresholdMetric, TABLE)).toBe(YELLOW)
    expect(barColor(90, 'unknown' as ThresholdMetric, TABLE)).toBe(RED)
    expect(barColor(89.9, 'unknown' as ThresholdMetric, TABLE)).toBe(YELLOW)
  })

  it('阈值项字段缺失时,缺的那个字段单独回退到 70/90', () => {
    const partial = { cpu: { warning: 60 } } as unknown as MetricThresholdTable
    expect(barColor(60, 'cpu', partial)).toBe(YELLOW)
    expect(barColor(89.9, 'cpu', partial)).toBe(YELLOW)
    expect(barColor(90, 'cpu', partial)).toBe(RED)
  })
})

describe('barColor 有阈值表时按指标分级', () => {
  it('CPU 85–89.9 是 critical(旧的一刀切只给黄,低估)', () => {
    expect(barColor(84.9, 'cpu', TABLE)).toBe(YELLOW)
    expect(barColor(85, 'cpu', TABLE)).toBe(RED)
    expect(barColor(89.9, 'cpu', TABLE)).toBe(RED)
  })

  it('内存 70–74.9 仍是 normal(旧的一刀切给黄,高估)', () => {
    expect(barColor(69.9, 'memory', TABLE)).toBe(GREEN)
    expect(barColor(70, 'memory', TABLE)).toBe(GREEN)
    expect(barColor(74.9, 'memory', TABLE)).toBe(GREEN)
    expect(barColor(75, 'memory', TABLE)).toBe(YELLOW)
    expect(barColor(90, 'memory', TABLE)).toBe(RED)
  })

  it('磁盘 70–79.9 仍是 normal(高估),90–94.9 只是 warning(旧的给红,高估)', () => {
    expect(barColor(70, 'disk', TABLE)).toBe(GREEN)
    expect(barColor(79.9, 'disk', TABLE)).toBe(GREEN)
    expect(barColor(80, 'disk', TABLE)).toBe(YELLOW)
    expect(barColor(90, 'disk', TABLE)).toBe(YELLOW)
    expect(barColor(94.9, 'disk', TABLE)).toBe(YELLOW)
    expect(barColor(95, 'disk', TABLE)).toBe(RED)
  })

  it('每个指标的边界一律 >= 含入', () => {
    // 这里把契约数字再抄一遍,阈值被改动时这条会先红
    const cases: Array<[ThresholdMetric, number, number]> = [
      ['cpu', 70, 85],
      ['memory', 75, 90],
      ['disk', 80, 95],
    ]
    for (const [metric, warning, critical] of cases) {
      expect(barColor(warning - 0.1, metric, TABLE)).toBe(GREEN)
      expect(barColor(warning, metric, TABLE)).toBe(YELLOW)
      expect(barColor(critical - 0.1, metric, TABLE)).toBe(YELLOW)
      expect(barColor(critical, metric, TABLE)).toBe(RED)
      expect(barColor(100, metric, TABLE)).toBe(RED)
    }
  })

  it('同一百分比在不同指标下可以落到不同颜色', () => {
    // 85% 对 CPU 已经 critical,对内存只是 warning,对磁盘刚过 warning 线不远
    expect(barColor(85, 'cpu', TABLE)).toBe(RED)
    expect(barColor(85, 'memory', TABLE)).toBe(YELLOW)
    expect(barColor(85, 'disk', TABLE)).toBe(YELLOW)
  })

  it('纯函数:同样的入参多次调用结果一致,不会因为调用顺序变化', () => {
    expect(barColor(85, 'cpu', TABLE)).toBe(barColor(85, 'cpu', TABLE))
    expect(barColor(85, 'cpu')).toBe(YELLOW)
    expect(barColor(85, 'cpu', TABLE)).toBe(RED)
  })
})

describe('fmtBps', () => {
  it('空值返回 "-"', () => {
    expect(fmtBps(null)).toBe('-')
    expect(fmtBps(undefined)).toBe('-')
  })

  it('<100 保留一位小数,>=100 取整', () => {
    expect(fmtBps(0)).toBe('0.0 B/s')
    expect(fmtBps(99)).toBe('99.0 B/s')
    expect(fmtBps(100)).toBe('100 B/s')
    expect(fmtBps(1023)).toBe('1023 B/s')
  })

  it('按 1024 进制升级单位', () => {
    expect(fmtBps(1024)).toBe('1.0 KB/s')
    expect(fmtBps(1536)).toBe('1.5 KB/s')
    expect(fmtBps(1024 * 1024)).toBe('1.0 MB/s')
    expect(fmtBps(1.5 * 1024 * 1024 * 1024)).toBe('1.5 GB/s')
    expect(fmtBps(1024 * 1024 * 1024)).toBe('1.0 GB/s')
  })

  it('最高只升到 GB/s,超出后数值继续增大', () => {
    expect(fmtBps(1024 * 1024 * 1024 * 1024)).toBe('1024 GB/s')
  })
})

describe('fmtBytes', () => {
  it('空值返回 "-"', () => {
    expect(fmtBytes(null)).toBe('-')
    expect(fmtBytes(undefined)).toBe('-')
  })

  it('与 fmtBps 同刻度但无 /s 后缀', () => {
    expect(fmtBytes(0)).toBe('0.0 B')
    expect(fmtBytes(2048)).toBe('2.0 KB')
    expect(fmtBytes(5 * 1024 * 1024)).toBe('5.0 MB')
    expect(fmtBytes(1024 * 1024 * 1024)).toBe('1.0 GB')
  })
})

describe('fmtUptime', () => {
  it('空值返回 "-"', () => {
    expect(fmtUptime(null)).toBe('-')
    expect(fmtUptime(undefined)).toBe('-')
  })

  it('不足 1 小时只显示分钟', () => {
    expect(fmtUptime(0)).toBe('0分钟')
    expect(fmtUptime(59)).toBe('0分钟')
    expect(fmtUptime(60)).toBe('1分钟')
    expect(fmtUptime(3599)).toBe('59分钟')
  })

  it('不足 1 天显示 小时+分钟', () => {
    expect(fmtUptime(3600)).toBe('1小时0分')
    expect(fmtUptime(3661)).toBe('1小时1分')
    expect(fmtUptime(5 * 3600 + 12 * 60 + 30)).toBe('5小时12分')
    expect(fmtUptime(86399)).toBe('23小时59分')
  })

  it('>=1 天显示 天+小时', () => {
    expect(fmtUptime(86400)).toBe('1天0小时')
    expect(fmtUptime(86400 + 3661)).toBe('1天1小时')
    expect(fmtUptime(3 * 86400 + 4 * 3600)).toBe('3天4小时')
  })
})

describe('fmtClock', () => {
  it('空值与非法输入返回 "-"', () => {
    expect(fmtClock(null)).toBe('-')
    expect(fmtClock(undefined)).toBe('-')
    expect(fmtClock('')).toBe('-')
    expect(fmtClock('abc')).toBe('-')
  })

  it('ISO 时间转本地 HH:MM:SS(UTC+8)并补零', () => {
    expect(fmtClock('2024-01-15T10:30:45Z')).toBe('18:30:45')
    expect(fmtClock('2024-01-15T01:02:03Z')).toBe('09:02:03')
    expect(fmtClock('2024-01-15T16:00:00Z')).toBe('00:00:00')
  })
})

describe('fmtPointTs', () => {
  it('非法输入返回空串', () => {
    expect(fmtPointTs('abc', false)).toBe('')
    expect(fmtPointTs('abc', true)).toBe('')
  })

  it('withDate=false 只带时分', () => {
    expect(fmtPointTs('2024-01-15T10:30:00Z', false)).toBe('18:30')
  })

  it('withDate=true 带 月-日 时分(7d 曲线)', () => {
    expect(fmtPointTs('2024-01-15T10:30:00Z', true)).toBe('01-15 18:30')
    expect(fmtPointTs('2024-01-15T20:05:00Z', true)).toBe('01-16 04:05')
  })

  it('跨年时显示本地日期', () => {
    // UTC 2024-12-31 18:00 → 本地 2025-01-01 02:00
    expect(fmtPointTs('2024-12-31T18:00:00Z', true)).toBe('01-01 02:00')
  })
})

describe('fmtMb', () => {
  it('空值返回 "-"', () => {
    expect(fmtMb(null)).toBe('-')
    expect(fmtMb(undefined)).toBe('-')
  })

  it('<1024 原样显示 MB', () => {
    expect(fmtMb(0)).toBe('0 MB')
    expect(fmtMb(512)).toBe('512 MB')
    expect(fmtMb(1023)).toBe('1023 MB')
    expect(fmtMb(1023.5)).toBe('1023.5 MB')
  })

  it('>=1024 转 GB 保留一位小数(1024 为边界含入)', () => {
    expect(fmtMb(1024)).toBe('1.0 GB')
    expect(fmtMb(1536)).toBe('1.5 GB')
    expect(fmtMb(2048)).toBe('2.0 GB')
  })
})
