/**
 * 5 段 cron 表达式（分 时 日 月 周）的构建、解析与中文化。
 *
 * 单一真源：ScriptPanel 的计划任务与 AutomationPanel 的周期执行（含健康巡检）
 * 共用这里，避免两处各自维护一份 build/parse 逻辑后悄悄漂移。
 *
 * 后端 `app/services/cron.py` 支持 `*` / 具体值 / 区间 / 步长 / 列表，
 * 周字段 Sunday=0（7 会被归一化成 0）。
 *
 * 界面上不提供手写 cron 的入口，所以构建器必须能表达运维真正需要的周期——
 * 尤其是「工作日」「周末」这类多天组合。无法表达的表达式（如一天两次的
 * `0 2,14 * * *`）parseCron 返回 null，调用方应**原样保留原值并提示不可编辑**，
 * 绝不能猜一个近似值：猜错会让用户以为改的是原计划，实际提交的是另一个周期。
 */

export type CronMode = 'minutes' | 'hours' | 'daily' | 'weekly' | 'monthly'

export interface CronState {
  mode: CronMode
  /** minutes 模式：每隔几分钟；hours 模式：每隔几小时 */
  interval: number
  /** hours 模式：在第几分钟执行 */
  minute: number
  /** daily / weekly / monthly 模式的小时 */
  hour: number
  /** daily / weekly / monthly 模式的分钟 */
  timeMinute: number
  /** weekly 模式：0=周日 … 6=周六，可多选；全选等价于每天 */
  weekdays: number[]
  /** monthly 模式：1-31 日 */
  day: number
}

export const WEEKDAY_LABELS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'] as const

/** 中文习惯把周日排在最后，展示用；cron 字段本身仍按 0-6 升序写 */
const WEEKDAY_DISPLAY_ORDER = [1, 2, 3, 4, 5, 6, 0]

export const DEFAULT_CRON_STATE: CronState = {
  mode: 'daily',
  interval: 30,
  minute: 0,
  hour: 8,
  timeMinute: 0,
  weekdays: [1],
  day: 1,
}

const pad = (n: number) => String(n).padStart(2, '0')

function toInt(token: string): number | null {
  return /^\d+$/.test(token.trim()) ? parseInt(token.trim(), 10) : null
}

function normalizeWeekdays(days: number[]): number[] {
  return [...new Set(days)].filter((d) => Number.isInteger(d) && d >= 0 && d <= 6).sort((a, b) => a - b)
}

/**
 * 周字段编码：全选/空 → `*`，连续多天 → `1-5`，零散多天 → `1,3,5`。
 * 连续区间比逐个列举直观，也是运维手写工作日时的惯用形式。
 */
export function encodeWeekdays(days: number[]): string {
  const uniq = normalizeWeekdays(days)
  if (uniq.length === 0 || uniq.length === 7) return '*'
  const contiguous = uniq.every((d, i) => i === 0 || d === uniq[i - 1] + 1)
  if (contiguous && uniq.length >= 2) return `${uniq[0]}-${uniq[uniq.length - 1]}`
  return uniq.join(',')
}

/**
 * 周字段解码，支持星号 / 单值 / 区间 / 列表 / 区间与列表混写（`1-3,6`）。
 * 非法或含步长（斜杠写法，在周字段上语义模糊）时返回 null，不猜。
 *
 * 注：本注释里不写 `*` + `/` 连写的步长字面量，因为那会提前终止块注释。
 */
export function decodeWeekdays(field: string): number[] | null {
  const token = field.trim()
  if (!token) return null
  if (token === '*') return [0, 1, 2, 3, 4, 5, 6]
  if (token.includes('/')) return null

  const out = new Set<number>()
  for (const part of token.split(',')) {
    const piece = part.trim()
    if (!piece) return null
    if (piece.includes('-')) {
      const [startRaw, endRaw] = piece.split('-')
      const start = toInt(startRaw)
      const end = toInt(endRaw)
      if (start === null || end === null || start > end) return null
      // cron 惯例 7=周日，折回 0，与后端 _normalize_weekday 保持一致
      if (start < 0 || end > 7) return null
      for (let i = start; i <= end; i += 1) out.add(i === 7 ? 0 : i)
    } else {
      const value = toInt(piece)
      if (value === null || value < 0 || value > 7) return null
      out.add(value === 7 ? 0 : value)
    }
  }
  if (!out.size) return null
  return [...out].sort((a, b) => a - b)
}

/** 构建器状态 → cron 表达式。 */
export function buildCron(state: CronState): string {
  const { hour, timeMinute } = state
  switch (state.mode) {
    case 'minutes':
      return `*/${state.interval} * * * *`
    case 'hours':
      return `${state.minute} */${state.interval} * * *`
    case 'daily':
      return `${timeMinute} ${hour} * * *`
    case 'weekly':
      return `${timeMinute} ${hour} * * ${encodeWeekdays(state.weekdays)}`
    case 'monthly':
      return `${timeMinute} ${hour} ${state.day} * *`
    default:
      return '* * * * *'
  }
}

/**
 * cron 表达式 → 构建器状态；表达不了的形状返回 null（见文件头说明）。
 */
export function parseCron(expr: string | null | undefined): CronState | null {
  const trimmed = (expr ?? '').trim()
  if (!trimmed) return null
  const parts = trimmed.split(/\s+/)
  if (parts.length !== 5) return null
  const [min, hour, day, month, dow] = parts
  const base: CronState = { ...DEFAULT_CRON_STATE, weekdays: [...DEFAULT_CRON_STATE.weekdays] }

  // 每隔 N 分钟：*/N * * * *
  if (min.startsWith('*/') && hour === '*' && day === '*' && month === '*' && dow === '*') {
    const interval = toInt(min.slice(2))
    if (interval === null || interval < 1 || interval > 59) return null
    return { ...base, mode: 'minutes', interval }
  }

  const minuteValue = toInt(min)
  const hourValue = toInt(hour)

  // 每隔 N 小时：M */N * * *
  if (hour.startsWith('*/') && minuteValue !== null && day === '*' && month === '*' && dow === '*') {
    const interval = toInt(hour.slice(2))
    if (interval === null || interval < 1 || interval > 23) return null
    return { ...base, mode: 'hours', interval, minute: minuteValue }
  }

  if (minuteValue === null || minuteValue > 59) return null
  if (hourValue === null || hourValue > 23) return null

  // 每天 hh:mm
  if (day === '*' && month === '*' && dow === '*') {
    return { ...base, mode: 'daily', hour: hourValue, timeMinute: minuteValue }
  }

  // 每周（可多天）hh:mm
  if (day === '*' && month === '*' && dow !== '*') {
    const weekdays = decodeWeekdays(dow)
    if (!weekdays) return null
    // 七天全选在语义上就是每天，归到 daily，避免同一个周期有两种表述
    if (weekdays.length === 7) {
      return { ...base, mode: 'daily', hour: hourValue, timeMinute: minuteValue }
    }
    return { ...base, mode: 'weekly', weekdays, hour: hourValue, timeMinute: minuteValue }
  }

  // 每月 D 日 hh:mm
  if (day !== '*' && month === '*' && dow === '*') {
    const dayNum = toInt(day)
    if (dayNum === null || dayNum < 1 || dayNum > 31) return null
    return { ...base, mode: 'monthly', day: dayNum, hour: hourValue, timeMinute: minuteValue }
  }

  return null
}

/** 是否为合法的 5 段 cron（宽松校验：段数 + 字符集，严格校验交给后端）。 */
export function isCronShape(expr: string | null | undefined): boolean {
  const trimmed = (expr ?? '').trim()
  if (!trimmed) return false
  const parts = trimmed.split(/\s+/)
  return parts.length === 5 && parts.every((p) => /^[\d*/,.-]+$/.test(p))
}

/** 周集合 → 中文，例如「周一至周五」「周六、周日」。 */
export function humanizeWeekdays(days: number[]): string {
  const uniq = normalizeWeekdays(days)
  if (uniq.length === 0 || uniq.length === 7) return '每天'
  const contiguous = uniq.every((d, i) => i === 0 || d === uniq[i - 1] + 1)
  if (contiguous && uniq.length >= 2) {
    return `每${WEEKDAY_LABELS[uniq[0]]}至${WEEKDAY_LABELS[uniq[uniq.length - 1]]}`
  }
  const ordered = WEEKDAY_DISPLAY_ORDER.filter((d) => uniq.includes(d))
  return `每${ordered.map((d) => WEEKDAY_LABELS[d]).join('、')}`
}

/**
 * cron 表达式 → 中文描述。
 *
 * 认不出的形状原样返回表达式本身——宁可显示 `0 2,14 * * *`，也不要编一句
 * 可能错的「每天 02:00」误导用户。
 */
export function humanizeCron(expr: string | null | undefined): string {
  const trimmed = (expr ?? '').trim()
  if (!trimmed) return '-'
  const state = parseCron(trimmed)
  if (!state) return trimmed

  const time = `${pad(state.hour)}:${pad(state.timeMinute)}`
  switch (state.mode) {
    case 'minutes':
      return `每隔 ${state.interval} 分钟`
    case 'hours':
      return `每隔 ${state.interval} 小时（第 ${state.minute} 分）`
    case 'daily':
      return `每天 ${time}`
    case 'weekly':
      return `${humanizeWeekdays(state.weekdays)} ${time}`
    case 'monthly':
      return `每月 ${state.day} 日 ${time}`
    default:
      return trimmed
  }
}
