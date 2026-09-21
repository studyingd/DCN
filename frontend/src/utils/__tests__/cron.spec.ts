import { describe, expect, it } from 'vitest'
import {
  DEFAULT_CRON_STATE,
  WEEKDAY_LABELS,
  buildCron,
  decodeWeekdays,
  encodeWeekdays,
  humanizeCron,
  humanizeWeekdays,
  isCronShape,
  parseCron,
  type CronState,
} from '../cron'

const state = (over: Partial<CronState> = {}): CronState => ({
  ...DEFAULT_CRON_STATE,
  weekdays: [...DEFAULT_CRON_STATE.weekdays],
  ...over,
})

describe('buildCron', () => {
  it('五种频率都生成后端 cron.py 能解析的形状', () => {
    expect(buildCron(state({ mode: 'minutes', interval: 15 }))).toBe('*/15 * * * *')
    expect(buildCron(state({ mode: 'hours', interval: 6, minute: 30 }))).toBe('30 */6 * * *')
    expect(buildCron(state({ mode: 'daily', hour: 2, timeMinute: 0 }))).toBe('0 2 * * *')
    expect(buildCron(state({ mode: 'weekly', weekdays: [1], hour: 9, timeMinute: 30 }))).toBe('30 9 * * 1')
    expect(buildCron(state({ mode: 'monthly', day: 15, hour: 3, timeMinute: 5 }))).toBe('5 3 15 * *')
  })

  it('工作日生成区间而不是逐个列举', () => {
    expect(buildCron(state({ mode: 'weekly', weekdays: [1, 2, 3, 4, 5] }))).toBe('0 8 * * 1-5')
  })

  it('零散多天生成为列表', () => {
    expect(buildCron(state({ mode: 'weekly', weekdays: [1, 3, 5] }))).toBe('0 8 * * 1,3,5')
    expect(buildCron(state({ mode: 'weekly', weekdays: [0, 6] }))).toBe('0 8 * * 0,6')
  })

  it('周日编码为 0，与后端 cron 约定一致', () => {
    expect(buildCron(state({ mode: 'weekly', weekdays: [0] }))).toBe('0 8 * * 0')
    expect(WEEKDAY_LABELS[0]).toBe('周日')
  })

  it('七天全选等价于每天，收敛成 daily 形状', () => {
    expect(buildCron(state({ mode: 'weekly', weekdays: [0, 1, 2, 3, 4, 5, 6] }))).toBe('0 8 * * *')
  })

  it('乱序与重复输入会被归一化', () => {
    expect(buildCron(state({ mode: 'weekly', weekdays: [5, 1, 3, 1] }))).toBe('0 8 * * 1,3,5')
  })
})

describe('encodeWeekdays / decodeWeekdays', () => {
  it.each([
    ['*', [0, 1, 2, 3, 4, 5, 6]],
    ['1-5', [1, 2, 3, 4, 5]],
    ['1,3,5', [1, 3, 5]],
    ['0,6', [0, 6]],
    ['3', [3]],
    ['1-3,6', [1, 2, 3, 6]],
    ['5-7', [0, 5, 6]], // cron 惯例 7=周日
    ['7', [0]],
  ] as const)('decode %s', (field, expected) => {
    expect(decodeWeekdays(field)).toEqual([...expected].sort((a, b) => a - b))
  })

  it('decode 拒绝非法与语义模糊的写法', () => {
    expect(decodeWeekdays('')).toBeNull()
    expect(decodeWeekdays('*/2')).toBeNull() // 周字段上的步长语义模糊，不猜
    expect(decodeWeekdays('8')).toBeNull()
    expect(decodeWeekdays('-1')).toBeNull()
    expect(decodeWeekdays('5-1')).toBeNull() // 逆序区间
    expect(decodeWeekdays('MON')).toBeNull()
    expect(decodeWeekdays('1,')).toBeNull()
  })

  it('encode 后 decode 能还原（除全选收敛成 * 外）', () => {
    for (const days of [[1], [1, 2, 3, 4, 5], [0, 6], [1, 3, 5], [2, 3, 4]]) {
      expect(decodeWeekdays(encodeWeekdays(days))).toEqual(days)
    }
  })

  it('encode 对空集与全选都给 *', () => {
    expect(encodeWeekdays([])).toBe('*')
    expect(encodeWeekdays([0, 1, 2, 3, 4, 5, 6])).toBe('*')
  })

  it('encode 忽略越界值', () => {
    expect(encodeWeekdays([1, 9, -1])).toBe('1')
  })
})

describe('parseCron', () => {
  it.each([
    ['*/15 * * * *', { mode: 'minutes', interval: 15 }],
    ['30 */6 * * *', { mode: 'hours', interval: 6, minute: 30 }],
    ['0 2 * * *', { mode: 'daily', hour: 2, timeMinute: 0 }],
    ['30 9 * * 1', { mode: 'weekly', hour: 9, timeMinute: 30 }],
    ['5 3 15 * *', { mode: 'monthly', day: 15, hour: 3, timeMinute: 5 }],
  ] as const)('解析 %s', (expr, expected) => {
    expect(parseCron(expr)).toMatchObject(expected)
  })

  it('解析工作日区间（这是本次要支持的核心场景）', () => {
    const parsed = parseCron('0 2 * * 1-5')
    expect(parsed).toMatchObject({ mode: 'weekly', hour: 2, timeMinute: 0 })
    expect(parsed?.weekdays).toEqual([1, 2, 3, 4, 5])
  })

  it('解析周末与零散多天', () => {
    expect(parseCron('0 2 * * 0,6')?.weekdays).toEqual([0, 6])
    expect(parseCron('0 2 * * 1,3,5')?.weekdays).toEqual([1, 3, 5])
    expect(parseCron('0 2 * * 1-3,6')?.weekdays).toEqual([1, 2, 3, 6])
  })

  it('七天全选读回成 daily，避免同一周期两种表述', () => {
    expect(parseCron('0 2 * * 0-6')).toMatchObject({ mode: 'daily' })
    expect(parseCron('0 2 * * 0,1,2,3,4,5,6')).toMatchObject({ mode: 'daily' })
  })

  it('构建→解析 往返一致', () => {
    for (const s of [
      state({ mode: 'minutes', interval: 5 }),
      state({ mode: 'hours', interval: 12, minute: 45 }),
      state({ mode: 'daily', hour: 23, timeMinute: 59 }),
      state({ mode: 'weekly', weekdays: [1], hour: 0, timeMinute: 0 }),
      state({ mode: 'weekly', weekdays: [1, 2, 3, 4, 5], hour: 2, timeMinute: 0 }),
      state({ mode: 'weekly', weekdays: [0, 6], hour: 10, timeMinute: 30 }),
      state({ mode: 'weekly', weekdays: [1, 3, 5], hour: 7, timeMinute: 15 }),
      state({ mode: 'monthly', day: 31, hour: 12, timeMinute: 30 }),
    ]) {
      expect(parseCron(buildCron(s))).toEqual(s)
    }
  })

  it('构建器表达不了的形状返回 null，而不是猜一个近似值', () => {
    // 猜错会让用户以为改的是原计划，实际提交的是另一个周期
    expect(parseCron('0 2,14 * * *')).toBeNull() // 一天两次
    expect(parseCron('0 0 1 1 *')).toBeNull() // 指定月份
    expect(parseCron('*/5 */2 * * *')).toBeNull() // 分钟与小时都是步长
    expect(parseCron('0 2 1-15 * *')).toBeNull() // 日期区间
    expect(parseCron('0 2 * * */2')).toBeNull() // 周字段步长
  })

  it('非法输入返回 null', () => {
    expect(parseCron('')).toBeNull()
    expect(parseCron(null)).toBeNull()
    expect(parseCron(undefined)).toBeNull()
    expect(parseCron('   ')).toBeNull()
    expect(parseCron('* * * *')).toBeNull()
    expect(parseCron('* * * * * *')).toBeNull()
    expect(parseCron('abc def ghi jkl mno')).toBeNull()
  })

  it('越界数值返回 null', () => {
    expect(parseCron('*/60 * * * *')).toBeNull()
    expect(parseCron('*/0 * * * *')).toBeNull()
    expect(parseCron('0 */24 * * *')).toBeNull()
    expect(parseCron('60 2 * * *')).toBeNull()
    expect(parseCron('0 24 * * *')).toBeNull()
    expect(parseCron('0 2 32 * *')).toBeNull()
    expect(parseCron('0 2 0 * *')).toBeNull()
    expect(parseCron('0 2 * * 8')).toBeNull()
  })

  it('容忍多余空白', () => {
    expect(parseCron('  0   2  *  *  1-5  ')).toMatchObject({ mode: 'weekly' })
  })
})

describe('humanizeWeekdays', () => {
  it('连续多天用「至」', () => {
    expect(humanizeWeekdays([1, 2, 3, 4, 5])).toBe('每周一至周五')
    expect(humanizeWeekdays([1, 2])).toBe('每周一至周二')
  })

  it('零散多天用顿号，且周日排最后', () => {
    expect(humanizeWeekdays([1, 3, 5])).toBe('每周一、周三、周五')
    expect(humanizeWeekdays([0, 6])).toBe('每周六、周日')
  })

  it('全选或空集都是每天', () => {
    expect(humanizeWeekdays([0, 1, 2, 3, 4, 5, 6])).toBe('每天')
    expect(humanizeWeekdays([])).toBe('每天')
  })

  it('单天', () => {
    expect(humanizeWeekdays([1])).toBe('每周一')
    expect(humanizeWeekdays([0])).toBe('每周日')
  })
})

describe('humanizeCron', () => {
  it('五种频率都有中文描述', () => {
    expect(humanizeCron('*/15 * * * *')).toBe('每隔 15 分钟')
    expect(humanizeCron('30 */6 * * *')).toBe('每隔 6 小时（第 30 分）')
    expect(humanizeCron('0 2 * * *')).toBe('每天 02:00')
    expect(humanizeCron('30 9 * * 1')).toBe('每周一 09:30')
    expect(humanizeCron('5 3 15 * *')).toBe('每月 15 日 03:05')
  })

  it('工作日与周末', () => {
    expect(humanizeCron('0 2 * * 1-5')).toBe('每周一至周五 02:00')
    expect(humanizeCron('0 8 * * 0,6')).toBe('每周六、周日 08:00')
    expect(humanizeCron('0 8 * * 1,3,5')).toBe('每周一、周三、周五 08:00')
  })

  it('时间补零，避免显示成 2:0', () => {
    expect(humanizeCron('5 3 1 * *')).toBe('每月 1 日 03:05')
    expect(humanizeCron('0 0 * * 0')).toBe('每周日 00:00')
  })

  it('认不出的表达式原样返回，不编造描述', () => {
    expect(humanizeCron('0 2,14 * * *')).toBe('0 2,14 * * *')
    expect(humanizeCron('0 0 1 1 *')).toBe('0 0 1 1 *')
  })

  it('空值返回占位符', () => {
    expect(humanizeCron(null)).toBe('-')
    expect(humanizeCron('')).toBe('-')
    expect(humanizeCron(undefined)).toBe('-')
  })
})

describe('isCronShape', () => {
  it('合法 5 段通过', () => {
    expect(isCronShape('0 2 * * *')).toBe(true)
    expect(isCronShape('0 2 * * 1-5')).toBe(true)
    expect(isCronShape('0 2,14 * * *')).toBe(true)
    expect(isCronShape('*/15 * * * *')).toBe(true)
  })

  it('段数不对或含非法字符则拒绝', () => {
    expect(isCronShape('* * * *')).toBe(false)
    expect(isCronShape('* * * * * *')).toBe(false)
    expect(isCronShape('每天凌晨两点')).toBe(false)
    expect(isCronShape('0 2 * * MON')).toBe(false)
    expect(isCronShape('')).toBe(false)
    expect(isCronShape(null)).toBe(false)
  })
})
