/** 指标展示共用的格式化与阈值配色(MetricsPanel / DeviceDetail / PvePanel / ContainersPanel 共用) */

import type { MetricThresholdTable } from '@/types/inspection'

/** 阈值表里有对应项的指标名,与后端 item_type 对齐(不是 cpu_pct / mem_pct 这类指标字段名) */
export type ThresholdMetric = 'cpu' | 'memory' | 'disk'

// 阈值表没到位时的一刀切兜底,等同接入 /inspection/thresholds 之前的行为。
// 这里刻意不复制后端的分指标阈值(cpu 70/85、memory 75/90、disk 80/95),
// 否则阈值一改就要前后端各改一次、还会悄悄漂移。
const FALLBACK_WARNING = 70
const FALLBACK_CRITICAL = 90

/**
 * 阈值配色:>=critical 红,>=warning 黄,否则绿;空值中性蓝。边界一律 >=。
 * 命中 thresholds[metric] 时用后端下发的分指标阈值,否则回退到统一 70/90
 * (首屏还没加载完、请求失败、或该指标没有阈值项时都是这个行为)。
 * 保持纯函数:阈值表由调用方传入(组件侧见 @/composables/useMetricColor),便于单测。
 */
export function barColor(
  p: number | null | undefined,
  metric?: ThresholdMetric,
  thresholds?: MetricThresholdTable | null,
): string {
  if (p == null) return 'var(--dcn-primary-light)'
  const threshold = metric ? thresholds?.[metric] : undefined
  const warning = threshold?.warning ?? FALLBACK_WARNING
  const critical = threshold?.critical ?? FALLBACK_CRITICAL
  if (p >= critical) return 'var(--dcn-danger)'
  if (p >= warning) return 'var(--dcn-warning)'
  return 'var(--dcn-success)'
}

/** 速率/字节数人性化:B/s、KB/s、MB/s…(传入字节数时去掉 /s 后缀用 fmtBytes) */
export function fmtBps(v: number | null | undefined): string {
  if (v == null) return '-'
  const units = ['B/s', 'KB/s', 'MB/s', 'GB/s']
  let n = v
  let u = 0
  while (n >= 1024 && u < units.length - 1) {
    n /= 1024
    u++
  }
  return `${n >= 100 ? Math.round(n) : n.toFixed(1)} ${units[u]}`
}

export function fmtBytes(v: number | null | undefined): string {
  if (v == null) return '-'
  return fmtBps(v).replace('/s', '')
}

/** 运行时长:3天4小时 / 5小时12分 / 30分钟 */
export function fmtUptime(sec: number | null | undefined): string {
  if (sec == null) return '-'
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (d > 0) return `${d}天${h}小时`
  if (h > 0) return `${h}小时${m}分`
  return `${m}分钟`
}

function pad(n: number): string {
  return n < 10 ? `0${n}` : String(n)
}

/** ISO 时间 → HH:MM:SS(本地时区) */
export function fmtClock(ts: string | null | undefined): string {
  if (!ts) return '-'
  const d = new Date(ts)
  if (isNaN(d.getTime())) return '-'
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** 历史曲线 X 轴:7d 带日期,其余只带时分 */
export function fmtPointTs(ts: string, withDate: boolean): string {
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ''
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return withDate ? `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}` : hm
}

/** MB 人性化:≥1024 转 GB */
export function fmtMb(v: number | null | undefined): string {
  if (v == null) return '-'
  return v >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${v} MB`
}
