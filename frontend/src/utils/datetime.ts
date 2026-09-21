/**
 * 时间展示/提交的统一入口。
 *
 * 后端所有时间戳都是带时区偏移的 UTC(见 backend/app/database.py 的 UTCDateTime)，
 * 展示时交给浏览器换算成本地时区即可。切记不要直接截取 ISO 字符串——那样等于把
 * UTC 墙钟时间当本地时间显示，在国内会慢 8 小时。
 */

function toDate(value?: string | Date | null): Date | null {
  if (!value) return null
  const date = value instanceof Date ? value : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/** 本地时区的 `YYYY-MM-DD HH:mm`;withSeconds 为真时补到秒。 */
export function formatDateTime(value?: string | Date | null, withSeconds = false): string {
  const date = toDate(value)
  if (!date) return '-'
  const day = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  const hm = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  return withSeconds ? `${day} ${hm}:${pad(date.getSeconds())}` : `${day} ${hm}`
}

/** 表单里选的本地时间 → 带时区的 UTC ISO 字符串;后端按 UTC 存库与调度。 */
export function toUtcIso(value?: string | Date | null): string | null {
  const date = toDate(value)
  return date ? date.toISOString() : null
}
